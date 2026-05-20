import os, json, requests
from datetime import date, timedelta
from collections import defaultdict
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GEMINI_KEY   = os.environ["GEMINI_API_KEY"]

sb    = create_client(SUPABASE_URL, SUPABASE_KEY)
today = date.today().isoformat()
print(f"▶ 리포트 생성 시작: {today}")

# ── 1. 데이터 조회 ────────────────────────────────────────
def fetch_holdings(target_date):
    all_rows = []
    from_idx = 0
    while True:
        resp = sb.table("holdings") \
            .select("etf_idx, etf_name, code, name, weight, holdings_qty") \
            .eq("date", target_date) \
            .range(from_idx, from_idx + 999) \
            .execute()
        rows = resp.data or []
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        from_idx += 1000
    return all_rows

rows_today = fetch_holdings(today)
if not rows_today:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    rows_today = fetch_holdings(yesterday)
    today = yesterday
    print(f"  오늘 데이터 없음 → {yesterday} 사용")

if not rows_today:
    print("❌ 데이터 없음, 종료")
    exit(0)

# ── 2. 종목별 집계 (주식수 기준) ──────────────────────────
SKIP_NAMES = ("현금", "원화예금", "예금", "기타", "미수금", "미지급금")

def aggregate(rows):
    m = defaultdict(lambda: {"weight": 0.0, "qty": 0.0, "etf_count": 0})
    for r in rows:
        nm = (r.get("name") or "").strip()
        if any(nm.startswith(s) for s in SKIP_NAMES):
            continue
        key = (r.get("code") or r.get("name") or "").strip()
        m[key]["name"]      = nm
        m[key]["code"]      = (r.get("code") or "").strip()
        m[key]["weight"]    += float(r.get("weight") or 0)
        m[key]["qty"]       += float(r.get("holdings_qty") or 0)
        m[key]["etf_count"] += 1
    return m

cur_map = aggregate(rows_today)

# ── 3. 전일 비교 ──────────────────────────────────────────
prev_date = None
prev_map  = {}
prev_resp = sb.table("holdings").select("date") \
    .lt("date", today).order("date", desc=True).limit(1).execute()
if prev_resp.data:
    prev_date = prev_resp.data[0]["date"]
    prev_map  = aggregate(fetch_holdings(prev_date))
    print(f"  비교일: {prev_date}")

# ── 4. 주식수 기준 변화 계산 ─────────────────────────────
up_list, down_list, new_list = [], [], []
THRESHOLD_QTY = 500  # 500주 이상 변화만 집계

for key, cur in cur_map.items():
    prv = prev_map.get(key)
    if not prv:
        new_list.append({
            "name": cur["name"], "code": cur["code"],
            "weight": round(cur["weight"], 2),
            "etf_count": cur["etf_count"],
            "qty_change": round(cur["qty"])
        })
        continue
    diff_q = cur["qty"] - prv["qty"]
    if diff_q > THRESHOLD_QTY:
        up_list.append({
            "name": cur["name"], "code": cur["code"],
            "weight": round(cur["weight"], 2),
            "etf_count": cur["etf_count"],
            "qty_change": round(diff_q)
        })
    elif diff_q < -THRESHOLD_QTY:
        down_list.append({
            "name": cur["name"], "code": cur["code"],
            "weight": round(cur["weight"], 2),
            "etf_count": cur["etf_count"],
            "qty_change": round(diff_q)
        })

up_list.sort(key=lambda x: x["qty_change"], reverse=True)
down_list.sort(key=lambda x: x["qty_change"])
new_list.sort(key=lambda x: x["weight"], reverse=True)

top10 = sorted(cur_map.values(), key=lambda x: x["weight"], reverse=True)[:10]
top10 = [{"name": x["name"], "weight": round(x["weight"], 2), "etf_count": x["etf_count"]} for x in top10]

print(f"  증가: {len(up_list)}개 / 감소: {len(down_list)}개 / 신규: {len(new_list)}개")

# ── 5. Gemini API 호출 ────────────────────────────────────
target_stocks = [x["name"] for x in up_list[:5]] + [x["name"] for x in down_list[:5]]

prompt = f"""당신은 Timefolio 자산운용의 ETF 리서치 애널리스트입니다.
{today} 기준 Timefolio 18개 ETF 데이터를 분석해서 한국어 데일리 리포트를 작성하세요.

[TOP 10 보유종목]
{json.dumps(top10, ensure_ascii=False)}

[주식수 증가 TOP 5 (전일 대비 순매수)]
{json.dumps(up_list[:5], ensure_ascii=False)}

[주식수 감소 TOP 5 (전일 대비 순매도)]
{json.dumps(down_list[:5], ensure_ascii=False)}

[신규 편입]
{json.dumps(new_list[:3], ensure_ascii=False)}

다음 JSON 형식으로만 응답하세요 (마크다운, 추가 텍스트 절대 금지):
{{
  "title": "20자 이내 오늘의 핵심 한 문장 (종목명/숫자 포함)",
  "summary": "전체 흐름 3문장 요약",
  "signals": [
    {{"type": "bull", "text": "강세 시그널 (종목명 포함)"}},
    {{"type": "bull", "text": "강세 시그널 2"}},
    {{"type": "bear", "text": "약세 시그널 (종목명 포함)"}},
    {{"type": "neutral", "text": "중립 시그널"}}
  ],
  "comment": "150자 이내 운용 방향성 총평",
  "stock_comments": {{
    {chr(10).join([f'    "{n}": "이 종목의 매매 이유 30자 이내"' for n in target_stocks])}
  }}
}}"""

gemini_url = (
    "https://generativelanguage.googleapis.com/v1beta"
    f"/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
)
resp = requests.post(
    gemini_url,
    json={"contents": [{"parts": [{"text": prompt}]}]},
    timeout=30
)
resp.raise_for_status()

raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
if raw.startswith("```"):
    raw = raw.split("\n", 1)[1]
if raw.endswith("```"):
    raw = raw.rsplit("```", 1)[0]
ai = json.loads(raw.strip())
print(f"  AI 제목: {ai['title']}")

# ── 6. Supabase 저장 ──────────────────────────────────────
record = {
    "report_date": today,
    "title":       ai["title"],
    "summary":     ai["summary"],
    "content": {
        "signals":        ai["signals"],
        "comment":        ai["comment"],
        "stock_comments": ai.get("stock_comments", {}),
        "top10":          top10,
        "top_up":         up_list[:5],
        "top_down":       down_list[:5],
        "stats": {
            "total_stocks": len(cur_map),
            "up_count":     len(up_list),
            "down_count":   len(down_list),
        },
        "new_count": len(new_list)
    }
}

sb.table("daily_reports").upsert(record, on_conflict="report_date").execute()
print(f"✅ 완료: {today} — {ai['title']}")
