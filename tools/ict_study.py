#!/usr/bin/env python
"""ICT/SMC(오더블록+FVG) 기계화 검증 (2026-08-06, 사용자 요청).

규칙(사전 고정 — 튜닝 금지):
  변위(BOS): 종가가 직전 20봉 최고가 돌파 + 몸통 ≥ 1.0×ATR14 (숏은 대칭)
  OB: 변위봉 직전 3봉 내 마지막 반대색 캔들. 존 = [OB저가, OB고가]
  진입: 변위 후 24봉 내 존 상단 터치(되돌림) → 지정가 체결 가정
  손절: OB 반대끝 (최소 0.3ATR 보장) / 목표: 2R / 보유 48봉 / 수수료 반영
  FVG 변형: 변위봉에 3봉 갭(low[i] > high[i-2]) 동반일 때만
판정: avgR>0 & PF>1.1 & 전후반 모두 PF>1.0 일 때만 채택 논의.
사용: python tools/ict_study.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from daily_scan import FAPI, _get                      # noqa: E402
from radar_backtest import fetch_series                # noqa: E402
from radar_entry_study import _stats, FEE, HOLD        # noqa: E402

N_SYMBOLS = 30
SWING = 20          # 구조 돌파 기준 봉수
OB_LOOKBACK = 3     # 변위봉 직전 OB 탐색 범위
RETRACE_W = 24      # 되돌림 대기 봉수
RR = 2.0
MIN_STOP_ATR = 0.3


def _atr14(h, l, c, i):
    trs = [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1]))
           for k in range(max(1, i - 13), i + 1)]
    return sum(trs) / len(trs) if trs else 0.0


def find_setups(s):
    """(변위 인덱스, long?, 존상단, 존하단, fvg 동반?) 목록."""
    c = s["closes"]
    o = [c[0]] + c[:-1]          # 시가 ≈ 직전 종가 (연속 시장)
    h, l = s["highs"], s["lows"]
    n = len(c)
    out = []
    for i in range(SWING + 14, n - 1):
        atr = _atr14(h, l, c, i)
        if atr <= 0:
            continue
        body = c[i] - o[i]
        if c[i] > max(h[i - SWING:i]) and body >= atr:          # 상방 변위
            long = True
        elif c[i] < min(l[i - SWING:i]) and -body >= atr:       # 하방 변위
            long = False
        else:
            continue
        ob = None
        for k in range(i - 1, i - 1 - OB_LOOKBACK, -1):
            if k < 0:
                break
            if long and c[k] < o[k]:
                ob = k; break
            if not long and c[k] > o[k]:
                ob = k; break
        if ob is None:
            continue
        fvg = (l[i] > h[i - 2]) if long else (h[i] < l[i - 2])
        out.append((i, long, h[ob], l[ob], fvg, atr))
    return out


def sim(s, i, long, zone_hi, zone_lo, atr):
    """되돌림 진입 → 2R/손절/48봉. R 정규화 = 손절거리."""
    h, l, c = s["highs"], s["lows"], s["closes"]
    n = len(c)
    entry_px = zone_hi if long else zone_lo
    stop = zone_lo if long else zone_hi
    dist = abs(entry_px - stop)
    if dist < MIN_STOP_ATR * atr:
        dist = MIN_STOP_ATR * atr
        stop = entry_px - dist if long else entry_px + dist
    # 되돌림 체결 탐색
    ent = None
    for k in range(i + 1, min(i + 1 + RETRACE_W, n)):
        if long and l[k] <= entry_px:
            ent = k; break
        if not long and h[k] >= entry_px:
            ent = k; break
    if ent is None:
        return None
    # 체결봉에서 손절까지 같이 뚫었으면 보수적으로 손절 처리
    if (long and l[ent] <= stop) or (not long and h[ent] >= stop):
        return -1.0 - (entry_px + stop) * FEE / dist
    sgn = 1 if long else -1
    target = entry_px + sgn * RR * dist
    k2 = ent
    exit_p = None
    for k2 in range(ent + 1, min(ent + 1 + HOLD, n)):
        if long:
            if l[k2] <= stop:
                exit_p = stop; break
            if h[k2] >= target:
                exit_p = target; break
        else:
            if h[k2] >= stop:
                exit_p = stop; break
            if l[k2] <= target:
                exit_p = target; break
    if exit_p is None:
        exit_p = c[min(k2, n - 1)]
    return (exit_p - entry_px) * sgn / dist - (entry_px + exit_p) * FEE / dist


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ ICT/SMC 오더블록 검증: {len(syms)}심볼 × ~10개월 · "
          f"변위(20봉+1ATR)→OB 되돌림→2R\n")

    rows = {"LONG": [], "SHORT": [], "BOTH": [], "BOTH+FVG": []}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        for i, long, zh, zl, fvg, atr in find_setups(s):
            r = sim(s, i, long, zh, zl, atr)
            if r is None:
                continue
            row = (s["ts"][i], r)
            rows["BOTH"].append(row)
            rows["LONG" if long else "SHORT"].append(row)
            if fvg:
                rows["BOTH+FVG"].append(row)
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)

    print()
    print(f"\n{'변형':<10}{'n':>6}{'승률':>6}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}  판정")
    report = {}
    for name, tr in rows.items():
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        if len(vals) < 30:
            print(f"{name:<10}{len(vals):>6}  표본 부족")
            continue
        half = len(vals) // 2
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        ok = (st["avgR"] > 0 and st["pf"] > 1.1
              and a.get("pf", 0) > 1.0 and b.get("pf", 0) > 1.0)
        report[name] = {"all": st, "first": a, "second": b, "pass": ok}
        print(f"{name:<10}{st['n']:>6}{st['wr']*100:>5.0f}%{st['avgR']:>8.3f}"
              f"{st['pf']:>7.2f}{a.get('pf', 0):>8.2f}{b.get('pf', 0):>8.2f}"
              f"  {'✅ 통과' if ok else '❌ 미달'}")

    Path("ict_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] ict_study.json")
    print("게이트: avgR>0 & PF>1.1 & 전후반 모두 PF>1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
