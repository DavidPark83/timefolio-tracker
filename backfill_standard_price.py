"""
TIMEFOLIO ETF 기준가격(standard_price) 과거 데이터 일괄 백필
=============================================================
역할:
  - timeetf.co.kr/nav_xls.php?idx={idx} 에서 ETF별 전체 기준가격 히스토리를 다운로드
  - etf_daily 테이블의 standard_price 컬럼을 일자별/펀드별로 upsert

사용법 (1회성 실행):
  # 환경변수 설정 후 실행
  export SUPABASE_URL="https://lqpqummcoujmymydftlg.supabase.co"
  export SUPABASE_KEY="<service_role_key>"
  python backfill_standard_price.py

  # 특정 ETF만 테스트하려면:
  ETF_IDX=22 python backfill_standard_price.py

  # 시작일 지정 (이 날짜 이후만 처리):
  START_DATE=2024-01-01 python backfill_standard_price.py

엑셀 파일 구조 (nav_xls.php):
  일자         | 기준가격(원)  | 등락률(%)  | 종가(원)   | 등락률(%) | 과표기준가(원)
  2026.05.06   | 11,964.83    | -0.13      | 11,975.00  | -0.42    | 11,426.14
  2026.05.04   | 11,980.91    | 0.27       | 12,025.00  | 1.35     | 11,442.25
  ...
"""

import os
import sys
import time
import io
import re
from datetime import datetime, date
from typing import Optional

import requests
import pandas as pd
from supabase import create_client

# ============================================================
# 설정
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ 환경변수 SUPABASE_URL / SUPABASE_KEY 미설정")
    print("   export SUPABASE_URL='https://xxx.supabase.co'")
    print("   export SUPABASE_KEY='<service_role_key>'")
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
DELAY_BETWEEN_ETF = 2.0      # ETF 간 딜레이 (차단 방지)
UPSERT_BATCH_SIZE = 100      # Supabase 한 번에 upsert할 행 수

# 특정 ETF만 처리하려면 환경변수 ETF_IDX 설정 (미설정 시 전체)
SINGLE_ETF_IDX = os.environ.get("ETF_IDX")

# 시작일 필터 (이 날짜 이후 데이터만 처리, 미설정 시 전체)
START_DATE_STR = os.environ.get("START_DATE", "")

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
# 유틸
# ============================================================

def to_float(s) -> Optional[float]:
    """쉼표/공백/% 제거 후 float 변환. 실패 시 None 반환."""
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
    """
    사이트 날짜 형식 "2026.05.06" → "2026-05-06" (DB 저장 형식)
    실패 시 None
    """
    s = str(s).strip()
    # "2026.05.06" 형식
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # 이미 "2026-05-06" 형식
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    return None

# ============================================================
# 기준가격 엑셀 다운로드 및 파싱
# ============================================================

def download_nav_excel(idx: int) -> Optional[pd.DataFrame]:
    """
    nav_xls.php에서 전체 기준가격 히스토리를 다운로드하여 DataFrame 반환.

    반환 컬럼: date(str YYYY-MM-DD), standard_price(float)
    """
    url = f"https://timeetf.co.kr/nav_xls.php?idx={idx}&"
    print(f"  📥 다운로드: {url}")

    try:
        res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
    except Exception as e:
        print(f"  ❌ 다운로드 실패: {e}")
        return None

    if not res.content:
        print(f"  ❌ 빈 응답")
        return None

    content_type = res.headers.get("Content-Type", "")
    print(f"  📄 Content-Type: {content_type} / 크기: {len(res.content):,} bytes")

    # xlsx 또는 xls(HTML) 형식 모두 시도
    df = None

    # 1차: openpyxl (xlsx)
    try:
        df = pd.read_excel(io.BytesIO(res.content), dtype=str, engine="openpyxl")
        print(f"  ✅ xlsx 파싱 성공: {len(df)}행")
    except Exception:
        pass

    # 2차: xlrd (xls)
    if df is None:
        try:
            df = pd.read_excel(io.BytesIO(res.content), dtype=str, engine="xlrd")
            print(f"  ✅ xls 파싱 성공: {len(df)}행")
        except Exception:
            pass

    # 3차: HTML 테이블 형식 (Content-Type이 html이거나 xls 위장)
    if df is None:
        try:
            tables = pd.read_html(io.BytesIO(res.content), dtype=str)
            if tables:
                df = tables[0]
                print(f"  ✅ HTML 테이블 파싱 성공: {len(df)}행")
        except Exception as e:
            print(f"  ❌ 모든 파싱 실패: {e}")
            return None

    if df is None or df.empty:
        return None

    # 컬럼 확인 및 정규화
    df.columns = [str(c).strip() for c in df.columns]
    print(f"  📋 컬럼: {list(df.columns)}")

    # 날짜 컬럼 찾기 (첫 번째 컬럼이 보통 날짜)
    date_col = df.columns[0]
    # 기준가격 컬럼 찾기
    price_col = None
    for col in df.columns:
        if "기준가격" in col or ("기준가" in col and "과표" not in col):
            price_col = col
            break
    # 못 찾으면 두 번째 컬럼으로 가정
    if price_col is None and len(df.columns) >= 2:
        price_col = df.columns[1]

    print(f"  🗓️ 날짜컬럼: [{date_col}] / 기준가컬럼: [{price_col}]")

    # 파싱
    records = []
    for _, row in df.iterrows():
        date_str = parse_date_str(str(row[date_col]))
        if not date_str:
            continue  # 헤더나 빈 행 스킵

        price = to_float(row[price_col])
        if price is None or price <= 0:
            continue

        records.append({
            "date": date_str,
            "standard_price": price,
        })

    if not records:
        print(f"  ⚠️ 파싱된 레코드 없음")
        return None

    result_df = pd.DataFrame(records)
    print(f"  📊 파싱 완료: {len(result_df)}개 날짜 ({result_df['date'].min()} ~ {result_df['date'].max()})")
    return result_df

# ============================================================
# Supabase upsert
# ============================================================

def upsert_standard_prices(idx: int, etf_name: str, df: pd.DataFrame, start_date: Optional[str] = None) -> int:
    """
    etf_daily 테이블에 standard_price를 upsert.
    - on_conflict: date, etf_idx
    - standard_price 컬럼만 업데이트 (기존 다른 컬럼 유지)

    반환: upsert된 행 수
    """
    # 날짜 필터
    if start_date:
        original_len = len(df)
        df = df[df["date"] >= start_date].copy()
        print(f"  📅 날짜 필터 적용: {start_date} 이후 → {len(df)}/{original_len}개")

    if df.empty:
        print(f"  ⚠️ 필터 후 데이터 없음")
        return 0

    # upsert 데이터 구성
    rows = []
    for _, row in df.iterrows():
        rows.append({
            "date": row["date"],
            "etf_idx": idx,
            "etf_name": etf_name,
            "standard_price": row["standard_price"],
        })

    total_upserted = 0

    # 배치 처리
    for i in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[i:i + UPSERT_BATCH_SIZE]
        try:
            supabase.table("etf_daily").upsert(
                batch,
                on_conflict="date,etf_idx"
            ).execute()
            total_upserted += len(batch)
            print(f"  💾 upsert {i+1}~{i+len(batch)}/{len(rows)}행 완료")
        except Exception as e:
            print(f"  ❌ upsert 실패 (배치 {i}~{i+len(batch)}): {e}")

    return total_upserted

# ============================================================
# ETF 1개 처리
# ============================================================

def process_etf(idx: int, etf_name: str, start_date: Optional[str] = None) -> int:
    """반환: 저장된 행 수"""
    print(f"\n{'='*55}")
    print(f"📈 [{idx}] {etf_name}")
    print(f"{'='*55}")

    df = download_nav_excel(idx)
    if df is None:
        print(f"  ❌ 데이터 없음, 스킵")
        return 0

    count = upsert_standard_prices(idx, etf_name, df, start_date)
    print(f"  ✅ 총 {count}개 행 저장 완료")
    return count

# ============================================================
# 메인
# ============================================================

def main():
    print("=" * 60)
    print("🗃️  TIMEFOLIO ETF 기준가격 과거 데이터 백필")
    print("=" * 60)

    # 필터 설정
    start_date = START_DATE_STR if START_DATE_STR else None
    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            print(f"📅 시작일 필터: {start_date} 이후만 처리")
        except ValueError:
            print(f"❌ START_DATE 형식 오류: {start_date} (YYYY-MM-DD 형식으로 입력)")
            sys.exit(1)
    else:
        print("📅 시작일 필터: 없음 (전체 히스토리 처리)")

    # 대상 ETF 결정
    if SINGLE_ETF_IDX:
        target_etfs = [e for e in ETF_LIST if str(e["idx"]) == str(SINGLE_ETF_IDX)]
        if not target_etfs:
            print(f"❌ ETF_IDX={SINGLE_ETF_IDX} 에 해당하는 ETF 없음")
            sys.exit(1)
        print(f"🎯 단일 ETF 모드: [{target_etfs[0]['etf_name']}]")
    else:
        target_etfs = ETF_LIST
        print(f"🎯 전체 ETF 모드: {len(target_etfs)}개")

    print("=" * 60)

    total_rows = 0
    success_count = 0
    failed = []

    for i, etf in enumerate(target_etfs, 1):
        print(f"\n[{i}/{len(target_etfs)}]")
        try:
            n = process_etf(etf["idx"], etf["etf_name"], start_date)
            total_rows += n
            if n > 0:
                success_count += 1
            else:
                failed.append(etf["etf_name"])
        except Exception as e:
            print(f"  ❌ 예외 발생: {e}")
            failed.append(etf["etf_name"])

        # ETF 간 딜레이 (마지막 ETF 제외)
        if i < len(target_etfs):
            print(f"  ⏳ {DELAY_BETWEEN_ETF}초 대기...")
            time.sleep(DELAY_BETWEEN_ETF)

    # 결과 요약
    print(f"\n{'='*60}")
    print(f"🎉 백필 완료!")
    print(f"   성공: {success_count}/{len(target_etfs)} ETF")
    print(f"   총 저장: {total_rows:,}개 행")
    if failed:
        print(f"   실패: {', '.join(failed)}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
