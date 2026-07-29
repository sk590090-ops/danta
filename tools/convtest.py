#!/usr/bin/env python
"""
CONVERGENCE(수렴+거래량, bullstory1 노트) 셋업 검증 — 고정 규칙 롤링 OOS.

비교군 (재최적화 없음 — 과최적화 교훈):
  CONV-only : CONVERGENCE만 활성
  REV-only  : 기존 v1 (REVERSAL만)
  ALL       : 전 셋업(CONVERGENCE 포함)

사용: python tools/convtest.py --timeframe 1h --limit 5000
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
from ptrader import datafeed
from tune import clone
from revtest import rolling_oos

SETUPS = {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION",
          "REVERSAL", "CONVERGENCE"}
BASE = {"signal.min_score_approve": 60,
        "planner.rr_target": 2.0, "planner.atr_stop_mult": 1.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--oos", type=int, default=500)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--conv-bars", type=int, default=None,
                    help="conv_min_bars 오버라이드 (1d=15≈3주 등 TF 충실 번역용)")
    ap.add_argument("--out", default="convtest_report.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.exchange = args.exchange
    cfg.ccxt.limit = args.limit
    if args.conv_bars:
        cfg.signal.conv_min_bars = args.conv_bars
    risk_amt = cfg.equity * cfg.risk.risk_per_trade

    print(f"[데이터] {args.exchange} {args.symbols} {args.timeframe} {args.limit}봉")
    data = {}
    for s in args.symbols:
        data[s] = datafeed.load_cached(s, cfg, refresh=args.refresh)
        print(f"  {s:<10} {len(data[s])}봉 "
              f"{data[s].index[0].date()}~{data[s].index[-1].date()}")

    configs = {
        "CONV-only": clone(cfg, **BASE, **{
            "signal.disabled_setups": tuple(sorted(SETUPS - {"CONVERGENCE"}))}),
        "REV-only(v1)": clone(cfg, **BASE, **{
            "signal.disabled_setups": tuple(sorted(SETUPS - {"REVERSAL"}))}),
        "ALL": clone(cfg, **{**BASE, "signal.disabled_setups": (),
                             "signal.min_score_approve": 65}),
    }

    print(f"\n[롤링 OOS] warmup={args.warmup} / OOS={args.oos} 고정규칙\n")
    print(f"  {'config':<14}{'blocks+':>9}{'N':>6}{'WR':>6}{'avgR':>8}{'PF':>7}{'PnL':>10}")
    report = {}
    for name, c in configs.items():
        blocks, agg = rolling_oos(data, c, args.warmup, args.oos, risk_amt)
        pos = sum(1 for b in blocks if b["avgR"] > 0)
        report[name] = {"blocks": blocks, "agg": agg, "pos": pos}
        print(f"  {name:<14}{pos:>4}/{len(blocks):<4}{agg['n_trades']:>6}"
              f"{agg['win_rate']*100:>5.0f}%{agg['avg_R']:>8.3f}"
              f"{agg['profit_factor']:>7.2f}{agg['total_pnl']:>10.1f}")

    cb = report["CONV-only"]["blocks"]
    print(f"\n  [CONV-only 블록별] {'구간':<16}{'n':>4}{'avgR':>8}{'PF':>7}")
    for b in cb:
        print(f"    {str(b['oos']):<20}{b['n']:>4}{b['avgR']:>8.3f}{b['pf']:>7.2f}")

    agg = report["CONV-only"]["agg"]
    print("\n########## 판정 ##########")
    if agg["n_trades"] < 30:
        print(f"  ⚠️ 표본 부족 n={agg['n_trades']} — 통계적 판단 불가(참고용)")
    ok = agg["avg_R"] > 0 and agg["profit_factor"] > 1.1 and agg["n_trades"] >= 30
    print(f"  CONV-only: n={agg['n_trades']} avgR {agg['avg_R']:+.3f} "
          f"PF {agg['profit_factor']} (+블록 {report['CONV-only']['pos']}/{len(cb)})")
    print(f"  → {'✅ 양의 엣지 후보 — 워크포워드로 확증 필요' if ok else '❌ 엣지 없음/표본부족'}")

    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"\n[저장] {args.out}")


if __name__ == "__main__":
    main()
