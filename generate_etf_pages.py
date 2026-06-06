#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_etf_pages.py
─────────────────────────────────────────────────────────────
Supabase의 holdings / etf_daily 데이터를 읽어
ETF별 정적 HTML 페이지를 생성합니다.

생성물:
  - etf/{slug}.html      (ETF별 개별 페이지, SEO용)
  - etf/index.html       (ETF 목록 허브 페이지)
  - sitemap.xml          (검색엔진 색인용, repo 루트)

사용법:
  python generate_etf_pages.py

GitHub Actions 크롤 직후 실행하면 매일 자동 갱신됩니다.
"""

import os
import sys
import html
import datetime
import requests

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
SITE = "https://etftracker.co.kr"
SUPABASE_URL = "https://lqpqummcoujmymydftlg.supabase.co"
# 프론트엔드와 동일한 public anon 키 (읽기 전용)
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxxcHF1bW1jb3VqbXlteWRmdGxnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzc3NDQxMDAsImV4cCI6MjA5MzMyMDEwMH0.n6iaxMNx0pDR5vp3ed1Cat8kHqM5PVwxyFNMh9sWIw0"

GTAG_ID = "G-2JVGVXMPSB"
ADSENSE_CLIENT = "ca-pub-9155147769106740"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

TOP_N = 999  # 페이지당 노출할 상위 보유종목 수

# ─────────────────────────────────────────────────────────────
# ETF 메타데이터 (index.html의 ETF_META와 동일)
#   manager 필드는 향후 삼성액티브(KoAct) 등 확장 대비
# ─────────────────────────────────────────────────────────────
ETFS = {
    22: {"slug": "global-top-pick",        "name": "TIMEFOLIO 글로벌탑픽액티브",        "manager": "TIMEFOLIO", "region": "해외", "listDate": "2025.10.28", "risk": "2등급(높은 위험)",     "bench": "Bloomberg World Large, Mid & Small Cap Index(KRW)",     "desc": "전 세계 핵심 ETF만 선별하여 투자하는 초분산 액티브 포트폴리오 ETF"},
    2:  {"slug": "us-nasdaq100",           "name": "TIMEFOLIO 미국나스닥100액티브",      "manager": "TIMEFOLIO", "region": "해외", "listDate": "2022.05.11", "risk": "1등급(매우 높은 위험)", "bench": "NASDAQ 100 Index(KRW)",                                  "desc": "메가트렌드를 주도하는 글로벌 대표 테크 기업에 투자하는 액티브 ETF"},
    5:  {"slug": "us-sp500",               "name": "TIMEFOLIO 미국S&P500액티브",         "manager": "TIMEFOLIO", "region": "해외", "listDate": "2022.05.11", "risk": "2등급(높은 위험)",     "bench": "S&P500 Index(KRW)",                                      "desc": "글로벌 경제 성장을 이끄는 대형주가 편입된 S&P500지수를 더 액티브하게 운용하는 ETF"},
    6:  {"slug": "global-ai",              "name": "TIMEFOLIO 글로벌AI인공지능액티브",   "manager": "TIMEFOLIO", "region": "해외", "listDate": "2023.05.16", "risk": "2등급(높은 위험)",     "bench": "Solactive Global Artificial Intelligence Index(KRW)",    "desc": "빠르게 변화하고 발전하는 AI 인공지능 산업의 글로벌 리더를 엄선하여 투자하는 액티브 ETF"},
    9:  {"slug": "global-bio",             "name": "TIMEFOLIO 글로벌바이오액티브",       "manager": "TIMEFOLIO", "region": "해외", "listDate": "2024.07.02", "risk": "2등급(높은 위험)",     "bench": "KEDI 글로벌불로장생바이오지수(KRW)",                      "desc": "첨단 의학 혁신을 주도하는 글로벌 바이오 기업에 투자하여, 미래 건강 가치를 한발 앞서 선점하는 액티브 ETF"},
    20: {"slug": "global-space-defense",   "name": "TIMEFOLIO 글로벌우주테크&방산액티브","manager": "TIMEFOLIO", "region": "해외", "listDate": "2024.04.23", "risk": "2등급(높은 위험)",     "bench": "Solactive Aerospace and Defense USD Index(KRW)",         "desc": "우주산업의 폭발적인 성장성과 방산분야의 안정적인 실적을 겸비한 글로벌 기업들에 투자하는 액티브 ETF"},
    19: {"slug": "china-ai-tech",          "name": "TIMEFOLIO 차이나AI테크액티브",       "manager": "TIMEFOLIO", "region": "해외", "listDate": "2025.05.13", "risk": "2등급(높은 위험)",     "bench": "Solactive China Artificial Intelligence Index(KRW)",     "desc": "중국 AI산업의 성장을 주도하는 중국, 홍콩, 대만 리딩 테크 기업에 투자하는 ETF"},
    18: {"slug": "us-dividend-dow",        "name": "TIMEFOLIO 미국배당다우존스액티브",   "manager": "TIMEFOLIO", "region": "해외", "listDate": "2025.04.29", "risk": "2등급(높은 위험)",     "bench": "Dow Jones U.S. Dividend 100 Index(KRW)",                 "desc": "매월 중순 월배당을 지급하며 배당이 성장하는 미국 고배당기업에 투자 가능한 액티브 ETF"},
    10: {"slug": "us-nasdaq100-bond50",    "name": "TIMEFOLIO 미국나스닥100채권혼합50액티브","manager":"TIMEFOLIO","region": "해외","listDate": "2025.03.25", "risk": "4등급(보통 위험)",     "bench": "FnGuide 미국 나스닥100 단기채권혼합 지수",                "desc": "글로벌 테크 주도주와 국내 단기채 조합으로 연금계좌(DC/IRP)에서 100% 투자 가능한 액티브 ETF"},
    8:  {"slug": "global-consumer",        "name": "TIMEFOLIO 글로벌소비트렌드액티브",   "manager": "TIMEFOLIO", "region": "해외", "listDate": "2024.10.29", "risk": "2등급(높은 위험)",     "bench": "Solactive New Age Consumer USD Index(KRW)",              "desc": "빠르게 변하는 소비트렌드를 주도하는 기업을 더 빠르게 투자하는 액티브 ETF"},
    25: {"slug": "global-humanoid",        "name": "TIMEFOLIO 글로벌휴머노이드액티브",   "manager": "TIMEFOLIO", "region": "해외", "listDate": "2026.05.19", "risk": "2등급(높은 위험)",     "bench": "Solactive Global AI Humanoid Robotics Index(KRW)",       "desc": "글로벌 휴머노이드 로봇 기업에 투자하는 액티브 ETF"},
    12: {"slug": "korea-plus-dividend",    "name": "TIMEFOLIO Korea플러스배당액티브",    "manager": "TIMEFOLIO", "region": "국내", "listDate": "2022.09.27", "risk": "2등급(높은 위험)",     "bench": "KOSPI200",                                               "desc": "월배당과 자본이익을 동시에 추구하는 액티브 ETF"},
    11: {"slug": "kospi",                  "name": "TIMEFOLIO 코스피액티브",             "manager": "TIMEFOLIO", "region": "국내", "listDate": "2021.05.25", "risk": "2등급(높은 위험)",     "bench": "KOSPI",                                                  "desc": "주도 섹터와 종목을 시의 적절하게 발굴하여 코스피 대비 나은 성과를 추구하는 액티브 ETF"},
    15: {"slug": "korea-value-up",         "name": "TIMEFOLIO 코리아밸류업액티브",       "manager": "TIMEFOLIO", "region": "국내", "listDate": "2024.11.04", "risk": "2등급(높은 위험)",     "bench": "코리아 밸류업 지수",                                      "desc": "대한민국 주식시장의 가치를 올리는 코리아밸류업 주식에 투자하는 액티브 ETF"},
    24: {"slug": "kosdaq",                 "name": "TIMEFOLIO 코스닥액티브",             "manager": "TIMEFOLIO", "region": "국내", "listDate": "2026.03.10", "risk": "2등급(높은 위험)",     "bench": "코스닥",                                                  "desc": "국내 대표 성장산업과 유망 종목을 선별해 코스닥의 높은 성장성과 알파를 동시에 추구하는 액티브 ETF"},
    16: {"slug": "k-renewable",            "name": "TIMEFOLIO K신재생에너지액티브",      "manager": "TIMEFOLIO", "region": "국내", "listDate": "2021.10.29", "risk": "1등급(매우 높은 위험)", "bench": "KRX 기후변화 솔루션 지수",                                "desc": "태양광·풍력·수력·지열 등 신재생에너지에 투자하여 지속 가능한 미래로의 도약을 이끄는 액티브 ETF"},
    13: {"slug": "k-bio",                  "name": "TIMEFOLIO K바이오액티브",            "manager": "TIMEFOLIO", "region": "국내", "listDate": "2023.08.17", "risk": "2등급(높은 위험)",     "bench": "KRX 헬스케어 지수",                                       "desc": "신약/바이오베터, 의료기기, 디지털 헬스케어 등 국내 바이오 핵심기업을 엄선하여 투자하는 액티브 ETF"},
    17: {"slug": "k-innovation",           "name": "TIMEFOLIO K이노베이션액티브",        "manager": "TIMEFOLIO", "region": "국내", "listDate": "2021.05.25", "risk": "1등급(매우 높은 위험)", "bench": "KRX BBIG",                                                "desc": "미래 성장성 높은 테마에 투자하면서 신성장 기업들을 적극적으로 발굴 및 투자하는 액티브 ETF"},
    1:  {"slug": "k-culture",              "name": "TIMEFOLIO K컬처액티브",              "manager": "TIMEFOLIO", "region": "국내", "listDate": "2021.12.15", "risk": "1등급(매우 높은 위험)", "bench": "FnGuide K-컬쳐 지수",                                     "desc": "K-Pop, 영화, 드라마 등 글로벌 트렌드를 주도하는 한국의 문화경쟁력에 투자하는 액티브 ETF"},
}

CASH_PATTERN = ("현금", "원화예금", "예금", "기타", "미수금", "미지급금")


# ─────────────────────────────────────────────────────────────
# Supabase 조회 헬퍼
# ─────────────────────────────────────────────────────────────
def sb_get(table, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_latest_date():
    rows = sb_get("etf_daily", {"select": "date", "order": "date.desc", "limit": "1"})
    return rows[0]["date"] if rows else None


def get_holdings(idx, date):
    rows = sb_get("holdings", {
        "select": "code,name,weight,qty,value",
        "etf_idx": f"eq.{idx}",
        "date": f"eq.{date}",
        "order": "weight.desc",
    })
    # 현금성 항목 제외
    return [r for r in rows if not str(r.get("name", "")).strip().startswith(CASH_PATTERN)]


def get_nav(idx, date):
    rows = sb_get("etf_daily", {
        "select": "nav_total",
        "etf_idx": f"eq.{idx}",
        "date": f"eq.{date}",
    })
    return rows[0]["nav_total"] if rows else None


# ─────────────────────────────────────────────────────────────
# 포맷 유틸
# ─────────────────────────────────────────────────────────────
def fmt_money(v):
    if v is None:
        return "—"
    eok = abs(v) / 100_000_000
    sign = "-" if v < 0 else ""
    if eok >= 10000:
        return f"{sign}{eok/10000:.2f}조원"
    if eok >= 1:
        return f"{sign}{round(eok):,}억원"
    return f"{sign}{eok:.1f}억원"


def esc(s):
    return html.escape(str(s if s is not None else ""))


def is_korean(code):
    c = str(code or "").strip()
    return c.isdigit() or bool(c) and len(c) == 6 and c[:4].isdigit()


# ─────────────────────────────────────────────────────────────
# HTML 템플릿 (CSS 중괄호 충돌 방지를 위해 .replace 방식 사용)
# ─────────────────────────────────────────────────────────────
HEAD = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>__TITLE__</title>
  <meta name="description" content="__DESC__" />
  <link rel="canonical" href="__CANONICAL__" />
  <script type="application/ld+json">__JSONLD__</script>
  <script async src="https://www.googletagmanager.com/gtag/js?id=__GTAG__"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());
    gtag('config', '__GTAG__');
  </script>
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=__ADS__" crossorigin="anonymous"></script>
  <style>
    :root{--bg:#0a0e1a;--surface:#0d1526;--surface2:#111827;--border:#1e2d4a;--text:#e2e8f0;--muted:#94a3b8;--dim:#64748b;--accent:#00D4AA;}
    *{box-sizing:border-box;margin:0;padding:0;}
    body{background:var(--bg);color:var(--text);font-family:'Apple SD Gothic Neo',-apple-system,BlinkMacSystemFont,sans-serif;line-height:1.8;padding:0 16px;}
    header{background:var(--surface2);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;}
    header a.back{color:var(--accent);text-decoration:none;font-size:14px;}
    header h1.logo{font-size:18px;font-weight:700;}
    .nav-links{margin-left:auto;display:flex;gap:18px;flex-wrap:wrap;}
    .nav-links a{color:var(--muted);text-decoration:none;font-size:13px;}
    .nav-links a:hover{color:var(--accent);}
    .container{max-width:820px;margin:36px auto;}
    .breadcrumb{font-size:12px;color:var(--dim);margin-bottom:18px;}
    .breadcrumb a{color:var(--muted);text-decoration:none;}
    h1.title{font-size:27px;font-weight:800;color:#f8fafc;margin-bottom:6px;}
    .subtitle{font-size:13px;color:var(--dim);margin-bottom:24px;}
    .desc-box{font-size:15px;color:#cbd5e1;padding:16px 20px;background:var(--surface);border-left:3px solid var(--accent);border-radius:0 10px 10px 0;margin-bottom:24px;}
    .info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--border);border-radius:12px;overflow:hidden;margin-bottom:28px;}
    .info-cell{background:var(--surface);padding:14px 16px;}
    .info-cell .l{font-size:11px;color:var(--dim);margin-bottom:5px;}
    .info-cell .v{font-size:15px;font-weight:700;color:#f1f5f9;}
    h2.sec{font-size:18px;font-weight:700;color:var(--accent);margin:32px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--border);}
    table{width:100%;border-collapse:collapse;font-size:14px;background:var(--surface);border-radius:12px;overflow:hidden;}
    th{text-align:left;font-size:11px;color:var(--dim);text-transform:uppercase;padding:11px 14px;background:var(--surface2);border-bottom:1px solid var(--border);}
    td{padding:11px 14px;border-bottom:1px solid #1a2235;color:#cbd5e1;}
    tr:last-child td{border-bottom:none;}
    .rank{color:var(--dim);font-family:monospace;}
    .w{font-family:monospace;color:var(--accent);text-align:right;}
    .code{font-size:11px;color:var(--dim);font-family:monospace;}
    .related{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;}
    .related a{font-size:12px;padding:7px 14px;border-radius:16px;background:var(--surface);border:1px solid var(--border);color:var(--muted);text-decoration:none;}
    .related a:hover{border-color:var(--accent);color:var(--accent);}
    .cta-box{text-align:center;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:30px;margin:40px 0;}
    .cta-box h3{color:#f8fafc;margin-bottom:8px;}
    .cta-box p{color:var(--muted);font-size:14px;margin-bottom:18px;}
    .cta-btn{display:inline-block;background:var(--accent);color:#06231d;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;}
    .disclaimer-note{margin-top:36px;padding:16px 20px;background:rgba(255,255,255,0.02);border:1px solid var(--border);border-radius:10px;font-size:12px;color:var(--dim);line-height:1.7;}
    .disclaimer-note a{color:var(--accent);}
    footer{max-width:820px;margin:36px auto 0;text-align:center;color:var(--dim);font-size:13px;padding:28px 0;border-top:1px solid var(--border);}
    footer a{color:var(--muted);text-decoration:none;margin:0 8px;}
  </style>
</head>
<body>
<header>
  <a href="/" class="back">← 메인으로</a>
  <h1 class="logo">📊 Active ETF Tracker</h1>
  <nav class="nav-links">
    <a href="/guide.html">가이드</a>
    <a href="/glossary.html">용어사전</a>
    <a href="/etf/">ETF 목록</a>
  </nav>
</header>
"""

FOOT = """
<footer>
  <a href="/">홈</a>
  <a href="/about.html">서비스 소개</a>
  <a href="/disclaimer.html">투자 고지사항</a>
  <a href="/privacy.html">개인정보처리방침</a>
  <br/><br/>
  © 2026 Active ETF Tracker. All rights reserved.
</footer>
</body>
</html>
"""


def render_etf_page(idx, meta, date, holdings, nav):
    name = meta["name"]
    title = f"{name} ETF 보유종목·구성종목 현황 ({date} 기준) | Active ETF Tracker"
    desc = f"{name} ETF의 최신 보유종목과 비중을 한눈에. {meta['desc']} 종목 수 {len(holdings)}개, 순자산총액 {fmt_money(nav)}. 매일 자동 업데이트."
    canonical = f"{SITE}/etf/{meta['slug']}.html"

    jsonld = (
        '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":1,"name":"홈","item":"' + SITE + '/"},'
        '{"@type":"ListItem","position":2,"name":"ETF 목록","item":"' + SITE + '/etf/"},'
        '{"@type":"ListItem","position":3,"name":"' + esc(name) + '","item":"' + canonical + '"}'
        ']}'
    )

    # 보유종목 표 (상위 TOP_N)
    rows_html = ""
    for i, h in enumerate(holdings[:TOP_N], 1):
        w = h.get("weight") or 0
        rows_html += (
            f"<tr><td class='rank'>{i}</td>"
            f"<td>{esc(h.get('name'))}<div class='code'>{esc(h.get('code'))}</div></td>"
            f"<td class='w'>{w:.2f}%</td></tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='3' style='text-align:center;color:#475569;padding:24px'>해당 일자 데이터가 없습니다</td></tr>"

    # 관련 ETF (같은 region 3개)
    related = [m for j, m in ETFS.items()
               if j != idx and m["region"] == meta["region"]][:5]
    related_html = "".join(
        f"<a href='/etf/{m['slug']}.html'>{esc(m['name'].replace('TIMEFOLIO ',''))}</a>"
        for m in related
    )

    body = f"""
<div class="container">
  <div class="breadcrumb">
    <a href="/">홈</a> &nbsp;›&nbsp; <a href="/etf/">ETF 목록</a> &nbsp;›&nbsp; {esc(name)}
  </div>

  <h1 class="title">{esc(name)} 보유종목</h1>
  <p class="subtitle">{esc(meta['manager'])} · {esc(meta['region'])} · 기준일 {esc(date)} · 매일 자동 업데이트</p>

  <div class="desc-box">{esc(meta['desc'])}</div>

  <div class="info-grid">
    <div class="info-cell"><div class="l">순자산총액</div><div class="v">{fmt_money(nav)}</div></div>
    <div class="info-cell"><div class="l">보유 종목 수</div><div class="v">{len(holdings)}개</div></div>
    <div class="info-cell"><div class="l">상장일</div><div class="v">{esc(meta['listDate'])}</div></div>
    <div class="info-cell"><div class="l">위험등급</div><div class="v" style="font-size:13px">{esc(meta['risk'])}</div></div>
  </div>

  <h2 class="sec">📊 보유종목 TOP {min(TOP_N, len(holdings))}</h2>
  <table>
    <thead><tr><th>#</th><th>종목명</th><th style="text-align:right">비중</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="font-size:12px;color:#475569;margin-top:10px">비교지수(벤치마크): {esc(meta['bench'])}</p>

  <div class="cta-box">
    <h3>📈 전체 보유종목과 매일의 변화 보기</h3>
    <p>신규 편입·비중 증감·제외 종목 등 {esc(meta['region'])} 액티브 ETF의 모든 변화를 인터랙티브하게 확인하세요.</p>
    <a href="/" class="cta-btn">Active ETF Tracker 바로가기 →</a>
  </div>

  <h2 class="sec">🔗 같은 분류의 다른 ETF</h2>
  <div class="related">{related_html}</div>

  <div class="disclaimer-note">
    ※ 본 페이지의 데이터는 공시 자료 기반으로 매일 자동 수집되며, 실제와 차이가 있을 수 있습니다. 정보 제공 목적이며 투자 권유가 아닙니다. 투자 결정은 본인 책임 하에 이루어져야 하며 투자 전 투자설명서를 확인하세요. 자세한 내용은 <a href="/disclaimer.html">투자 고지사항</a> 참고.
  </div>
</div>
"""

    page = HEAD + body + FOOT
    page = (page
            .replace("__TITLE__", esc(title))
            .replace("__DESC__", esc(desc))
            .replace("__CANONICAL__", canonical)
            .replace("__JSONLD__", jsonld)
            .replace("__GTAG__", GTAG_ID)
            .replace("__ADS__", ADSENSE_CLIENT))
    return page


def render_hub_page(date, generated):
    overseas = [(i, m) for i, m in ETFS.items() if m["region"] == "해외" and i in generated]
    domestic = [(i, m) for i, m in ETFS.items() if m["region"] == "국내" and i in generated]

    def cards(items):
        out = ""
        for i, m in items:
            out += (
                f"<a href='/etf/{m['slug']}.html' style='display:block;background:var(--surface);"
                f"border:1px solid var(--border);border-radius:10px;padding:16px 18px;"
                f"text-decoration:none;margin-bottom:10px'>"
                f"<div style='font-size:15px;font-weight:700;color:#f1f5f9'>{esc(m['name'])}</div>"
                f"<div style='font-size:12px;color:#64748b;margin-top:4px'>{esc(m['desc'])}</div></a>"
            )
        return out

    title = "ETF 목록 — 국내 액티브 ETF 보유종목 한눈에 | Active ETF Tracker"
    desc = "타임폴리오 등 국내 액티브 ETF의 보유종목·구성종목 현황을 ETF별로 정리한 목록 페이지. 매일 자동 업데이트."
    canonical = f"{SITE}/etf/"
    jsonld = '{"@context":"https://schema.org","@type":"CollectionPage","name":"ETF 목록","url":"' + canonical + '"}'

    body = f"""
<div class="container">
  <div class="breadcrumb"><a href="/">홈</a> &nbsp;›&nbsp; ETF 목록</div>
  <h1 class="title">ETF 목록</h1>
  <p class="subtitle">국내 액티브 ETF의 보유종목·구성종목 현황 · 기준일 {esc(date)} · 매일 자동 업데이트</p>

  <h2 class="sec">🌍 해외투자</h2>
  {cards(overseas)}

  <h2 class="sec">🇰🇷 국내투자</h2>
  {cards(domestic)}

  <div class="cta-box">
    <h3>📈 전체 ETF를 한 화면에서 비교</h3>
    <p>여러 ETF의 공통 보유 종목과 차이를 인터랙티브하게 비교해보세요.</p>
    <a href="/" class="cta-btn">Active ETF Tracker 바로가기 →</a>
  </div>
</div>
"""
    page = HEAD + body + FOOT
    page = (page
            .replace("__TITLE__", esc(title))
            .replace("__DESC__", esc(desc))
            .replace("__CANONICAL__", canonical)
            .replace("__JSONLD__", jsonld)
            .replace("__GTAG__", GTAG_ID)
            .replace("__ADS__", ADSENSE_CLIENT))
    return page


def write_sitemap(generated):
    today = datetime.date.today().isoformat()
    statics = ["/", "/guide.html", "/glossary.html", "/about.html",
               "/disclaimer.html", "/privacy.html", "/etf/"]
    urls = statics + [f"/etf/{ETFS[i]['slug']}.html" for i in generated]
    items = ""
    for u in urls:
        items += (f"  <url><loc>{SITE}{u}</loc>"
                  f"<lastmod>{today}</lastmod>"
                  f"<changefreq>daily</changefreq></url>\n")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{items}</urlset>\n")
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"  ✓ sitemap.xml ({len(urls)} URLs)")


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
def main():
    print("=== ETF 페이지 생성 시작 ===")
    date = get_latest_date()
    if not date:
        print("✗ 최신 날짜를 찾을 수 없습니다. 종료.")
        sys.exit(1)
    print(f"기준일: {date}")

    os.makedirs("etf", exist_ok=True)
    generated = []

    for idx, meta in ETFS.items():
        try:
            holdings = get_holdings(idx, date)
            nav = get_nav(idx, date)
            page = render_etf_page(idx, meta, date, holdings, nav)
            path = os.path.join("etf", f"{meta['slug']}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(page)
            generated.append(idx)
            print(f"  ✓ {meta['slug']}.html ({len(holdings)}종목)")
        except Exception as e:
            print(f"  ✗ {meta['slug']} 실패: {e}")

    # 허브 페이지
    with open(os.path.join("etf", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_hub_page(date, generated))
    print(f"  ✓ etf/index.html (허브)")

    # 사이트맵
    write_sitemap(generated)

    print(f"=== 완료: {len(generated)}개 ETF 페이지 생성 ===")


if __name__ == "__main__":
    main()
