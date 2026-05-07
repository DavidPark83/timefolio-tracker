"""
TIMEFOLIO ETF 기준가격(standard_price) 과거 데이터 백필 v2
=============================================================
수정 내용:
  - nav_xls.php 엑셀에 날짜 파라미터(navStartDate, navEndDate) 추가
    → 상장일 ~ 오늘 전체 기간을 한 번에 요청
  - 엑셀 실패 시 HTML 페이지(m11_view.php) 크롤링으로 fallback
    (연도 단위로 쪼개서 페이지 제한 우회)

사용법:
  export SUPABASE_URL="https://lqpqummcoujmymydftlg.supabase.co"
  export SUPABASE_KEY="<service_role_key>"
  python backfill_standard_price.py

  # 특정 ETF만 테스트
  ETF_IDX=22 python backfill_standard_price.py

  # 누락된 구간만 재처리
  START_DATE=2026-04-01 python backfill_standard_price.py
"""

import os
import sys
import time
import io
import re
from datetime import datetime, date
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pandas as pd
from supabase import create_client

# ============================================================
# 설정
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ 환경변수 SUPABASE_URL / SUPABASE_KEY 미설정")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://timeetf.co.kr/",
}

REQUEST_TIMEOUT = 30
DELAY_BETWEEN_ETF  = 2.0
DELAY_BETWEEN_PAGE = 1.0
UPSERT_BATCH_SIZE  = 100

SINGLE_ETF_IDX = os.environ.get("ETF_IDX", "")
START_DATE_STR  = os.environ.get("START_DATE", "")
TODAY = str(date.today())

# 18개 ETF (상장일 포함 → 요청 범위 최소화)
ETF_LIST = [
    {"idx": 22, "etf_name": "글로벌탑픽액티브",           "listed": "2025-10-28"},
    {"idx": 6,  "etf_name": "글로벌AI인공지능액티브",      "listed": "2024-02-20"},
    {"idx": 20, "etf_name": "글로벌우주테크&방산액티브",    "listed": "2025-04-22"},
    {"idx": 8,  "etf_name": "글로벌소비트렌드액티브",       "listed": "2024-04-09"},
    {"idx": 9,  "etf_name": "글로벌바이오액티브",           "listed": "2024-04-09"},
    {"idx": 2,  "etf_name": "미국나스닥100액티브",          "listed": "2023-06-20"},
    {"idx": 5,  "etf_name": "미국S&P500액티브",             "listed": "2023-10-17"},
    {"idx": 18, "etf_name": "미국배당다우존스액티브",       "listed": "2025-01-21"},
    {"idx": 10, "etf_name": "미국나스닥100채권혼합50액티브","listed": "2024-06-04"},
    {"idx": 12, "etf_name": "Korea플러스배당액티브",        "listed": "2024-09-10"},
    {"idx": 15, "etf_name": "코리아밸류업액티브",           "listed": "2024-11-26"},
    {"idx": 11, "etf_name": "코스피액티브",                 "listed": "2024-06-04"},
    {"idx": 24, "etf_name": "코스닥액티브",                 "listed": "2026-01-14"},
    {"idx": 13, "etf_name": "K바이오액티브",                "listed": "2024-06-04"},
    {"idx": 16, "etf_name": "K신재생에너지액티브",          "listed": "2024-11-26"},
    {"idx": 17, "etf_name": "K이노베이션액티브",            "listed": "2024-11-26"},
    {"idx": 1,  "etf_name": "K컬처액티브",                  "listed": "2022-10-25"},
    {"idx": 19, "etf_name": "차이나AI테크액티브",           "listed": "2025-03-25"},
]

# ============================================================
# 유틸
# ============================================================

def to_float(s) -> Optional[float]:
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").replace(" ", "").strip()
    if not s or s in ("-", "nan", "None", ""):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None

def parse_date_str(s: str) -> Optional[str]:
    """'2026.05.06' 또는 '2026-05-06' → 'YYYY-MM-DD'"""
    s = str(s).strip()
    m = re.match(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

def fetch_with_retry(url: str, max_retry: int = 3) -> Optional[requests.Response]:
    for attempt in range(1, max_retry + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if res.status_code == 200:
                return res
            print(f"  ⚠️ HTTP {res.status_code} (시도 {attempt}/{max_retry})")
        except Exception as e:
            print(f"  ⚠️ 요청 실패: {e} (시도 {attempt}/{max_retry})")
        if attempt < max_retry:
            time.sleep(2 * attempt)
    return None

# ============================================================
# 방법 1: 엑셀 다운로드 (날짜 파라미터 포함)
# ============================================================

def download_via_excel(idx: int, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """
    nav_xls.php에 navStartDate/navEndDate 파라미터를 붙여
    원하는 기간의 기준가격 엑셀을 한 번에 다운로드.
    """
    url = (
        f"https://timeetf.co.kr/nav_xls.php"
        f"?idx={idx}&navStartDate={start_date}&navEndDate={end_date}"
    )
    print(f"  📥 [엑셀] {url}")

    res = fetch_with_retry(url)
    if not res or not res.content:
        print(f"  ❌ 응답 없음")
        return None

    print(f"  📄 크기: {len(res.content):,} bytes")

    df = None
    for engine in ["openpyxl", "xlrd", "html"]:
        try:
            if engine == "html":
                tables = pd.read_html(io.BytesIO(res.content), dtype=str)
                df = tables[0] if tables else None
            else:
                df = pd.read_excel(io.BytesIO(res.content), dtype=str, engine=engine)
            if df is not None and not df.empty:
                print(f"  ✅ 파싱 성공 ({engine}): {len(df)}행")
                break
        except Exception:
            df = None

    if df is None or df.empty:
        return None

    df.columns = [str(c).strip() for c in df.columns]
    date_col  = df.columns[0]
    price_col = next(
        (c for c in df.columns if "기준가격" in c or ("기준가" in c and "과표" not in c)),
        df.columns[1] if len(df.columns) >= 2 else None
    )
    if not price_col:
        print(f"  ❌ 기준가격 컬럼 못 찾음: {list(df.columns)}")
        return None

    print(f"  📋 날짜=[{date_col}] / 기준가=[{price_col}]")

    records = []
    for _, row in df.iterrows():
        d = parse_date_str(str(row[date_col]))
        p = to_float(row[price_col])
        if d and p and p > 0:
            records.append({"date": d, "standard_price": p})

    if not records:
        print(f"  ⚠️ 파싱 레코드 없음 (날짜 파라미터 미지원 가능성)")
        return None

    result = pd.DataFrame(records)
    print(f"  📊 {len(result)}개 ({result['date'].min()} ~ {result['date'].max()})")
    return result

# ============================================================
# 방법 2: HTML 페이지 크롤링 (Fallback)
# ============================================================

def _parse_price_table(soup: BeautifulSoup) -> list:
    """기준가격 테이블에서 (date, standard_price) 레코드 추출."""
    records = []
    date_pattern = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or not date_pattern.match(cells[0]):
                continue
            d = parse_date_str(cells[0])
            p = to_float(cells[1])
            if d and p and p > 0:
                records.append({"date": d, "standard_price": p})
    return records

def crawl_html_chunk(idx: int, start_date: str, end_date: str) -> list:
    """m11_view.php에 날짜 파라미터를 넣어 해당 기간 테이블 크롤링."""
    url = (
        f"https://timeetf.co.kr/m11_view.php"
        f"?idx={idx}&cate=&navStartDate={start_date}&navEndDate={end_date}#standardPrice"
    )
    print(f"  🌐 [HTML] {start_date} ~ {end_date}")
    res = fetch_with_retry(url)
    if not res:
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    return _parse_price_table(soup)

def crawl_html_by_year(idx: int, fetch_start: str, fetch_end: str) -> Optional[pd.DataFrame]:
    """
    연도 단위로 쪼개서 HTML 크롤링 → 합치기.
    (한 페이지에 표시 개수 제한이 있을 수 있으므로 연도별 분할)
    """
    start = datetime.strptime(fetch_start, "%Y-%m-%d").date()
    end   = datetime.strptime(fetch_end,   "%Y-%m-%d").date()

    all_records = []
    current_year = start.year

    while True:
        chunk_start = date(current_year, 1, 1) if current_year > start.year else start
        chunk_end   = date(current_year, 12, 31)

        if chunk_start > end:
            break
        if chunk_end > end:
            chunk_end = end

        records = crawl_html_chunk(idx, str(chunk_start), str(chunk_end))
        if records:
            all_records.extend(records)
            print(f"  → {len(records)}개 수집 ({chunk_start} ~ {chunk_end})")
        else:
            print(f"  → 데이터 없음 ({chunk_start} ~ {chunk_end})")

        if chunk_end >= end:
            break
        current_year += 1
        time.sleep(DELAY_BETWEEN_PAGE)

    if not all_records:
        return None

    df = (pd.DataFrame(all_records)
          .drop_duplicates("date")
          .sort_values("date")
          .reset_index(drop=True))
    print(f"  📊 HTML 총 {len(df)}개 ({df['date'].min()} ~ {df['date'].max()})")
    return df

# ============================================================
# Supabase upsert
# ============================================================

def upsert_standard_prices(idx: int, etf_name: str, df: pd.DataFrame,
                            start_date_filter: Optional[str] = None) -> int:
    if start_date_filter:
        before = len(df)
        df = df[df["date"] >= start_date_filter].copy()
        print(f"  📅 날짜 필터 {start_date_filter} 이후: {len(df)}/{before}개")

    if df.empty:
        print(f"  ⚠️ 저장할 데이터 없음")
        return 0

    rows = [
        {"date": row["date"], "etf_idx": idx, "etf_name": etf_name,
         "standard_price": row["standard_price"]}
        for _, row in df.iterrows()
    ]

    total = 0
    for i in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[i:i + UPSERT_BATCH_SIZE]
        try:
            supabase.table("etf_daily").upsert(
                batch, on_conflict="date,etf_idx"
            ).execute()
            total += len(batch)
            print(f"  💾 upsert {i+1}~{i+len(batch)}/{len(rows)}행")
        except Exception as e:
            print(f"  ❌ upsert 실패 (배치 {i}): {e}")

    return total

# ============================================================
# ETF 1개 처리
# ============================================================

def process_etf(etf: dict, start_date_filter: Optional[str] = None) -> int:
    idx      = etf["idx"]
    name     = etf["etf_name"]
    listed   = etf["listed"]

    # 요청 시작일: START_DATE 필터가 상장일보다 늦으면 필터 날짜 사용
    fetch_start = listed
    if start_date_filter and start_date_filter > listed:
        fetch_start = start_date_filter

    print(f"\n{'='*58}")
    print(f"📈 [{idx}] {name}  (상장: {listed})")
    print(f"   요청 범위: {fetch_start} ~ {TODAY}")
    print(f"{'='*58}")

    df = None

    # ── 방법 1: 날짜 파라미터 포함 엑셀 ──────────────────────
    df = download_via_excel(idx, fetch_start, TODAY)

    # ── 방법 2: HTML 크롤링 fallback ─────────────────────────
    if df is None or df.empty:
        print(f"  🔄 엑셀 실패 → HTML 크롤링 fallback")
        df = crawl_html_by_year(idx, fetch_start, TODAY)

    if df is None or df.empty:
        print(f"  ❌ 수집 실패, 스킵")
        return 0

    count = upsert_standard_prices(idx, name, df, start_date_filter)
    print(f"  ✅ 총 {count}개 저장 완료")
    return count

# ============================================================
# 메인
# ============================================================

def main():
    print("=" * 60)
    print("🗃️  TIMEFOLIO ETF 기준가격 백필 v2")
    print(f"   실행일: {TODAY}")
    print("=" * 60)

    start_filter = START_DATE_STR or None
    if start_filter:
        try:
            datetime.strptime(start_filter, "%Y-%m-%d")
            print(f"📅 START_DATE 필터: {start_filter} 이후만 처리")
        except ValueError:
            print(f"❌ START_DATE 형식 오류: '{start_filter}'  (YYYY-MM-DD)")
            sys.exit(1)
    else:
        print("📅 START_DATE 필터: 없음 (상장일부터 전체 처리)")

    if SINGLE_ETF_IDX:
        target_etfs = [e for e in ETF_LIST if str(e["idx"]) == str(SINGLE_ETF_IDX)]
        if not target_etfs:
            print(f"❌ ETF_IDX={SINGLE_ETF_IDX} 없음")
            sys.exit(1)
        print(f"🎯 단일 ETF: [{target_etfs[0]['etf_name']}]")
    else:
        target_etfs = ETF_LIST
        print(f"🎯 전체 {len(target_etfs)}개 ETF")

    print("=" * 60)

    total_rows = 0
    success    = 0
    failed     = []

    for i, etf in enumerate(target_etfs, 1):
        print(f"\n[{i}/{len(target_etfs)}]")
        try:
            n = process_etf(etf, start_filter)
            total_rows += n
            if n > 0:
                success += 1
            else:
                failed.append(etf["etf_name"])
        except Exception as e:
            print(f"  ❌ 예외: {e}")
            import traceback; traceback.print_exc()
            failed.append(etf["etf_name"])

        if i < len(target_etfs):
            print(f"  ⏳ {DELAY_BETWEEN_ETF}초 대기...")
            time.sleep(DELAY_BETWEEN_ETF)

    print(f"\n{'='*60}")
    print(f"🎉 백필 완료!")
    print(f"   성공: {success}/{len(target_etfs)} ETF")
    print(f"   총 저장: {total_rows:,}개 행")
    if failed:
        print(f"   실패/스킵: {', '.join(failed)}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
