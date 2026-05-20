import os, json, requests
from datetime import date, timedelta
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GEMINI_KEY   = os.environ["GEMINI_API_KEY"]

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
today = date.today().isoformat()

# ── 1. 오늘 보유종목 데이터 조회 ──────────────────────────
rows = (sb.table("holdings")
          .select("name, code, weight, etf_name")
          .eq("date", today)
          .order("weight", desc=True)
          .execute()).data

if not rows:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    rows = (sb.table("holdings")
              .select("name, code, weight, etf_name")
              .eq("date", yesterday)
              .order("weight", desc=True)
              .execute()).data
    today = yesterday

if not rows:
    print("데이터 없음, 종료")
    exit(0)

# ── 2. 종목별 ETF 집계 ────────────────────────────────────
from collections import defaultdict

stock_map = defaultdict(lambda: {"weight": 0.0, "etfs": []})
for r in rows:
    name = r["name"]
    stock_map[name]["weight"] += float(r["weight"] or 0)
    stock_map[name]["etfs"].append(r["etf_name"])

ranked = sorted(stock_map.items(), key=lambda x: x[1]["weight"], reverse=True)

top10 = [
    {"name": n, "weight": round(v["weight"], 2), "etf_count": len(set(v["etfs"]))}
    for n, v in ranked[:10]
]

# ── 3. 전일 대비 비중 변화 계산 ───────────────────────────
prev_date = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
prev_rows = (sb.table("holdings")
               .select("name, weight")
               .eq("date", prev_date)
               .execute()).data

prev_map = defaultdict(float)
for r in prev_rows:
    prev_map[r["name"]] += float(r["weight"] or 0)

changes = []
for n, v in ranked:
    cur  = round(v["weight"], 2)
    prev = round(prev_map.get(n, 0), 2)
    diff = round(cur - prev, 2)
    if diff != 0:
        changes.append({"name": n, "weight": cur, "change": diff})

changes.sort(key=lambda x: x["change"])
top_up   = [c for c in changes if c["change"] > 0][-5:][::-1]
top_down = [c for c in changes if c["change"] < 0][:5]

# ── 4. Gemini API 호출 ────────────────────────────────────
prompt = f"""
당신은 Timefolio 자산운용의 ETF 리서치 애널리스트입니다.
아래 {today} 기준 보유종목 데이터를 분석해서 한국어로 데일리 리포트를 작성하세요.

[TOP 10 보유종목 (합산 비중 기준)]
{json.dumps(top10, ensure_ascii=False)}

[비중 상위 증가 종목]
{json.dumps(top_up, ensure_ascii=False)}

[비중 상위 감소 종목]
{json.dumps(top_down, ensure_ascii=False)}

다음 JSON 형식으로만 응답하세요 (다른 텍스트 금지):
{{
  "title": "20자 이내 오늘의 핵심 한 문장",
  "summary": "3문장 이내 요약",
  "signals": [
    {{"type": "bull", "text": "강세 시그널 1"}},
    {{"type": "bull", "text": "강세 시그널 2"}},
    {{"type": "bear", "text": "약세 시그널 1"}},
    {{"type": "neutral", "text": "중립 시그널 1"}}
  ],
  "comment": "150자 이내 AI 운용 코멘트"
}}
"""

resp = requests.post(
    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}",
    json={"contents": [{"parts": [{"text": prompt}]}]},
    timeout=30
)
resp.raise_for_status()

raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
ai  = json.loads(raw)

# ── 5. Supabase 저장 ──────────────────────────────────────
record = {
    "report_date": today,
    "title":       ai["title"],
    "summary":     ai["summary"],
    "content": {
        "signals":  ai["signals"],
        "comment":  ai["comment"],
        "top10":    top10,
        "top_up":   top_up,
        "top_down": top_down,
        "stats": {
            "total_stocks": len(ranked),
            "up_count":     len(top_up),
            "down_count":   len(top_down),
        }
    }
}

sb.table("daily_reports").upsert(record, on_conflict="report_date").execute()
print(f"✅ 리포트 저장 완료: {today} — {ai['title']}")
