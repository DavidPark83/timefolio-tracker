#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inject_narrative.py — Supabase etf_briefs에 이미 매일 쌓이고 있는 LLM 해설(brief_text)을
briefing/YYYY-MM-DD.html 정적 페이지에 '오늘의 운용 코멘터리' 섹션으로 주입

배경:
  - 애드센스 '저가치 콘텐츠' 판정의 핵심 원인 = 브리핑 페이지가 표(table)로만 구성
  - 그런데 etf_briefs 테이블에는 ETF별 편집 해설이 이미 7/3까지 매일 생성돼 있음
  - 이 스크립트는 새 LLM 호출 없이, 있는 해설을 정적 HTML에 실어주는 다리 역할

특징:
  - 멱등: 마커(<!-- ai-narrative:start/end -->) 사이를 교체하므로 여러 번 실행해도 안전
  - 의존성: 표준 라이브러리만 사용 (urllib로 Supabase REST 호출)

사용:
  export SUPABASE_SERVICE_KEY="..."           # (또는 SUPABASE_KEY)
  python3 inject_narrative.py --root . --date 2026-07-03
  python3 inject_narrative.py --root .                 # 날짜 생략 시 briefing/ 최신 파일
  python3 inject_narrative.py --demo --root .          # 네트워크 없이 샘플로 미리보기
"""
import argparse
import html as htmllib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://lqpqummcoujmymydftlg.supabase.co")
PROVIDER_LABEL = {"timefolio": "TIME", "samsungactive": "KoAct"}
PROVIDERS = ["timefolio", "samsungactive"]
TOP_K_PER_PROVIDER = 3   # provider별 AUM 상위 몇 개 ETF의 코멘터리를 실을지 (TIME 3 + KoAct 3 = 총 6)
BULLETS_PER_ETF = 3
MARK_S = "<!-- ai-narrative:start -->"
MARK_E = "<!-- ai-narrative:end -->"

DEMO_ROWS = [{
    "etf_name": "KoAct K수출핵심기업TOP30액티브", "provider": "samsungactive",
    "nav_total": 151300000000,
    "brief_text": ("### 포트폴리오 핵심 요약\n"
                   "**집중 투자**: SK하이닉스 비중이 18.6%로 가장 높아 반도체 집중도가 큽니다.\n"
                   "**적극적 리밸런싱**: 신규 편입·제외가 빈번해 시장 변화에 적극 대응 중입니다.\n"
                   "## 주요 변화 분석\n**신규 편입**: ...")
}]


def esc(s: str) -> str:
    return htmllib.escape(str(s), quote=True)


# ---------------------------------------------------------------- Supabase
def fetch_briefs(base_date: str):
    """provider(TIME/KoAct)별로 각각 AUM 상위 TOP_K_PER_PROVIDER개씩 조회 →
    한쪽 provider가 통째로 밀리는 것 방지 (예: TIME AUM이 커서 KoAct가 전멸하는 문제)"""
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    if not key:
        sys.exit("[ERROR] SUPABASE_SERVICE_KEY(또는 SUPABASE_KEY) 환경변수가 필요합니다")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    all_rows = []
    for provider in PROVIDERS:
        qs = urllib.parse.urlencode({
            "select": "etf_name,provider,brief_text,nav_total",
            "base_date": f"eq.{base_date}",
            "provider": f"eq.{provider}",
            "order": "nav_total.desc",
            "limit": str(TOP_K_PER_PROVIDER),
        })
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/etf_briefs?{qs}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = json.loads(r.read().decode("utf-8"))
        if not rows:
            print(f"  [WARN] provider={provider} 데이터 없음 (base_date={base_date})")
        all_rows.extend(rows)
    return all_rows


# ---------------------------------------------------------------- 해설 파싱
def summary_bullets(brief_text: str, limit: int = BULLETS_PER_ETF):
    """brief_text의 첫 섹션(핵심 요약)에서 '**라벨**: 내용' 불릿을 추출"""
    first = re.split(r"\n##\s", brief_text, maxsplit=1)[0]   # '## 주요 변화 분석' 이전까지
    out = []
    for line in first.splitlines():
        m = re.match(r"\*\*(.+?)\*\*\s*[:：]?\s*(.+)", line.strip())
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
        if len(out) >= limit:
            break
    return out


def build_section(base_date: str, rows) -> str:
    y, m, d = base_date.split("-")
    blocks = []
    for r in rows:
        bl = summary_bullets(r.get("brief_text") or "")
        if not bl:
            continue
        tag = PROVIDER_LABEL.get(r.get("provider", ""), r.get("provider", ""))
        lis = "".join(f"<li><strong>{esc(k)}</strong> — {esc(v)}</li>" for k, v in bl)
        blocks.append(
            f'<div class="ain-etf"><h3><span class="ain-tag">{esc(tag)}</span>'
            f'{esc(r["etf_name"])}</h3><ul>{lis}</ul></div>'
        )
    if not blocks:
        return ""
    style = (
        "<style>"
        ".ain{background:#151a33;border:1px solid #262d55;border-radius:16px;"
        "padding:22px 24px;margin:26px 0}"
        ".ain h2{font-size:19px;margin:0 0 4px}"
        ".ain .ain-sub{color:#98a1c8;font-size:12.5px;margin:0 0 14px}"
        ".ain .ain-etf{margin:14px 0 0}"
        ".ain h3{font-size:15px;margin:0 0 8px;display:flex;align-items:center;gap:8px}"
        ".ain .ain-tag{background:#232a52;color:#a7b2ff;font-size:11.5px;"
        "padding:2px 8px;border-radius:6px;font-weight:700}"
        ".ain ul{margin:0;padding-left:18px}"
        ".ain li{font-size:14px;line-height:1.7;color:#cdd3ee;margin:4px 0}"
        ".ain li strong{color:#e9ecfb}"
        "</style>"
    )
    return (
        f"{MARK_S}{style}"
        f'<section class="ain"><h2>🧠 오늘의 운용 코멘터리</h2>'
        f'<p class="ain-sub">{int(m)}월 {int(d)}일 보유종목 변화 데이터를 바탕으로 '
        f'자동 생성된 해설 요약입니다 · 투자 권유가 아닙니다</p>'
        f'{"".join(blocks)}</section>{MARK_E}'
    )


# ---------------------------------------------------------------- 주입
def inject(page: str, section: str) -> str:
    # 이미 주입돼 있으면 교체 (멱등)
    if MARK_S in page and MARK_E in page:
        pre, rest = page.split(MARK_S, 1)
        _, post = rest.split(MARK_E, 1)
        return pre + section + post
    # '스마트머니 시그널' 제목 앞(= 시장 스냅샷 뒤)에 삽입
    idx = page.find("스마트머니 시그널")
    if idx != -1:
        anchor = max(page.rfind("<h2", 0, idx), page.rfind("<section", 0, idx))
        if anchor != -1:
            return page[:anchor] + section + page[anchor:]
    # 폴백: 첫 </h1> 바로 뒤
    m = re.search(r"</h1>", page, re.I)
    if m:
        return page[:m.end()] + section + page[m.end():]
    return page + section


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="정적 사이트 루트 (briefing/ 폴더의 부모)")
    ap.add_argument("--date", help="YYYY-MM-DD (생략 시 briefing/ 최신 파일)")
    ap.add_argument("--demo", action="store_true", help="네트워크 없이 샘플 데이터로 미리보기")
    args = ap.parse_args()

    brief_dir = Path(args.root).resolve() / "briefing"
    if args.date:
        target = brief_dir / f"{args.date}.html"
    else:
        files = sorted(brief_dir.glob("2*.html"))
        if not files:
            sys.exit(f"[ERROR] {brief_dir}에 브리핑 파일 없음")
        target = files[-1]
    if not target.exists():
        sys.exit(f"[ERROR] {target} 없음 — 개별 브리핑 페이지를 먼저 생성하세요")

    base_date = target.stem
    rows = DEMO_ROWS if args.demo else fetch_briefs(base_date)
    if not rows:
        sys.exit(f"[WARN] etf_briefs에 base_date={base_date} 데이터 없음 — 주입 생략")

    section = build_section(base_date, rows)
    if not section:
        sys.exit(f"[WARN] {base_date} 해설에서 요약 불릿을 찾지 못함 — 주입 생략")

    page = target.read_text(encoding="utf-8", errors="replace")
    new_page = inject(page, section)
    target.write_text(new_page, encoding="utf-8")
    print(f"[OK] {target.name} ← 코멘터리 {len(rows)}개 ETF 주입 "
          f"({'교체' if MARK_S in page else '신규'}, +{len(new_page)-len(page):+,}자)")


if __name__ == "__main__":
    main()