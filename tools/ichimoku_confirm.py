#!/usr/bin/env python
"""일목 4h 롱 확증 — 재난손절(2ATR) 공존 여부 + 4분할 견고성.

배선 전 마지막 관문: 지표 청산만으로는 단일 트레이드 손실이 무한정이라
리스크 프레임과 충돌 → 2ATR 재난손절을 얹어도 엣지가 살아야 채택.
사용: python tools/ichimoku_confirm.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from daily_scan import FAPI, _get                      # noqa: E402
from radar_backtest import fetch_series                # noqa: E402
from radar_entry_study import _stats                   # noqa: E402
from connors_study import resample_4h                  # noqa: E402
from ichimoku_study import sim                         # noqa: E402


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:30]]
    print("▶ 일목 4h 롱 확증: 재난손절 유무 + 4분할\n")

    variants = {"손절없음(원형)": None, "재난손절 2ATR": 2.0,
                "재난손절 3ATR": 3.0}
    trades = {k: [] for k in variants}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        s4 = resample_4h(s)
        for name, sa in variants.items():
            trades[name] += sim(s4, "LONG", stop_atr=sa)
        print(f"  [{kk}/30] {sym}", end="\r")
        time.sleep(0.05)

    print()
    report = {}
    print(f"\n{'변형':<16}{'n':>5}{'승률':>7}{'avgR':>8}{'PF':>7}  4분할 PF")
    for name, tr in trades.items():
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        st = _stats(vals)
        q = len(vals) // 4
        folds = [_stats(vals[i * q:(i + 1) * q if i < 3 else len(vals)])
                 for i in range(4)]
        fstr = "/".join(f"{f.get('pf', 0):.2f}" for f in folds)
        report[name] = {"all": st, "folds": [f.get("pf", 0) for f in folds]}
        if st["n"]:
            print(f"{name:<16}{st['n']:>5}{st['wr']*100:>6.0f}%"
                  f"{st['avgR']:>8.3f}{st['pf']:>7.2f}  {fstr}")

    Path("ichimoku_confirm.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] ichimoku_confirm.json")
    print("판정: 손절 얹은 변형이 avgR>0 & PF>1.1 & 4분할 중 3+ 흑자면 채택.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
