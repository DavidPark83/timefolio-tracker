"""
삼성액티브(KoAct) ETF 일일 크롤러
================================
역할: 매일, 삼성액티브 23종 ETF의 구성종목과 NAV를 수집하여 Supabase에 저장
실행: GitHub Actions가 .github/workflows/crawl_samsung.yml 스케줄에 따라 자동 호출

핵심 설계 원칙 (timeetf 크롤러와 동일 철학):
1. JSON API를 직접 호출  → HTML 파싱 불필요, 페이지 구조 변경에 강함
   - 구성종목: /api/v1/product/etf-pdf/{fId}.do?gijunYMD=YYYY.MM.DD
   - NAV/순자산: /api/v1/product/etf.do (목록 API에 NAV 포함)
2. 요청 사이 딜레이 → 차단 방지
3. 실패해도 다른 ETF는 계속 진행 → 부분 성공 허용
4. upsert(있으면 갱신) → 같은 날 재실행해도 안전
5. provider='samsungactive' 으로 기존 holdings/etf_daily 테이블에 통합 (별도 테이블 없음)

timeetf 크롤러와의 차이:
- 종목코드(itmNo)가 "SPCX US Equity"(미국), "005930"(한국), "CASH00000001",
  "KRD010010001"(원화현금) 등 다양 → normalize_stock_code_samsung()로 분기 처리
- 수량(applyQ)이 소수("397.85") → qty를 float로 (DB는 numeric)
"""

import os
import sys
import time
from datetime import date
from typing import List, Dict, Optional

import requests
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

BASE = "https://www.samsungactive.co.kr"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.samsungactive.co.kr/etf/list.do",
}
REQUEST_TIMEOUT = 20
DELAY_BETWEEN_REQUESTS = 2.5   # 종목 간 간격 (429 방어: 1.0 → 2.5초)
MAX_RETRY = 4                  # 429 백오프 재시도 횟수
# 429(Too Many Requests) 만날 때 대기 시간(초). attempt 순서대로 사용.
RATE_LIMIT_BACKOFF = [30, 60, 120]
# 연속 429가 누적되면 그 실행을 조기 중단 (서버 보호 + 다음 실행에서 누락분 재시도)
MAX_CONSECUTIVE_429 = 5

# ============================================================
# 23종 ETF 목록 (etf.do API에서 수집한 fId / 이름 / 거래소코드)
# fId         = 삼성 내부 ETF 코드 (API 호출용)
# stk_ticker  = 거래소 코드 (참고용)
# etf_idx     = 우리 DB용 정수 ID (200번대로 부여 → timeetf 1~24와 충돌 방지)
# ============================================================
ETF_LIST = [
    {"etf_idx": 201, "fId": "2ETFU5", "stk_ticker": "0174B0", "etf_name": "KoAct 글로벌AI메모리반도체액티브"},
    {"etf_idx": 202, "fId": "2ETFM8", "stk_ticker": "482030", "etf_name": "KoAct 반도체&2차전지핵심소재액티브"},
    {"etf_idx": 203, "fId": "2ETFL3", "stk_ticker": "471040", "etf_name": "KoAct 글로벌AI&로봇액티브"},
    {"etf_idx": 204, "fId": "2ETFR6", "stk_ticker": "0074K0", "etf_name": "KoAct K수출핵심기업TOP30액티브"},
    {"etf_idx": 205, "fId": "2ETFV2", "stk_ticker": "0193G0", "etf_name": "KoAct 코스피액티브"},
    {"etf_idx": 206, "fId": "2ETFQ1", "stk_ticker": "0015B0", "etf_name": "KoAct 미국나스닥성장기업액티브"},
    {"etf_idx": 207, "fId": "2ETFR9", "stk_ticker": "0093D0", "etf_name": "KoAct 팔란티어밸류체인액티브"},
    {"etf_idx": 208, "fId": "2ETFP3", "stk_ticker": "495230", "etf_name": "KoAct 코리아밸류업액티브"},
    {"etf_idx": 209, "fId": "2ETFU7", "stk_ticker": "0186L0", "etf_name": "KoAct 미국로봇피지컬AI액티브"},
    {"etf_idx": 210, "fId": "2ETFT9", "stk_ticker": "0150K0", "etf_name": "KoAct 수소전력ESS인프라액티브"},
    {"etf_idx": 211, "fId": "2ETFM2", "stk_ticker": "476850", "etf_name": "KoAct 배당성장액티브"},
    {"etf_idx": 212, "fId": "2ETFR2", "stk_ticker": "0051A0", "etf_name": "KoAct 브로드컴밸류체인액티브"},
    {"etf_idx": 213, "fId": "2ETFN8", "stk_ticker": "487130", "etf_name": "KoAct AI인프라액티브"},
    {"etf_idx": 214, "fId": "2ETFQ5", "stk_ticker": "0020H0", "etf_name": "KoAct 글로벌양자컴퓨팅액티브"},
    {"etf_idx": 215, "fId": "2ETFL9", "stk_ticker": "475070", "etf_name": "KoAct 글로벌친환경전력인프라액티브"},
    {"etf_idx": 216, "fId": "2ETFJ9", "stk_ticker": "462900", "etf_name": "KoAct 바이오헬스케어액티브"},
    {"etf_idx": 217, "fId": "2ETFU6", "stk_ticker": "0163Y0", "etf_name": "KoAct 코스닥액티브"},
    {"etf_idx": 218, "fId": "2ETFT2", "stk_ticker": "0132D0", "etf_name": "KoAct 글로벌K컬처밸류체인액티브"},
    {"etf_idx": 219, "fId": "2ETFO5", "stk_ticker": "490330", "etf_name": "KoAct 미국치매&뇌질환치료제액티브"},
    {"etf_idx": 220, "fId": "2ETFS3", "stk_ticker": "0104H0", "etf_name": "KoAct 미국나스닥채권혼합50액티브"},
    {"etf_idx": 221, "fId": "2ETFS9", "stk_ticker": "0113G0", "etf_name": "KoAct 미국바이오헬스케어액티브"},
    {"etf_idx": 222, "fId": "2ETFT6", "stk_ticker": "0154H0", "etf_name": "KoAct 차이나바이오헬스케어액티브"},
    {"etf_idx": 223, "fId": "2ETFO9", "stk_ticker": "497780", "etf_name": "KoAct 미국천연가스인프라액티브"},
]

# ============================================================
# 유틸 함수
# ============================================================
def to_int(s) -> int:
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
    if s is None:
        return 0.0
    s = str(s).replace(",", "").replace("%", "").replace(" ", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def normalize_stock_code_samsung(itm_no, sec_nm) -> str:
    """
    삼성 itmNo를 정규화.
    형식이 다양하므로 분기 처리:
      "005930"          → "005930"            (한국 6자리, zero-pad 보존)
      "SPCX US Equity"  → "SPCX US Equity"    (블룸버그 티커는 그대로)
      "CASH00000001"    → "CASH"              (설정현금액)
      "KRD010010001"    → "CASH_KRW"          (원화현금)
      None / ""         → 종목명 기반 fallback
    """
    if itm_no is None:
        s = ""
    else:
        s = str(itm_no).strip()

    # 현금성 항목 표준화
    if s.upper().startswith("CASH"):
        return "CASH"
    if s.upper().startswith("KRD"):
        return "CASH_KRW"

    if not s or s.lower() in ("nan", "none"):
        # itmNo가 비면 종목명으로 fallback
        nm = str(sec_nm or "").strip()
        if "현금" in nm:
            return "CASH_KRW" if "원화" in nm else "CASH"
        return f"UNKNOWN_{nm[:10]}" if nm else "UNKNOWN"

    # 순수 숫자 + 6자리 이하면 한국 종목코드로 보고 zero-pad
    if s.isdigit() and len(s) <= 6:
        return s.zfill(6)

    # 그 외(미국 "XXX US Equity" 등)는 원본 유지
    return s


class RateLimited(Exception):
    """429가 백오프 후에도 계속될 때 발생 — 호출 측이 조기 중단 판단에 사용."""
    pass


def fetch_json(url: str, max_retry: int = MAX_RETRY) -> Optional[dict]:
    """
    재시도 로직 포함 JSON GET.
    - 일반 오류: 짧은 백오프(2*attempt초) 후 재시도
    - 429(Too Many Requests): 긴 백오프(RATE_LIMIT_BACKOFF: 30/60/120초) 후 재시도
      → 끝까지 429면 RateLimited 예외를 던져 호출 측이 연속 429를 집계/중단하게 함
    """
    saw_429 = False
    for attempt in range(1, max_retry + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if res.status_code == 200:
                return res.json()

            if res.status_code == 429:
                saw_429 = True
                # 마지막 시도였다면 더 못 기다리고 종료
                if attempt >= max_retry:
                    break
                wait = RATE_LIMIT_BACKOFF[min(attempt - 1, len(RATE_LIMIT_BACKOFF) - 1)]
                print(f"      🚦 429 Too Many Requests → {wait}초 대기 후 재시도 "
                      f"(시도 {attempt}/{max_retry})")
                time.sleep(wait)
                continue

            print(f"      ⚠️ HTTP {res.status_code} (시도 {attempt}/{max_retry})")
        except Exception as e:
            print(f"      ⚠️ 요청 실패: {e} (시도 {attempt}/{max_retry})")

        if attempt < max_retry:
            time.sleep(2 * attempt)

    if saw_429:
        raise RateLimited(url)
    return None


def ymd_to_date(ymd: str) -> str:
    """'20260617' → '2026-06-17'. 형식이 8자리가 아니면 원본 반환."""
    ymd = str(ymd or "")
    if len(ymd) != 8 or not ymd.isdigit():
        return ymd
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"


# ============================================================
# 구성종목 크롤링 (etf-pdf API)
# ============================================================
def crawl_holdings(etf: Dict, target_date: str) -> List[Dict]:
    """
    etf-pdf API에서 전체 구성종목을 가져온다.
    target_date 형식: 'YYYY.MM.DD' (API가 점 구분자 요구)
    """
    url = f"{BASE}/api/v1/product/etf-pdf/{etf['fId']}.do?gijunYMD={target_date}"
    data = fetch_json(url)
    if not data:
        return []

    pdf = data.get("pdf") or {}
    rows = pdf.get("list") or []
    if not rows:
        return []

    # API가 돌려준 실제 기준일(YYYYMMDD) → DB 저장용 YYYY-MM-DD 로 변환
    api_ymd = pdf.get("gijunYMD") or target_date.replace(".", "")
    db_date = ymd_to_date(api_ymd)

    holdings = []
    for row in rows:
        sec_nm = str(row.get("secNm", "")).strip()
        if not sec_nm or sec_nm.lower() in ("nan", "none", ""):
            continue
        holdings.append({
            "date": db_date,
            "etf_idx": etf["etf_idx"],
            "etf_name": etf["etf_name"],
            "code": normalize_stock_code_samsung(row.get("itmNo"), sec_nm),
            "name": sec_nm,
            "qty": to_float(row.get("applyQ")),    # 소수 가능
            "value": to_int(row.get("evalA")),     # 평가금액(원)
            "weight": to_float(row.get("ratio")),  # 비중(%)
            "provider": "samsungactive",
        })
    return holdings


# ============================================================
# 기본정보(순자산총액/기준가) 크롤링 (etf 상세 API)
# ============================================================
def _extract_realtime_nav(detail: dict) -> Optional[float]:
    """
    실시간 기준가(iNAV) 추출 → nav_price.
    info.realIdx.basp = 실시간 기준가. (timeetf의 nav_price와 동일 성격:
    아침 크롤링 시점의 실시간 기준가)
    """
    try:
        basp = ((detail.get("info") or {}).get("realIdx") or {}).get("basp")
        v = to_float(basp)
        return v if v > 0 else None
    except Exception:
        return None


def _extract_confirmed_price(detail: dict) -> Optional[Dict]:
    """
    확정 기준가 추출 → standard_price (+ 해당 일자).
    suik.standardList[0].F_P = 확정 기준가, EVAL_D = 그 기준일(YYYYMMDD).
    반환: {"eval_date": "YYYY-MM-DD", "f_p": float} 또는 None
    """
    try:
        slist = (detail.get("suik") or {}).get("standardList") or []
        if not slist:
            return None
        row = slist[0]
        f_p = to_float(row.get("F_P"))
        eval_d = str(row.get("EVAL_D") or "")
        if f_p > 0 and len(eval_d) == 8:
            return {"eval_date": ymd_to_date(eval_d), "f_p": f_p}
    except Exception:
        pass
    return None


def crawl_daily(etf: Dict, today_date: str) -> Dict:
    """
    상세 API에서 순자산총액 / 실시간 기준가 / 확정 기준가를 추출.

    timeetf 동작과 동일하게 두 종류의 row를 만든다:
      (A) 오늘 행      : date=today, nav_total, nav_price(실시간 basp), standard_price=NULL
      (B) 확정 기준가 행: date=F_P의 EVAL_D, standard_price=F_P  (보통 전 영업일 → upsert로 백필)

    반환: {"today_row": {...} or None, "confirmed_row": {...} or None}
    """
    url = f"{BASE}/api/v1/product/etf/{etf['fId']}.do"
    data = fetch_json(url)
    if not data:
        return {"today_row": None, "confirmed_row": None}

    product = (data.get("info") or {}).get("product") or {}

    # 순자산총액: nav 는 '억원' 단위 → ×1e8 원 단위 환산
    nav_eok = to_float(product.get("nav"))
    nav_total = int(nav_eok * 100_000_000) if nav_eok > 0 else None

    # 실시간 기준가(iNAV) → nav_price
    nav_price = _extract_realtime_nav(data)

    # 설정단위(qPerCu) → holdings.creation_unit (예: 50000)
    creation_unit = to_int(product.get("qPerCu")) or None

    # (A) 오늘 행
    today_row = None
    if nav_total is not None or nav_price is not None:
        today_row = {
            "date": today_date,
            "etf_idx": etf["etf_idx"],
            "etf_name": etf["etf_name"],
            "nav_total": nav_total,
            "nav_price": nav_price,
            "standard_price": None,  # 확정 기준가는 익일 백필
            "provider": "samsungactive",
        }

    # (B) 확정 기준가 행 (EVAL_D 날짜에 standard_price 백필)
    confirmed_row = None
    conf = _extract_confirmed_price(data)
    if conf:
        confirmed_row = {
            "date": conf["eval_date"],
            "etf_idx": etf["etf_idx"],
            "etf_name": etf["etf_name"],
            "standard_price": conf["f_p"],
            "provider": "samsungactive",
        }

    return {
        "today_row": today_row,
        "confirmed_row": confirmed_row,
        "nav_total": nav_total,          # holdings enrich용
        "creation_unit": creation_unit,  # holdings enrich용
    }


# ============================================================
# 저장
# ============================================================
def enrich_holdings(holdings: List[Dict], nav_total: Optional[int],
                    creation_unit: Optional[int]) -> List[Dict]:
    """
    timeetf와 동일하게 holding_amount / holdings_qty / creation_unit 파생값을 채운다.

    배율(ratio) = nav_total / Σvalue
      → "전체 펀드가 1 설정단위(CU)의 몇 배인가"를 의미.
      (etf-pdf의 value/qty는 1CU 기준이므로, 전체 펀드 기준으로 환산)

    - holding_amount = value × ratio   (종목의 전체펀드 기준 평가금액, 합 = 순자산총액)
    - holdings_qty   = qty   × ratio   (종목의 전체펀드 기준 보유주식수, 소수)
    - creation_unit  = 설정단위(qPerCu)

    nav_total 또는 Σvalue 가 없으면 파생값은 None으로 둔다(프론트가 weight×nav_total로 폴백).
    """
    value_sum = sum((h.get("value") or 0) for h in holdings)
    ratio = (nav_total / value_sum) if (nav_total and value_sum) else None

    for h in holdings:
        h["creation_unit"] = creation_unit
        if ratio is not None:
            h["holding_amount"] = round((h.get("value") or 0) * ratio)
            h["holdings_qty"] = round((h.get("qty") or 0) * ratio, 10)
        else:
            h["holding_amount"] = None
            h["holdings_qty"] = None
    return holdings


def save_holdings(holdings: List[Dict]) -> int:
    if not holdings:
        return 0
    try:
        supabase.table("holdings").upsert(
            holdings, on_conflict="date,etf_idx,code"
        ).execute()
        return len(holdings)
    except Exception as e:
        print(f"   ❌ holdings 저장 실패: {e}")
        return 0


def save_nav(nav_row: Dict) -> bool:
    try:
        supabase.table("etf_daily").upsert(
            [nav_row], on_conflict="date,etf_idx"
        ).execute()
        return True
    except Exception as e:
        print(f"   ❌ NAV 저장 실패: {e}")
        return False


def update_standard_price(etf_idx: int, eval_date: str, f_p: float) -> str:
    """
    확정 기준가 백필: 해당 (date, etf_idx) 행의 standard_price만 갱신.
    upsert(전체 덮어쓰기) 대신 update를 써서 nav_total/nav_price를 보존한다.
    반환: 'updated'(기존 행 갱신) / 'absent'(해당 날짜 행 없음) / 'error'
    """
    try:
        res = (
            supabase.table("etf_daily")
            .update({"standard_price": f_p})
            .eq("date", eval_date)
            .eq("etf_idx", etf_idx)
            .eq("provider", "samsungactive")
            .execute()
        )
        return "updated" if res.data else "absent"
    except Exception as e:
        print(f"   ❌ 확정기준가 갱신 실패: {e}")
        return "error"


# ============================================================
# 메인
# ============================================================
def main():
    # API는 'YYYY.MM.DD' 형식 요구
    raw = os.environ.get("TARGET_DATE") or str(date.today())  # 'YYYY-MM-DD'
    target_date = raw.replace("-", ".")                       # 'YYYY.MM.DD'

    print(f"🚀 삼성액티브 크롤링 시작: {target_date}")
    print(f"   대상 ETF: {len(ETF_LIST)}개")
    print("=" * 60)

    total_holdings = 0
    success_etfs = 0
    consecutive_429 = 0
    skipped = []  # 429 등으로 처리 못 한 ETF (다음 실행에서 자연 재시도됨)

    for etf in ETF_LIST:
        print(f"   📊 [{etf['etf_name']}] ({etf['fId']})")
        try:
            # 1) 구성종목 수집 (저장은 NAV로 파생값 채운 뒤로 미룸)
            holdings = crawl_holdings(etf, target_date)
            consecutive_429 = 0  # 정상 응답 받으면 연속 카운트 리셋
            time.sleep(DELAY_BETWEEN_REQUESTS)

            if not holdings:
                print("      ⏭️ 데이터 없음 (주말/공휴일/미상장 추정)")
                continue

            db_date = holdings[0]["date"]

            # 2) NAV / 기준가 수집 (nav_total·creation_unit 이 holdings enrich에 필요)
            #    NAV 단계가 429로 막혀도 holdings 자체는 잃지 않도록 예외를 따로 흡수.
            try:
                daily = crawl_daily(etf, db_date)
                time.sleep(DELAY_BETWEEN_REQUESTS)
            except RateLimited:
                # NAV는 다음 실행에서 보강. holdings는 파생값 없이라도 저장.
                daily = {"today_row": None, "confirmed_row": None,
                         "nav_total": None, "creation_unit": None}
                print("      🚦 NAV 단계 429 → holdings만 저장하고 NAV는 다음 실행에서 보강")

            # 3) holdings 파생값(holding_amount·holdings_qty·creation_unit) 채워서 저장
            enrich_holdings(holdings, daily.get("nav_total"), daily.get("creation_unit"))
            n = save_holdings(holdings)
            if n > 0:
                success_etfs += 1
                total_holdings += n
                print(f"      ✅ holdings: {n}개 저장 (기준일 {db_date})")

            # 4-A) 오늘 행: nav_total + 실시간 기준가(nav_price)
            today_row = daily.get("today_row")
            if today_row and save_nav(today_row):
                print(f"      ✅ NAV 저장 (순자산: {today_row['nav_total']}, "
                      f"실시간기준가: {today_row['nav_price']})")

            # 4-B) 확정 기준가 백필: F_P 의 EVAL_D 날짜 행의 standard_price만 갱신
            #      (보통 전 영업일 → 어제 행 갱신. upsert가 아닌 update라 nav 값 보존)
            conf_row = daily.get("confirmed_row")
            if conf_row and conf_row["date"] != db_date:
                st = update_standard_price(
                    etf["etf_idx"], conf_row["date"], conf_row["standard_price"]
                )
                if st == "updated":
                    print(f"      ↩️ 확정기준가 백필 ({conf_row['date']}: "
                          f"{conf_row['standard_price']})")
                elif st == "absent":
                    print(f"      ℹ️ 확정기준가 대상일({conf_row['date']}) 행 없음 → 스킵")

        except RateLimited:
            # 429가 백오프 후에도 지속 → 이 ETF는 건너뛰고 연속 카운트 증가
            consecutive_429 += 1
            skipped.append(etf["etf_name"])
            print(f"      🚦 429 지속 → 스킵 (연속 {consecutive_429}/{MAX_CONSECUTIVE_429})")
            if consecutive_429 >= MAX_CONSECUTIVE_429:
                print(f"   ⛔ 연속 429 {MAX_CONSECUTIVE_429}회 → 이번 실행 조기 중단 "
                      f"(남은 종목은 다음 실행에서 재시도)")
                break
            # 다음 종목 시도 전 한 템포 더 쉬기
            time.sleep(DELAY_BETWEEN_REQUESTS * 2)
        except Exception as e:
            print(f"      ❌ 예외: {e}")

    print("=" * 60)
    print(f"✅ 완료: {success_etfs}/{len(ETF_LIST)} ETF, 총 {total_holdings}개 종목")
    if skipped:
        print(f"⚠️ 미처리 {len(skipped)}종 (다음 실행에서 재시도): {', '.join(skipped)}")


if __name__ == "__main__":
    # 삼성 크롤링은 timeetf 크롤링 '이후' 단계에서 실행된다.
    # 어떤 이유로든 삼성 크롤러가 죽더라도 워크플로우 전체를 실패(빨강) 처리하지
    # 않도록, 최상위에서 예외를 흡수하고 항상 정상 종료(exit 0)한다.
    # (그날 못 받은 종목은 다음 실행에서 자연 재시도되므로 데이터 손실 없음)
    try:
        main()
    except Exception as e:
        print(f"⛔ 삼성 크롤러 최상위 예외 — 이번 실행만 중단합니다: {e}")
        # exit code 0 유지: timeetf 결과/워크플로우 전체에 영향 주지 않음
        sys.exit(0)
