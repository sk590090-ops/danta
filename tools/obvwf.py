#!/usr/bin/env python
"""
OBV_DIV 워크포워드 확증 — 롤링 OOS에서 PF 1.16~1.21 나온 셋업이
파라미터 선택까지 포함해도 미래에 일반화되는지 최종 검증.

각 폴드: IS(1000봉)에서 (rr_target, atr_stop_mult, min_score) 8조합 그리드 →
IS 최적을 직후 OOS(500봉)에 적용. 비교군 FIXED = 롤링OOS 때 쓴 고정값(2.0/1.0/60).
OBV_DIV-only. 판정: WFA 풀링 avgR>0 & PF>1.1 & WFA≥FIXED 근접이면 통과.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from ptrader.config import load_config
from ptrader import datafeed, backtest
from tune import clone, _pool_stats

SETUPS = {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION",
          "REVERSAL", "CONVERGENCE", "OBV_DIV", "OBV_PRD", "VWAP"}
GRID = [{"planner.rr_target": rr, "planner.atr_stop_mult": sm,
         "signal.min_score_approve": ms}
        for rr in (2.0, 2.5) for sm in (1.0, 1.5) for ms in (60, 70)]
FIXED = {"planner.rr_target": 2.0, "planner.atr_stop_mult": 1.0,
         "signal.min_score_approve": 60}
MIN_IS_TRADES = 10
WARMUP = 300


def run_slice(data, cfg, a, b, start_off, risk_amt):
    """data[a:b] 구간 백테스트(진입은 a+start_off부터) → 트레이드 풀."""
    trades = []
    for sym, df in data.items():
        res = backtest.run(df.iloc[a:b], cfg, symbol=sym, start_idx=start_off)
        for t in res.trades:
            t["symbol"] = sym
            trades.append(t)
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--is-len", type=int, default=1000)
    ap.add_argument("--oos-len", type=int, default=500)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="obvwf_report.json")
    ap.add_argument("--setup", default="OBV_DIV", choices=sorted(SETUPS),
                    help="단독 검증할 셋업 (기본 OBV_DIV)")
    args = ap.parse_args()
    only = tuple(sorted(SETUPS - {args.setup}))

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.limit = args.limit
    risk_amt = cfg.equity * cfg.risk.risk_per_trade

    data = {s: datafeed.load_cached(s, cfg) for s in args.symbols}
    n = min(len(df) for df in data.values())
    print(f"[{args.timeframe}] {len(data)}심볼 {n}봉 · "
          f"IS={args.is_len}/OOS={args.oos_len} · {args.setup}-only WFA")

    base = {"signal.disabled_setups": only}
    folds, wfa_pool, fix_pool = [], [], []
    a = 0
    fi = 0
    while a + args.is_len + args.oos_len <= n:
        fi += 1
        is_b = a + args.is_len
        oos_b = is_b + args.oos_len
        # IS 그리드 (IS 내부 앞 300봉은 워밍업)
        best = None
        for over in GRID:
            c = clone(cfg, **{**base, **over})
            st = _pool_stats(run_slice(data, c, a, is_b, WARMUP, risk_amt),
                             risk_amt)
            score = st["avg_R"] if st["n_trades"] >= MIN_IS_TRADES \
                else -9 + st["n_trades"] * 0.001
            if best is None or score > best[0]:
                best = (score, over, st)
        # OOS: 직전 300봉 워밍업 포함, 진입은 is_b부터
        oa = is_b - WARMUP
        wfa = run_slice(data, clone(cfg, **{**base, **best[1]}),
                        oa, oos_b, WARMUP, risk_amt)
        fix = run_slice(data, clone(cfg, **{**base, **FIXED}),
                        oa, oos_b, WARMUP, risk_amt)
        wfa_pool += wfa
        fix_pool += fix
        ws, fs = _pool_stats(wfa, risk_amt), _pool_stats(fix, risk_amt)
        p = best[1]
        folds.append({"fold": fi, "chosen": p, "is_avgR": best[2]["avg_R"],
                      "wfa": ws, "fix": fs})
        print(f"  f{fi} IS[{a}:{is_b}] rr={p['planner.rr_target']} "
              f"sm={p['planner.atr_stop_mult']} "
              f"ms={p['signal.min_score_approve']} "
              f"(IS avgR {best[2]['avg_R']:+.2f}) → OOS "
              f"n={ws['n_trades']} avgR={ws['avg_R']:+.3f} PF={ws['profit_factor']}"
              f"  | FIX n={fs['n_trades']} avgR={fs['avg_R']:+.3f}")
        a += args.oos_len

    W = _pool_stats(wfa_pool, risk_amt)
    F = _pool_stats(fix_pool, risk_amt)
    pos = sum(1 for f in folds if f["wfa"]["avg_R"] > 0)
    print(f"\n########## OOS 종합 ({len(folds)}폴드) ##########")
    print(f"  WFA(적응형): n={W['n_trades']} WR={W['win_rate']*100:.0f}% "
          f"avgR={W['avg_R']:+.3f} PF={W['profit_factor']} PnL={W['total_pnl']}")
    print(f"  FIXED     : n={F['n_trades']} WR={F['win_rate']*100:.0f}% "
          f"avgR={F['avg_R']:+.3f} PF={F['profit_factor']} PnL={F['total_pnl']}")
    print(f"  OOS 수익 폴드: {pos}/{len(folds)}")
    ok = W["avg_R"] > 0 and W["profit_factor"] > 1.1 and pos >= (len(folds)+1)//2
    print(f"  판정: {'✅ 워크포워드 통과 — 페이퍼 봇 투입 후보' if ok else '❌ 확증 실패 — 과최적화/불안정'}")

    Path(args.out).write_text(json.dumps(
        {"folds": folds, "wfa": W, "fixed": F}, ensure_ascii=False,
        indent=2, default=str), encoding="utf-8")
    print(f"[저장] {args.out}")


if __name__ == "__main__":
    main()
