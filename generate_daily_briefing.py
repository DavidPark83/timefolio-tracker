#!/usr/bin/env python3
"""
generate_daily_briefing.py
- Supabase에서 TIME + KoAct 양쪽 데이터 확인 후 일일 브리핑 HTML 생성
- briefing/index.html 카드 자동 삽입
- git commit & push → Vercel 자동 배포

사용법:
  python generate_daily_briefing.py

환경변수:
  SUPABASE_URL, SUPABASE_KEY (없으면 스크립트 내 기본값 사용)
"""

import os, sys, json, time, subprocess
from datetime import datetime, timedelta
from pathlib import Path

# ── Supabase 설정 ──
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://lqpqummcoujmymydftlg.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxxcHF1bW1jb3VqbXlteWRmdGxnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzc3NDQxMDAsImV4cCI6MjA5MzMyMDEwMH0.n6iaxMNx0pDR5vp3ed1Cat8kHqM5PVwxyFNMh9sWIw0")

# ── 프로젝트 루트 (이 스크립트가 있는 디렉토리) ──
PROJECT_ROOT = Path(__file__).parent
BRIEFING_DIR = PROJECT_ROOT / "briefing"

from briefing_angle import build_headline, build_lead   # ← 추가

# ── 요일 한글 ──
DAY_NAMES = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# ── 현금/예금 필터 ──
CASH_NAMES = {"현금", "원화현금", "설정현금", "원화예금", "예금", "기타", "미수금", "미지급금"}

try:
    import requests
except ImportError:
    print("requests 설치 중...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--break-system-packages", "-q"])
    import requests

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}


def supabase_get(table, params=None):
    """Supabase REST API GET (페이지네이션 자동 처리)"""
    all_rows = []
    offset = 0
    limit = 1000
    while True:
        p = dict(params or {})
        p["offset"] = str(offset)
        p["limit"] = str(limit)
        resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=p)
        resp.raise_for_status()
        rows = resp.json()
        all_rows.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
    return all_rows


def get_latest_dates():
    """양쪽 프로바이더의 최신 데이터 날짜 확인"""
    rows = supabase_get("holdings", {
        "select": "date,provider",
        "order": "date.desc",
        "limit": "1000"
    })
    dates_by_provider = {}
    for r in rows:
        prov = r["provider"]
        if prov not in dates_by_provider:
            dates_by_provider[prov] = set()
        dates_by_provider[prov].add(r["date"])

    time_dates = sorted(dates_by_provider.get("timefolio", set()), reverse=True)
    koact_dates = sorted(dates_by_provider.get("samsungactive", set()), reverse=True)
    return time_dates, koact_dates


def wait_for_both_providers(max_wait_minutes=60, poll_interval=120, target_date=None):
    """양쪽 프로바이더 모두 오늘 데이터가 있을 때까지 대기"""
    today = target_date or datetime.now().strftime("%Y-%m-%d")
    # 주말 체크 (토=5, 일=6)
    weekday = datetime.strptime(today, "%Y-%m-%d").weekday()
    if weekday >= 5:
        print(f"⏭ 오늘은 {'토요일' if weekday == 5 else '일요일'}이라 브리핑을 생성하지 않습니다.")
        return None, None

    print(f"📅 오늘 날짜: {today}")
    start = time.time()

    while (time.time() - start) < max_wait_minutes * 60:
        time_dates, koact_dates = get_latest_dates()
        time_latest = time_dates[0] if time_dates else None
        koact_latest = koact_dates[0] if koact_dates else None

        print(f"  TIME 최신: {time_latest} | KoAct 최신: {koact_latest}")

        if time_latest == today and koact_latest == today:
            print("✅ 양쪽 데이터 모두 준비 완료!")
            # 전일 찾기
            prev_date = time_dates[1] if len(time_dates) > 1 else None
            return today, prev_date

        # 양쪽 최신 날짜가 같으면 (오늘은 아니지만) 그걸로 진행
        if time_latest and koact_latest and time_latest == koact_latest:
            print(f"⚠ 오늘({today}) 데이터는 없지만, 양쪽 최신({time_latest})으로 생성합니다.")
            prev_date = time_dates[1] if len(time_dates) > 1 else None
            return time_latest, prev_date

        elapsed = int(time.time() - start)
        print(f"  ⏳ {elapsed}초 경과... {poll_interval}초 후 재확인")
        time.sleep(poll_interval)

    print("❌ 타임아웃: 데이터가 준비되지 않았습니다.")
    return None, None


def fetch_holdings(date):
    """특정 날짜의 전체 보유종목 조회"""
    return supabase_get("holdings", {
        "select": "etf_idx,etf_name,code,name,weight,value,holding_amount,qty,provider",
        "date": f"eq.{date}",
        "order": "weight.desc"
    })


def fetch_etf_daily(date):
    """ETF별 일일 데이터 (NAV 등) 조회"""
    return supabase_get("etf_daily", {
        "select": "etf_idx,nav_total,provider",
        "date": f"eq.{date}"
    })


def is_cash(name):
    return any(name.startswith(c) for c in CASH_NAMES) if name else True


def normalize_code(code):
    if not code:
        return ""
    code = str(code).strip()
    if code.replace(".", "").isdigit() and "." in code:
        code = code.split(".")[0]
    if code.isdigit():
        return code.zfill(6)
    return code.upper()


def is_korean(code, name):
    c = str(code or "").strip()
    if c and c.isdigit():
        return True
    n = str(name or "").strip()
    if n and all(0xAC00 <= ord(ch) <= 0xD7AF or ch == ' ' for ch in n):
        return True
    return False


def analyze(cur_rows, prev_rows, daily_rows):
    """브리핑 분석 데이터 생성"""
    # 캐시 필터링
    cur = [r for r in cur_rows if not is_cash(r.get("name", ""))]
    prev = [r for r in prev_rows if not is_cash(r.get("name", ""))] if prev_rows else []

    # 코드 정규화
    for r in cur:
        r["code"] = normalize_code(r.get("code", ""))
    for r in prev:
        r["code"] = normalize_code(r.get("code", ""))

    # ── AUM 계산 ──
    time_aum = sum(r.get("nav_total", 0) or 0 for r in daily_rows if r.get("etf_idx", 0) < 200)
    koact_aum = sum(r.get("nav_total", 0) or 0 for r in daily_rows if r.get("etf_idx", 0) >= 200)

    # ── 스마트머니: 다수 ETF 보유 종목 ──
    stock_map = {}
    for r in cur:
        key = r["code"] or r["name"]
        if key not in stock_map:
            stock_map[key] = {"name": r["name"], "code": r["code"], "etfs": set(), "value": 0, "providers": set()}
        stock_map[key]["etfs"].add(r.get("etf_name", ""))
        stock_map[key]["value"] += r.get("holding_amount", 0) or 0
        stock_map[key]["providers"].add(r.get("provider", ""))

    smart_money = sorted(
        [{"name": v["name"], "code": v["code"], "etf_count": len(v["etfs"]),
          "value": v["value"], "both": len(v["providers"]) > 1}
         for v in stock_map.values() if len(v["etfs"]) >= 5],
        key=lambda x: (-x["etf_count"], -x["value"])
    )[:10]

    if not prev:
        return {
            "time_aum": time_aum, "koact_aum": koact_aum,
            "smart_money": smart_money,
            "new_entries": [], "exits": [],
            "weight_up": [], "weight_down": [],
            "divergence": []
        }

    # ── 전일 대비: 신규 편입 / 제외 / 비중 변화 ──
    prev_map = {}
    for r in prev:
        key = (r.get("provider", ""), r.get("etf_idx"), r["code"])
        prev_map[key] = r

    cur_keys = set()
    new_entries = []
    weight_changes = []

    for r in cur:
        key = (r.get("provider", ""), r.get("etf_idx"), r["code"])
        cur_keys.add(key)
        p = prev_map.get(key)
        if not p:
            if r.get("weight", 0) > 0.3:
                new_entries.append({
                    "name": r["name"], "code": r["code"],
                    "etf_name": r.get("etf_name", ""), "weight": r.get("weight", 0),
                    "provider": r.get("provider", "")
                })
        else:
            diff = (r.get("weight", 0) or 0) - (p.get("weight", 0) or 0)
            if abs(diff) >= 0.3:
                weight_changes.append({
                    "name": r["name"], "code": r["code"],
                    "etf_name": r.get("etf_name", ""),
                    "prev_weight": p.get("weight", 0),
                    "curr_weight": r.get("weight", 0),
                    "diff": round(diff, 2),
                    "provider": r.get("provider", "")
                })

    exits = []
    for key, r in prev_map.items():
        if key not in cur_keys and r.get("weight", 0) > 0.3:
            exits.append({
                "name": r["name"], "code": r["code"],
                "etf_name": r.get("etf_name", ""), "weight": r.get("weight", 0),
                "provider": r.get("provider", "")
            })

    new_entries.sort(key=lambda x: -x["weight"])
    exits.sort(key=lambda x: -x["weight"])
    weight_up = sorted([w for w in weight_changes if w["diff"] > 0], key=lambda x: -x["diff"])[:5]
    weight_down = sorted([w for w in weight_changes if w["diff"] < 0], key=lambda x: x["diff"])[:5]

    # ── 운용사 의견 충돌 ──
    time_changes = {}
    koact_changes = {}
    for w in weight_changes:
        key = w["code"] or w["name"]
        if w.get("provider") == "timefolio":
            time_changes[key] = time_changes.get(key, 0) + w["diff"]
        elif w.get("provider") == "samsungactive":
            koact_changes[key] = koact_changes.get(key, 0) + w["diff"]

    divergence = []
    for key in set(time_changes) & set(koact_changes):
        td, kd = time_changes[key], koact_changes[key]
        if (td > 0 and kd < 0) or (td < 0 and kd > 0):
            name = next((w["name"] for w in weight_changes if (w["code"] or w["name"]) == key), key)
            divergence.append({"name": name, "code": key, "time_diff": round(td, 2), "koact_diff": round(kd, 2)})

    return {
        "time_aum": time_aum, "koact_aum": koact_aum,
        "smart_money": smart_money,
        "new_entries": new_entries[:5], "exits": exits[:5],
        "weight_up": weight_up, "weight_down": weight_down,
        "divergence": divergence
    }


def fmt_억(v):
    if not v:
        return "—"
    eok = abs(v) / 1e8
    sign = "-" if v < 0 else ""
    if eok >= 10000:
        return f"{sign}{eok/10000:.2f}조"
    return f"{sign}{eok:,.0f}억"


def fmt_조(v):
    if not v:
        return "0"
    return f"{v / 1e12:.1f}조"


def prov_badge(provider):
    if provider == "timefolio":
        return '<span style="background:rgba(96,165,250,.15);color:#60a5fa;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">TIME</span>'
    return '<span style="background:rgba(52,211,153,.15);color:#34d399;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">KoAct</span>'


def generate_html(date, prev_date, data):
    """브리핑 HTML 생성"""
    d = datetime.strptime(date, "%Y-%m-%d")
    day_name = DAY_NAMES[d.weekday()]
    month = d.month
    day = d.day
    year = d.year

    # 제목 생성 — 그날 가장 특이한 사건을 앵글로 선택(26.8.1 수정)
    title, angle = build_headline(data, date)
    lead_html = build_lead(data, date, angle)

    # ── 스마트머니 테이블 ──
    smart_rows = ""
    for s in data["smart_money"]:
        both_badge = "TIME + KoAct" if s["both"] else ("TIME" if any(True for _ in []) else "—")
        smart_rows += f"""<tr>
          <td><strong>{s['name']}</strong></td>
          <td>{s['etf_count']}개</td>
          <td>~{fmt_억(s['value'])}</td>
        </tr>\n"""

    # ── 비중 증가 TOP ──
    up_rows = ""
    for w in data["weight_up"]:
        up_rows += f"""<tr>
          <td>{w['name']}</td><td>{w['etf_name']}</td>
          <td>{w['prev_weight']:.2f}%</td><td>{w['curr_weight']:.2f}%</td>
          <td class="up">{w['diff']:+.2f}%p</td>
        </tr>\n"""

    # ── 비중 감소 TOP ──
    down_rows = ""
    for w in data["weight_down"]:
        down_rows += f"""<tr>
          <td>{w['name']}</td><td>{w['etf_name']}</td>
          <td>{w['prev_weight']:.2f}%</td><td>{w['curr_weight']:.2f}%</td>
          <td class="down">{w['diff']:+.2f}%p</td>
        </tr>\n"""

    # ── 신규 편입 ──
    new_rows = ""
    for n in data["new_entries"]:
        new_rows += f"""<tr>
          <td><strong>{n['name']}</strong></td>
          <td>{n['etf_name']}</td>
          <td>{n['weight']:.2f}%</td>
        </tr>\n"""

    # ── 제외 종목 ──
    exit_rows = ""
    for e in data["exits"]:
        exit_rows += f"""<tr>
          <td><strong>{e['name']}</strong></td>
          <td>{e['etf_name']}</td>
          <td>{e['weight']:.2f}%</td>
        </tr>\n"""

    # ── 의견 충돌 ──
    divergence_html = ""
    for dv in data["divergence"]:
        t_cls = "up" if dv["time_diff"] > 0 else "down"
        k_cls = "up" if dv["koact_diff"] > 0 else "down"
        divergence_html += f"""<div class="highlight diverge">
          <strong>{dv['name']}</strong><br>
          TIME: <span class="{t_cls}">{dv['time_diff']:+.2f}%p</span> &nbsp;|&nbsp;
          KoAct: <span class="{k_cls}">{dv['koact_diff']:+.2f}%p</span>
        </div>\n"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — {month}월 {day}일 액티브 ETF 브리핑 | Active ETF Tracker</title>
  <meta name="description" content="{year}년 {month}월 {day}일 국내 액티브 ETF 42종 보유종목 변화 브리핑. {title}">
  <link rel="canonical" href="https://etftracker.co.kr/briefing/{date}.html">
  <style>
    :root{{--bg:#0f1117;--surface:#1a1d27;--surface-alt:#232736;--border:#2d3143;--text:#e4e5ea;--text-dim:#9ca3af;--accent-blue:#60a5fa;--accent-green:#34d399;--accent-red:#f87171;--accent-amber:#fbbf24}}
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.7;padding:0 1rem}}
    .container{{max-width:820px;margin:0 auto;padding:2rem 0 4rem}}
    a{{color:var(--accent-blue);text-decoration:none}}a:hover{{text-decoration:underline}}
    .back-link{{display:inline-block;margin-bottom:1.5rem;color:var(--text-dim);font-size:.9rem}}
    .briefing-date{{color:var(--accent-blue);font-size:.85rem;font-weight:600;letter-spacing:.05em}}
    .briefing-title{{font-size:1.8rem;font-weight:700;margin:.4rem 0 .8rem;line-height:1.3}}
    .briefing-meta{{color:var(--text-dim);font-size:.85rem;margin-bottom:2.5rem}}
    .section{{margin-bottom:2.5rem}}
    .section h2{{font-size:1.15rem;font-weight:700;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:1px solid var(--border)}}
    .section p{{margin-bottom:.8rem;font-size:.95rem}}
    .summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.8rem;margin-bottom:1.5rem}}
    .summary-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1rem 1.2rem;text-align:center}}
    .summary-card .label{{font-size:.75rem;color:var(--text-dim);margin-bottom:.3rem}}
    .summary-card .value{{font-size:1.5rem;font-weight:700}}
    table{{width:100%;border-collapse:collapse;font-size:.88rem;margin:.8rem 0 1.2rem}}
    th{{background:var(--surface-alt);color:var(--text-dim);font-weight:600;text-align:left;padding:.5rem .7rem;font-size:.8rem}}
    td{{padding:.5rem .7rem;border-bottom:1px solid var(--border)}}
    .up{{color:var(--accent-green)}}.down{{color:var(--accent-red)}}
    .highlight{{background:var(--surface);border-left:3px solid var(--accent-blue);padding:1rem 1.2rem;border-radius:0 8px 8px 0;margin:1rem 0}}
    .highlight.diverge{{border-left-color:var(--accent-red)}}
    .cta-box{{background:var(--surface);border:1px solid var(--accent-blue);border-radius:10px;padding:1.2rem;text-align:center;margin:2rem 0}}
    .cta-box a{{font-weight:600;font-size:1rem}}
    .briefing-footer{{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);color:var(--text-dim);font-size:.8rem;text-align:center}}
    .briefing-footer a{{color:var(--text-dim);margin:0 8px}}
    @media(max-width:600px){{.briefing-title{{font-size:1.4rem}}.summary-grid{{grid-template-columns:repeat(2,1fr)}}table{{font-size:.8rem}}}}
  </style>
</head>
<body>
<div class="container">
  <a href="/briefing/" class="back-link">← 브리핑 목록</a>
  <header>
    <div class="briefing-date">📊 Daily Briefing</div>
    <h1 class="briefing-title">{title} — {month}월 {day}일 액티브 ETF 브리핑</h1>
    <p class="briefing-meta">{year}년 {month}월 {day}일 {day_name} 기준 · TIME 19종 + KoAct 23종 · 총 42개 액티브 ETF 추적</p>
  </header>

  <section class="section">
    <h2>📈 시장 스냅샷</h2>
    <div class="summary-grid">
      <div class="summary-card"><div class="label">TIME 총 AUM</div><div class="value">{fmt_조(data['time_aum'])}</div></div>
      <div class="summary-card"><div class="label">KoAct 총 AUM</div><div class="value">{fmt_조(data['koact_aum'])}</div></div>
      <div class="summary-card"><div class="label">추적 ETF</div><div class="value">42개</div></div>
    </div>
  </section>

  {lead_html}   <!-- ← 이 한 줄 추가 -->

  {"<section class='section'><h2>🏅 스마트머니 시그널</h2><table><thead><tr><th>종목</th><th>보유 ETF 수</th><th>합산 보유액</th></tr></thead><tbody>" + smart_rows + "</tbody></table></section>" if smart_rows else ""}

  {"<section class='section'><h2>🔺 비중 증가 TOP</h2><table><thead><tr><th>종목</th><th>ETF</th><th>전일</th><th>금일</th><th>변화</th></tr></thead><tbody>" + up_rows + "</tbody></table></section>" if up_rows else ""}

  {"<section class='section'><h2>🔻 비중 감소 TOP</h2><table><thead><tr><th>종목</th><th>ETF</th><th>전일</th><th>금일</th><th>변화</th></tr></thead><tbody>" + down_rows + "</tbody></table></section>" if down_rows else ""}

  {"<section class='section'><h2>✨ 신규 편입</h2><table><thead><tr><th>종목</th><th>ETF</th><th>편입 비중</th></tr></thead><tbody>" + new_rows + "</tbody></table></section>" if new_rows else ""}

  {"<section class='section'><h2>🚪 제외 종목</h2><table><thead><tr><th>종목</th><th>ETF</th><th>최종 비중</th></tr></thead><tbody>" + exit_rows + "</tbody></table></section>" if exit_rows else ""}

  {"<section class='section'><h2>🔀 운용사 의견 충돌</h2>" + divergence_html + "</section>" if divergence_html else ""}

  <div class="cta-box">
    <p style="margin-bottom:.5rem;color:var(--text-dim);font-size:.85rem">42개 액티브 ETF의 보유종목 변화를 직접 확인하세요</p>
    <a href="/">Active ETF Tracker 바로가기 →</a>
  </div>

  <footer class="briefing-footer">
    <p>※ 본 브리핑은 공시 데이터 기반 정보 제공 목적이며, 투자 권유가 아닙니다.</p>
    <p style="margin-top:.8rem">
      <a href="/">홈</a> · <a href="/briefing/">브리핑</a> · <a href="/blog/">블로그</a> · <a href="/about.html">서비스 소개</a>
    </p>
    <p style="margin-top:.8rem">© {year} Active ETF Tracker. All rights reserved.</p>
  </footer>
</div>
</body>
</html>"""
    return html, title, day_name


def update_index(date, title, day_name, time_aum=0, koact_aum=0):
    """briefing/index.html에 새 카드 삽입"""
    index_path = BRIEFING_DIR / "index.html"
    if not index_path.exists():
        print(f"⚠ {index_path} 없음 — 인덱스 업데이트 건너뜀")
        return

    d = datetime.strptime(date, "%Y-%m-%d")
    month = d.month
    day = d.day
    year = d.year
    month_label = f"{year}년 {month}월"

    # 태그 생성 (제목에서 추출)
    tags = []
    if "-" in title and "%p" in title:
        part = title.split(",")[0].strip() if "," in title else title.split("—")[0].strip()
        tags.append(f'<span class="briefing-tag tag-down">{part}</span>')
    tags_html = "\n      ".join(tags) if tags else ""

    new_card = f"""
    <a href="/briefing/{date}.html" class="briefing-card">
      <div class="briefing-top">
        <span class="briefing-date-badge">{month}월 {day}일</span>
        <span class="briefing-day">{day_name}</span>
      </div>
      <div class="briefing-title">{title}</div>
      <div class="briefing-tags">
        {tags_html}
      </div>
    </a>
"""

    content = index_path.read_text(encoding="utf-8")

    # 이미 존재하면 기존 카드를 새 내용으로 교체 (제목/태그/요일 갱신)
    if f"/briefing/{date}.html" in content:
        import re
        # 해당 날짜의 <a ...>...</a> 카드 블록 전체를 찾아 교체
        pattern = re.compile(
            r'\s*<a href="/briefing/' + re.escape(date) + r'\.html".*?</a>',
            re.DOTALL
        )
        if pattern.search(content):
            content = pattern.sub(new_card.rstrip("\n"), content, count=1)
            index_path.write_text(content, encoding="utf-8")
            print(f"🔄 index.html의 {date} 카드 갱신 완료")
        else:
            print(f"⚠ {date} 링크는 있으나 카드 블록을 못 찾음 — 건너뜀")
        return

    # 월 그룹 존재 여부 확인
    if month_label in content:
        # 해당 월의 month-label 바로 뒤에 삽입
        label_tag = f'<div class="month-label">{month_label}</div>'
        content = content.replace(
            label_tag,
            label_tag + new_card,
            1
        )
    else:
        # 새 월 그룹 생성
        new_month = f"""
  <div class="month-group">
    <div class="month-label">{month_label}</div>
    <!-- BRIEFING_INSERT_POINT -->
{new_card}
  </div>
"""
        # 기존 첫 번째 month-group 앞에 삽입
        if '<div class="month-group">' in content:
            content = content.replace(
                '<div class="month-group">',
                new_month + '\n  <div class="month-group">',
                1
            )

    # AUM 자동 갱신
    if time_aum and koact_aum:
        import re
        time_str = f'{time_aum / 1e12:.1f}조'
        koact_str = f'{koact_aum / 1e12:.1f}조'
        content = re.sub(
            r'(<strong>TIME</strong>\s+)[\d.]+조(\s+AUM)',
            rf'\g<1>{time_str}\g<2>',
            content
        )
        content = re.sub(
            r'(<strong>KoAct</strong>\s+)[\d.]+조(\s+AUM)',
            rf'\g<1>{koact_str}\g<2>',
            content
        )

    index_path.write_text(content, encoding="utf-8")
    print(f"✅ index.html에 {date} 카드 추가 완료")


def git_push(date):
    """git commit & push"""
    os.chdir(PROJECT_ROOT)
    try:
        subprocess.run(["git", "add", "briefing/"], check=True)
        subprocess.run(["git", "commit", "-m", f"add daily briefing {date}"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("✅ git push 완료 → Vercel 자동 배포")
    except subprocess.CalledProcessError as e:
        print(f"⚠ git 오류: {e}")


def main():
    print("=" * 50)
    print("📊 일일 브리핑 생성 시작")
    print("=" * 50)

    # 1. 데이터 대기 (인자로 날짜 지정 시 폴링 없이 해당 날짜로 재생성)
    import sys
    arg_date = sys.argv[1] if len(sys.argv) > 1 else None
    if arg_date:
        # 과거 날짜 재생성: 폴링 생략
        base_date = arg_date
        time_dates, koact_dates = get_latest_dates()
        # 전일 = base_date보다 이전인 가장 가까운 날짜 (양쪽 공통 우선, 없으면 TIME 기준)
        common = sorted(set(time_dates) & set(koact_dates), reverse=True)
        candidates = common if common else time_dates
        prev_date = next((d for d in candidates if d < base_date), None)
        print(f"📅 수동 지정 날짜: {base_date} (전일: {prev_date})")
        # 지정 날짜 데이터 존재 확인
        if base_date not in time_dates or base_date not in koact_dates:
            print(f"⚠ 경고: {base_date} 데이터가 한쪽 이상 없습니다 "
                  f"(TIME={base_date in time_dates}, KoAct={base_date in koact_dates})")
    else:
        base_date, prev_date = wait_for_both_providers()
    if not base_date:
        return

    # 2. 이미 생성됐는지 확인
    output_path = BRIEFING_DIR / f"{base_date}.html"
    if output_path.exists():
        print(f"⚠ {output_path} 이미 존재 — 건너뜀")
        return

    # 3. 데이터 조회
    print(f"📥 {base_date} 데이터 조회 중...")
    cur_rows = fetch_holdings(base_date)
    prev_rows = fetch_holdings(prev_date) if prev_date else []
    daily_rows = fetch_etf_daily(base_date)
    print(f"  보유종목: {len(cur_rows)}행 | 전일: {len(prev_rows)}행 | 일일: {len(daily_rows)}행")

    # 4. 분석
    print("🔍 분석 중...")
    data = analyze(cur_rows, prev_rows, daily_rows)

    # 5. HTML 생성
    print("📝 HTML 생성 중...")
    html, title, day_name = generate_html(base_date, prev_date, data)
    BRIEFING_DIR.mkdir(exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"✅ {output_path} 생성 완료")

    # 6. index.html 업데이트
    update_index(base_date, title, day_name, data['time_aum'], data['koact_aum'])

    # 7. git push
    git_push(base_date)

    print("\n🎉 완료!")


if __name__ == "__main__":
    main()
