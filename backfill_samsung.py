"""
삼성액티브(KoAct) ETF 과거 데이터 백필  [backfill_samsung.py]
================================================================
timeetf 의 backfill.py 와 동일한 구조/호출 규약:
    python backfill_samsung.py START END   # START~END 범위
    python backfill_samsung.py START       # START~어제
    python backfill_samsung.py             # 기본 시작일~어제

역할: 지정한 과거 날짜 범위에 대해 삼성 23종의 구성종목/기준가/순자산을 적재.

==== 데이터 소스별 과거 가용성 (실측 확인됨) ====
- 구성종목(etf-pdf, gijunYMD 파라미터): ✅ 과거 날짜별 실제 데이터 제공
- 확정 기준가(suik.standardList F_P):   ⚠️ 최근 ~6영업일치만 API에 존재
- 순자산총액(product.nav) / 실시간기준가(realIdx.basp):
       ❌ 과거 소급 불가 — 항상 '현재값'만 반환

==== 순자산 처리 정책 (timeetf backfill 과 동일) ====
timeetf 의 과거 etf_daily 를 보면, 백필 구간의 nav_total/nav_price 는
"백필 실행 시점의 현재값"을 과거 모든 날짜에 동일하게 복사해 넣었고
(그래서 그 구간은 nav_total 이 며칠이고 같은 값), standard_price 만
날짜별 실제값이 들어가 있다.
→ 본 스크립트도 동일하게:
   - nav_total / nav_price : 백필 시작 시 1회 수집한 '오늘 현재값'을 전 날짜에 복사
   - standard_price        : suik.standardList 의 날짜별 실제 F_P (있는 날짜만)
   - holding_amount/holdings_qty : nav_total(복사값) 기준으로 timeetf 와 동일 계산
   - creation_unit         : qPerCu

실행은 GitHub Actions(crawl.yml)의 backfill 모드에서 timeetf backfill 직후 호출된다.
삼성이 실패해도 워크플로우 전체에 영향 주지 않도록 최상위에서 예외 흡수 후 exit 0.
"""

import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

# crawler_samsung.py 의 검증된 함수/상수를 그대로 재사용 (로직 동일성 보장)
from crawler_samsung import (
    ETF_LIST,
    BASE,
    DELAY_BETWEEN_REQUESTS,
    MAX_CONSECUTIVE_429,
    RateLimited,
    supabase,
    to_float,
    fetch_json,
    ymd_to_date,
    crawl_holdings,
    crawl_daily,
    enrich_holdings,
    save_holdings,
    save_nav,
    update_standard_price,
)

# 기본 시작일 (인자 없이 실행할 때). timeetf 와 동일하게 연초 기준.
DEFAULT_START = "2026-01-01"


# ============================================================
# 날짜 유틸
# ============================================================
def daterange_weekdays(start: str, end: str) -> List[str]:
    """start~end(YYYY-MM-DD) 사이의 '평일'만 리스트로. 주말은 데이터가 없으므로 제외."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    cur = d0
    while cur <= d1:
        if cur.weekday() < 5:  # 0=월 ~ 4=금
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# ============================================================
# 백필 시작 시 1회: 23종의 '현재' nav_total/nav_price/creation_unit 수집
#   (과거 소급 불가 → 현재값을 과거 전체에 복사하기 위함. timeetf 방식)
# ============================================================
def fetch_current_nav_map() -> Dict[int, Dict]:
    """
    반환: { etf_idx: {nav_total, nav_price, creation_unit} }
    crawl_daily 를 오늘 날짜로 호출해 현재 스냅샷을 얻는다.
    """
    today = date.today().isoformat()
    nav_map = {}
    consecutive_429 = 0
    print("📌 현재 NAV 스냅샷 수집 (과거 날짜에 복사용)")
    for etf in ETF_LIST:
        try:
            daily = crawl_daily(etf, today)
            consecutive_429 = 0
            time.sleep(DELAY_BETWEEN_REQUESTS)
            today_row = daily.get("today_row") or {}
            nav_map[etf["etf_idx"]] = {
                "nav_total": daily.get("nav_total"),
                "nav_price": today_row.get("nav_price"),
                "creation_unit": daily.get("creation_unit"),
            }
            print(f"   • {etf['etf_name']}: nav_total={daily.get('nav_total')}")
        except RateLimited:
            consecutive_429 += 1
            print(f"   🚦 NAV 스냅샷 429 ({etf['etf_name']}) "
                  f"(연속 {consecutive_429}/{MAX_CONSECUTIVE_429})")
            if consecutive_429 >= MAX_CONSECUTIVE_429:
                print("   ⛔ 연속 429 → NAV 스냅샷 중단 (수집된 종목만 사용)")
                break
            time.sleep(DELAY_BETWEEN_REQUESTS * 2)
        except Exception as e:
            print(f"   ❌ NAV 스냅샷 예외 ({etf['etf_name']}): {e}")
    return nav_map


# ============================================================
# 확정 기준가(standard_price) 백필 헬퍼
#   상세 API의 suik.standardList 전체를 긁어 날짜→F_P 매핑 반환
# ============================================================
def fetch_standard_price_series(etf: Dict) -> Dict[str, float]:
    """반환: { 'YYYY-MM-DD': F_P }  (API에 남아있는 최근 며칠치)"""
    url = f"{BASE}/api/v1/product/etf/{etf['fId']}.do"
    data = fetch_json(url)  # 429면 RateLimited 발생 → 호출 측에서 처리
    series = {}
    if not data:
        return series
    slist = (data.get("suik") or {}).get("standardList") or []
    for row in slist:
        ed = ymd_to_date(str(row.get("EVAL_D") or ""))
        fp = to_float(row.get("F_P"))
        if ed and fp > 0:
            series[ed] = fp
    return series


# ============================================================
# 메인
# ============================================================
def run_backfill(start: str, end: str):
    target_dates = daterange_weekdays(start, end)
    if not target_dates:
        print(f"⚠️ 백필 대상 평일이 없습니다: {start} ~ {end}")
        return

    print(f"🚀 삼성액티브 백필: {start} ~ {end}  (평일 {len(target_dates)}일)")
    print(f"   대상 ETF: {len(ETF_LIST)}개")
    print("=" * 60)

    # 1) 현재 NAV 스냅샷 (과거 전체에 복사)
    nav_map = fetch_current_nav_map()
    print("=" * 60)

    total_holdings = 0
    consecutive_429 = 0

    # 2) ETF 단위로 순회 (ETF별 standardList 1회 + 날짜별 holdings)
    for etf in ETF_LIST:
        idx = etf["etf_idx"]
        print(f"   📊 [{etf['etf_name']}] ({etf['fId']})")
        nav_info = nav_map.get(idx, {})
        nav_total = nav_info.get("nav_total")
        nav_price = nav_info.get("nav_price")
        creation_unit = nav_info.get("creation_unit")

        # 2-1) 확정 기준가 시계열 (있는 날짜만)
        try:
            std_series = fetch_standard_price_series(etf)
            consecutive_429 = 0
            time.sleep(DELAY_BETWEEN_REQUESTS)
        except RateLimited:
            std_series = {}
            consecutive_429 += 1
            print(f"      🚦 기준가 시계열 429 → 생략 (연속 {consecutive_429}/{MAX_CONSECUTIVE_429})")
            if consecutive_429 >= MAX_CONSECUTIVE_429:
                print("   ⛔ 연속 429 → 백필 조기 중단")
                break

        # 2-2) 날짜별 holdings + etf_daily
        for d in target_dates:
            d_dot = d.replace("-", ".")  # API는 'YYYY.MM.DD'
            try:
                holdings = crawl_holdings(etf, d_dot)
                consecutive_429 = 0
                time.sleep(DELAY_BETWEEN_REQUESTS)

                if not holdings:
                    continue  # 그 날 데이터 없음(공휴일 등)

                db_date = holdings[0]["date"]  # 실제 반환된 기준일

                # holdings 파생값: nav_total(현재값 복사) 기준으로 timeetf 동일 계산
                enrich_holdings(holdings, nav_total, creation_unit)
                n = save_holdings(holdings)
                if n > 0:
                    total_holdings += n

                # etf_daily 행: nav_total/nav_price = 현재값 복사,
                #               standard_price = 그 날짜의 실제 F_P (있으면)
                etf_daily_row = {
                    "date": db_date,
                    "etf_idx": idx,
                    "etf_name": etf["etf_name"],
                    "nav_total": nav_total,
                    "nav_price": nav_price,
                    "standard_price": std_series.get(db_date),
                    "provider": "samsungactive",
                }
                save_nav(etf_daily_row)
                print(f"      ✅ {db_date}: holdings {n}개, "
                      f"기준가 {std_series.get(db_date) or '—'}")

            except RateLimited:
                consecutive_429 += 1
                print(f"      🚦 {d} 429 → 스킵 (연속 {consecutive_429}/{MAX_CONSECUTIVE_429})")
                if consecutive_429 >= MAX_CONSECUTIVE_429:
                    print("   ⛔ 연속 429 → 백필 조기 중단 (남은 범위는 재실행)")
                    return
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)
            except Exception as e:
                print(f"      ❌ {d} 예외: {e}")

    print("=" * 60)
    print(f"✅ 백필 완료: 총 {total_holdings}개 종목 적재")


def main():
    args = sys.argv[1:]
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if len(args) >= 2:
        start, end = args[0], args[1]
    elif len(args) == 1:
        start, end = args[0], yesterday
    else:
        start, end = DEFAULT_START, yesterday

    run_backfill(start, end)


if __name__ == "__main__":
    # timeetf backfill 이후 호출. 삼성 백필이 실패해도 워크플로우 전체에
    # 영향 주지 않도록 최상위에서 예외 흡수 후 정상 종료(exit 0).
    try:
        main()
    except Exception as e:
        print(f"⛔ 삼성 백필 최상위 예외 — 이번 실행만 중단: {e}")
        sys.exit(0)
