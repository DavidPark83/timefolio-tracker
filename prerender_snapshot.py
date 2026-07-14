#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prerender_snapshot.py — 홈페이지의 "데이터 계산 중..." 문제 해결

배경:
  index.html의 7개 분석 카드(aaSectorBet, aaSectorFlow, aaScoresBuy, aaScoresSell,
  aaDivergence, aaSpread, aaStealth)는 순수 클라이언트 JS가 브라우저에서
  Supabase를 fetch해 채운다. 그래서 크롤러(및 JS 실행 전 최초 HTML)는
  "데이터 계산 중..." 플레이스홀더만 본다.

해법 (제1원칙 — 최소 변경으로 최대 효과):
  7개 카드의 계산 로직을 Python으로 재구현하지 않는다(이미 잘 작동하는 로직을
  중복 유지하면 두 로직이 갈라질 위험만 커진다). 대신 Playwright 헤드리스
  브라우저로 실제 페이지를 그대로 실행시켜, JS가 다 채운 뒤의 최종 HTML을
  "스냅샷"으로 떠서 정적 index.html의 같은 위치에 얼려넣는다.

  - 실사용자: 페이지 열리면 기존 <script>가 그대로 실행되어 실시간 데이터로 다시 채움
    (스냅샷은 JS 로딩 전 잠깐 보이는 대체 콘텐츠 역할)
  - 크롤러: JS 실행 여부와 무관하게 스냅샷 텍스트를 바로 본다

  마커 없이 id 속성(aaSectorBet 등)을 앵커로 삼아 해당 div의 innerHTML만
  치환하므로 여러 번 실행해도 안전(멱등) — 항상 최신 스냅샷으로 덮어씀.

설치 (최초 1회, 로컬 Mac에서):
  pip install playwright --break-system-packages
  playwright install chromium

사용법:
  python3 prerender_snapshot.py --root ~/vscode/timefolio-tracker
  python3 prerender_snapshot.py --root ~/vscode/timefolio-tracker --url https://www.etftracker.co.kr/
"""
import argparse
import re
import sys
from pathlib import Path

CARD_IDS = [
    "aaSectorBet", "aaSectorFlow", "aaScoresBuy", "aaScoresSell",
    "aaDivergence", "aaSpread", "aaStealth",
]
EMPTY_MARK = "데이터 계산 중..."
WAIT_TIMEOUT_MS = 45_000  # Supabase fetch + 렌더링 대기 상한


def check_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        sys.exit(
            "[ERROR] playwright 미설치.\n"
            "  pip install playwright --break-system-packages\n"
            "  playwright install chromium"
        )


def capture_rendered_cards(url: str) -> dict:
    """헤드리스 브라우저로 실제 페이지를 열어 7개 카드가 채워질 때까지 대기 후
    각 카드의 최종 innerHTML을 수집"""
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        print(f"[INFO] 페이지 로드: {url}")
        page.goto(url, wait_until="networkidle", timeout=WAIT_TIMEOUT_MS)

        for cid in CARD_IDS:
            try:
                # "데이터 계산 중..."이 사라질 때까지 대기 (최대 WAIT_TIMEOUT_MS)
                page.wait_for_function(
                    f"""() => {{
                        const el = document.getElementById('{cid}');
                        return el && !el.innerText.includes('{EMPTY_MARK}');
                    }}""",
                    timeout=WAIT_TIMEOUT_MS,
                )
                html = page.eval_on_selector(f"#{cid}", "el => el.innerHTML")
                results[cid] = html
                print(f"  [OK]   #{cid}  ({len(html)}자)")
            except Exception as e:
                print(f"  [SKIP] #{cid}  채워지지 않음 ({type(e).__name__}) — 원본 유지")

        browser.close()
    return results


DIV_TAG_RE = re.compile(r"<(/?)div\b[^>]*>", re.IGNORECASE)


def find_div_span(html: str, open_tag_end: int) -> int:
    """open_tag_end(여는 <div ...> 태그의 '>' 다음 위치)부터 태그 깊이를 추적해
    짝이 맞는 </div>의 시작 인덱스를 반환. 렌더링된 내용에 div가 몇 겹 중첩돼도
    정확히 대응 짝을 찾는다(단순 정규식 그리디/논그리디 매칭은 신뢰 불가)."""
    depth = 1
    for m in DIV_TAG_RE.finditer(html, open_tag_end):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.start()
    raise ValueError("짝이 맞는 </div>를 찾지 못함 (마크업 손상 의심)")


def inject(index_html: str, snapshots: dict) -> tuple[str, int]:
    """id="aaXxx" 를 가진 div의 innerHTML을 스냅샷으로 교체.
    태그 깊이 추적으로 정확한 종료 지점을 찾으므로, 스냅샷 안에 표·차트 등
    중첩 div가 얼마나 있든 안전하게 해당 div만 치환한다."""
    changed = 0
    for cid, html in snapshots.items():
        id_pos = index_html.find(f'id="{cid}"')
        if id_pos == -1:
            print(f"  [WARN] id={cid} 를 index.html에서 찾지 못함")
            continue
        # id 속성을 포함하는 여는 <div ...> 태그의 시작/끝 위치 확정
        tag_start = index_html.rfind("<div", 0, id_pos)
        tag_end = index_html.find(">", id_pos) + 1
        if tag_start == -1 or tag_end == 0:
            print(f"  [WARN] id={cid} 의 div 여는 태그를 파싱하지 못함")
            continue
        try:
            close_start = find_div_span(index_html, tag_end)
        except ValueError as e:
            print(f"  [WARN] id={cid}: {e}")
            continue
        index_html = index_html[:tag_end] + html + index_html[close_start:]
        changed += 1
    return index_html, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="정적 사이트 루트 (index.html 위치)")
    ap.add_argument("--url", default="https://www.etftracker.co.kr/",
                     help="렌더링에 사용할 실제 URL (라이브 사이트 권장 — 최신 배포 기준)")
    args = ap.parse_args()

    check_playwright()

    index_path = Path(args.root).resolve() / "index.html"
    if not index_path.exists():
        sys.exit(f"[ERROR] {index_path} 없음")

    snapshots = capture_rendered_cards(args.url)
    if not snapshots:
        sys.exit("[ERROR] 채워진 카드가 하나도 없음 — 사이트 정상 로드되는지 먼저 확인하세요")

    content = index_path.read_text(encoding="utf-8")
    new_content, changed = inject(content, snapshots)

    if changed == 0:
        sys.exit("[ERROR] 삽입된 카드 0개 — index.html 구조가 예상과 다름, 패턴 점검 필요")

    index_path.write_text(new_content, encoding="utf-8")
    print(f"\n[OK] index.html 갱신 완료 — {changed}/{len(CARD_IDS)}개 카드 스냅샷 반영")
    if changed < len(CARD_IDS):
        print(f"[WARN] {len(CARD_IDS)-changed}개는 반영 안 됨 — 위 SKIP/WARN 로그 확인")


if __name__ == "__main__":
    main()