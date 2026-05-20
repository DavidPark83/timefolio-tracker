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

SKIP_NAMES = ("현금", "원화예금", "예금", "기타", "미수금", "미지급금")

# ── 1. 데이터 조회 ────────────────────────────────────────
def fetch_holdings(target_date):
    all_rows, from_idx = [], 0
    while True:
        resp = sb.table("holdings") \
            .select("etf_idx, etf_name, code, name, weight, holdings_qty") \
            .eq("date", target_date) \
            .range(from_idx, from_idx + 999).execute()
        rows = resp.data or []
        all_rows.extend(rows)
        if len(rows) < 1000: break
        from_idx += 1000
    return all_rows

rows_today = fetch_holdings(today)
if not rows_today:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    rows_today = fetch_holdings(yesterday)
    today = yesterday
    print(f"  오늘 데이터 없음 → {yesterday} 사용")
if not rows_today:
    print("❌ 데이터 없음, 종료"); exit(0)

# 전일 데이터
prev_date, rows_prev = None, []
prev_resp = sb.table("holdings").select("date") \
    .lt("date", today).order("date", desc=True).limit(1).execute()
if prev_resp.data:
    prev_date = prev_resp.data[0]["date"]
    rows_prev = fetch_holdings(prev_date)
    print(f"  비교일: {prev_date}")

# ── 2. 전체 합산 집계 ─────────────────────────────────────
def aggregate_all(rows):
    m = defaultdict(lambda: {"weight": 0.0, "qty": 0.0, "etf_count": 0})
    for r in rows:
        nm = (r.get("name") or "").strip()
        if any(nm.startswith(s) for s in SKIP_NAMES): continue
        key = (r.get("code") or nm).strip()
        m[key]["name"]      = nm
        m[key]["code"]      = (r.get("code") or "").strip()
        m[key]["weight"]    += float(r.get("weight") or 0)
        m[key]["qty"]       += float(r.get("holdings_qty") or 0)
        m[key]["etf_count"] += 1
    return m

cur_map  = aggregate_all(rows_today)
prev_map = aggregate_all(rows_prev) if rows_prev else {}

top10 = sorted(cur_map.values(), key=lambda x: x["weight"], reverse=True)[:10]
top10 = [{"name": x["name"], "weight": round(x["weight"], 2), "etf_count": x["etf_count"]} for x in top10]

THRESHOLD = 500
up_all, down_all, new_all = [], [], []
for key, cur in cur_map.items():
    prv  = prev_map.get(key)
    dq   = cur["qty"] - (prv["qty"] if prv else 0)
    item = {"name": cur["name"], "code": cur["code"],
            "weight": round(cur["weight"], 2),
            "etf_count": cur["etf_count"], "qty_change": round(dq)}
    if not prv:    new_all.append(item)
    elif dq >  THRESHOLD: up_all.append(item)
    elif dq < -THRESHOLD: down_all.append(item)

up_all.sort(key=lambda x: x["qty_change"], reverse=True)
down_all.sort(key=lambda x: x["qty_change"])
print(f"  전체 증가:{len(up_all)} 감소:{len(down_all)} 신규:{len(new_all)}")

# ── 3. ETF별 집계 ─────────────────────────────────────────
def group_by_etf(rows):
    m = defaultdict(lambda: {"name": "", "stocks": {}})
    for r in rows:
        nm = (r.get("name") or "").strip()
        if any(nm.startswith(s) for s in SKIP_NAMES): continue
        idx = str(r.get("etf_idx", ""))
        key = (r.get("code") or nm).strip()
        m[idx]["name"] = r.get("etf_name", "")
        m[idx]["stocks"][key] = {
            "name": nm, "code": (r.get("code") or "").strip(),
            "weight": float(r.get("weight") or 0),
            "qty":    float(r.get("holdings_qty") or 0)
        }
    return m

etf_cur  = group_by_etf(rows_today)
etf_prev = group_by_etf(rows_prev) if rows_prev else {}

ETF_THR = 100   # ETF별 임계값 100주
etf_reports  = {}   # 저장용
etf_summaries = {}  # Gemini 프롬프트용

for idx, data in etf_cur.items():
    prev_stocks = etf_prev.get(idx, {}).get("stocks", {})
    e_up, e_down = [], []
    for key, s in data["stocks"].items():
        p   = prev_stocks.get(key)
        dq  = s["qty"] - (p["qty"] if p else 0)
        itm = {"name": s["name"], "weight": round(s["weight"], 2), "qty_change": round(dq)}
        if   dq >  ETF_THR: e_up.append(itm)
        elif dq < -ETF_THR: e_down.append(itm)
    e_up.sort(key=lambda x: x["qty_change"], reverse=True)
    e_down.sort(key=lambda x: x["qty_change"])
    etf_reports[idx] = {"name": data["name"], "top_up": e_up[:5], "top_down": e_down[:5]}
    if e_up or e_down:
        etf_summaries[data["name"]] = {
            "순매수": [f"{x['name']}(+{x['qty_change']:,}주)" for x in e_up[:3]],
            "순매도": [f"{x['name']}({x['qty_change']:,}주)" for x in e_down[:3]]
        }

# ── 4. Gemini 호출 ────────────────────────────────────────
etf_names_with_data = list(etf_summaries.keys())

prompt = f"""당신은 Timefolio 자산운용의 ETF 리서치 애널리스트입니다.
{today} 기준 데이터를 분석하여 한국어 데일리 리포트를 작성하세요.

[전체 TOP10 보유종목]
{json.dumps(top10, ensure_ascii=False)}

[전체 순매수 TOP5]
{json.dumps(up_all[:5], ensure_ascii=False)}

[전체 순매도 TOP5]
{json.dumps(down_all[:5], ensure_ascii=False)}

[ETF별 당일 매매 현황]
{json.dumps(etf_summaries, ensure_ascii=False)}

다음 JSON 형식으로만 응답하세요 (마크다운 절대 금지):
{{
  "title": "20자 이내 오늘의 핵심 한 문장",
  "summary": "전체 흐름 3문장 요약",
  "signals": [
    {{"type": "bull", "text": "강세 시그널 (종목명 포함)"}},
    {{"type": "bull", "text": "강세 시그널 2"}},
    {{"type": "bear", "text": "약세 시그널 (종목명 포함)"}},
    {{"type": "neutral", "text": "중립 시그널"}}
  ],
  "comment": "150자 이내 전체 총평",
  "etf_comments": {{
    {chr(10).join([f'    "{n}": "이 ETF 당일 운용 방향 50자 이내"' for n in etf_names_with_data])}
  }}
}}"""

resp = requests.post(
    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
    json={"contents": [{"parts": [{"text": prompt}]}]},
    timeout=60
)
resp.raise_for_status()
raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
if raw.startswith("```"): raw = raw.split("\n", 1)[1]
if raw.endswith("```"):   raw = raw.rsplit("```", 1)[0]
ai = json.loads(raw.strip())
print(f"  AI 제목: {ai['title']}")

# ETF 코멘트 병합
for name, comment in ai.get("etf_comments", {}).items():
    for idx, data in etf_reports.items():
        if data["name"] == name:
            data["comment"] = comment

# ── 5. 저장 ───────────────────────────────────────────────
sb.table("daily_reports").upsert({
    "report_date": today,
    "title":       ai["title"],
    "summary":     ai["summary"],
    "content": {
        "signals":     ai["signals"],
        "comment":     ai["comment"],
        "etf_reports": etf_reports,
        "top10":       top10,
        "top_up":      up_all[:5],
        "top_down":    down_all[:5],
        "stats":       {"total_stocks": len(cur_map), "up_count": len(up_all), "down_count": len(down_all)},
        "new_count":   len(new_all)
    }
}, on_conflict="report_date").execute()
print(f"✅ 완료: {today} — {ai['title']}")
