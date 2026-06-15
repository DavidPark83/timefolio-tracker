#!/usr/bin/env python3
"""
TIMEFOLIO ETF 일일 분석 브리핑 생성기
실행: Mac Mini cron → 매일 오전 8:30 (수집 완료 후)
"""

import requests
import json
from supabase import create_client
from datetime import date, timedelta

SUPABASE_URL = "..."
SUPABASE_KEY = "..."
OLLAMA_URL   = "http://localhost:11434/api/generate"
MODEL        = "gemma3:12b"

# TIMEFOLIO 19개 ETF 코드 목록
TIMEFOLIO_ETFS = [
    # 해외 11개
    {"code": "A441680", "name": "TIMEFOLIO 글로벌AI인공지능액티브", "type": "해외"},
    # ... 나머지
    # 국내 8개
    {"code": "A367470", "name": "TIMEFOLIO Korea플러스배당액티브", "type": "국내"},
    # ...
]

def get_holdings_data(supabase, etf_code, base_date):
    """오늘 + 어제 보유종목 데이터 조회"""
    today = supabase.table("etf_holdings")\
        .select("*")\
        .eq("etf_code", etf_code)\
        .eq("base_date", str(base_date))\
        .order("weight", desc=True)\
        .limit(20)\
        .execute()
    
    yesterday = supabase.table("etf_holdings")\
        .select("*")\
        .eq("etf_code", etf_code)\
        .eq("base_date", str(base_date - timedelta(days=1)))\
        .execute()
    
    return today.data, yesterday.data

def build_prompt(etf_name, etf_type, today_holdings, yesterday_holdings, nav):
    """LLM 프롬프트 구성"""
    
    # 변화 종목 계산
    today_map = {h["code"].upper(): h for h in today_holdings}
    yesterday_map = {h["code"].upper(): h for h in yesterday_holdings}
    
    new_stocks = [today_map[c]["stock_name"] for c in today_map if c not in yesterday_map]
    removed = [yesterday_map[c]["stock_name"] for c in yesterday_map if c not in today_map]
    increased = [(today_map[c]["stock_name"], today_map[c]["weight"]-yesterday_map[c]["weight"]) 
                 for c in today_map if c in yesterday_map 
                 and today_map[c]["weight"] - yesterday_map[c]["weight"] > 0.5]
    decreased = [(today_map[c]["stock_name"], today_map[c]["weight"]-yesterday_map[c]["weight"]) 
                 for c in today_map if c in yesterday_map 
                 and today_map[c]["weight"] - yesterday_map[c]["weight"] < -0.5]
    
    top10 = today_holdings[:10]
    
    prompt = f"""당신은 한국 ETF 전문 애널리스트입니다.
다음 데이터를 바탕으로 {etf_name} ETF의 오늘 포트폴리오 변화를 분석하는 브리핑을 작성해주세요.

**ETF 정보**
- 이름: {etf_name} ({'국내주식형' if etf_type=='국내' else '해외주식형'})
- NAV: {nav:,.0f}원

**Top 10 보유종목 (오늘)**
{chr(10).join([f"{i+1}. {h['stock_name']}: {h['weight']:.1f}%" for i, h in enumerate(top10)])}

**전일 대비 변화**
- 신규 편입: {', '.join(new_stocks) if new_stocks else '없음'}
- 제외 종목: {', '.join(removed) if removed else '없음'}
- 비중 증가 (0.5%p↑): {', '.join([f"{s}(+{d:.1f}%p)" for s,d in increased]) if increased else '없음'}
- 비중 감소 (0.5%p↓): {', '.join([f"{s}({d:.1f}%p)" for s,d in decreased]) if decreased else '없음'}

**작성 요건**
- 400~600자 한국어
- 주요 포트폴리오 변화의 투자적 의미 해석
- 섹터/테마 관점에서의 시사점
- 투자자에게 유용한 인사이트 포함
- 투자 권유 문구 제외 (정보 제공 목적)
"""
    return prompt

def call_ollama(prompt):
    """Ollama API 호출"""
    response = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 800}
    }, timeout=120)
    return response.json()["response"]

def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    today = date.today()
    
    print(f"=== ETF 브리핑 생성 시작: {today} ===")
    
    for etf in TIMEFOLIO_ETFS:
        try:
            print(f"처리중: {etf['name']}")
            today_h, yesterday_h = get_holdings_data(supabase, etf["code"], today)
            
            if not today_h:
                print(f"  → 데이터 없음, 스킵")
                continue
            
            nav = today_h[0].get("nav_price", 0) if today_h else 0
            prompt = build_prompt(etf["name"], etf["type"], 
                                   today_h, yesterday_h, nav)
            brief = call_ollama(prompt)
            
            # 변화 종목 JSON 저장용
            today_map = {h["code"].upper(): h for h in today_h}
            yesterday_map = {h["code"].upper(): h for h in yesterday_h}
            changes = {
                "new": [today_map[c]["stock_name"] for c in today_map if c not in yesterday_map],
                "removed": [yesterday_map[c]["stock_name"] for c in yesterday_map if c not in today_map],
                "increased": {today_map[c]["stock_name"]: round(today_map[c]["weight"]-yesterday_map[c]["weight"], 2)
                              for c in today_map if c in yesterday_map
                              and today_map[c]["weight"]-yesterday_map[c]["weight"] > 0.5},
                "decreased": {today_map[c]["stock_name"]: round(today_map[c]["weight"]-yesterday_map[c]["weight"], 2)
                              for c in today_map if c in yesterday_map
                              and today_map[c]["weight"]-yesterday_map[c]["weight"] < -0.5}
            }
            
            supabase.table("etf_briefs").upsert({
                "etf_code":     etf["code"],
                "etf_name":     etf["name"],
                "base_date":    str(today),
                "brief_text":   brief,
                "top_holdings": today_h[:10],
                "changes":      changes,
                "nav_price":    nav
            }, on_conflict="etf_code,base_date").execute()
            
            print(f"  → 완료 ({len(brief)}자)")
            
        except Exception as e:
            print(f"  → 오류: {e}")
    
    print("=== 완료 ===")

if __name__ == "__main__":
    main()
