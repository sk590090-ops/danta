#!/usr/bin/env python
"""OBV_DIV / VWAP 셋업 검증 — 고정 규칙 롤링 OOS (convtest와 동일 방법론)."""
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
from ptrader import datafeed
from tune import clone
from revtest import rolling_oos

SETUPS = {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION",
          "REVERSAL", "CONVERGENCE", "OBV_DIV", "VWAP"}
BASE = {"signal.min_score_approve": 60,
        "planner.rr_target": 2.0, "planner.atr_stop_mult": 1.0}


def only(name):
    return tuple(sorted(SETUPS - {name}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--oos", type=int, default=500)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="sigtest2_report.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.limit = args.limit
    risk_amt = cfg.equity * cfg.risk.risk_per_trade

    data = {}
    for s in args.symbols:
        data[s] = datafeed.load_cached(s, cfg)
    print(f"[{args.timeframe}] {len(data)}심볼 {args.limit}봉 "
          f"(warmup={args.warmup}/oos={args.oos})")

    configs = {
        "OBV_DIV-only": clone(cfg, **BASE, **{"signal.disabled_setups": only("OBV_DIV")}),
        "VWAP-only": clone(cfg, **BASE, **{"signal.disabled_setups": only("VWAP")}),
        "REV-only(기준)": clone(cfg, **BASE, **{"signal.disabled_setups": only("REVERSAL")}),
    }
    print(f"  {'config':<16}{'blocks+':>9}{'N':>6}{'WR':>6}{'avgR':>8}{'PF':>7}{'PnL':>10}")
    report = {}
    for name, c in configs.items():
        blocks, agg = rolling_oos(data, c, args.warmup, args.oos, risk_amt)
        pos = sum(1 for b in blocks if b["avgR"] > 0)
        report[name] = {"blocks": blocks, "agg": agg, "pos": pos}
        print(f"  {name:<16}{pos:>4}/{len(blocks):<4}{agg['n_trades']:>6}"
              f"{agg['win_rate']*100:>5.0f}%{agg['avg_R']:>8.3f}"
              f"{agg['profit_factor']:>7.2f}{agg['total_pnl']:>10.1f}")

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False,
                                         indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
