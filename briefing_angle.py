#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
briefing_angle.py — 일일 브리핑의 '앵글(angle)'을 그날 데이터에서 골라
제목과 리드 문단을 매일 다르게 만드는 모듈

문제:
  기존 제목 = "{최대 비중감소 종목} {변화}%p, {최다보유 종목} N개 ETF 최다 보유"
  → 뒷부분("삼성전자 14개 ETF 최다 보유")이 몇 달간 상수라 매일 동일.
    30여 개 브리핑 페이지가 사실상 같은 제목 → 자동 생성물로 인식됨.

해법:
  그날 일어난 일 중 '가장 특이한 것'을 골라 앵글로 삼는다.
  신규 편입 > 전량 제외 > 운용사 의견 충돌 > 편입 확산 > 비중 급변 순으로
  임계값을 넘는 사건을 탐색하고, 앵글에 따라 제목·리드 문단·섹션 순서를 바꾼다.

  리드 문단은 LLM이 아니라 그날 수치로 조립하는 규칙 기반 서술이다.
  (사실과 어긋날 수 없고, 데이터가 다르면 문장도 달라진다)

사용:
  from briefing_angle import build_headline, build_lead
  headline, angle = build_headline(data, base_date)
  lead_html = build_lead(data, base_date, angle)

의존성: 표준 라이브러리만
"""
from datetime import datetime

# ── 앵글 선정 임계값 ────────────────────────────────────────────
TH_NEW_ENTRY = 2.0      # 신규 편입 비중 %
TH_EXIT = 2.0           # 제외 종목 최종 비중 %
TH_DIVERGENCE = 2.0     # 의견 충돌: |TIME변화| + |KoAct변화| %p
TH_WEIGHT = 2.0         # 비중 급변 %p

PROV_KR = {"timefolio": "TIME", "samsungactive": "KoAct"}


def _josa(word, pair):
    """받침 유무에 따라 조사를 고른다. pair는 ('이','가') 같은 (받침O, 받침X) 튜플.
    종목명이 영문·숫자로 끝나는 경우도 흔해서 마지막 '한글' 글자를 기준으로 판정하고,
    한글이 전혀 없으면 받침 없음으로 처리한다."""
    ch = None
    for c in reversed(str(word or "")):
        if 0xAC00 <= ord(c) <= 0xD7A3:
            ch = c
            break
    if ch is None:
        return pair[1]
    has_final = (ord(ch) - 0xAC00) % 28 != 0
    return pair[0] if has_final else pair[1]


def _fmt_eok(v):
    if not v:
        return "0억"
    eok = abs(v) / 1e8
    sign = "-" if v < 0 else ""
    if eok >= 10000:
        return f"{sign}{eok/10000:.2f}조"
    return f"{sign}{eok:,.0f}억"


def _short_etf(name):
    """ETF 정식명이 길어 제목에 다 못 넣을 때 축약"""
    if not name:
        return ""
    n = name.replace("TIMEFOLIO ", "TIME ").replace("삼성 ", "")
    return n if len(n) <= 22 else n[:21] + "…"


# ── 앵글 후보 탐색 ──────────────────────────────────────────────
# 점수 = 사건의 실제 크기(%/%p). 범주별 고정 가산점을 주지 않는다.
#   (고정 가산점을 주면 매일 나오는 '신규 편입'이 항상 이겨서 제목이 또 획일화됨)
#   가중치는 뉴스 가치를 아주 약하게만 반영한다.
W_NEW, W_EXIT, W_DIV, W_WEIGHT = 1.15, 1.10, 1.05, 1.00

# 카테고리 순환 보정: 날짜에 따라 한 범주에 소폭 가산.
#   신규 편입 비중(1~7%)이 비중 변동(1~5%p)보다 수치가 커서 그대로 두면
#   신규 편입 앵글이 과반을 차지한다. 4일 주기로 다른 범주가 한 번씩
#   유리해지도록 해 목록 전체의 앵글 분포를 고르게 만든다.
ROTATION = ["new_entry", "weight", "exit", "divergence"]
ROTATION_BONUS = 1.35


def _rot_bonus(base_date, family):
    d = datetime.strptime(base_date, "%Y-%m-%d")
    return ROTATION_BONUS if ROTATION[d.toordinal() % len(ROTATION)] == family else 1.0


def _tpl_idx(base_date, n):
    """날짜에서 결정적으로 문장 틀을 고른다 (같은 날은 항상 같은 틀, 날마다 달라짐)"""
    d = datetime.strptime(base_date, "%Y-%m-%d")
    return (d.toordinal()) % n


def _cand_new_entry(data, base_date):
    for n in data.get("new_entries", []):
        if n.get("weight", 0) >= TH_NEW_ENTRY:
            etf, nm, w = _short_etf(n["etf_name"]), n["name"], n["weight"]
            tpls = [
                f"{nm}, {etf}에 {w:.1f}% 신규 편입",
                f"{etf}, {nm} 신규 편입 — 비중 {w:.1f}%",
                f"{etf}가 새로 담은 {nm} ({w:.1f}%)",
            ]
            return {"key": "new_entry", "score": w * W_NEW * _rot_bonus(base_date, "new_entry"), "item": n,
                    "title": tpls[_tpl_idx(base_date, len(tpls))]}
    return None


def _cand_exit(data, base_date):
    for e in data.get("exits", []):
        if e.get("weight", 0) >= TH_EXIT:
            etf, nm, w = _short_etf(e["etf_name"]), e["name"], e["weight"]
            tpls = [
                f"{nm}, {etf}에서 전량 제외 (최종 {w:.1f}%)",
                f"{etf}, {nm} 전량 매도 — 직전 비중 {w:.1f}%",
                f"{etf} 포트폴리오에서 사라진 {nm} ({w:.1f}%)",
            ]
            return {"key": "exit", "score": w * W_EXIT * _rot_bonus(base_date, "exit"), "item": e,
                    "title": tpls[_tpl_idx(base_date, len(tpls))]}
    return None


def _cand_divergence(data, base_date):
    best, best_gap = None, 0
    for d in data.get("divergence", []):
        gap = abs(d.get("time_diff", 0)) + abs(d.get("koact_diff", 0))
        if gap > best_gap:
            best, best_gap = d, gap
    if best and best_gap >= TH_DIVERGENCE:
        nm, td, kd = best["name"], best["time_diff"], best["koact_diff"]
        up, down = ("TIME", "KoAct") if td > 0 else ("KoAct", "TIME")
        tpls = [
            f"{nm} 두고 갈린 운용사 — {up} 확대, {down} 축소",
            f"{up}은 담고 {down}은 덜어낸 {nm}",
            f"{nm}, TIME {td:+.1f}%p vs KoAct {kd:+.1f}%p",
        ]
        return {"key": "divergence", "score": best_gap * W_DIV * _rot_bonus(base_date, "divergence"), "item": best,
                "title": tpls[_tpl_idx(base_date, len(tpls))]}
    return None


def _cand_weight(data, base_date):
    up = data.get("weight_up") or []
    down = data.get("weight_down") or []
    cands = []
    if up and up[0]["diff"] >= TH_WEIGHT:
        cands.append(("up", up[0], abs(up[0]["diff"])))
    if down and abs(down[0]["diff"]) >= TH_WEIGHT:
        cands.append(("down", down[0], abs(down[0]["diff"])))
    if not cands:
        return None

    kind, w, mag = max(cands, key=lambda x: x[2])
    etf, nm, diff = _short_etf(w["etf_name"]), w["name"], w["diff"]
    pv, cv = w["prev_weight"], w["curr_weight"]
    if kind == "up":
        tpls = [
            f"{nm} {diff:+.1f}%p, {etf} 비중 확대",
            f"{etf}, {nm} 비중 {pv:.1f}%→{cv:.1f}% 확대",
            f"{nm} 비중 늘린 {etf} ({diff:+.1f}%p)",
        ]
    else:
        tpls = [
            f"{nm} {diff:+.1f}%p, {etf} 비중 축소",
            f"{etf}, {nm} 비중 {pv:.1f}%→{cv:.1f}% 축소",
            f"{nm} 덜어낸 {etf} ({diff:+.1f}%p)",
        ]
    return {"key": f"weight_{kind}", "score": mag * W_WEIGHT * _rot_bonus(base_date, "weight"), "item": w,
            "title": tpls[_tpl_idx(base_date, len(tpls))]}


def _cand_fallback(data, month, day):
    sm = data.get("smart_money") or []
    if sm:
        top = sm[0]
        return {"key": "consensus", "score": 0, "item": top,
                "title": f"{top['name']} {top['etf_count']}개 ETF 보유 — "
                         f"{month}월 {day}일 액티브 ETF 브리핑"}
    return {"key": "quiet", "score": 0, "item": None,
            "title": f"{month}월 {day}일 액티브 ETF 보유종목 브리핑"}


def build_headline(data, base_date):
    """그날 가장 특이한 사건을 골라 제목 생성.
    반환: (headline, angle_key)"""
    d = datetime.strptime(base_date, "%Y-%m-%d")
    cands = [c for c in (
        _cand_new_entry(data, base_date), _cand_exit(data, base_date),
        _cand_divergence(data, base_date), _cand_weight(data, base_date),
    ) if c]
    best = max(cands, key=lambda c: c["score"]) if cands else _cand_fallback(data, d.month, d.day)
    return best["title"], best["key"]


# ── 리드 문단 ───────────────────────────────────────────────────
def build_lead(data, base_date, angle):
    """그날 수치로 조립하는 서술형 리드 문단(3~5문장).
    앵글에 따라 첫 문장이 달라지고, 그날 있는 사건만 언급한다."""
    d = datetime.strptime(base_date, "%Y-%m-%d")
    md = f"{d.month}월 {d.day}일"
    s = []

    new_n = len(data.get("new_entries", []))
    exit_n = len(data.get("exits", []))
    up_n = len(data.get("weight_up", []))
    down_n = len(data.get("weight_down", []))
    div = data.get("divergence", [])
    sm = data.get("smart_money", [])

    # 1문장: 앵글별 도입
    if angle == "new_entry" and data["new_entries"]:
        n = data["new_entries"][0]
        s.append(f"{md} 공시에서 가장 눈에 띈 변화는 {n['name']}의 신규 편입입니다. "
                 f"{n['etf_name']}가 {n['weight']:.2f}% 비중으로 새로 담았습니다.")
    elif angle == "exit" and data["exits"]:
        e = data["exits"][0]
        s.append(f"{md} 공시에서 {e['name']}{_josa(e['name'], ('이','가'))} "
                 f"{e['etf_name']} 포트폴리오에서 사라졌습니다. "
                 f"직전 비중은 {e['weight']:.2f}%였습니다.")
    elif angle == "divergence" and div:
        v = div[0]
        s.append(f"{md}은 {v['name']}{_josa(v['name'], ('을','를'))} 두고 "
                 f"두 운용사의 판단이 엇갈린 날입니다. "
                 f"TIME은 {v['time_diff']:+.2f}%p, KoAct는 {v['koact_diff']:+.2f}%p 움직였습니다.")
    elif angle.startswith("weight_"):
        w = (data.get("weight_up") or data.get("weight_down"))[0] \
            if angle == "weight_up" else data["weight_down"][0]
        direction = "늘렸습니다" if w["diff"] > 0 else "줄였습니다"
        s.append(f"{md} 가장 큰 비중 변화는 {w['etf_name']}의 {w['name']}입니다. "
                 f"{w['prev_weight']:.2f}%에서 {w['curr_weight']:.2f}%로 {direction}.")
    else:
        s.append(f"{md} 액티브 ETF 42종의 보유종목 공시에는 큰 폭의 구조 변화가 없었습니다.")

    # 2문장: 전체 규모 요약
    moves = []
    if new_n:
        moves.append(f"신규 편입 {new_n}건")
    if exit_n:
        moves.append(f"제외 {exit_n}건")
    if up_n or down_n:
        moves.append(f"유의미한 비중 변동 {up_n + down_n}건")
    if moves:
        s.append(f"이날 전체적으로는 {', '.join(moves)}이 확인됐습니다.")

    # 3문장: 컨센서스
    if sm:
        top = sm[0]
        both = "양쪽 운용사가 모두" if top.get("both") else "한쪽 운용사가"
        s.append(f"보유 ETF 수 기준으로는 {top['name']}{_josa(top['name'], ('이','가'))} "
                 f"{top['etf_count']}개 ETF에 담겨 "
                 f"가장 넓게 퍼져 있으며, {both} 보유 중입니다.")

    # 4문장: 의견 충돌 (앵글이 아닐 때만 추가 언급)
    if div and angle != "divergence":
        s.append(f"한편 {div[0]['name']} 등 {len(div)}개 종목에서는 "
                 f"두 운용사의 매매 방향이 반대로 나타났습니다.")

    body = " ".join(s)
    return (
        '<section class="section briefing-lead">'
        '<h2>📝 오늘의 요약</h2>'
        f'<p style="font-size:.98rem;line-height:1.85">{body}</p>'
        '</section>'
    )


# ── 섹션 순서 ───────────────────────────────────────────────────
SECTION_PRIORITY = {
    "new_entry":  ["new", "exit", "up", "down", "smart", "div"],
    "exit":       ["exit", "new", "down", "up", "smart", "div"],
    "divergence": ["div", "up", "down", "new", "exit", "smart"],
    "weight_up":  ["up", "down", "new", "exit", "smart", "div"],
    "weight_down": ["down", "up", "new", "exit", "smart", "div"],
    "consensus":  ["smart", "up", "down", "new", "exit", "div"],
    "quiet":      ["smart", "up", "down", "new", "exit", "div"],
}


def section_order(angle):
    """앵글에 따라 본문 섹션 노출 순서를 바꾼다 (매일 같은 순서 방지)"""
    return SECTION_PRIORITY.get(angle, SECTION_PRIORITY["quiet"])