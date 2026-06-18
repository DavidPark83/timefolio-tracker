"""
TIMEFOLIO ETF Backfill (과거 데이터 수집)
==========================================
역할: 지정 기간(또는 2026-01-01 ~ 어제) 영업일 데이터를 한 번에 수집

사용법:
  $ python backfill.py                              # 전체 (2026-01-01 ~ 어제)
  $ python backfill.py 2026-01-01 2026-01-31        # 1월만
  $ python backfill.py 2026-04-23                   # 4/23 ~ 어제

설계 원칙:
1. 주말 자동 스킵 (월~금만)
2. 시작 시 이미 저장된 (date, etf_idx) 조합을 한 번에 미리 조회 → DB 왕복 절감
3. 진행률 실시간 출력 + 예상 종료시각 표시
4. 중단되어도 다시 실행하면 이어서 진행 가능

변경 이력:
  - recalc_holding_amounts(파이썬 함수) import 제거.
    crawler.py가 holding_amount 재계산을 DB RPC(recalc_holdings_for_date)로
    처리하도록 바뀌어, 더 이상 존재하지 않는 함수였음(ImportError 원인).
    → 백필 종료 후 재계산도 동일하게 supabase.rpc("recalc_holdings_for_date")
      를 직접 호출하도록 변경.
"""

import os
import sys
import time
from datetime import date, timedelta, datetime
from typing import Set, Tuple

from supabase import create_client

# crawler.py에서 함수와 상수를 가져온다
# (recalc_holding_amounts 는 crawler.py에 없는 함수라 import에서 제거함.
#  재계산은 아래에서 DB RPC recalc_holdings_for_date 로 직접 수행한다.)
from crawler import (
    ETF_LIST,
    process_one,
    SUPABASE_URL,
    SUPABASE_KEY,
)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def daterange_business_days(start: date, end: date):
    """월~금만 yield"""
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 0=월 ~ 4=금
            yield cur
        cur += timedelta(days=1)


def load_already_saved(start: date, end: date) -> Set[Tuple[str, int]]:
    """
    백필 시작 시 한 번만 호출.
    기간 내 이미 holdings에 저장된 (date, etf_idx) 조합 집합을 반환.
    매 ETF마다 DB 조회하던 것을 1회로 압축 → 속도 개선.
    """
    print(f"🔍 기존 데이터 조회 중...")
    saved = set()
    page_size = 1000
    offset = 0

    while True:
        try:
            res = (
                supabase.table("holdings")
                .select("date, etf_idx")
                .gte("date", start.isoformat())
                .lte("date", end.isoformat())
                .range(offset, offset + page_size - 1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                saved.add((str(r["date"]), int(r["etf_idx"])))
            if len(rows) < page_size:
                break
            offset += page_size
        except Exception as e:
            print(f"  ⚠️  기존 데이터 조회 실패 (계속 진행): {e}")
            break

    print(f"   이미 저장된 조합: {len(saved)}건")
    return saved


def recalc_holding_amounts_for_date(target_date: str) -> None:
    """
    특정 날짜의 holding_amount/holdings_qty를 DB에서 재계산.
    crawler.py의 process_one()이 사용하는 것과 동일한 RPC를 호출한다.
    (기존 backfill.py가 부르던 파이썬 함수 recalc_holding_amounts를 대체)
    """
    try:
        supabase.rpc(
            "recalc_holdings_for_date",
            {"target_date": target_date}
        ).execute()
        print(f"   ✅ {target_date} holding_amount 재계산")
    except Exception as e:
        print(f"   ⚠️ {target_date} 재계산 실패: {e}")


def main():
    # 인자 파싱
    if len(sys.argv) >= 3:
        start = parse_date(sys.argv[1])
        end   = parse_date(sys.argv[2])
    elif len(sys.argv) == 2:
        start = parse_date(sys.argv[1])
        end   = date.today() - timedelta(days=1)
    else:
        start = date(2026, 1, 1)
        end   = date.today() - timedelta(days=1)

    print(f"🔄 Backfill: {start} ~ {end}")
    print(f"   대상 ETF: {len(ETF_LIST)}개")
    print("=" * 60)

    # 1) 이미 저장된 조합을 한 번에 조회
    saved = load_already_saved(start, end)

    # 2) 영업일 목록
    biz_days = list(daterange_business_days(start, end))
    total_combinations = len(biz_days) * len(ETF_LIST)
    todo = total_combinations - len(saved)
    print(f"📅 영업일: {len(biz_days)}일, 총 조합: {total_combinations}개, 처리 예정: {todo}개")
    print(f"⏱  예상 소요: 약 {todo * 3.5 / 60:.1f}분")
    print("=" * 60)

    started_at = time.time()
    total_calls = 0
    total_skipped = 0
    total_saved_holdings = 0
    total_failed = 0
    processed_dates = set()  # 보유금액 재계산 대상 날짜

    for d_idx, d in enumerate(biz_days, 1):
        target_date = d.strftime("%Y-%m-%d")
        print(f"\n📅 [{d_idx}/{len(biz_days)}] {target_date} ({d.strftime('%a')})")

        for etf in ETF_LIST:
            key = (target_date, etf["idx"])
            if key in saved:
                total_skipped += 1
                continue

            try:
                n, _ = process_one(etf["idx"], etf["etf_name"], target_date)
                total_calls += 1
                total_saved_holdings += n
                if n > 0:
                    processed_dates.add(target_date)
                if n == 0:
                    total_failed += 1
            except Exception as e:
                print(f"  ❌ [{etf['etf_name']}] 예외: {e}")
                total_failed += 1

        # 진행률 보고 (매일 끝마다)
        elapsed = time.time() - started_at
        if total_calls > 0:
            avg = elapsed / total_calls
            remaining = todo - total_calls
            eta_sec = avg * remaining
            print(f"   ⏱  경과 {elapsed/60:.1f}분 / 남은 호출 {remaining}개 / ETA {eta_sec/60:.1f}분")

    print("\n" + "=" * 60)
    # 신규 데이터가 들어간 날짜에 대해 보유금액 일괄 산출
    # (process_one 안에서도 날짜별로 재계산하지만, 누락 대비 한 번 더 안전하게 수행)
    if processed_dates:
        print(f"💰 보유금액 산출: {len(processed_dates)}개 날짜")
        for d in sorted(processed_dates):
            recalc_holding_amounts_for_date(d)
        print("=" * 60)
    print(f"✅ Backfill 완료")
    print(f"   처리한 영업일: {len(biz_days)}일")
    print(f"   API 호출: {total_calls}회")
    print(f"   스킵(이미 저장): {total_skipped}건")
    print(f"   저장된 종목 데이터: {total_saved_holdings}개")
    print(f"   실패/빈응답: {total_failed}건")
    print(f"   총 소요시간: {(time.time()-started_at)/60:.1f}분")


if __name__ == "__main__":
    main()
