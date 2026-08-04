#!/usr/bin/env python
"""매일 단타 스캐너 — 전 코인 거래량 스크리닝 → 검증 신호 → 트레이드 플랜.

매일 실행하는 단타 워크플로우 (Gemini PRD 유니버스 로직 + WFA 통과 신호):
  ① 바이낸스 USDT-M 선물 **전 심볼** 24h 티커 1콜 → 거래대금 상위 --top개
  ② 거래량 Z-Score = (현재 24h 거래대금 − 20일 평균) / 20일 표준편차 ≥ --min-z
     (그날 돈이 몰린 코인만 — 평소보다 이례적으로 거래량이 실린 것)
  ③ 상대강도(RS): 24h 등락률 > BTCUSDT (시장 대비 강한 놈만)
  ④ 생존 심볼에 1h OBV_DIV(워크포워드 통과 셋업) 평가
     → 신호 뜨면 진입/손절/목표/R:R 플랜 출력

사용:
  python tools/daily_scan.py                # 기본: top50, z>=2.0, RS 필터 on
  python tools/daily_scan.py --min-z 1.0 --no-rs   # 완화(후보 더 보기)
  python tools/daily_scan.py --selftest     # 오프라인 로직 자가검증

출력: 콘솔 표 + scan_result.json (최신 1회분).
⚠️ 신호는 판단 보조. 실주문은 별도 게이트(테스트넷→소액) 거칠 것.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

FAPI = "https://fapi.binance.com"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 인버스/변동성 ETF 토큰 제외 (2026-07-30, SOXS 2연패 교훈):
#   인버스 상품은 기초자산 하락 시 오르는 구조라 차트 신호의 방향 의미가
#   왜곡됨 (SOXS 숏 = 사실상 반도체 롱). 자동 신호·레이더·브래킷 대상에서 제외.
#   ※ watchlist.json에 사용자가 직접 넣은 심볼은 존중(제외 안 함).
EXCLUDED_BASES = {
    "SOXS", "SQQQ", "SPXS", "SPXU", "SDOW", "TZA", "FAZ", "SRTY",
    "TECS", "LABD", "DRIP", "DUST", "FNGD", "WEBS",          # 3x 인버스
    "UVXY", "VIXY", "VXX", "SVIX", "SVXY",                    # 변동성 상품
}


def is_excluded(symbol: str) -> bool:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return base in EXCLUDED_BASES


# 주식토큰 클러스터(동반등락 성향) — 동시 보유 상한 (2026-07-30 실전 사고 대응:
# 반도체/한국물 9건 1승8패 −$37 쏠림. portfolio_sim E검증: 상한2 = 수익 −1.8%p로
# MDD −13.3%→−10.4%. 상한1은 수익 희생 과다로 기각). 휴리스틱 명단 — 수동 유지.
EQUITY_TOKEN_BASES = {
    "SNDK", "SKHYNIX", "SOXL", "MU", "KORU", "SKHY", "QQQ", "EWY", "DRAM",
    "SAMSUNG", "INTC", "NVDA", "SPCX", "AAPL", "MSFT", "TSLA", "GOOGL",
    "META", "AMZN", "COIN", "HOOD", "CRCL", "IONQ", "AEHR", "MARA", "POET",
    "TSEM", "MUU", "AMD", "AVGO", "TSM", "ARM", "SMCI", "PLTR", "MSTR",
    "HYUNDAI", "KIA", "POSCO", "LGES", "NAVER", "KAKAO"}
EQUITY_CLUSTER_CAP = 2


def is_equity_token(symbol: str) -> bool:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return base in EQUITY_TOKEN_BASES


def _equity_cluster_full(led: dict) -> bool:
    n = sum(1 for p in led["open"] if is_equity_token(p["symbol"]))
    return n >= EQUITY_CLUSTER_CAP


# 텔레그램 자격증명 탐색 경로 (기존 봇들과 공유 — 값은 코드에 저장 안 함)
_ENV_PATHS = [
    Path(__file__).resolve().parent.parent / ".env",              # pattern_trader
    Path(r"D:\ai\01_trading\lbank_qmf(최신)") / ".env",           # lbank(TTSS) 봇
]


def _telegram_creds() -> tuple[str, str] | None:
    import os
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (tok and chat):
        for p in _ENV_PATHS:
            if not p.exists():
                continue
            kv = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    kv[k.strip()] = v.strip()
            tok = tok or kv.get("TELEGRAM_BOT_TOKEN", "")
            chat = chat or kv.get("TELEGRAM_CHAT_ID", "")
            if tok and chat:
                break
    return (tok, chat) if tok and chat else None


def notify(text: str) -> bool:
    """텔레그램 전송 (fundarb notify 패턴). 자격증명 없으면 no-op False."""
    creds = _telegram_creds()
    if creds is None:
        return False
    tok, chat = creds
    import urllib.parse
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            r.read()
        return True
    except Exception:
        return False


def _get(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ───────────────── 선물 단타 자동매매 (--trade) ─────────────────
# $500 페이퍼 계좌를 실시간 가격으로 운용. 신호 → 진입, 매 사이클 SL/TP 정산.
# 실계좌 전환 시 이 원장 로직이 그대로 주문 로직이 된다 (진입가/손절/목표 동일).
LEDGER = Path(__file__).resolve().parent.parent / "futures_paper.json"

# --live 시 실주문 미러링 (binance_live 모듈, 기본 테스트넷). main()에서 설정.
LIVE_EXEC = False


def _live(fn: str, *a) -> str | None:
    """실주문 호출 래퍼 — 실패해도 페이퍼 원장은 계속(진실은 원장이 기록),
    실패 사실은 메시지로 정직하게 노출."""
    if not LIVE_EXEC:
        return None
    try:
        import binance_live
        r = getattr(binance_live, fn)(*a)
        if fn == "open_trade":
            return (f"{r['env']} 실주문 체결 {r['symbol']} {r['qty']}개 "
                    f"(${r['notional']:,.0f}) + 거래소측 SL/TP 설치")
        if fn == "update_stops":
            return f"{r['env']} 거래소 손절 재설치 {r['symbol']} → {r['stop']:g}"
        return f"{r['env']} 실주문 정리 {r['symbol']} " + \
            (f"{r.get('closed_qty')}개 청산" if "closed_qty" in r else "잔여 없음")
    except Exception as e:
        return f"⚠️ 실주문 실패({fn}): {e}"
START_EQUITY = 500.0          # 할당 자본
RISK_PER_TRADE = 0.015        # 트레이드당 리스크 1.5% (Gemini PRD)
MAX_POSITIONS = 3             # 동시 포지션 (PRD)
MAX_LEVERAGE = 2.0            # 명목가 상한 = 자본×2 (청산 리스크 억제)
HOLD_MAX_BARS = 48            # 시간청산 (백테스트와 동일)
TAKER_FEE = 0.0005            # 체결당 0.05%
DAILY_LOSS_STOP = 0.05        # 하루 -5% 도달 시 신규 진입 중단


def _load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"equity": START_EQUITY, "start_equity": START_EQUITY,
            "day": "", "day_start_equity": START_EQUITY,
            "open": [], "closed": []}


def _save_ledger(led: dict) -> None:
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=2),
                      encoding="utf-8")


def _walk_bars(pos: dict, bars: list[tuple[int, float, float, float]]):
    """진입 후 봉들을 걸어 청산 판정. bars=[(openTime, high, low, close)].

    반환 (exit_price, reason, bars_held) 또는 None(계속 보유).
    같은 봉에서 손절·목표 동시 터치면 손절 우선(보수적).

    브래킷 포지션 한정 '본절 이동'(exit_study C안 2026-07-30 채택,
    발동 기준은 2026-08-04 exit_grid_study로 +1R→+1.3R 상향):
    +BE_TRIGGER_R×R 도달 확인 후 손절을 진입가로 올린다(다음 봉부터 적용 —
    같은 봉 내 순서를 알 수 없으므로 보수적).
    OBV_DIV 포지션은 검증 범위 밖이라 원형 유지."""
    held = pos.get("bars_held", 0)
    long = pos["direction"] == "LONG"
    be_on = pos.get("setup") == "RADAR_BRACKET"
    r0 = pos.get("risk0", abs(pos["entry"] - pos["stop"]))
    # last_ts: 이미 정산한 봉은 다시 세지 않는다. (2026-08-03 수정 — 종전엔
    # 매 스캔 진입시점부터 재계산해 bars_held가 이중 누적됐고, 시간청산 조기
    # 발동 + BE 소급 적용 + 청산시각 왜곡을 일으켰다.)
    for ts, hi, lo, close in bars:
        if ts <= pos.get("last_ts", pos["entry_ts"]):
            continue
        pos["last_ts"] = ts
        pos["exit_ts"] = ts + 3600_000     # 청산 시 이 봉의 마감시각이 남는다
        held += 1
        tgt = pos.get("target")            # None = 지표 청산형(일목) — 목표가 없음
        if long:
            if lo <= pos["stop"]:
                return pos["stop"], ("BE" if pos.get("be_armed") else "STOP"), held
            if tgt is not None and hi >= tgt:
                return tgt, "TARGET", held
        else:
            if hi >= pos["stop"]:
                return pos["stop"], ("BE" if pos.get("be_armed") else "STOP"), held
            if tgt is not None and lo <= tgt:
                return tgt, "TARGET", held
        if held >= pos.get("hold_max", HOLD_MAX_BARS):
            return close, "TIME", held
        # +1.3R 본절 이동 (봉 마감 후 적용 → 다음 봉부터 유효)
        if be_on and not pos.get("be_armed") and r0 > 0:
            be_px = BE_TRIGGER_R * r0
            hit_1r = ((hi >= pos["entry"] + be_px) if long
                      else (lo <= pos["entry"] - be_px))
            if hit_1r:
                pos["stop"] = pos["entry"]
                pos["be_armed"] = True
                pos["_be_new"] = True          # settle이 실주문 손절도 이동
    pos["bars_held"] = held
    return None


def settle_positions(led: dict, timeout: float) -> list[str]:
    """열린 포지션 전부 최신 1h봉으로 정산. 청산 발생 시 메시지 반환."""
    msgs, still = [], []
    for pos in led["open"]:
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={pos['symbol']}"
                      f"&interval=1h&startTime={pos['entry_ts']}&limit=500",
                      timeout)
            bars = [(int(k[0]), float(k[2]), float(k[3]), float(k[4]))
                    for k in kl[:-1]]        # 마지막 진행중 봉 제외
        except Exception:
            still.append(pos)
            continue
        hit = _walk_bars(pos, bars)
        if hit is None:
            if pos.pop("_be_new", False):      # +1R 도달 → 본절 이동됨
                msgs.append(f"🛡️ 본절 이동 {pos['symbol']} — +1R 도달, "
                            f"손절을 진입가({pos['entry']:g})로 상향")
                lm = _live("update_stops", pos["symbol"], pos["direction"],
                           pos["stop"], pos["target"])
                if lm:
                    msgs.append("  " + lm)
            still.append(pos)
            continue
        pos.pop("_be_new", None)
        exit_p, reason, held = hit
        sgn = 1 if pos["direction"] == "LONG" else -1
        gross = (exit_p - pos["entry"]) * sgn * pos["qty"]
        fees = (pos["entry"] + exit_p) * pos["qty"] * TAKER_FEE
        pnl = gross - fees
        led["equity"] += pnl
        risk = pos.get("risk0", abs(pos["entry"] - pos["stop"])) * pos["qty"]
        pos.update(exit=exit_p, reason=reason, bars_held=held,
                   pnl=round(pnl, 2), r=round(pnl / risk, 2) if risk else 0)
        led["closed"].append(pos)
        emo = "🟢" if pnl > 0 else "🔴"
        msgs.append(f"{emo} 청산 {pos['symbol']} {pos['direction']} "
                    f"[{reason}] {held}봉 · net ${pnl:+.2f} ({pos['r']:+.2f}R) "
                    f"→ 자본 ${led['equity']:.2f}")
        # 실주문 동기화: SL/TP는 거래소가 이미 집행했을 수 있음 —
        # 잔여 주문 취소 + 남은 포지션 정리(멱등).
        lm = _live("close_trade", pos["symbol"])
        if lm:
            msgs.append("  " + lm)
    led["open"] = still
    return msgs


def enter_positions(led: dict, cands, t0: str) -> list[str]:
    """신호 후보 → 리스크 사이징 진입. 일손실 한도·중복·슬롯 체크."""
    today = t0[:10]
    if led.get("day") != today:
        led["day"] = today
        led["day_start_equity"] = led["equity"]
    if led["equity"] <= led["day_start_equity"] * (1 - DAILY_LOSS_STOP):
        return [f"⛔ 일손실 한도(-{DAILY_LOSS_STOP*100:.0f}%) 도달 — 오늘 신규 진입 중단"]

    msgs = []
    open_syms = {p["symbol"] for p in led["open"]}
    for sym, info in cands:
        if len(led["open"]) >= MAX_POSITIONS:
            break
        if info.get("signal") not in ("LONG", "SHORT") or "plan" not in info:
            continue
        if sym in open_syms:
            continue
        if is_equity_token(sym) and _equity_cluster_full(led):
            msgs.append(f"⛔ {sym} 신호 스킵 — 주식토큰 동시보유 상한"
                        f"({EQUITY_CLUSTER_CAP}) 도달 (쏠림 방지)")
            continue
        p = info["plan"]
        entry, stop, target = p["entry"], p["stop"], p["target"]
        dist = abs(entry - stop)
        if dist <= 0:
            continue
        if abs(target - entry) < dist:      # R:R<1 = 뭉개진 플랜(ZIL 사고) 거부
            msgs.append(f"⛔ {sym} 플랜 무결성 실패(R:R<1) — 진입 거부")
            continue
        qty = (led["equity"] * RISK_PER_TRADE) / dist
        qty = min(qty, led["equity"] * MAX_LEVERAGE / entry)   # 레버리지 상한
        notional = qty * entry
        if notional < 5:                                        # 거래소 최소주문
            continue
        led["open"].append({
            "symbol": sym, "direction": info["signal"], "entry": entry,
            "stop": stop, "target": target, "qty": qty, "risk0": dist,
            "entry_ts": int(time.time() * 1000), "opened": t0, "bars_held": 0})
        open_syms.add(sym)
        msgs.append(f"🎯 진입 {sym} {info['signal']} ${notional:,.0f} "
                    f"(레버 {notional/led['equity']:.2f}x, Z{info['z']:.1f}) · "
                    f"진입 {entry} 손절 {stop} 목표 {target} "
                    f"(리스크 ${led['equity']*RISK_PER_TRADE:.1f})")
        lm = _live("open_trade", sym, info["signal"], qty, stop, target)
        if lm:
            msgs.append("  " + lm)
    return msgs


DASH = Path(__file__).resolve().parent.parent / "dashboard_danta.html"


def _charts_payload(led: dict, timeout: float) -> list[dict]:
    """차트 데이터: 보유 포지션(진입/손절/목표선) + 브래킷(양방향 진입선).

    최근 96시간 종가 + 수평 기준선. 최대 6개 심볼."""
    items = []
    for p in led["open"]:
        lines = [("진입", p["entry"], "#6d8cff"), ("손절", p["stop"], "#ff6b6b")]
        if p.get("target") is not None:
            lines.append(("목표", p["target"], "#34d399"))
        tag = p["direction"] + (" · 브래킷" if p.get("setup") == "RADAR_BRACKET"
                                else " · 일목" if p.get("setup") == "ICHIMOKU_4H" else "")
        items.append((p["symbol"], tag, lines))
    for b in led.get("brackets", []):
        items.append((b["symbol"], "브래킷 대기 — 먼저 뚫리는 쪽으로 진입",
                      [("롱 진입선(상단 돌파)", b["up"], "#fbbf24"),
                       ("숏 진입선(하단 이탈)", b["dn"], "#a78bff")]))
    out, seen = [], set()
    for sym, tag, lines in items:
        if sym in seen or len(out) >= 6:
            continue
        seen.add(sym)
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}"
                      f"&interval=1h&limit=96", timeout)
        except Exception:
            continue
        out.append({
            "sym": sym, "tag": tag,
            "labels": [time.strftime("%d일 %H시", time.localtime(int(k[0]) / 1000))
                       for k in kl],
            "closes": [float(k[4]) for k in kl],
            "lines": [{"label": lb, "value": v, "color": c}
                      for lb, v, c in lines]})
    return out


def _calendar_html(closed: list) -> str:
    """매매 캘린더 — 청산일 기준 일별 손익·건수를 월 달력으로. (복기와 동일하게
    청산시각 = entry_ts + bars_held×1h 로 계산)"""
    import calendar as _cal
    from datetime import datetime as _dt
    days: dict = {}
    now_ms = int(time.time() * 1000)
    for t in closed:
        ts = min(t.get("exit_ts")
                 or (t.get("entry_ts", 0) + t.get("bars_held", 0) * 3600_000),
                 now_ms)                       # 구버전 부풀린 bars_held 방어
        d = _dt.fromtimestamp(ts / 1000).date()
        rec = days.setdefault(d, [0.0, 0])
        rec[0] += t.get("pnl", 0)
        rec[1] += 1
    if not days:
        return "<div class=mut>청산 기록 없음</div>"
    today = _dt.now().date()
    out = ""
    for y, m in sorted({(d.year, d.month) for d in days}):
        mvals = [v for d, v in days.items() if (d.year, d.month) == (y, m)]
        mt, mn = sum(v[0] for v in mvals), sum(v[1] for v in mvals)
        cells = "".join(f"<div class='cal h'>{w}</div>" for w in "월화수목금토일")
        first_wd, ndays = _cal.monthrange(y, m)
        cells += "<div class=cal></div>" * first_wd
        for dd in range(1, ndays + 1):
            d = _dt(y, m, dd).date()
            v = days.get(d)
            cls = "cal" + (" today" if d == today else "")
            body = ""
            if v:
                cls += " calup" if v[0] > 0 else (" caldn" if v[0] < 0 else "")
                body = f"<b>${v[0]:+.1f}</b><span>{v[1]}건</span>"
            cells += f"<div class='{cls}'><i>{dd}</i>{body}</div>"
        out += (f"<div class=calmon><div class=calhead>📅 {y}년 {m}월 &nbsp;"
                f"<span class='{'up' if mt >= 0 else 'dn'}'>${mt:+.2f}</span> "
                f"<span class=mut>· {mn}건</span></div>"
                f"<div class=calgrid>{cells}</div></div>")
    return out


def render_dashboard(led: dict, cands, t0: str, radar: list | None = None,
                     track: dict | None = None,
                     charts: list | None = None) -> None:
    """한눈 상황판 HTML 생성 — 매 스캔 사이클마다 덮어쓴다.

    브라우저가 60초마다 자동 새로고침하며, 생성시각이 75분 이상 오래되면
    화면 스스로 '시스템 멈춤' 경고를 띄운다(감시 내장)."""
    now_ms = int(time.time() * 1000)
    ret = led["equity"] / led["start_equity"] - 1
    closed = led["closed"]
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    wr = f"{len(wins)/len(closed)*100:.0f}%" if closed else "—"
    day_dd = led["equity"] / led.get("day_start_equity", led["equity"]) - 1
    col = "#34d399" if ret >= 0 else "#ff6b6b"

    def esc(s):
        return str(s).replace("<", "&lt;")

    # 스캔 시점 현재가(차트 마지막 종가) — 브라우저 JS가 30초마다 실시간 갱신
    pxmap = {c["sym"]: c["closes"][-1] for c in (charts or [])}
    tot_upnl = 0.0
    tot_notional = 0.0
    open_rows = ""
    for p in led["open"]:
        sym = p["symbol"]
        sgn = 1 if p["direction"] == "LONG" else -1
        cur = pxmap.get(sym)
        upnl = (cur - p["entry"]) * sgn * p["qty"] if cur else 0.0
        tot_upnl += upnl
        notional = p["qty"] * p["entry"]
        tot_notional += notional
        lev = notional / led["equity"] if led["equity"] else 0.0
        tgt = p.get("target")
        prog = ((cur - p["entry"]) / (tgt - p["entry"]) * 100
                if cur and tgt is not None and tgt != p["entry"] else 0.0)
        w = max(0, min(100, prog))
        open_rows += (
            f"<tr><td>{esc(sym)}"
            f"{' <span class=mut>[브래킷]</span>' if p.get('setup')=='RADAR_BRACKET' else ''}</td>"
            f"<td class='{p['direction'].lower()}'>{p['direction']}</td>"
            f"<td><b>${notional:,.0f}</b> <span class=mut>{lev:.2f}x</span></td>"
            f"<td>{p['entry']:g}</td>"
            f"<td class=cur data-sym='{esc(sym)}'>{f'{cur:g}' if cur else '—'}</td>"
            f"<td class='upnl {'up' if upnl>=0 else 'dn'}' data-sym='{esc(sym)}'>"
            f"${upnl:+.2f}</td>"
            f"<td><div class=pbar><div class='{'neg' if prog<0 else ''}' "
            f"data-sym='{esc(sym)}' style='width:{w if prog>=0 else min(100,-prog):.0f}%'></div></div>"
            f"<span class='mut pt' data-sym='{esc(sym)}' style=font-size:10px>"
            f"{prog:.0f}%</span></td>"
            f"<td class=mut>{p['stop']:g} / "
            f"{f'{tgt:g}' if tgt is not None else '지표청산'}</td>"
            f"<td>{p.get('bars_held',0)}봉</td></tr>")
    open_rows += "".join(
        f"<tr><td>📐 {esc(b['symbol'])}</td><td class=mut>브래킷 대기</td>"
        f"<td colspan=4>↑{b['up']:g} 롱 / ↓{b['dn']:g} 숏</td>"
        f"<td colspan=2 class=mut>먼저 뚫리는 쪽으로 진입</td>"
        f"<td class=mut>{BRACKET_WINDOW_H}h</td></tr>"
        for b in led.get("brackets", []))
    open_rows = open_rows or "<tr><td colspan=9 class=mut>보유 없음 — 신호 대기 중</td></tr>"
    real_eq = led["equity"] + tot_upnl
    real_ret = real_eq / led["start_equity"] - 1
    rcol = "#34d399" if real_ret >= 0 else "#ff6b6b"
    openpos_json = json.dumps(
        [{"symbol": p["symbol"], "direction": p["direction"],
          "entry": p["entry"], "target": p["target"], "qty": p["qty"]}
         for p in led["open"]], ensure_ascii=False)
    closed_rows = "".join(
        f"<tr><td>{esc(t['symbol'])}</td><td class='{t['direction'].lower()}'>"
        f"{t['direction']}</td><td>{t.get('reason','')}</td>"
        f"<td class='{'up' if t.get('pnl',0)>0 else 'dn'}'>${t.get('pnl',0):+.2f}"
        f" ({t.get('r',0):+.2f}R)</td><td>{t.get('bars_held','')}봉</td>"
        f"<td class=mut>{esc(t.get('review','대기'))}</td></tr>"
        for t in reversed(closed[-10:])) or "<tr><td colspan=6 class=mut>청산 이력 없음</td></tr>"
    cand_rows = "".join(
        f"<tr><td>{esc(s)}</td><td>${i['vol_usd']/1e6:,.0f}M</td>"
        f"<td>{i['z']:.1f}</td><td>{i['chg']:+.1f}%</td>"
        f"<td>{'🎯 '+i['signal'] if i.get('signal') in ('LONG','SHORT') else '감시중'}</td></tr>"
        for s, i in cands) or "<tr><td colspan=5 class=mut>오늘 거래량 급증 코인 없음</td></tr>"
    radar_rows = "".join(
        f"<tr><td><b>{r['score']}/{RADAR_MAX}</b></td><td>{esc(s)}</td>"
        f"<td>{'+'.join(_FLAG_KR[k] for k, v in r['flags'].items() if v)}</td>"
        f"<td>{esc(r['hint'])}</td></tr>"
        for s, r in (radar or [])[:12]) \
        or "<tr><td colspan=4 class=mut>전조 감지 없음 (6-way 중 2개 미만)</td></tr>"

    # 전조 추적: 진행중 + 최근 판정 + 실전 적중률
    evs = (track or {}).get("events", [])
    resolved = [e for e in evs if e["status"] == "resolved"]
    booms = sum(1 for e in resolved if e.get("boom"))
    hitrate = (f"실전 적중률 {booms}/{len(resolved)}"
               f" ({booms/len(resolved)*100:.0f}%)" if resolved else "판정 대기")
    import time as _t
    now_ms = int(_t.time() * 1000)
    track_rows = ""
    for e in [x for x in evs if x["status"] == "tracking"]:
        eh = (now_ms - e["ts"]) / 3600_000
        track_rows += (f"<tr><td>⏳ {esc(e['symbol'])}</td>"
                       f"<td>{e['score']}/{RADAR_MAX}</td>"
                       f"<td>{eh:.0f}h/{TRACK_HOURS}h</td>"
                       f"<td class=up>+{e.get('up',0)*100:.1f}%</td>"
                       f"<td class=dn>-{e.get('dn',0)*100:.1f}%</td><td></td></tr>")
    for e in resolved[-8:][::-1]:
        emo = "✅ 폭발" if e.get("boom") else "➖ 무산"
        track_rows += (f"<tr><td>{esc(e['symbol'])}</td>"
                       f"<td>{e['score']}/{RADAR_MAX}</td><td>판정완료</td>"
                       f"<td colspan=2>{esc(e.get('outcome',''))}</td>"
                       f"<td>{emo}</td></tr>")
    track_rows = track_rows or "<tr><td colspan=6 class=mut>추적 중인 전조 없음</td></tr>"

    html = f"""<!DOCTYPE html><html lang=ko><head><meta charset=utf-8>
<meta http-equiv=refresh content=60><title>⚡ 선물 단타 상황판</title><style>
body{{background:#0d1220;color:#e8edf8;font-family:'Malgun Gothic',sans-serif;
 margin:0;padding:18px 22px;font-size:14px}}
h1{{font-size:17px;margin:0 0 4px}} .mut{{color:#7f8bab}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
 gap:10px;margin:14px 0}}
.card{{background:#151d31;border:1px solid #26304d;border-radius:10px;padding:11px 14px}}
.card b{{font-size:19px}} .card .l{{font-size:11px;color:#7f8bab}}
table{{width:100%;border-collapse:collapse;margin:6px 0 16px}}
th{{text-align:left;font-size:11px;color:#7f8bab;padding:5px 8px;
 border-bottom:1px solid #26304d}}
td{{padding:6px 8px;border-bottom:1px solid #1a2340}}
h2{{font-size:13px;color:#9fb0d6;margin:16px 0 2px}}
.up,.long{{color:#34d399}} .dn,.short{{color:#ff6b6b}}
#alarm{{display:none;background:#5b1a1a;border:1px solid #ff6b6b;color:#ffb3b3;
 padding:10px 14px;border-radius:10px;margin:10px 0;font-weight:700}}
.chip{{display:inline-block;background:#151d31;border:1px solid #26304d;
 border-radius:20px;padding:3px 11px;font-size:12px;margin-right:6px}}
.pbar{{background:#1a2340;border-radius:6px;height:10px;width:110px;overflow:hidden}}
.pbar div{{height:100%;background:linear-gradient(90deg,#34d399,#2dd4bf);border-radius:6px}}
.pbar div.neg{{background:linear-gradient(90deg,#ff6b6b,#f43f5e)}}
.calmon{{display:inline-block;vertical-align:top;margin:0 18px 14px 0}}
.calhead{{font-size:13px;margin:4px 0 6px}}
.calgrid{{display:grid;grid-template-columns:repeat(7,64px);gap:4px}}
.cal{{background:#151d31;border:1px solid #1a2340;border-radius:7px;min-height:44px;
 padding:3px 5px;font-size:10px;position:relative}}
.cal.h{{background:none;border:none;min-height:14px;color:#7f8bab;text-align:center}}
.cal i{{font-style:normal;color:#7f8bab;font-size:9px}}
.cal b{{display:block;font-size:11px;margin-top:2px}}
.cal span{{color:#7f8bab;font-size:9px}}
.cal.calup{{border-color:#1f5a44}} .cal.calup b{{color:#34d399}}
.cal.caldn{{border-color:#6b2530}} .cal.caldn b{{color:#ff6b6b}}
.cal.today{{outline:1px solid #4a6ff0}}</style></head><body>
<h1>⚡ 선물 단타 상황판 <span class=mut style=font-size:12px>갱신 {t0} · 매시 5분 자동 스캔 · 60초 자동 새로고침</span></h1>
<div><span class=chip id=live>🟢 시스템 정상</span>
<span class=chip>📲 텔레그램 알림 ON</span>
<span class=chip>리스크 1.5%/건 · 최대 3포지션 · 일손실 −5% 중단</span></div>
<div id=alarm>🔴 스캔이 <span id=stale></span>분째 갱신되지 않았습니다 — 예약작업(AIDailyScan) 점검 필요</div>
<div class=grid>
<div class=card><div class=l>💰 실질 자본 (미실현 포함)</div><b id=realeq style="color:{rcol}">${real_eq:.2f}</b><div class=l id=realret>{real_ret*100:+.2f}% · 미실현 ${tot_upnl:+.2f}</div></div>
<div class=card><div class=l>확정 자본 (시작 $500)</div><b style="color:{col}">${led['equity']:.2f}</b><div class=l>{ret*100:+.2f}%</div></div>
<div class=card><div class=l>보유 포지션</div><b>{len(led['open'])}</b><div class=l>/ 최대 3 · 명목 ${tot_notional:,.0f} · 레버 {(tot_notional/led['equity'] if led['equity'] else 0):.2f}x</div></div>
<div class=card><div class=l>청산 누적</div><b>{len(closed)}건</b><div class=l>승률 {wr}</div></div>
<div class=card><div class=l>오늘 손익</div><b style="color:{'#34d399' if day_dd>=0 else '#ff6b6b'}">{day_dd*100:+.2f}%</b><div class=l>한도 −5%</div></div>
</div>
<h2>📌 보유 포지션 <span class=mut style="text-transform:none;font-weight:500">— 현재가·미실현은 30초마다 실시간 갱신</span></h2>
<table><tr><th>심볼</th><th>방향</th><th>진입금액(레버)</th><th>진입</th><th>현재가</th><th>미실현</th><th>목표 진행률</th><th>손절/목표</th><th>보유</th></tr>{open_rows}</table>
<h2>📈 차트 — 진입선·손절·목표 표시 (최근 96시간)</h2>
<div id=chartgrid style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;margin:6px 0 16px">
{'' if charts else '<div class=mut>표시할 포지션/브래킷 없음</div>'}</div>
<h2>🕒 오늘의 선정 코인 (거래량 급증 + BTC 대비 강세)</h2>
<table><tr><th>심볼</th><th>거래대금</th><th>Z</th><th>24h</th><th>신호</th></tr>{cand_rows}</table>
<h2>🔭 전조 레이더 (백테스트 가중: 점화×2+펀딩×2+연료, 만점{RADAR_MAX} · 알림 {RADAR_ALERT_SCORE}+ — 관심목록, 자동진입 아님)</h2>
<table><tr><th>점수</th><th>심볼</th><th>충족 신호</th><th>방향 힌트</th></tr>{radar_rows}</table>
<h2>📊 전조 추적 — {hitrate} (알림 등급 전조의 24h 실제 결과)</h2>
<table><tr><th>심볼</th><th>전조</th><th>경과</th><th>최대상방</th><th>최대하방</th><th>판정</th></tr>{track_rows}</table>
<h2>📜 최근 청산 <span class=mut style="text-transform:none;font-weight:500">— {review_summary(closed)}</span></h2>
<table><tr><th>심볼</th><th>방향</th><th>사유</th><th>손익</th><th>보유</th><th>📚 복기 (24h 후 판정)</th></tr>{closed_rows}</table>
<h2>📅 매매 캘린더 <span class=mut style="text-transform:none;font-weight:500">— 청산일 기준 일별 손익</span></h2>
{_calendar_html(closed)}
<script>
const gen={now_ms}, mins=Math.floor((Date.now()-gen)/60000);
if(mins>75){{document.getElementById('alarm').style.display='block';
 document.getElementById('stale').textContent=mins;
 const lv=document.getElementById('live');lv.textContent='🔴 시스템 멈춤 의심';lv.style.borderColor='#ff6b6b';}}
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
const CHARTS={json.dumps(charts or [], ensure_ascii=False)};
Chart.defaults.color='#7f8bab';
Chart.defaults.borderColor='rgba(127,139,171,.14)';
const grid=document.getElementById('chartgrid');
for(const c of CHARTS){{
  const box=document.createElement('div');
  box.style.cssText='background:#151d31;border:1px solid #26304d;border-radius:10px;padding:10px 12px';
  box.innerHTML=`<div style="font-size:12px;font-weight:700;margin-bottom:6px">${{c.sym}} <span style="color:#7f8bab;font-weight:500">${{c.tag}}</span></div>`;
  const cv=document.createElement('canvas'); cv.height=190; box.appendChild(cv);
  grid.appendChild(box);
  const n=c.closes.length;
  const ds=[{{label:'종가',data:c.closes,borderColor:'#e8edf8',borderWidth:1.6,
             pointRadius:0,tension:.15}}];
  for(const l of c.lines)
    ds.push({{label:l.label+' '+l.value,data:Array(n).fill(l.value),
             borderColor:l.color,borderWidth:1.4,borderDash:[6,4],pointRadius:0}});
  new Chart(cv,{{type:'line',data:{{labels:c.labels,datasets:ds}},
    options:{{responsive:true,animation:false,interaction:{{intersect:false,mode:'index'}},
      plugins:{{legend:{{labels:{{boxWidth:10,font:{{size:10}}}}}}}},
      scales:{{x:{{ticks:{{maxTicksLimit:6,font:{{size:9}}}},grid:{{display:false}}}},
              y:{{ticks:{{font:{{size:9}}}}}}}}}}}});
}}

// ── 실시간 손익: 30초마다 바이낸스 현재가 → 미실현·진행률·실질자본 갱신 ──
const OPENPOS={openpos_json};
const FIXED_EQ={led['equity']:.2f}, START_EQ={led['start_equity']:.2f};
async function liveUpdate(){{
  let tot=0;
  for(const p of OPENPOS){{
    try{{
      const r=await fetch('https://fapi.binance.com/fapi/v1/ticker/price?symbol='+p.symbol);
      const cur=parseFloat((await r.json()).price);
      const sgn=p.direction==='LONG'?1:-1;
      const upnl=(cur-p.entry)*sgn*p.qty; tot+=upnl;
      const prog=p.target?(cur-p.entry)/(p.target-p.entry)*100:0;
      const q=(sel)=>document.querySelector(sel+`[data-sym="${{p.symbol}}"]`);
      if(q('.cur')) q('.cur').textContent=cur;
      const u=q('.upnl');
      if(u){{u.textContent=(upnl>=0?'$+':'$')+upnl.toFixed(2);
            u.className='upnl '+(upnl>=0?'up':'dn')+' '; u.dataset.sym=p.symbol;}}
      const bar=q('.pbar div'); const pt=q('.pt');
      if(bar){{bar.style.width=Math.max(0,Math.min(100,Math.abs(prog)))+'%';
              bar.className=prog<0?'neg':'';bar.dataset.sym=p.symbol;}}
      if(pt) pt.textContent=prog.toFixed(0)+'%';
    }}catch(e){{}}
  }}
  const re=document.getElementById('realeq');
  if(re && OPENPOS.length){{
    const eq=FIXED_EQ+tot, ret=(eq/START_EQ-1)*100;
    re.textContent='$'+eq.toFixed(2);
    re.style.color=ret>=0?'#34d399':'#ff6b6b';
    document.getElementById('realret').textContent=
      (ret>=0?'+':'')+ret.toFixed(2)+'% · 미실현 '+(tot>=0?'$+':'$')+tot.toFixed(2)+' (실시간)';
  }}
}}
liveUpdate(); setInterval(liveUpdate, 30000);
</script></body></html>"""
    DASH.write_text(html, encoding="utf-8")


def zscore(cur: float, hist: list[float]) -> float:
    """현재값이 과거 분포에서 몇 σ인가. 표본<2 or σ=0이면 0."""
    if len(hist) < 2:
        return 0.0
    m = sum(hist) / len(hist)
    var = sum((x - m) ** 2 for x in hist) / (len(hist) - 1)
    sd = math.sqrt(var)
    return (cur - m) / sd if sd > 0 else 0.0


def screen_universe(top: int, min_z: float, use_rs: bool, timeout: float):
    """①~③: 전 심볼 → 거래량 상위 → Z-Score·RS 필터. [(sym, info)] 반환."""
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", timeout)
    perp = [t for t in tickers
            if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
            and not is_excluded(t["symbol"])]
    btc_chg = next((float(t["priceChangePercent"]) for t in perp
                    if t["symbol"] == "BTCUSDT"), 0.0)
    perp.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    print(f"① 전 심볼 {len(perp)}개 → 거래대금 상위 {top}개")

    out = []
    for t in perp[:top]:
        sym = t["symbol"]
        cur_vol = float(t["quoteVolume"])
        chg = float(t["priceChangePercent"])
        # 20일 일봉의 거래대금(quote, idx7). 마지막 봉=오늘 진행중 → 제외.
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}"
                      f"&interval=1d&limit=21", timeout)
        except Exception:
            continue
        hist = [float(k[7]) for k in kl[:-1]]
        z = zscore(cur_vol, hist)
        rs_ok = chg > btc_chg
        if z >= min_z and (rs_ok or not use_rs):
            out.append((sym, {"vol_usd": cur_vol, "z": z, "chg": chg,
                              "rs": rs_ok}))
    print(f"②③ Z≥{min_z}" + (f" & RS>BTC({btc_chg:+.1f}%)" if use_rs else "")
          + f" 통과: {len(out)}개")
    out.sort(key=lambda x: x[1]["z"], reverse=True)
    return out, btc_chg


def evaluate_signals(cands, timeout: float):
    """④: 후보에 1h OBV_DIV 평가. 신호·플랜을 info에 주석."""
    import pandas as pd
    from ptrader.config import load_config
    from ptrader import scanner, planner
    from ptrader.signals import evaluate

    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = load_config(cfg_path if cfg_path.exists() else None)
    cfg.timeframe = "1h"
    cfg.signal.disabled_setups = tuple(sorted(
        {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION",
         "REVERSAL", "CONVERGENCE", "OBV_PRD", "VWAP"}))  # OBV_DIV만

    for sym, info in cands:
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}"
                      f"&interval=1h&limit=300", timeout)
            df = pd.DataFrame(
                [[float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                  float(k[5])] for k in kl],
                columns=["open", "high", "low", "close", "volume"],
                index=pd.to_datetime([int(k[0]) for k in kl], unit="ms"))
            feats = scanner.scan(df, cfg)
            sig = evaluate(df, feats, cfg)
            if sig.setup == "OBV_DIV":
                plan = planner.build(df, feats, sig, cfg)
                info["signal"] = sig.direction
                if plan:
                    info["plan"] = plan.as_dict()
            else:
                info["signal"] = "-"
        except Exception as e:
            info["signal"] = f"err:{type(e).__name__}"
        time.sleep(0.1)  # 레이트리밋 여유
    return cands


# ───────────────── 6-way 전조 레이더 (--radar, 기본 on) ─────────────────
# 거래량 폭발 '직전/직후 첫 시간'을 잡는 관측 6종. 자동진입 근거가 아니라
# 관심목록 — 검증된 진입은 여전히 OBV_DIV 뿐이다.
#   ① ign  점화: 직전 1h봉 거래량 Z ≥ 3 (24h 누적보다 몇 시간 빠른 감지)
#   ② fuel 연료: 가격 횡보(20봉 ±3%) + OI 24h +15%↑ (레버리지 축적)
#   ③ sq   압축: BB(20)폭이 최근 백분위 하위 15% + 거래량 고갈 (⚠CONVERGENCE
#              검증에서 대형코인 기각된 서사 — 6-way 중 1표 이상 취급 금지)
#   ④ acc  매집: 가격 횡보 + OBV 20봉 순증이 평상 변화의 8배↑ (조용한 매집)
#   ⑤ fund 펀딩: |펀딩비| ≥ 5bp/8h (포지셔닝 압력)
#   ⑥ flow 수급: 테이커 매수/매도 비율 6h 평균 ≥1.25 or ≤0.8 (공격적 쏠림)
RADAR_TOP_VOL = 40          # 레이더 대상: 거래대금 상위
RADAR_TOP_MOVE = 15         # + 등락률 상위 (거래대금 $10M 이상만)
RADAR_COOLDOWN_H = 6        # 같은 심볼 재알림 쿨다운
RADAR_STATE = Path(__file__).resolve().parent.parent / "radar_state.json"

# ── 백테스트 캘리브레이션 (tools/radar_backtest.py) ──
# 단기 30일(19,031샘플, 기저24.9%) + 장기 10개월(132,884샘플, 기저22.3%):
#   점화  1.74 / 1.57  ← 두 창 모두 강건 → 2표
#   펀딩  2.42 / 1.38  ← 장기에도 양성, 단 강도는 국면 의존 → 2표(월별 재보정)
#   연료  1.12 / 측정불가(OI 30일 한계) → 1표
#   압축  0.53 / 0.68 ❌ · 매집 0.61 / 0.58 ❌ · 수급 0.61 / — ❌ → 표시만
#   조합 검증: 점화+펀딩 동시 = 폭발률 단기 60% · 장기 41.6%(lift 1.87) → 알림 기준
# ⚠️ 연료·수급은 30일 창만 검증됨. 국면 전환 시 radar_backtest.py 재실행.
RADAR_WEIGHTS = {"fund": 2, "ign": 2, "fuel": 1, "sq": 0, "acc": 0, "flow": 0}
RADAR_MAX = sum(RADAR_WEIGHTS.values())     # 만점 5
RADAR_ALERT_SCORE = 4       # 알림 문턱: 점화+펀딩 동시 조합(양 창 최강)부터


def _radar_eval(closes, vols, oi_vals, funding, taker_ratio):
    """6-way 평가 (순수 계산 — selftest 대상). 봉은 완결봉만 넘길 것.

    반환 {"flags": {...}, "score": n, "hint": 방향힌트}"""
    n = len(closes)
    f = {"ign": False, "fuel": False, "sq": False,
         "acc": False, "fund": False, "flow": False}
    flat20 = n >= 21 and abs(closes[-1] / closes[-21] - 1) <= 0.03

    # ① 점화
    if n >= 40:
        f["ign"] = zscore(vols[-1], vols[-101:-1]) >= 3.0
    # ② 연료
    if len(oi_vals) >= 2 and oi_vals[0] > 0:
        f["fuel"] = flat20 and (oi_vals[-1] / oi_vals[0] - 1) >= 0.15
    # ③ 압축: BB폭 백분위 (점화봉 오염 방지 위해 직전봉까지로 고갈 판정)
    if n >= 60:
        bbw = []
        for i in range(40, n):
            w = closes[i - 20:i]
            m = sum(w) / 20
            sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
            bbw.append(sd / m if m else 0.0)
        rank = sum(1 for b in bbw if b <= bbw[-1]) / len(bbw)
        v_prev = vols[:-1]
        dry = (sum(v_prev[-10:]) / 10) <= (sum(v_prev[-50:]) / 50) * 0.85 \
            if len(v_prev) >= 50 else False
        f["sq"] = rank <= 0.15 and dry
    # ④ 매집
    obv_slope = 0.0
    if n >= 40:
        d_obv = [(1 if closes[i] > closes[i - 1] else
                  -1 if closes[i] < closes[i - 1] else 0) * vols[i]
                 for i in range(1, n)]
        slope20 = sum(d_obv[-20:])
        typ = sum(abs(x) for x in d_obv[-100:]) / min(len(d_obv), 100)
        obv_slope = slope20 / typ if typ else 0.0
        f["acc"] = flat20 and obv_slope >= 8.0
    # ⑤ 펀딩
    f["fund"] = funding is not None and abs(funding) >= 0.0005
    # ⑥ 수급
    if taker_ratio is not None:
        f["flow"] = taker_ratio >= 1.25 or taker_ratio <= 0.80

    hint = []
    if taker_ratio is not None and taker_ratio >= 1.25:
        hint.append("매수쏠림")
    if taker_ratio is not None and taker_ratio <= 0.80:
        hint.append("매도쏠림")
    if obv_slope >= 8.0:
        hint.append("매집")
    if funding is not None and abs(funding) >= 0.0005:
        hint.append("롱과열" if funding > 0 else "숏과열")
    return {"flags": f,
            "score": sum(RADAR_WEIGHTS[k] for k, v in f.items() if v),
            "hint": "/".join(hint) or "중립"}


def radar_scan(timeout: float) -> list[tuple[str, dict]]:
    """레이더 유니버스 선정 → 심볼별 6-way 관측 수집·평가. score≥3만 반환."""
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", timeout)
    perp = [t for t in tickers
            if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
            and not is_excluded(t["symbol"])]
    by_vol = sorted(perp, key=lambda t: float(t["quoteVolume"]), reverse=True)
    movers = sorted([t for t in perp if float(t["quoteVolume"]) >= 1e7],
                    key=lambda t: abs(float(t["priceChangePercent"])),
                    reverse=True)
    syms: list[str] = []
    for t in by_vol[:RADAR_TOP_VOL] + movers[:RADAR_TOP_MOVE]:
        if t["symbol"] not in syms:
            syms.append(t["symbol"])

    # 펀딩은 벌크 1콜
    funding_map = {}
    try:
        for r in _get(f"{FAPI}/fapi/v1/premiumIndex", timeout):
            v = r.get("lastFundingRate")
            if v not in (None, ""):
                funding_map[r["symbol"]] = float(v)
    except Exception:
        pass

    out = []
    for sym in syms:
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}"
                      f"&interval=1h&limit=121", timeout)[:-1]  # 완결봉만
            closes = [float(k[4]) for k in kl]
            vols = [float(k[7]) for k in kl]                    # quote 거래대금
            oi = _get(f"{FAPI}/futures/data/openInterestHist?symbol={sym}"
                      f"&period=1h&limit=25", timeout)
            oi_vals = [float(x["sumOpenInterestValue"]) for x in oi]
            tk = _get(f"{FAPI}/futures/data/takerlongshortRatio?symbol={sym}"
                      f"&period=1h&limit=6", timeout)
            ratios = [float(x["buySellRatio"]) for x in tk]
            taker = sum(ratios) / len(ratios) if ratios else None
            r = _radar_eval(closes, vols, oi_vals,
                            funding_map.get(sym), taker)
            # 관측상 3+ 동시충족은 희귀(정상) — 표시는 2부터, 알림은 4부터.
            if r["score"] >= 2:
                out.append((sym, r))
        except Exception:
            continue
        time.sleep(0.05)
    out.sort(key=lambda x: x[1]["score"], reverse=True)
    return out


_FLAG_KR = {"ign": "점화", "fuel": "연료", "sq": "압축",
            "acc": "매집", "fund": "펀딩", "flow": "수급"}

# ── RADAR_BRACKET 진입 (2026-07-29 radar_entry_study.json 검증 통과) ──
# 전조(점화+펀딩, 4/5+) 시점 방향예측 4규칙 전패(PF 0.82~0.98) vs
# 브래킷(방향 무예측, ±0.5ATR 먼저 뚫리는 쪽): n127 · 승률46% · avgR+0.292 ·
# PF 1.52 (전반 1.22/후반 1.90) — 파라미터 무튜닝 통과. 슬리피지 미반영 유의.
BRACKET_ATR_MULT = 0.5      # 트리거 폭 (연구와 동일 — 튜닝 금지)
BRACKET_WINDOW_H = 6        # 트리거 유효시간
# 2026-08-04 exit_grid_study 채택 (복기 트리거: 추세지속6·BE휩쏘5 도달):
# 목표 2.0R→2.5R, 본절 발동 +1.0R→+1.3R. 2.5R×BE1.3 = avgR 0.743·PF 3.23
# (현행 0.552·2.92), 전후반 3.20/3.26 일관·4분할 최저 2.87. 무본절은 전 조합
# 열세(PF 2.21↓) → 본절 규칙 자체는 유지. 브래킷 경로 한정(OBV·일목 원형).
BRACKET_TARGET_R = 2.5
BE_TRIGGER_R = 1.3


def _bracket_hit(b: dict, bars: list[tuple[int, float, float]]):
    """브래킷 판정 (순수 로직 — selftest 대상). bars=[(openTime, high, low)].

    반환 ("LONG"|"SHORT", 트리거가, 발동봉ts) | "ambig" | "expire" | None(유지)."""
    seen = 0
    for ts, hi, lo in bars:
        if ts <= b["ts"]:
            continue
        seen += 1
        hit_up, hit_dn = hi >= b["up"], lo <= b["dn"]
        if hit_up and hit_dn:
            return "ambig"                    # 같은 봉 양쪽 관통 — 모호, 포기
        if hit_up:
            return ("LONG", b["up"], ts)
        if hit_dn:
            return ("SHORT", b["dn"], ts)
        if seen >= BRACKET_WINDOW_H:
            return "expire"
    return None


def _atr_from_klines(kl: list) -> tuple[float, float]:
    """완결봉 리스트 → (ATR14, 마지막 완결 종가)."""
    hs = [float(k[2]) for k in kl]
    ls = [float(k[3]) for k in kl]
    cs = [float(k[4]) for k in kl]
    trs = [max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1]))
           for i in range(len(kl) - 14, len(kl))]
    return sum(trs) / len(trs), cs[-1]


def arm_brackets(led: dict, radar: list, timeout: float) -> list[str]:
    """알림 등급 전조에 브래킷 설치 (이미 보유/대기 중이면 스킵)."""
    if led["equity"] <= led.get("day_start_equity", led["equity"]) * (1 - DAILY_LOSS_STOP):
        return []
    msgs = []
    busy = {p["symbol"] for p in led["open"]} | \
           {b["symbol"] for b in led.get("brackets", [])}
    for sym, r in radar:
        if r["score"] < RADAR_ALERT_SCORE or sym in busy:
            continue
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}"
                      f"&interval=1h&limit=16", timeout)[:-1]
            atr, c = _atr_from_klines(kl)
        except Exception:
            continue
        if atr <= 0:
            continue
        led.setdefault("brackets", []).append({
            "symbol": sym, "ts": int(time.time() * 1000), "atr": atr,
            "up": c + BRACKET_ATR_MULT * atr, "dn": c - BRACKET_ATR_MULT * atr,
            "score": r["score"]})
        busy.add(sym)
        msgs.append(f"📐 브래킷 설치 {sym} (전조 {r['score']}/{RADAR_MAX}) "
                    f"상단 {c + BRACKET_ATR_MULT*atr:g} / "
                    f"하단 {c - BRACKET_ATR_MULT*atr:g} · {BRACKET_WINDOW_H}h 유효")
    return msgs


def check_brackets(led: dict, timeout: float) -> list[str]:
    """대기 브래킷 판정 → 발동 시 포지션 개설 (연구와 동일 파라미터)."""
    msgs, keep = [], []
    for b in led.get("brackets", []):
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={b['symbol']}"
                      f"&interval=1h&startTime={b['ts']}&limit=10",
                      timeout)[:-1]
            bars = [(int(k[0]), float(k[2]), float(k[3])) for k in kl]
        except Exception:
            keep.append(b)
            continue
        hit = _bracket_hit(b, bars)
        if hit is None:
            keep.append(b)
            continue
        if hit in ("ambig", "expire"):
            msgs.append(f"📐 브래킷 해제 {b['symbol']} "
                        f"({'모호(양방향 관통)' if hit == 'ambig' else '6h 만료'})")
            continue
        direction, entry, bar_ts = hit
        if len(led["open"]) >= MAX_POSITIONS:
            msgs.append(f"📐 {b['symbol']} 발동했으나 슬롯 만석 — 미진입")
            continue
        if is_equity_token(b["symbol"]) and _equity_cluster_full(led):
            msgs.append(f"⛔ 📐 {b['symbol']} 발동했으나 주식토큰 상한"
                        f"({EQUITY_CLUSTER_CAP}) — 미진입 (쏠림 방지)")
            continue
        atr = b["atr"]
        qty = (led["equity"] * RISK_PER_TRADE) / atr
        qty = min(qty, led["equity"] * MAX_LEVERAGE / entry)
        if qty * entry < 5:
            continue
        sgn = 1 if direction == "LONG" else -1
        tgt = entry + sgn * BRACKET_TARGET_R * atr
        led["open"].append({
            "symbol": b["symbol"], "direction": direction, "entry": entry,
            "stop": entry - sgn * atr, "target": tgt,
            "qty": qty, "risk0": atr, "entry_ts": bar_ts,
            "setup": "RADAR_BRACKET",
            "opened": time.strftime("%Y-%m-%d %H:%M"), "bars_held": 0})
        msgs.append(f"🎯 진입 {b['symbol']} {direction} [브래킷] "
                    f"${qty*entry:,.0f} (레버 {qty*entry/led['equity']:.2f}x) · "
                    f"진입 {entry:g} 손절 {entry - sgn*atr:g} "
                    f"목표 {tgt:g}")
        lm = _live("open_trade", b["symbol"], direction, qty,
                   entry - sgn * atr, tgt)
        if lm:
            msgs.append("  " + lm)
    led["brackets"] = keep
    return msgs


# ── ICHIMOKU_4H 셋업 (2026-07-31 검증 채택: ichimoku_study/confirm) ──
# 4h 롱 온리: 전환>기준 교차 ∧ 구름 위 종가 ∧ 후행스팬↑ (표준 9/26/52 무튜닝).
# 원형 PF 1.71(n196, 전후반 1.64/1.80) · +2ATR 재난손절 공존 확인(PF 1.71 유지,
# 4분할 3/4 흑자). 청산 = 기준선 하회 or 구름 재진입(지표) + 2ATR 손절 + 96봉(4h).
# 3ATR(PF 1.83)이 아닌 2ATR 채택 = 성적 아닌 리스크 근거(튜닝 회피).
ICHI_UNIVERSE_N = 30        # 검증과 동일: 거래대금 상위 30 (Z스크리너와 별개)
ICHI_STOP_ATR = 2.0
ICHI_HOLD_1H = 384          # 96 × 4h봉 = 16일 (1h 단위)


def ichimoku_cycle(led: dict, timeout: float) -> list[str]:
    """4h봉 마감마다 1회: 보유 일목 포지션 지표 청산 + 신규 롱 스캔."""
    from ichimoku_study import ichimoku          # 지연 임포트(순환 방지)
    from radar_entry_study import _atr14
    msgs: list[str] = []
    bucket = int(time.time() * 1000) // (4 * 3600_000)
    if led.get("ichi_bucket") == bucket:
        return msgs
    led["ichi_bucket"] = bucket

    def _ichi_state(sym):
        kl = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}"
                  f"&interval=4h&limit=140", timeout)[:-1]
        if len(kl) < 130:
            return None
        h = [float(k[2]) for k in kl]
        l = [float(k[3]) for k in kl]
        c = [float(k[4]) for k in kl]
        return h, l, c

    # (1) 보유 일목 포지션 — 지표 청산 판정
    for pos in list(led["open"]):
        if pos.get("setup") != "ICHIMOKU_4H":
            continue
        try:
            st = _ichi_state(pos["symbol"])
        except Exception:
            continue
        if st is None:
            continue
        h, l, c = st
        tk, kj, sa, sb = ichimoku(h, l, len(c) - 1)
        if sa is None:
            continue
        if c[-1] < kj or c[-1] < max(sa, sb):
            exit_p = c[-1]
            gross = (exit_p - pos["entry"]) * pos["qty"]
            fees = (pos["entry"] + exit_p) * pos["qty"] * TAKER_FEE
            pnl = gross - fees
            led["equity"] += pnl
            risk = pos.get("risk0", 1e-9) * pos["qty"]
            pos.update(exit=exit_p, reason="ICHI",
                       pnl=round(pnl, 2), r=round(pnl / risk, 2) if risk else 0)
            led["open"].remove(pos)
            led["closed"].append(pos)
            emo = "🟢" if pnl > 0 else "🔴"
            msgs.append(f"{emo} 청산 {pos['symbol']} LONG [일목지표] "
                        f"net ${pnl:+.2f} ({pos['r']:+.2f}R) "
                        f"→ 자본 ${led['equity']:.2f}")
            lm = _live("close_trade", pos["symbol"])
            if lm:
                msgs.append("  " + lm)

    # (2) 신규 스캔 — 공용 게이트(일손실·슬롯·클러스터) 준수
    if led["equity"] <= led.get("day_start_equity", led["equity"]) * (1 - DAILY_LOSS_STOP):
        return msgs
    try:
        tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", timeout)
        perp = sorted([t for t in tickers
                       if t["symbol"].endswith("USDT")
                       and "_" not in t["symbol"]
                       and not is_excluded(t["symbol"])],
                      key=lambda t: float(t["quoteVolume"]),
                      reverse=True)[:ICHI_UNIVERSE_N]
    except Exception:
        return msgs
    open_syms = {p["symbol"] for p in led["open"]}
    for t in perp:
        sym = t["symbol"]
        if len(led["open"]) >= MAX_POSITIONS:
            break
        if sym in open_syms:
            continue
        if is_equity_token(sym) and _equity_cluster_full(led):
            continue
        try:
            st = _ichi_state(sym)
        except Exception:
            continue
        if st is None:
            continue
        h, l, c = st
        i = len(c) - 1
        tk, kj, sa, sb = ichimoku(h, l, i)
        if sa is None:
            continue
        tk_p, kj_p, _, _ = ichimoku(h, l, i - 1)
        cross = tk > kj and tk_p <= kj_p
        if not (cross and c[i] > max(sa, sb) and c[i] > c[i - 26]):
            continue
        atr = _atr14(h, l, c, i)
        if atr <= 0:
            continue
        entry = c[i]
        stop = entry - ICHI_STOP_ATR * atr
        qty = (led["equity"] * RISK_PER_TRADE) / (ICHI_STOP_ATR * atr)
        qty = min(qty, led["equity"] * MAX_LEVERAGE / entry)
        if qty * entry < 5:
            continue
        led["open"].append({
            "symbol": sym, "direction": "LONG", "entry": entry, "stop": stop,
            "target": None, "qty": qty, "risk0": ICHI_STOP_ATR * atr,
            "entry_ts": int(time.time() * 1000), "setup": "ICHIMOKU_4H",
            "hold_max": ICHI_HOLD_1H,
            "opened": time.strftime("%Y-%m-%d %H:%M"), "bars_held": 0})
        open_syms.add(sym)
        msgs.append(f"🎯 진입 {sym} LONG [일목4h] ${qty*entry:,.0f} "
                    f"(레버 {qty*entry/led['equity']:.2f}x) · 진입 {entry:g} "
                    f"손절 {stop:g} · 청산=지표(기준선/구름)+2ATR")
        lm = _live("open_trade", sym, "LONG", qty, stop, None)
        if lm:
            msgs.append("  " + lm)
    return msgs


# ── 청산 복기(post-mortem): 청산 24h 후 가격으로 '왜'를 분류·축적 ──
# 목적: 같은 실수의 반복 감지. 단 복기는 '관찰'이며, 규칙 변경은 반드시
# 연구 하니스(exit_study 등) 검증을 거친다 — 복기→즉흥 튜닝 금지.
REVIEW_WAIT_H = 24
REVIEW_BARS = 24


def _verdict(t: dict, highs: list[float], lows: list[float]) -> str:
    """청산 후 24h 고저로 청산의 성격 분류 (순수 로직 — selftest 대상).

    STOP/BE: 휩쏘(원방향 복귀) / 정당(1R+ 추가 역행) / 중립
    TARGET : 추세지속(1R+ 연장=이익 일부 놓침) / 완벽(직후 반전) / 중립
    TIME   : 조기(청산 후 원방향 1R+) / 적기(청산 후 역행 1R+) / 중립"""
    long = t["direction"] == "LONG"
    sgn = 1 if long else -1
    r0 = t.get("risk0") or abs(t["entry"] - t.get("stop", t["entry"])) or 1e-9
    if not highs:
        return "미확정"
    fav = max(highs) if long else min(lows)     # 원방향 극값
    adv = min(lows) if long else max(highs)     # 역방향 극값
    reason = t["reason"]
    if reason in ("STOP", "BE"):
        if (fav - t["entry"]) * sgn >= 0:
            return "휩쏘(청산 후 원방향 복귀)"
        if (t["exit"] - adv) * sgn >= r0:
            return "정당(청산 후 1R+ 추가 역행)"
        return "중립(청산가 부근 횡보)"
    if reason == "TARGET":
        if (fav - t["exit"]) * sgn >= r0:
            return "추세지속(익절 후 1R+ 연장)"
        if (t["exit"] - adv) * sgn >= r0:
            return "완벽(익절 직후 반전)"
        return "중립"
    if (fav - t["exit"]) * sgn >= r0:
        return "조기(시간청산 후 원방향 1R+)"
    if (t["exit"] - adv) * sgn >= r0:
        return "적기(시간청산 후 역행)"
    return "중립(계속 횡보)"


def review_exits(led: dict, timeout: float) -> list[str]:
    """복기 대기(청산 24h 경과) 트레이드를 분류하고 원장에 기록."""
    msgs = []
    now = int(time.time() * 1000)
    for t in led["closed"]:
        if t.get("review"):
            continue
        exit_ts = min(t.get("exit_ts")
                      or (t["entry_ts"] + t.get("bars_held", 0) * 3600_000), now)
        if now - exit_ts < REVIEW_WAIT_H * 3600_000:
            continue
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={t['symbol']}"
                      f"&interval=1h&startTime={exit_ts}"
                      f"&limit={REVIEW_BARS + 1}", timeout)[:REVIEW_BARS]
            highs = [float(k[2]) for k in kl]
            lows = [float(k[3]) for k in kl]
        except Exception:
            continue
        t["review"] = _verdict(t, highs, lows)
        emo = "🟢" if t["pnl"] > 0 else "🔴"
        msgs.append(f"📚 복기 {t['symbol']} [{t['reason']}] {emo}"
                    f"${t['pnl']:+.2f} → {t['review']}")
    return msgs


def review_summary(closed: list) -> str:
    """복기 누적 통계 한 줄 (대시보드·리포트용)."""
    revs = [t["review"] for t in closed if t.get("review")]
    if not revs:
        return "복기 데이터 누적 중"
    def n(key):
        return sum(1 for v in revs if v.startswith(key))
    return (f"복기 {len(revs)}건 — 손절중 휩쏘 {n('휩쏘')}·정당 {n('정당')} / "
            f"익절중 추세지속 {n('추세지속')}·완벽 {n('완벽')} / "
            f"시간청산 조기 {n('조기')}·적기 {n('적기')}")


# ── 전조 추적: 알림 등급(4/5+) 전조가 뜬 코인의 24h 결과를 기록 (자기검증) ──
RADAR_TRACK = Path(__file__).resolve().parent.parent / "radar_track.json"
TRACK_HOURS = 24            # 백테스트 라벨과 동일 창
TRACK_BOOM = 0.08           # 폭발 판정 ±8%


def _resolve_event(e: dict, highs: list[float], lows: list[float],
                   now_ms: int) -> str | None:
    """추적 이벤트 갱신·판정 (순수 로직 — selftest 대상).

    완결봉 고저로 최대 상방/하방을 갱신하고, 24h 경과 시 폭발 여부 확정.
    반환: 판정 메시지(확정 시) 또는 None."""
    if highs:
        e["up"] = round(max(highs) / e["price0"] - 1, 4)
        e["dn"] = round(1 - min(lows) / e["price0"], 4)
    if now_ms - e["ts"] < TRACK_HOURS * 3600_000:
        return None
    mx = max(e.get("up", 0.0), e.get("dn", 0.0))
    side = "상방" if e.get("up", 0.0) >= e.get("dn", 0.0) else "하방"
    e["status"] = "resolved"
    e["boom"] = mx >= TRACK_BOOM
    e["outcome"] = f"{side} {mx*100:.1f}%"
    return (f"🔭 전조 결과 {e['symbol']}: "
            f"{'✅ 폭발 적중' if e['boom'] else '➖ 무산'} "
            f"({side} 최대 {mx*100:.1f}%/24h)")


def load_track() -> dict:
    try:
        return json.loads(RADAR_TRACK.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"events": []}


def track_precursors(radar: list, timeout: float) -> tuple[dict, list[str]]:
    """알림 등급 전조 → 추적 등록 + 진행 갱신 + 24h 판정. (state, 메시지)"""
    st = load_track()
    msgs: list[str] = []
    now = int(time.time() * 1000)
    tracking = {e["symbol"] for e in st["events"] if e["status"] == "tracking"}

    # 신규 등록 (이미 추적 중이면 중복 등록 안 함)
    for sym, r in radar:
        if r["score"] < RADAR_ALERT_SCORE or sym in tracking:
            continue
        try:
            px = float(_get(f"{FAPI}/fapi/v1/ticker/price?symbol={sym}",
                            timeout)["price"])
        except Exception:
            continue
        st["events"].append({
            "symbol": sym, "ts": now, "score": r["score"],
            "flags": [k for k, v in r["flags"].items() if v],
            "hint": r["hint"], "price0": px,
            "status": "tracking", "up": 0.0, "dn": 0.0})
        tracking.add(sym)
        msgs.append(f"🔭 추적 시작 {sym} (전조 {r['score']}/{RADAR_MAX}, "
                    f"기준가 {px:g}) — 24h 결과 판정 예정")

    # 진행 갱신 + 판정
    for e in st["events"]:
        if e["status"] != "tracking":
            continue
        try:
            kl = _get(f"{FAPI}/fapi/v1/klines?symbol={e['symbol']}"
                      f"&interval=1h&startTime={e['ts']}&limit=30",
                      timeout)[:-1]          # 완결봉만
        except Exception:
            continue
        m = _resolve_event(e, [float(k[2]) for k in kl],
                           [float(k[3]) for k in kl], now)
        if m:
            msgs.append(m)
    RADAR_TRACK.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return st, msgs


def radar_alerts(radar: list, t0: str) -> list[str]:
    """score≥문턱 + 쿨다운 통과분만 알림 문자열 생성 (상태파일 갱신)."""
    try:
        st = json.loads(RADAR_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    now = time.time()
    msgs = []
    for sym, r in radar:
        if r["score"] < RADAR_ALERT_SCORE:
            continue
        if now - st.get(sym, 0) < RADAR_COOLDOWN_H * 3600:
            continue
        st[sym] = now
        got = "+".join(_FLAG_KR[k] for k, v in r["flags"].items() if v)
        msgs.append(f"🔭 전조 {r['score']}/{RADAR_MAX} {sym} [{got}] "
                    f"{r['hint']} — 관심(자동진입 아님)")
    RADAR_STATE.write_text(json.dumps(st), encoding="utf-8")
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50, help="거래대금 상위 N (기본 50)")
    ap.add_argument("--min-z", type=float, default=2.0,
                    help="거래량 Z-Score 최소 (기본 2.0, PRD)")
    ap.add_argument("--no-rs", action="store_true", help="RS>BTC 필터 끔")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--notify", action="store_true",
                    help="OBV_DIV 신호 발생 시 텔레그램 알림 (무신호면 침묵)")
    ap.add_argument("--trade", action="store_true",
                    help="자동매매: 신호 자동 진입 + SL/TP/시간 자동 청산 ($500 원장)")
    ap.add_argument("--no-radar", action="store_true",
                    help="6-way 전조 레이더 끔")
    ap.add_argument("--live", action="store_true",
                    help="실주문 미러링 (바이낸스 — 기본 테스트넷, "
                         "실서버는 DANTA_LIVE_CONFIRMED=YES + 키 필요)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.live:
        global LIVE_EXEC
        LIVE_EXEC = True
        import binance_live
        print(f"⚡ 실주문 모드: {binance_live.env_label()} · "
              f"주문 상한 ${binance_live.max_notional():,.0f}")

    if args.selftest:
        assert abs(zscore(30, [10] * 19 + [10]) - 0.0) == 0  # σ=0 → 0
        hist = [10, 12, 8, 11, 9] * 4
        assert zscore(30, hist) > 5          # 급증은 큰 z
        assert zscore(10, hist) < 1          # 평범은 작은 z
        # 정산 로직: LONG 손절/목표/시간, 같은 봉 동시터치=손절 우선
        pos = {"direction": "LONG", "entry": 100.0, "stop": 95.0,
               "target": 110.0, "qty": 1.0, "entry_ts": 0, "bars_held": 0}
        assert _walk_bars(dict(pos), [(1, 101, 94, 96)])[1] == "STOP"
        assert _walk_bars(dict(pos), [(1, 111, 99, 108)])[1] == "TARGET"
        assert _walk_bars(dict(pos), [(1, 111, 94, 96)])[1] == "STOP"   # 동시→보수
        p2 = dict(pos)
        assert _walk_bars(p2, [(1, 101, 99, 100)]) is None and p2["bars_held"] == 1
        # 재정산해도 같은 봉은 다시 세지 않는다 (이중 누적 버그 회귀 방지)
        assert _walk_bars(p2, [(1, 101, 99, 100), (2, 101, 99, 100)]) is None \
            and p2["bars_held"] == 2
        long_bars = [(i, 101, 99, 100) for i in range(1, HOLD_MAX_BARS + 2)]
        assert _walk_bars(dict(pos), long_bars)[1] == "TIME"
        s = {"direction": "SHORT", "entry": 100.0, "stop": 105.0,
             "target": 90.0, "qty": 1.0, "entry_ts": 0, "bars_held": 0}
        assert _walk_bars(dict(s), [(1, 106, 99, 100)])[1] == "STOP"
        assert _walk_bars(dict(s), [(1, 101, 89, 95)])[1] == "TARGET"
        # 6-way 전조 레이더 순수 로직: 점화+연료+압축+펀딩+수급 = 5/6 시나리오
        closes = [100 + (2 if i % 2 else -2) for i in range(80)] + [100.0] * 20
        vols = [50.0] * 70 + [30.0] * 29 + [400.0]   # 최근 고갈 후 말봉 점화
        oi = [100.0] * 5 + [130.0]                    # +30% 연료
        r = _radar_eval(closes, vols, oi, funding=0.001, taker_ratio=1.4)
        fl = r["flags"]
        assert fl["ign"] and fl["fuel"] and fl["sq"] and fl["fund"] and fl["flow"], fl
        # 가중점수: fund2+ign2+fuel1=5 (sq·flow는 역예측이라 0표)
        assert r["score"] == 5 and "매수쏠림" in r["hint"]
        # 매집(acc): 관측은 되나 투표권 없음(lift 0.61)
        c2 = [100.0] + [100 + i * 0.02 for i in range(1, 100)]
        v2 = [50.0] * 100
        r2 = _radar_eval(c2, v2, [], funding=None, taker_ratio=None)
        assert r2["flags"]["acc"] and r2["score"] == 0, r2
        # 전조 추적 판정: 진행중 갱신 / 24h 후 폭발·무산 확정
        ev = {"symbol": "T", "ts": 0, "price0": 100.0, "status": "tracking",
              "up": 0.0, "dn": 0.0, "score": 4}
        assert _resolve_event(dict(ev), [105.0], [98.0], 3600_000) is None  # 1h: 미판정
        e1 = dict(ev)
        m1 = _resolve_event(e1, [109.0], [97.0], 25 * 3600_000)
        assert m1 and "폭발 적중" in m1 and e1["boom"] and e1["status"] == "resolved"
        e2 = dict(ev)
        m2 = _resolve_event(e2, [103.0], [98.0], 25 * 3600_000)
        assert m2 and "무산" in m2 and not e2["boom"]
        e3 = dict(ev)   # 하방 폭발도 적중
        m3 = _resolve_event(e3, [101.0], [90.0], 25 * 3600_000)
        assert m3 and e3["boom"] and "하방" in e3["outcome"]
        # 청산 복기 분류: 휩쏘/정당/추세지속/조기
        tw = {"direction": "LONG", "entry": 100.0, "stop": 95.0, "exit": 95.0,
              "reason": "STOP", "risk0": 5.0}
        assert _verdict(dict(tw), [101.0], [94.0]).startswith("휩쏘")
        assert _verdict(dict(tw), [96.0], [88.0]).startswith("정당")
        ts_ = {"direction": "SHORT", "entry": 100.0, "stop": 105.0,
               "exit": 90.0, "reason": "TARGET", "risk0": 5.0}
        assert _verdict(dict(ts_), [92.0], [84.0]).startswith("추세지속")
        tt = {"direction": "LONG", "entry": 100.0, "stop": 95.0, "exit": 98.0,
              "reason": "TIME", "risk0": 5.0}
        assert _verdict(dict(tt), [104.0], [97.0]).startswith("조기")
        # 주식토큰 클러스터 상한: 분류·판정
        assert is_equity_token("SKHYNIXUSDT") and is_equity_token("NVDAUSDT")
        assert not is_equity_token("BTCUSDT") and not is_equity_token("ACHUSDT")
        _led = {"open": [{"symbol": "KORUUSDT"}, {"symbol": "EWYUSDT"}]}
        assert _equity_cluster_full(_led)                      # 2개 = 상한 도달
        _led2 = {"open": [{"symbol": "KORUUSDT"}, {"symbol": "BTCUSDT"}]}
        assert not _equity_cluster_full(_led2)                 # 주식토큰은 1개뿐
        # +1R 본절 이동 (브래킷 한정): 도달→본절 청산 / OBV는 원형 유지
        bp = {"symbol": "T", "direction": "LONG", "entry": 100.0, "stop": 95.0,
              "target": 110.0, "entry_ts": 0, "bars_held": 0, "risk0": 5.0,
              "setup": "RADAR_BRACKET"}
        p5 = dict(bp)
        assert _walk_bars(p5, [(1, 106, 99, 105)]) is None      # +1.2R: 미발동
        assert not p5.get("be_armed")
        assert _walk_bars(p5, [(2, 107, 99, 105)]) is None      # +1.3R 도달
        assert p5["be_armed"] and p5["stop"] == 100.0 and p5["_be_new"]
        assert _walk_bars(p5, [(3, 103, 99.5, 100.5)])[1] == "BE"  # 본절 청산
        p6 = dict(bp)
        assert _walk_bars(p6, [(1, 106, 94, 96)])[1] == "STOP"  # 동시봉=손절우선
        p7 = dict(bp); p7.pop("setup")                          # OBV: 미적용
        assert _walk_bars(p7, [(1, 106, 99, 105)]) is None and not p7.get("be_armed")
        # 브래킷 판정: 상방/하방/모호/만료/유지
        bb = {"ts": 0, "up": 105.0, "dn": 95.0}
        assert _bracket_hit(dict(bb), [(1, 106, 100)]) == ("LONG", 105.0, 1)
        assert _bracket_hit(dict(bb), [(1, 101, 94)]) == ("SHORT", 95.0, 1)
        assert _bracket_hit(dict(bb), [(1, 106, 94)]) == "ambig"
        assert _bracket_hit(dict(bb), [(i, 101, 99) for i in range(1, 7)]) == "expire"
        assert _bracket_hit(dict(bb), [(1, 101, 99)]) is None
        print("selftest OK (zscore+settlement+radar[가중]+추적판정+브래킷)")
        return 0

    t0 = time.strftime("%Y-%m-%d %H:%M")
    print(f"═══ 매일 단타 스캔 {t0} ═══")

    trade_msgs: list[str] = []
    led = None
    if args.trade:
        led = _load_ledger()
        trade_msgs += settle_positions(led, args.timeout)   # 청산은 신호와 무관하게 매회
        trade_msgs += check_brackets(led, args.timeout)     # 대기 브래킷 발동 판정
        trade_msgs += review_exits(led, args.timeout)       # 청산 24h 후 복기
        trade_msgs += ichimoku_cycle(led, args.timeout)     # 일목 4h (마감시 1회)

    cands, btc_chg = screen_universe(args.top, args.min_z, not args.no_rs,
                                     args.timeout)
    if not cands:
        print("\n오늘은 거래량 급증(Z≥%.1f) 코인이 없습니다. "
              "--min-z 1.0 으로 완화해 관심 후보를 볼 수 있습니다." % args.min_z)
        cands = []
    # 관심추적(watchlist.json): 스크리닝 통과 여부와 무관하게 매 사이클 신호 평가.
    # 전조 레이더 고득점 심볼을 사용자가 지켜보는 용도 — 신호 뜨면 알림+자동진입.
    wl_path = Path(__file__).resolve().parent.parent / "watchlist.json"
    if wl_path.exists():
        try:
            wl = json.loads(wl_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            wl = []
        have = {s for s, _ in cands}
        for wsym in wl:
            if wsym in have:
                continue
            try:
                t = _get(f"{FAPI}/fapi/v1/ticker/24hr?symbol={wsym}",
                         args.timeout)
                cands.append((wsym, {
                    "vol_usd": float(t["quoteVolume"]), "z": 0.0,
                    "chg": float(t["priceChangePercent"]),
                    "rs": False, "watch": True}))
            except Exception:
                print(f"  ⚠️ 관심추적 {wsym} 티커 실패 — 이번 사이클 제외")
        if wl:
            print(f"👁 관심추적: {', '.join(wl)}")

    if cands:
        cands = evaluate_signals(cands, args.timeout)

    if cands:
        print(f"\n{'심볼':<14}{'거래대금':>10}{'Z':>6}{'24h%':>8}{'RS':>4}  신호")
        print("-" * 56)
    for sym, i in cands:
        vol_m = f"${i['vol_usd']/1e6:,.0f}M"
        sig = i.get("signal", "-")
        mark = "🟢" + sig if sig in ("LONG", "SHORT") else sig
        print(f"{sym:<14}{vol_m:>10}{i['z']:>6.1f}{i['chg']:>+8.1f}"
              f"{'✓' if i['rs'] else '✗':>4}  {mark}")
        if "plan" in i:
            p = i["plan"]
            print(f"    └ 진입 {p['entry']} / 손절 {p['stop']} / "
                  f"목표 {p['target']} (R:R {p['rr']})")

    n_sig = sum(1 for _, i in cands if i.get("signal") in ("LONG", "SHORT"))
    print(f"\n선정 {len(cands)}개 중 OBV_DIV 신호 {n_sig}개")
    if n_sig == 0:
        print("신호 없는 날은 쉬는 게 규칙입니다 — 후보만 관심목록으로.")

    # 자동매매: 진입 시도 + 원장 저장 + 요약
    if args.trade and led is not None:
        trade_msgs += enter_positions(led, cands, t0)
        _save_ledger(led)
        ret = led["equity"] / led["start_equity"] - 1
        print(f"\n💰 원장: 자본 ${led['equity']:.2f} ({ret*100:+.2f}%) · "
              f"보유 {len(led['open'])} · 청산누적 {len(led['closed'])}건")
        for m in trade_msgs:
            print("  " + m)

    # 6-way 전조 레이더 (관심목록 — 자동진입과 무관)
    radar: list = []
    radar_msgs: list[str] = []
    if not args.no_radar:
        try:
            radar = radar_scan(args.timeout)
        except Exception as e:
            print(f"⚠️ 레이더 실패(스캔은 계속): {type(e).__name__}")
        if radar:
            print(f"\n🔭 전조 레이더 (6-way 관측·가중점수 만점{RADAR_MAX}, ≥2): "
                  f"{len(radar)}종")
            for sym, r in radar[:10]:
                got = "+".join(_FLAG_KR[k] for k, v in r["flags"].items() if v)
                print(f"  {r['score']}/{RADAR_MAX} {sym:<14} [{got}] {r['hint']}")
        if args.notify:   # 쿨다운은 실제 발송될 때만 소모
            radar_msgs = radar_alerts(radar, t0)
        if args.trade and led is not None:   # 검증 통과 브래킷 진입 설치
            bmsgs = arm_brackets(led, radar, args.timeout)
            _save_ledger(led)
            trade_msgs += bmsgs
            for m in bmsgs:
                print("  " + m)
        track_st, track_msgs = track_precursors(radar, args.timeout)
        radar_msgs += track_msgs
        for m in track_msgs:
            print("  " + m)

    # 텔레그램: 체결/청산(trade_msgs)·신호·강한 전조(4/6+) 시에만
    if args.notify and (trade_msgs or radar_msgs or n_sig > 0):
        lines = [f"⚡ [선물단타] {t0}"]
        lines += trade_msgs
        lines += radar_msgs
        if not args.trade and n_sig > 0:
            for sym, i in cands:
                if i.get("signal") not in ("LONG", "SHORT"):
                    continue
                lines.append(f"{sym} {i['signal']} (Z{i['z']:.1f}, {i['chg']:+.1f}%)")
                if "plan" in i:
                    p = i["plan"]
                    lines.append(f"  진입 {p['entry']} 손절 {p['stop']} "
                                 f"목표 {p['target']} R:R {p['rr']}")
        if led is not None:
            ret = led["equity"] / led["start_equity"] - 1
            lines.append(f"💰 자본 ${led['equity']:.2f} ({ret*100:+.2f}%) · "
                         f"보유 {len(led['open'])}")
        ok = notify("\n".join(lines))
        print(f"📲 텔레그램: {'전송됨' if ok else 'skip(자격증명/네트워크)'}")

    _led = led if led is not None else _load_ledger()
    render_dashboard(_led, cands, t0, radar=radar, track=load_track(),
                     charts=_charts_payload(_led, args.timeout))

    out = {"time": t0, "btc_chg": btc_chg,
           "candidates": [{"symbol": s, **i} for s, i in cands]}
    out_p = Path(__file__).resolve().parent.parent / "scan_result.json"
    out_p.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    print(f"[저장] {out_p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
