#!/usr/bin/env python
"""목표폭 × 본절기준 그리드 — 복기 트리거 도달에 따른 정면 재검증 (2026-08-04).

트리거: TARGET 복기 '추세지속' 6건(≥5) + BE 복기 '휩쏘' 5건(≥5).
가설1(러너): 익절 후 1R+ 연장이 반복 → 목표 2R이 짧다. 2.5R/3R이 나은가?
가설2(본절): BE 청산 5/5 휩쏘 → +1R 발동이 이르다. +1.3R/+1.5R/무BE가 나은가?

통제: 브래킷 진입 고정, 손절 1ATR 고정, 48봉, 수수료, 전/후반 + 4분할.
판정: 현행(2.0R × BE1.0) 대비 avgR·PF 전후반 일관 개선일 때만 교체.
사용: python tools/exit_grid_study.py
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
TARGETS = (2.0, 2.5, 3.0)
BES = (1.0, 1.3, 1.5, None)          # None = 본절 이동 없음


def sim(s, k, long, entry, atr, tgt_r, be_r):
    """손절 1ATR / 목표 tgt_r×ATR / 본절 +be_r×ATR(봉마감 후 적용) / 48봉."""
    h, l, c = s["highs"], s["lows"], s["closes"]
    n = len(c)
    sgn = 1 if long else -1
    stop = entry - sgn * atr
    target = entry + sgn * tgt_r * atr
    be = False
    k2 = k
    for k2 in range(k + 1, min(k + 1 + HOLD, n)):
        hi, lo = h[k2], l[k2]
        if long:
            if lo <= stop:
                exit_p = stop; break
            if hi >= target:
                exit_p = target; break
        else:
            if hi >= stop:
                exit_p = stop; break
            if lo <= target:
                exit_p = target; break
        if be_r is not None and not be:
            if (hi >= entry + be_r * atr) if long else (lo <= entry - be_r * atr):
                stop = entry
                be = True
    else:
        exit_p = c[min(k2, n - 1)]
    return (exit_p - entry) * sgn / atr - (entry + exit_p) * FEE / atr


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 목표×본절 그리드: {len(syms)}심볼 × ~10개월 브래킷 · 손절 1ATR 고정\n")

    grid = {(tg, be): [] for tg in TARGETS for be in BES}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        for k, long, entry, atr in bracket_entries(s):
            for (tg, be), acc in grid.items():
                acc.append((s["ts"][k], sim(s, k, long, entry, atr, tg, be)))
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)

    print()
    print(f"\n{'목표':>5} {'본절':>6}{'n':>6}{'승률':>6}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}  4분할")
    report = {}
    for (tg, be), tr in grid.items():
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        if not vals:
            continue
        half = len(vals) // 2
        q = len(vals) // 4
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        folds = [_stats(vals[i * q:(i + 1) * q if i < 3 else len(vals)])
                 for i in range(4)]
        name = (f"{tg}R×BE{be if be is not None else '무'}"
                + ("(현행)" if (tg, be) == (2.0, 1.0) else ""))
        report[name] = {"all": st, "first": a, "second": b,
                        "folds": [f.get("pf", 0) for f in folds]}
        print(f"{tg:>4}R {str(be) if be is not None else '무':>6}{st['n']:>6}"
              f"{st['wr']*100:>5.0f}%{st['avgR']:>8.3f}{st['pf']:>7.2f}"
              f"{a.get('pf', 0):>8.2f}{b.get('pf', 0):>8.2f}"
              f"  {'/'.join(f'{f.get('pf', 0):.2f}' for f in folds)}")

    Path("exit_grid_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] exit_grid_study.json")
    print("판정: 현행(2.0R×BE1.0) 대비 avgR·PF 전후반 일관 개선일 때만 교체.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
