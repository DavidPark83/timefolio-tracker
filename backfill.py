"""
TIMEFOLIO ETF Backfill (과거 데이터 수집)
==========================================
역할: 2026-04-01 ~ 어제까지 영업일 데이터를 한 번에 수집
실행: 로컬에서 1회만 수동 실행 (또는 GitHub Actions의 workflow_dispatch)

  $ export SUPABASE_URL=...
  $ export SUPABASE_KEY=...
  $ python backfill.py
  $ python backfill.py 2026-01-01 2026-04-30   # 기간 지정도 가능

설계 원칙:
1. 주말 자동 스킵 (월~금만)
2. 이미 저장된 (date, etf_idx) 조합은 자동 스킵 → 중간에 멈춰도 이어서 가능
3. crawler.py의 함수를 재사용 → 코드 중복 X
"""

import os
import sys
import time
from datetime import date, timedelta, datetime

from supabase import create_client

# crawler.py에서 함수와 상수를 가져온다
from crawler import (
    ETF_LIST,
    process_one,
    DELAY_BETWEEN_REQUESTS,
    SUPABASE_URL,
    SUPABASE_KEY,
)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def daterange_business_days(start: date, end: date):
    """월~금만 yield (공휴일은 사이트가 빈 응답 → 자연 스킵됨)"""
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 0=월 ~ 4=금
            yield cur
        cur += timedelta(days=1)


def already_saved(etf_idx: int, target_date: str) -> bool:
    """이 ETF, 이 날짜의 holdings 데이터가 이미 있는지 확인"""
    try:
        res = (
            supabase.table("holdings")
            .select("id", count="exact")
            .eq("etf_idx", etf_idx)
            .eq("date", target_date)
            .limit(1)
            .execute()
        )
        return (res.count or 0) > 0
    except Exception:
        return False


def main():
    # 인자 파싱
    if len(sys.argv) >= 3:
        start = parse_date(sys.argv[1])
        end   = parse_date(sys.argv[2])
    else:
        start = date(2026, 4, 1)
        end   = date.today() - timedelta(days=1)  # 어제까지

    print(f"🔄 Backfill: {start} ~ {end}")
    print(f"   대상 ETF: {len(ETF_LIST)}개")
    print("=" * 60)

    total_days = 0
    total_calls = 0
    total_skipped = 0
    total_saved_holdings = 0

    for d in daterange_business_days(start, end):
        target_date = d.strftime("%Y-%m-%d")
        total_days += 1
        print(f"\n📅 {target_date} ({d.strftime('%a')})")

        for etf in ETF_LIST:
            # 이미 있으면 스킵
            if already_saved(etf["idx"], target_date):
                total_skipped += 1
                continue

            try:
                n, _ = process_one(etf["idx"], etf["etf_name"], target_date)
                total_calls += 1
                total_saved_holdings += n
            except Exception as e:
                print(f"    ❌ [{etf['etf_name']}] 예외: {e}")

    print("\n" + "=" * 60)
    print(f"✅ Backfill 완료")
    print(f"   처리한 영업일: {total_days}일")
    print(f"   API 호출: {total_calls}회")
    print(f"   스킵(이미 저장됨): {total_skipped}건")
    print(f"   저장된 종목 데이터: {total_saved_holdings}개")


if __name__ == "__main__":
    main()
