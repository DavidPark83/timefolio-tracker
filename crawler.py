"""
TIMEFOLIO ACTIVE ETF 일일 크롤러
================================
역할: 매일 오후 6시(KST), 18개 ETF의 그날 구성종목과 NAV를 수집하여 Supabase에 저장
실행: GitHub Actions가 .github/workflows/crawl.yml 스케줄에 따라 자동 호출

핵심 설계 원칙:
1. timeetf.co.kr의 엑셀 다운로드 엔드포인트(pdf_excel.php)를 사용
   → HTML 파싱보다 안정적이고 페이지 구조 변경에 강함
2. 요청 사이 1초 딜레이 → 차단 방지
3. 실패해도 다른 ETF는 계속 진행 → 부분 성공 허용
4. upsert(있으면 갱신, 없으면 삽입) → 같은 날 재실행해도 안전
"""

import os
import sys
import time
import io
from datetime import date, datetime
from typing import List, Dict, Optional

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

REQUEST_TIMEOUT = 20         # 초
DELAY_BETWEEN_REQUESTS = 1.5 # 초 (차단 방지)
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


def normalize_stock_code(raw) -> str:
    """
    종목코드를 항상 6자리 zero-padded 문자열로 정규화.
    
    한국 거래소 종목코드는 정확히 6자리 문자열이어야 한다 (예: 005930, 000660).
    pandas가 실수로 numeric 추론을 했거나, 이미 망가진 값("660.0")이 들어와도 복구한다.
    
    예시:
        "000660"  → "000660"
        "660.0"   → "000660"  (망가진 값 복구)
        660       → "000660"
        660.0     → "000660"
        "CASH"    → "CASH"    (비숫자는 그대로)
        None/""   → ""
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    
    # "660.0" → "660" (소수점 제거)
    if "." in s:
        try:
            s = str(int(float(s)))
        except (ValueError, TypeError):
            pass
    
    # 숫자로만 이루어진 한국 종목코드라면 6자리 zero-pad
    if s.isdigit() and len(s) <= 6:
        s = s.zfill(6)
    
    return s


def fetch_with_retry(url: str, max_retry: int = MAX_RETRY) -> Optional[requests.Response]:

   
    """재시도 로직 포함 HTTP GET"""
    for attempt in range(1, max_retry + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if res.status_code == 200:
                return res
            print(f"    ⚠️ HTTP {res.status_code} (시도 {attempt}/{max_retry})")
        except Exception as e:
            print(f"    ⚠️ 요청 실패: {e} (시도 {attempt}/{max_retry})")
        if attempt < max_retry:
            time.sleep(2 * attempt)  # 지수 백오프
    return None


# ============================================================
# 구성종목 크롤링 (엑셀 다운로드 사용)
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

    # 엑셀 파일은 .xls (HTML 형식) 또는 .xlsx 가능
    # pandas가 자동 판별
    # ⚠️ dtype=str 필수: 종목코드 leading zero 보존 ("000660" → 660.0 방지)
    try:
        # 1차: pandas로 직접 시도
        df = pd.read_html(io.BytesIO(res.content), dtype=str)[0]
    except Exception:
        try:
            # 2차: 엑셀 형식 시도
            df = pd.read_excel(io.BytesIO(res.content), dtype=str)
        except Exception as e:
            print(f"    ❌ 엑셀 파싱 실패: {e}")
            return []

    # 컬럼명 표준화 (사이트가 한글 헤더 사용)
    df.columns = [str(c).strip() for c in df.columns]
    col_map = {}
    for col in df.columns:
        if "종목코드" in col:    col_map[col] = "code"
        elif "종목명" in col:    col_map[col] = "name"
        elif "수량" in col:      col_map[col] = "qty"
        elif "평가금액" in col:  col_map[col] = "value"
        elif "비중" in col:      col_map[col] = "weight"
    df = df.rename(columns=col_map)

    required = {"name", "weight"}
    if not required.issubset(df.columns):
        print(f"    ❌ 필수 컬럼 누락: 보유 컬럼={list(df.columns)}")
        return []

    holdings = []
    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip()
        if not name or name.lower() in ("nan", "none", ""):
            continue

        # ⚠️ normalize_stock_code: leading zero 복구 + 망가진 값 방어
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
# NAV(순자산총액/기준가) 크롤링
# ============================================================
def crawl_nav(idx: int, etf_name: str, target_date: str) -> Optional[Dict]:
    """
    상세 페이지에서 순자산총액, 기준가, 설정단위(좌)를 추출.
    """
    url = f"https://timeetf.co.kr/m11_view.php?idx={idx}&pdfDate={target_date}"
    res = fetch_with_retry(url)
    if not res:
        return None

    soup = BeautifulSoup(res.text, "html.parser")
    text = soup.get_text(separator="\n")

    nav_total = None
    nav_price = None
    creation_unit = None  # ← 추가

    import re

    # 순자산총액: "순자산총액 : 980 억원" 같은 패턴
    nav_total = None
    nav_price = None

    import re
    # "순자산총액" 다음에 나오는 숫자(억원)
    m = re.search(r"순자산총액[^0-9]*([0-9,]+)\s*억", text)
    if m:
        nav_total = to_int(m.group(1)) * 100_000_000  # 억 → 원

    # "기준가" 패턴 (실시간 기준가가 아닌 일별 기준가)
    m = re.search(r"기준가\s*\(원\)\s*[:\s]*([0-9,]+\.?[0-9]*)", text)
    if m:
        nav_price = to_float(m.group(1))

    # ─── 설정단위(좌) 추출 (신규) ───────────────────────────
    # 페이지 형태: "설정단위(좌)" 라벨 다음에 "100,000" 형태로 등장
    # "(좌)"의 괄호가 전각/반각 모두 가능하므로 [\(\（][좌][\)\）] 로 처리
    m = re.search(r"설정단위\s*[\(（]\s*좌\s*[\)）][^0-9]*([0-9,]+)", text)
    if m:
        creation_unit = to_int(m.group(1))
    # ────────────────────────────────────────────────────────

    if nav_total is None and nav_price is None and creation_unit is None:
        return None

    return {
        "date": target_date,
        "etf_idx": idx,
        "etf_name": etf_name,
        "nav_total": nav_total,
        "nav_price": nav_price,
        "creation_unit": creation_unit,  # ← 추가 (process_one에서 꺼내 씀)
    }


# ============================================================
# 한 ETF, 한 날짜 처리
# ============================================================
def process_one(idx: int, etf_name: str, target_date: str) -> tuple[int, bool]:
    """반환: (저장된 종목 수, NAV 저장 여부)"""
    print(f"  📊 [{etf_name}] {target_date}")

    # ─── 순서 변경: NAV 먼저 호출하여 creation_unit 확보 ───
    # 1) NAV + 설정단위
    nav = crawl_nav(idx, etf_name, target_date)
    time.sleep(DELAY_BETWEEN_REQUESTS)

    # 2) 구성종목
    holdings = crawl_holdings(idx, etf_name, target_date)
    time.sleep(DELAY_BETWEEN_REQUESTS)

    # 데이터 없는 날(주말/공휴일) 스킵
    if not holdings and not nav:
        print(f"     ⏭️  데이터 없음 (주말/공휴일 추정)")
        return 0, False

    # ─── 신규: holdings 각 row에 creation_unit 주입 ───
    creation_unit = nav.get("creation_unit") if nav else None
    if holdings and creation_unit is not None:
        for h in holdings:
            h["creation_unit"] = creation_unit
    elif holdings:
        # 설정단위를 못 가져왔으면 None으로 명시 (스키마 일관성)
        for h in holdings:
            h["creation_unit"] = None
    # ──────────────────────────────────────────────────

    # Supabase 저장
    if holdings:
        try:
            supabase.table("holdings").upsert(
                holdings, on_conflict="date,etf_idx,code"
            ).execute()
            cu_str = f", 설정단위: {creation_unit:,}" if creation_unit else ""
            print(f"     ✅ holdings: {len(holdings)}개 저장{cu_str}")
        except Exception as e:
            print(f"     ❌ holdings 저장 실패: {e}")
            holdings = []

    nav_saved = False
    if nav:
        # etf_daily에는 creation_unit을 저장하지 않으므로 페이로드에서 제거
        nav_payload = {k: v for k, v in nav.items() if k != "creation_unit"}
        try:
            supabase.table("etf_daily").upsert(
                [nav_payload], on_conflict="date,etf_idx"
            ).execute()
            print(f"     ✅ NAV 저장 (총액: {nav.get('nav_total')}, 기준가: {nav.get('nav_price')})")
            nav_saved = True
        except Exception as e:
            print(f"     ❌ NAV 저장 실패: {e}")

    return len(holdings), nav_saved


# ============================================================
# 메인
# ============================================================

# 자동 실행 시 "충분히 수집됨"으로 간주할 ETF 수 (18개 중 X개 이상)
ENOUGH_ETF_COUNT = 17  # 17개 이상이면 1차에서 거의 다 받은 것 → 2차 스킵


def count_saved_etfs(target_date: str) -> int:
    """해당 날짜에 holdings에 데이터가 있는 ETF 개수를 반환"""
    try:
        res = (
            supabase.table("holdings")
            .select("etf_idx")
            .eq("date", target_date)
            .limit(2000)
            .execute()
        )
        rows = res.data or []
        unique_etfs = set(r["etf_idx"] for r in rows)
        return len(unique_etfs)
    except Exception as e:
        print(f"  ⚠️  사전 체크 실패 (계속 진행): {e}")
        return 0


def get_missing_etfs(target_date: str) -> list:
    """해당 날짜에 아직 데이터가 없는 ETF 목록 반환"""
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
        return [etf for etf in ETF_LIST if etf["idx"] not in saved_idx]
    except Exception as e:
        print(f"  ⚠️  누락 ETF 조회 실패 (전체 재시도): {e}")
        return ETF_LIST


def main():
    target_date = os.environ.get("TARGET_DATE") or str(date.today())
    is_manual = bool(os.environ.get("TARGET_DATE"))  # 수동 실행 여부

    print(f"🚀 크롤링 시작: {target_date}")
    print(f"   대상 ETF: {len(ETF_LIST)}개")
    print(f"   실행 모드: {'수동(특정날짜)' if is_manual else '자동(오늘)'}")
    print("=" * 60)

    # 자동 실행일 때만: 이미 충분히 수집됐으면 스킵
    # (수동 실행은 강제 재수집 의도이므로 항상 진행)
    if not is_manual:
        already = count_saved_etfs(target_date)
        print(f"📊 사전 체크: 이미 {already}/{len(ETF_LIST)}개 ETF 수집됨")
        if already >= ENOUGH_ETF_COUNT:
            print(f"✅ {ENOUGH_ETF_COUNT}개 이상 수집 완료 상태 → 이번 실행은 스킵합니다")
            print("   (1차 cron이 성공했거나 이미 처리된 날짜)")
            return

        # 일부만 수집됐으면 누락된 ETF만 처리
        if already > 0:
            missing = get_missing_etfs(target_date)
            print(f"🔄 일부만 수집됨 → 누락된 {len(missing)}개 ETF만 재시도")
            target_etfs = missing
        else:
            target_etfs = ETF_LIST
    else:
        # 수동 실행은 항상 전체 ETF
        target_etfs = ETF_LIST

    total_holdings = 0
    success_etfs = 0

    for etf in target_etfs:
        try:
            n, _ = process_one(etf["idx"], etf["etf_name"], target_date)
            total_holdings += n
            if n > 0:
                success_etfs += 1
        except Exception as e:
            print(f"  ❌ [{etf['etf_name']}] 예외: {e}")

    print("=" * 60)
    print(f"✅ 완료: {success_etfs}/{len(target_etfs)} ETF, 총 {total_holdings}개 종목")


if __name__ == "__main__":
    main()
