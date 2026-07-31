#!/usr/bin/env python
"""손절폭 스윕 — 복기 "손절 6/6 휩쏘" 패턴의 정면 검증.

가설: 1ATR 손절이 노이즈 폭보다 좁아 흔들림에 털린다. 넓히면(1.5/2ATR)
털림이 줄어 성과가 개선될 것이다.

조건 통제(공정 비교):
  - R:R 2 고정: 목표 = 2 × 손절거리 (손절 넓히면 목표도 비례 확대)
  - R 정규화: 손절거리 기준 (리스크 사이징 동일 가정 — 넓은 손절 = 작은 수량)
  - 브래킷 진입(주 손실원) 고정, 48봉, 수수료 반영, 전/후반 + 4분할
판정: 1.0ATR(현행) 대비 전후반 일관 개선일 때만 채택. 한쪽만 좋으면 노이즈.
사용: python tools/stopwidth_study.py
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
from exit_study import bracket_entries                 # noqa: E402
from radar_entry_study import _stats, FEE, HOLD        # noqa: E402

N_SYMBOLS = 30
WIDTHS = (1.0, 1.5, 2.0, 3.0)


def sim_width(s, k, long, entry, atr, mult):
    """손절 mult×ATR / 목표 2R / 48봉. R = 손절거리 정규화."""
    h, l, c = s["highs"], s["lows"], s["closes"]
    n = len(c)
    dist = mult * atr
    sgn = 1 if long else -1
    stop = entry - sgn * dist
    target = entry + sgn * 2 * dist
    exit_p, k2 = None, k
    for k2 in range(k + 1, min(k + 1 + HOLD, n)):
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
    return (exit_p - entry) * sgn / dist - (entry + exit_p) * FEE / dist


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 손절폭 스윕: {len(syms)}심볼 × ~10개월 브래킷 · R:R 2 고정\n")

    trades = {w: [] for w in WIDTHS}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        for k, long, entry, atr in bracket_entries(s):
            for w in WIDTHS:
                trades[w].append((s["ts"][k], sim_width(s, k, long, entry, atr, w)))
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)

    print()
    print(f"\n{'손절폭':<10}{'n':>5}{'승률':>7}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}  4분할")
    report = {}
    for w in WIDTHS:
        tr = trades[w]
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        half = len(vals) // 2
        q = len(vals) // 4
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        folds = [_stats(vals[i * q:(i + 1) * q if i < 3 else len(vals)])
                 for i in range(4)]
        fstr = "/".join(f"{f.get('pf', 0):.2f}" for f in folds)
        name = f"{w}ATR" + ("(현행)" if w == 1.0 else "")
        report[name] = {"all": st, "first": a, "second": b,
                        "folds": [f.get("pf", 0) for f in folds]}
        if st["n"]:
            print(f"{name:<10}{st['n']:>5}{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b.get('pf',0):>8.2f}"
                  f"  {fstr}")

    Path("stopwidth_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] stopwidth_study.json")
    print("판정: 현행(1.0) 대비 avgR·PF 전후반 일관 개선일 때만 교체.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
