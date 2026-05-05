"""
1회성: 기존 holdings 테이블의 creation_unit이 NULL인 행을 채운다.
설정단위는 거의 변하지 않으므로, 각 etf_idx별로 1회 fetch한 값을
해당 etf_idx의 모든 NULL 행에 일괄 UPDATE.

사용법: python fill_creation_unit.py
"""

import time
from supabase import create_client
from crawler import (
    ETF_LIST, SUPABASE_URL, SUPABASE_KEY,
    crawl_nav, DELAY_BETWEEN_REQUESTS,
)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def main():
    print("🔄 기존 holdings의 creation_unit 일괄 채우기")
    print("=" * 60)

    # 최신 영업일 1개로 ETF별 설정단위 fetch (과거에도 동일하다고 가정)
    # 더 정확하게 하려면 etf별로 가장 오래된 date를 사용해도 됨
    from datetime import date, timedelta
    target_date = (date.today() - timedelta(days=1)).isoformat()

    for etf in ETF_LIST:
        idx = etf["idx"]
        name = etf["etf_name"]

        nav = crawl_nav(idx, name, target_date)
        time.sleep(DELAY_BETWEEN_REQUESTS)

        if not nav or nav.get("creation_unit") is None:
            print(f"  ⚠️  [{name}] 설정단위 추출 실패 — 스킵")
            continue

        cu = nav["creation_unit"]

        # 해당 etf_idx의 모든 행을 UPDATE (NULL이든 아니든 덮어씀)
        # NULL만 채우고 싶다면 .is_("creation_unit", "null") 추가
        try:
            res = (
                supabase.table("holdings")
                .update({"creation_unit": cu})
                .eq("etf_idx", idx)
                .execute()
            )
            print(f"  ✅ [{name}] creation_unit={cu:,} 일괄 적용")
        except Exception as e:
            print(f"  ❌ [{name}] UPDATE 실패: {e}")

    print("=" * 60)
    print("✅ 완료")


if __name__ == "__main__":
    main()
