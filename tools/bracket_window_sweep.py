#!/usr/bin/env python
"""브래킷 유효시간(window) 민감도 스윕 — 6h가 절벽인가 고원인가.

파라미터를 '최적화'하려는 게 아니라 **견고성 확인**이 목적:
  - 성적이 window에 따라 널뛰면 → 브래킷 규칙 자체가 노이즈 (경고)
  - 넓은 구간에서 고르게 양수면 → 6h는 무난한 선택 (유지)
전·후반 반분 성적도 함께 봐서 한쪽 구간에서만 좋은 값은 걸러낸다.

사용: python tools/bracket_window_sweep.py
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

from daily_scan import FAPI, _get                       # noqa: E402
from radar_backtest import fetch_series                 # noqa: E402
from radar_entry_study import sim_symbol, _stats        # noqa: E402

WINDOWS = (2, 3, 4, 6, 8, 12, 18, 24)
N_SYMBOLS = 30


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 브래킷 유효시간 스윕: {len(syms)}심볼 × ~10개월 × "
          f"{len(WINDOWS)}개 설정\n")

    series = {}
    for k, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if s is not None:
            series[sym] = s
        print(f"  [{k}/{len(syms)}] {sym} 수집", end="\r")
        time.sleep(0.05)
    print(f"\n  {len(series)}심볼 확보\n")

    print(f"{'창(h)':<7}{'n':>6}{'승률':>7}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}")
    report = {}
    for w in WINDOWS:
        trades = []
        for sym, s in series.items():
            trades += sim_symbol(s, "bracket", window=w)
        trades.sort(key=lambda x: x[0])
        vals = [t[1] for t in trades]
        half = len(vals) // 2
        st, a, bb = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        report[w] = {"all": st, "first": a, "second": bb}
        if st["n"]:
            print(f"{w:<7}{st['n']:>6}{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{bb.get('pf',0):>8.2f}")

    Path("bracket_window_sweep.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] bracket_window_sweep.json")
    print("해석: 넓은 구간에서 PF가 고르게 >1.2면 견고(현행 유지). "
          "특정 값만 튀면 그 값은 노이즈 — 채택 금지.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
