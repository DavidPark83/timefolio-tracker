"""
TIMEFOLIO ACTIVE ETF 일일 크롤러
================================
역할: 매일 오전 9시(KST), 18개 ETF의 그날 구성종목 / NAV / 기준가격을 수집하여 Supabase에 저장
실행: GitHub Actions가 .github/workflows/crawl.yml 스케줄에 따라 자동 호출

변경 이력:
  - crawl_nav(): standard_price(기준가격) 수집 추가
    1차) nav_xls.php?navStartDate={date}&navEndDate={date} 엑셀에서 당일 행 추출
         (backfill에서 검증된 방식 — 날짜 파라미터로 원하는 기간만 정확히 요청)
    2차) 엑셀 실패 시 m11_view.php HTML 테이블 fallback 파싱
  - etf_daily upsert 시 standard_price 컬럼 함께 저장

핵심 설계 원칙:
  1. pdf_excel.php 엑셀 다운로드로 구성종목 수집 → HTML 파싱보다 안정적
  2. nav_xls.php 날짜 파라미터로 기준가격 수집 → 1개월 제한 없이 정확한 날짜 취득
  3. 요청 사이 딜레이 → 차단 방지
  4. 실패해도 다른 ETF는 계속 진행 → 부분 성공 허용
  5. upsert → 같은 날 재실행해도 안전
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import os
import sys
import time
import io
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional

import re
import requests
from bs4 import BeautifulSoup
import pandas as pd
from supabase import create_client

# ============================================================
# 설정
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role 키

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

REQUEST_TIMEOUT = 20       # 초
DELAY_BETWEEN_REQUESTS = 1.5  # 초 (차단 방지)
MAX_RETRY = 3

# 18개 ETF 목록
ETF_LIST = [
    {"idx": 22, "etf_name": "글로벌탑픽액티브"},
    {"idx": 6,  "etf_name": "글로벌AI인공지능액티브"},
    {"idx": 20, "etf_name": "글로벌우주테크&방산액티브"},
    {"idx": 8,  "etf_name": "글로벌소비트렌드액티브"},
    {"idx": 9,  "etf_name": "글로벌바이오액티브"},
    {"idx": 2,  "etf_name": "미국나스닥100액티브"},
    {"idx": 5,  "etf_name": "미국S&P500액티브"},
    {"idx": 18, "etf_name": "미국배당다우존스액티브"},
    {"idx": 10, "etf_name": "미국나스닥100채권혼합50액티브"},
    {"idx": 12, "etf_name": "Korea플러스배당액티브"},
    {"idx": 15, "etf_name": "코리아밸류업액티브"},
    {"idx": 11, "etf_name": "코스피액티브"},
    {"idx": 24, "etf_name": "코스닥액티브"},
    {"idx": 13, "etf_name": "K바이오액티브"},
    {"idx": 16, "etf_name": "K신재생에너지액티브"},
    {"idx": 17, "etf_name": "K이노베이션액티브"},
    {"idx": 1,  "etf_name": "K컬처액티브"},
    {"idx": 19, "etf_name": "차이나AI테크액티브"},
]

# ============================================================
# 유틸 함수
# ============================================================

def to_int(s) -> int:
    """문자열을 정수로 안전하게 변환 (콤마, 공백 제거)"""
    if s is None:
        return 0
    s = str(s).replace(",", "").replace(" ", "").strip()
    if not s or s == "-":
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0

def to_float(s) -> float:
    """문자열을 실수로 안전하게 변환"""
    if s is None:
        return 0.0
    s = str(s).replace(",", "").replace("%", "").replace(" ", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0

def to_float_or_none(s) -> Optional[float]:
    """변환 실패 시 None 반환 (standard_price용 — 0과 NULL을 구분)"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").replace(" ", "").strip()
    if not s or s in ("-", "nan", "None", ""):
        return None
    try:
        v = float(s)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None

def get_prev_business_date(target_date: str) -> str:
    """주어진 날짜의 직전 영업일 반환 (토·일 건너뜀, 공휴일 미처리)"""
    from datetime import timedelta
    d = datetime.strptime(target_date, "%Y-%m-%d").date()
    d -= timedelta(days=1)
    while d.weekday() >= 5:   # 5=토, 6=일
        d -= timedelta(days=1)
    return str(d)

def normalize_stock_code(raw) -> str:
    """
    종목코드를 항상 6자리 zero-padded 문자열로 정규화.
    한국 거래소 종목코드는 정확히 6자리 문자열이어야 한다 (예: 005930, 000660).
    pandas가 실수로 numeric 추론을 했거나, 이미 망가진 값("660.0")이 들어와도 복구한다.

    예시:
      "000660" → "000660"
      "660.0"  → "000660"  (망가진 값 복구)
      660      → "000660"
      "CASH"   → "CASH"    (비숫자는 그대로)
      None/""  → ""
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    if "." in s:
        try:
            s = str(int(float(s)))
        except (ValueError, TypeError):
            pass
    if s.isdigit() and len(s) <= 6:
        s = s.zfill(6)
    return s

def parse_date_str(s: str) -> Optional[str]:
    """'2026.05.06' 또는 '2026-05-06' → 'YYYY-MM-DD'. 실패 시 None."""
    m = re.match(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", str(s).strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

def fetch_with_retry(url: str, max_retry: int = MAX_RETRY) -> Optional[requests.Response]:
    """재시도 로직 포함 HTTP GET"""
    for attempt in range(1, max_retry + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if res.status_code == 200:
                return res
            print(f"  ⚠️ HTTP {res.status_code} (시도 {attempt}/{max_retry})")
        except Exception as e:
            print(f"  ⚠️ 요청 실패: {e} (시도 {attempt}/{max_retry})")
        if attempt < max_retry:
            time.sleep(2 * attempt)  # 지수 백오프
    return None

# ============================================================
# 구성종목 크롤링 (엑셀 다운로드 사용) — 기존과 동일
# ============================================================

def crawl_holdings(idx: int, etf_name: str, target_date: str) -> List[Dict]:
    """
    엑셀 다운로드 URL에서 구성종목 데이터를 가져온다.
    URL 예시: https://timeetf.co.kr/pdf_excel.php?idx=22&pdfDate=2025-12-30
    """
    url = f"https://timeetf.co.kr/pdf_excel.php?idx={idx}&pdfDate={target_date}"
    res = fetch_with_retry(url)
    if not res or not res.content:
        return []

    # ⚠️ dtype=str 필수: 종목코드 leading zero 보존 ("000660" → 660.0 방지)
    # pdf_excel.php는 항상 Excel을 반환 → read_html 시도 불필요
    try:
        df = pd.read_excel(io.BytesIO(res.content), dtype=str)
    except Exception as e:
        print(f"  ❌ 엑셀 파싱 실패: {e}")
        return []

    df.columns = [str(c).strip() for c in df.columns]
    col_map = {}
    for col in df.columns:
        if "종목코드" in col:   col_map[col] = "code"
        elif "종목명" in col:   col_map[col] = "name"
        elif "수량" in col:     col_map[col] = "qty"
        elif "평가금액" in col: col_map[col] = "value"
        elif "비중" in col:     col_map[col] = "weight"
    df = df.rename(columns=col_map)

    if not {"name", "weight"}.issubset(df.columns):
        print(f"  ❌ 필수 컬럼 누락: 보유 컬럼={list(df.columns)}")
        return []

    holdings = []
    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip()
        if not name or name.lower() in ("nan", "none", ""):
            continue
        code = normalize_stock_code(row.get("code"))
        if not code:
            code = "CASH" if "현금" in name else f"UNKNOWN_{name[:10]}"
        holdings.append({
            "date":     target_date,
            "etf_idx":  idx,
            "etf_name": etf_name,
            "code":     code,
            "name":     name,
            "qty":      to_int(row.get("qty")),
            "value":    to_int(row.get("value")),
            "weight":   to_float(row.get("weight")),
        })
    return holdings

# ============================================================
# 기준가격(standard_price) 수집 — ★ 핵심 추가 함수
# ============================================================

def _parse_standard_price_from_excel(content: bytes, target_date: str) -> Optional[float]:
    """
    nav_xls.php 응답 바이트에서 target_date 행의 기준가격(원)을 추출.
    날짜 파라미터로 1일치만 요청하므로 보통 첫 번째 데이터 행이 정답.
    """
    df = None
    for engine in ["openpyxl", "xlrd", "html"]:
        try:
            if engine == "html":
                tables = pd.read_html(io.BytesIO(content), dtype=str)
                df = tables[0] if tables else None
            else:
                df = pd.read_excel(io.BytesIO(content), dtype=str, engine=engine)
            if df is not None and not df.empty:
                break
        except Exception:
            df = None

    if df is None or df.empty:
        return None

    df.columns = [str(c).strip() for c in df.columns]
    date_col  = df.columns[0]
    price_col = next(
        (c for c in df.columns if "기준가격" in c or ("기준가" in c and "과표" not in c)),
        df.columns[1] if len(df.columns) >= 2 else None,
    )
    if not price_col:
        return None

    for _, row in df.iterrows():
        d = parse_date_str(str(row[date_col]))
        p = to_float_or_none(row[price_col])
        # 날짜가 일치하거나(정확 매칭), 유효한 첫 행 반환
        if p is not None:
            if d == target_date:
                return p
            # 날짜가 다르면(사이트가 파라미터를 무시하는 경우 대비) 그냥 첫 유효값
            # → 아래 루프를 다 돌고 나서 처리하기 위해 일단 continue

    # 정확한 날짜 매칭 실패 → 첫 번째 유효 값으로 fallback
    for _, row in df.iterrows():
        p = to_float_or_none(row[price_col])
        if p is not None:
            return p

    return None


def _parse_standard_price_from_html(soup: BeautifulSoup, target_date: str) -> Optional[float]:
    """
    m11_view.php HTML에서 기준가격 테이블을 파싱하여 target_date 행의 기준가격 반환.
    날짜 형식: 페이지는 "2026.05.06", target_date는 "2026-05-06"
    """
    date_display = target_date.replace("-", ".")  # "2026-05-06" → "2026.05.06"
    date_pattern = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or not date_pattern.match(cells[0]):
                continue
            if cells[0] == date_display:
                p = to_float_or_none(cells[1])
                if p is not None:
                    return p

    return None

def crawl_standard_price(idx: int, target_date: str) -> Optional[float]:
    """
    기준가격(standard_price) 수집.
    1차: nav_xls.php 엑셀 (날짜 파라미터)
    2차: m11_view.php HTML fallback
    """
    # ── 1차: 엑셀 ──────────────────────────────────────────
    url_xls = (
        f"https://timeetf.co.kr/nav_xls.php"
        f"?idx={idx}&navStartDate={target_date}&navEndDate={target_date}"
    )
    res = fetch_with_retry(url_xls)
    if res and res.content:
        price = _parse_standard_price_from_excel(res.content, target_date)
        if price is not None:
            print(f"  💰 standard_price (엑셀): {price:,.2f}원")
            return price
        print(f"  ⚠️ 엑셀 파싱 실패 → HTML fallback")
    else:
        print(f"  ⚠️ 엑셀 응답 없음 → HTML fallback")

    # ── 2차: HTML fallback ──────────────────────────────────
    url_html = (
        f"https://timeetf.co.kr/m11_view.php"
        f"?idx={idx}&cate=&navStartDate={target_date}&navEndDate={target_date}#standardPrice"
    )
    res2 = fetch_with_retry(url_html)
    if res2:
        soup = BeautifulSoup(res2.text, "html.parser")
        price = _parse_standard_price_from_html(soup, target_date)
        if price is not None:
            print(f"  💰 standard_price (HTML): {price:,.2f}원")
            return price

    print(f"  ⚠️ standard_price 수집 실패 (주말/공휴일이거나 미공시)")
    return None

# ============================================================
# NAV(순자산총액/기준가) 크롤링 — standard_price 통합
# ============================================================

def crawl_nav(idx: int, etf_name: str, target_date: str) -> Optional[Dict]:
    """
    당일 순자산총액(nav_total)과 기준가(nav_price)만 수집.
    standard_price는 전일자 기준으로 별도 수집 (process_one 참고).
    """
    url = f"https://timeetf.co.kr/m11_view.php?idx={idx}&pdfDate={target_date}"
    res = fetch_with_retry(url)
    if not res:
        return None

    soup = BeautifulSoup(res.text, "html.parser")
    text = soup.get_text(separator="\n")

    nav_total = None
    m = re.search(r"순자산총액[^0-9]*([0-9,]+)\s*억", text)
    if m:
        nav_total = to_int(m.group(1)) * 100_000_000

    nav_price = None
    m = re.search(r"기준가\s*\(원\)\s*[:\s]*([0-9,]+\.?[0-9]*)", text)
    if m:
        nav_price = to_float(m.group(1))

    if nav_total is None and nav_price is None:
        return None

    result: Dict = {
        "date":     target_date,
        "etf_idx":  idx,
        "etf_name": etf_name,
    }
    if nav_total is not None: result["nav_total"] = nav_total
    if nav_price is not None: result["nav_price"] = nav_price

    return result

# ============================================================
# 한 ETF, 한 날짜 처리 — 기존과 동일
# ============================================================

def process_one(idx: int, etf_name: str, target_date: str) -> tuple[int, bool]:
    """반환: (저장된 종목 수, NAV 저장 여부)"""
    print(f"  📊 [{etf_name}] {target_date}")

    # 1) 구성종목 (당일)
    holdings = crawl_holdings(idx, etf_name, target_date)
    time.sleep(DELAY_BETWEEN_REQUESTS)

    # 2) NAV (당일) — standard_price 제외
    nav = crawl_nav(idx, etf_name, target_date)
    time.sleep(DELAY_BETWEEN_REQUESTS)

    # 데이터 없는 날(주말/공휴일) 스킵
    if not holdings and not nav:
        print(f"  ⏭️ 데이터 없음 (주말/공휴일 추정)")
        return 0, False

    # Supabase 저장 — holdings
    if holdings:
        try:
            supabase.table("holdings").upsert(
                holdings, on_conflict="date,etf_idx,code"
            ).execute()
            print(f"  ✅ holdings: {len(holdings)}개 저장")
        except Exception as e:
            print(f"  ❌ holdings 저장 실패: {e}")
            holdings = []

    # Supabase 저장 — etf_daily (nav_total, nav_price)
    nav_saved = False
    if nav:
        try:
            supabase.table("etf_daily").upsert(
                [nav], on_conflict="date,etf_idx"
            ).execute()
            print(f"  ✅ etf_daily 저장 (nav_total, nav_price)")
            nav_saved = True
        except Exception as e:
            print(f"  ❌ etf_daily 저장 실패: {e}")

    # 3) 전일 standard_price 업데이트
    #    당일 크롤링 시점(오전 9시)에는 당일 기준가격 미공시
    #    → 전 영업일 날짜로 수집해서 해당 날짜 행 업데이트
    prev_date = get_prev_business_date(target_date)
    print(f"  🔍 전일({prev_date}) standard_price 수집 시도")
    prev_sp = crawl_standard_price(idx, prev_date)
    if prev_sp is not None:
        try:
            supabase.table("etf_daily").update(
                {"standard_price": prev_sp}
            ).eq("date", prev_date).eq("etf_idx", idx).execute()
            print(f"  ✅ 전일({prev_date}) standard_price 업데이트: {prev_sp:,.2f}원")
        except Exception as e:
            print(f"  ❌ 전일 standard_price 저장 실패: {e}")
    time.sleep(DELAY_BETWEEN_REQUESTS)

    return len(holdings), nav_saved
  
# ============================================================
# 메인 — 기존과 동일
# ============================================================

ENOUGH_ETF_COUNT = 17  # 17개 이상이면 1차에서 거의 다 받은 것 → 2차 스킵

def get_etf_status(target_date: str) -> tuple[int, list]:
    """저장된 ETF 수와 누락된 ETF 목록을 한 번의 쿼리로 반환"""
    try:
        res = (
            supabase.table("holdings")
            .select("etf_idx")
            .eq("date", target_date)
            .limit(2000)
            .execute()
        )
        rows = res.data or []
        saved_idx = set(r["etf_idx"] for r in rows)
        missing = [etf for etf in ETF_LIST if etf["idx"] not in saved_idx]
        return len(saved_idx), missing
    except Exception as e:
        print(f"  ⚠️ 상태 조회 실패 (전체 재시도): {e}")
        return 0, ETF_LIST

def main():
    target_date = os.environ.get("TARGET_DATE") or str(date.today())
    is_manual   = bool(os.environ.get("TARGET_DATE"))  # 수동 실행 여부

    print(f"🚀 크롤링 시작: {target_date}")
    print(f"   대상 ETF: {len(ETF_LIST)}개")
    print(f"   실행 모드: {'수동(특정날짜)' if is_manual else '자동(오늘)'}")
    print("=" * 60)

    if not is_manual:
        already, missing = get_etf_status(target_date)   # ← 쿼리 1번으로 통합
        print(f"📊 사전 체크: 이미 {already}/{len(ETF_LIST)}개 ETF 수집됨")
        if already >= ENOUGH_ETF_COUNT:
            print(f"✅ {ENOUGH_ETF_COUNT}개 이상 수집 완료 상태 → 이번 실행은 스킵합니다")
            print(" (1차 cron이 성공했거나 이미 처리된 날짜)")
            return

        if already > 0:
            print(f"🔄 일부만 수집됨 → 누락된 {len(missing)}개 ETF만 재시도")
            target_etfs = missing
        else:
            target_etfs = ETF_LIST
    else:
        target_etfs = ETF_LIST

    total_holdings = 0
    success_etfs = 0
    
    MAX_WORKERS = 4  # timeetf.co.kr 부하 고려, 4 이상은 올리지 마세요
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_one, etf["idx"], etf["etf_name"], target_date): etf
            for etf in target_etfs
        }
        for future in as_completed(futures):
            etf = futures[future]
            try:
                n, _ = future.result()
                total_holdings += n
                if n > 0:
                    success_etfs += 1
            except Exception as e:
                print(f"  ❌ [{etf['etf_name']}] 예외: {e}")

    print("=" * 60)
    print(f"✅ 완료: {success_etfs}/{len(target_etfs)} ETF, 총 {total_holdings}개 종목")

if __name__ == "__main__":
    main()
