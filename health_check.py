#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
health_check.py — etftracker.co.kr 종합 점검

이 대화에서 실제로 발생했던 문제들을 재발 즉시 잡기 위한 체크리스트:
  - 홈페이지 "데이터 계산 중..." 재발 (프리렌더 실패)
  - 방법론 섹션 유실
  - 브리핑 며칠째 정지 (파이프라인 조용한 죽음)
  - 브리핑 제목 중복 재발 (앵글 로직 회귀)
  - 운용 코멘터리 누락 (커밋 타이밍 문제 재발)
  - ads.txt 소실
  - 블로그 목록 정상 여부

모든 검사는 라이브 사이트를 대상으로 하며, Googlebot User-Agent로 요청해
"심사자가 실제로 보는 화면" 기준으로 판정한다. 표준 라이브러리만 사용.

사용법:
  python3 health_check.py                    # 전체 검사, 요약 출력
  python3 health_check.py --notify            # 실패 시 macOS 알림
  python3 health_check.py --verbose           # 상세 로그
  python3 health_check.py --stale-days 3      # 브리핑 최신성 허용 일수 (기본 3)

exit code: 0 = 전체 통과, 1 = 하나 이상 실패
"""
import argparse
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

BASE = "https://www.etftracker.co.kr"
UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
TIMEOUT = 20


class Result:
    def __init__(self):
        self.rows = []   # (status, label, detail)
        self.fail = False

    def ok(self, label, detail=""):
        self.rows.append(("OK", label, detail))

    def warn(self, label, detail=""):
        self.rows.append(("WARN", label, detail))

    def bad(self, label, detail=""):
        self.rows.append(("FAIL", label, detail))
        self.fail = True


def fetch(path, timeout=TIMEOUT):
    """Googlebot UA로 페이지를 가져온다. 실패 시 (None, 에러메시지)"""
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace"), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception as e:
        return None, str(e)


def strip_tags(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.I | re.S)
    return re.sub(r"<[^>]+>", " ", html)


# ── 파싱 로직 (테스트 가능하도록 fetch와 분리) ──────────────────────
def check_homepage_content(html):
    """홈페이지 HTML에서 플레이스홀더/방법론 섹션 여부 판정.
    반환: (placeholder_count, has_methodology)"""
    placeholder_count = html.count("데이터 계산 중")
    has_methodology = 'id="methodology"' in html
    return placeholder_count, has_methodology


def extract_briefing_dates(listing_html):
    """브리핑 목록 페이지에서 /briefing/YYYY-MM-DD 링크의 날짜를 최신순으로 추출"""
    dates = re.findall(r'/briefing/(\d{4}-\d{2}-\d{2})', listing_html)
    seen, out = set(), []
    for d in dates:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out  # 문서 등장 순서 (보통 최신 먼저)


def extract_briefing_titles(listing_html):
    """브리핑 목록에서 각 카드의 제목(헤드라인) 텍스트를 추출.
    카드 마크업: <a href="/briefing/DATE...">...날짜뱃지...요일...제목...</a> 형태를 가정하고,
    href로 앵커를 나눠 각 블록의 텍스트에서 헤드라인만 뽑는다."""
    blocks = re.split(r'(?=<a[^>]*href="/briefing/\d{4}-\d{2}-\d{2})', listing_html)
    titles = []
    for b in blocks:
        m = re.search(r'/briefing/(\d{4}-\d{2}-\d{2})', b)
        if not m:
            continue
        text = strip_tags(b)
        text = re.sub(r"\s+", " ", text).strip()
        # "7월 31일 금요일 <제목> <제목반복태그>" 형태 → 요일 뒤부터 취해서 앞부분만
        wd = re.search(r"(월|화|수|목|금|토|일)요일\s*(.+)", text)
        headline = wd.group(2).strip() if wd else text
        # 카드 안에 태그(칩)로 헤드라인이 한 번 더 반복되는 구버전 대비, 앞 60자만 지문으로 사용
        titles.append((m.group(1), headline[:80]))
    return titles


def dup_titles(titles, recent_n=15):
    """최근 N개 중 완전히 같은 헤드라인이 있는지"""
    recent = [t for _, t in titles[:recent_n]]
    c = Counter(recent)
    return {t: n for t, n in c.items() if n > 1}


def extract_blog_count(blog_html):
    """블로그 목록의 '전체 N' 카운트를 읽는다"""
    m = re.search(r'전체\s*(\d+)', strip_tags(blog_html))
    return int(m.group(1)) if m else None


def business_days_since(date_str, today=None):
    """해당 날짜로부터 오늘까지 평일(월~금) 며칠 지났는지"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    t = today or datetime.now().date()
    if d >= t:
        return 0
    n, cur = 0, d
    while cur < t:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


# ── 개별 검사 ───────────────────────────────────────────────────
def check_home(res, verbose):
    html, status = fetch("/")
    if html is None:
        res.bad("홈페이지 접근", f"status={status}")
        return
    if status != 200:
        res.bad("홈페이지 응답코드", f"{status}")
        return

    placeholder_n, has_method = check_homepage_content(html)
    if placeholder_n > 0:
        res.bad("홈페이지 실데이터", f"'데이터 계산 중...' {placeholder_n}곳 잔존 → 프리렌더 실패 의심")
    else:
        res.ok("홈페이지 실데이터", "플레이스홀더 없음")

    if has_method:
        res.ok("방법론 섹션", "존재")
    else:
        res.bad("방법론 섹션", "누락됨 (id=\"methodology\" 없음)")


def check_ads_robots_sitemap(res, verbose):
    for path, needle, label in [
        ("/ads.txt", "pub-9155147769106740", "ads.txt"),
        ("/robots.txt", "Sitemap:", "robots.txt"),
        ("/sitemap.xml", "<urlset", "sitemap.xml"),
    ]:
        body, status = fetch(path)
        if body is None or status != 200:
            res.bad(label, f"status={status}")
        elif needle not in body:
            res.bad(label, f"200이지만 예상 내용('{needle}') 없음")
        else:
            res.ok(label, "정상")


def check_blog(res, verbose):
    html, status = fetch("/blog")
    if html is None or status != 200:
        res.bad("블로그 목록", f"status={status}")
        return
    n = extract_blog_count(html)
    if n is None:
        res.warn("블로그 목록", "'전체 N' 카운트 파싱 실패 — 수동 확인 권장")
    elif n == 0:
        res.bad("블로그 목록", "0편")
    else:
        res.ok("블로그 목록", f"{n}편")


def check_briefing(res, verbose, stale_days):
    html, status = fetch("/briefing")
    if html is None or status != 200:
        res.bad("브리핑 목록", f"status={status}")
        return

    dates = extract_briefing_dates(html)
    if not dates:
        res.bad("브리핑 목록", "날짜를 하나도 못 찾음 — 마크업 변경 의심")
        return

    latest = dates[0]
    bdays = business_days_since(latest)
    if bdays > stale_days:
        res.bad("브리핑 최신성", f"최신 {latest} — 영업일 기준 {bdays}일 경과 (허용 {stale_days}일)")
    else:
        res.ok("브리핑 최신성", f"최신 {latest} ({bdays}영업일 전)")

    titles = extract_briefing_titles(html)
    dups = dup_titles(titles, recent_n=15)
    if dups:
        detail = ", ".join(f"'{t[:30]}…' x{n}" for t, n in dups.items())
        res.bad("브리핑 제목 다양성", f"최근 15개 중 중복 — {detail}")
    else:
        res.ok("브리핑 제목 다양성", f"최근 {min(15, len(titles))}개 전부 고유")

    if verbose:
        print("  [최근 브리핑 제목]")
        for d, t in titles[:8]:
            print(f"    {d}  {t}")

    # 최신 브리핑 상세 페이지 — 요약/코멘터리/중복 섹션 확인
    detail_html, dstatus = fetch(f"/briefing/{latest}")
    if detail_html is None or dstatus != 200:
        res.bad("최신 브리핑 상세", f"status={dstatus}")
        return

    has_lead = "briefing-lead" in detail_html or "오늘의 요약" in detail_html
    res.ok("리드 문단") if has_lead else res.warn("리드 문단", "없음")

    has_comment = "ai-narrative" in detail_html or "오늘의 운용 코멘터리" in detail_html
    if has_comment:
        res.ok("운용 코멘터리", "존재")
    else:
        res.warn("운용 코멘터리", f"{latest}에 없음 — etf_briefs 데이터 없거나 커밋 누락 가능성")

    sm_count = detail_html.count("스마트머니 시그널")
    if sm_count > 1:
        res.bad("섹션 중복", f"'스마트머니 시그널' {sm_count}회 등장 — 템플릿 중복 삽입 의심")
    else:
        res.ok("섹션 중복 없음")


def check_domain_consistency(res, verbose):
    apex_html, apex_status = fetch("https://etftracker.co.kr/briefing")
    www_html, www_status = fetch("https://www.etftracker.co.kr/briefing")
    if apex_status == 200 and www_status == 200 and apex_html == www_html:
        res.ok("apex/www 동일성", "일치")
    elif apex_status != 200 or www_status != 200:
        res.bad("apex/www 동일성", f"apex={apex_status}, www={www_status}")
    else:
        res.warn("apex/www 동일성", "응답은 되나 내용 불일치 — 캐시 시점 차이일 수 있음")


# ── 실행 ────────────────────────────────────────────────────────
def notify(msg):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "ETF Tracker 점검"'],
            check=False, timeout=5,
        )
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="실패 시 macOS 알림")
    ap.add_argument("--verbose", action="store_true", help="상세 로그 출력")
    ap.add_argument("--stale-days", type=int, default=3, help="브리핑 최신성 허용 영업일 수 (기본 3)")
    args = ap.parse_args()

    res = Result()
    print(f"etftracker.co.kr 점검 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    check_home(res, args.verbose)
    check_ads_robots_sitemap(res, args.verbose)
    check_blog(res, args.verbose)
    check_briefing(res, args.verbose, args.stale_days)
    check_domain_consistency(res, args.verbose)

    print()
    for status, label, detail in res.rows:
        mark = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[status]
        line = f"{mark} {label}"
        if detail:
            line += f" — {detail}"
        print(line)

    n_fail = sum(1 for s, _, _ in res.rows if s == "FAIL")
    n_warn = sum(1 for s, _, _ in res.rows if s == "WARN")
    print()
    print(f"결과: FAIL {n_fail} / WARN {n_warn} / 전체 {len(res.rows)}항목")

    if res.fail:
        if args.notify:
            notify(f"점검 실패 {n_fail}건 — 로그 확인 필요")
        sys.exit(1)
    else:
        print("전체 통과")
        sys.exit(0)


if __name__ == "__main__":
    main()