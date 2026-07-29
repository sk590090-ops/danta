#!/usr/bin/env python
"""
REVERSAL 신호 질 개선 검증 — v1(현재) vs v2(≥2요인) 고정 규칙 롤링 OOS 비교.

과최적화 교훈 반영: 파라미터 재최적화 없음. 각 규칙은 '고정'으로,
연속 OOS 블록마다 성적을 내고 이어붙여 견고성(양의 블록 비율·PF·평균R)을 본다.
각 OOS 블록은 앞선 warmup봉으로 지표 워밍업(backtest start_idx).

configs:
  v1        : REVERSAL-only, 컨플루언스 OFF (현재 config.yaml)
  v2        : REVERSAL-only, 컨플루언스 ON (거래량+RSI+상위TF stretch+확정형)
  v2_noHTF  : v2에서 상위TF stretch 게이트만 완화(기여도 확인)
  v2_noVol  : v2에서 거래량 게이트만 완화

사용:
  python tools/revtest.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT --limit 5000
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

ALL_SETUPS = {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION", "REVERSAL"}
REV_ONLY = tuple(sorted(ALL_SETUPS - {"REVERSAL"}))


def base_over(cfg):
    return {"signal.disabled_setups": REV_ONLY,
            "signal.min_score_approve": 60,
            "planner.rr_target": 2.0, "planner.atr_stop_mult": 1.0}


def make_configs(cfg):
    b = base_over(cfg)
    v1 = clone(cfg, **b, **{"signal.reversal_confluence": False})
    v2_k2 = clone(cfg, **b, **{"signal.reversal_confluence": True,
                               "signal.rev_min_confluence": 2})
    v2_k3 = clone(cfg, **b, **{"signal.reversal_confluence": True,
                               "signal.rev_min_confluence": 3})
    all_setups = clone(cfg, **{"signal.disabled_setups": (),
                               "signal.min_score_approve": 65,
                               "planner.rr_target": 2.0,
                               "planner.atr_stop_mult": 1.0})
    return {"v1(현재)": v1, "v2(≥2요인)": v2_k2, "v2(≥3요인)": v2_k3,
            "ALL(전셋업)": all_setups}


def rolling_oos(data, cfg, warmup_ctx, oos_len, risk_amt):
    """고정 cfg를 연속 OOS 블록마다 평가 → (블록별 stats, 전체 pooled)."""
    n = min(len(df) for df in data.values())
    blocks, pooled = [], []
    a = warmup_ctx
    while a + oos_len <= n:
        b = a + oos_len
        ctx = {s: df.iloc[a - warmup_ctx:b] for s, df in data.items()}
        smap = {s: warmup_ctx for s in data}
        trades = []
        for s, df in ctx.items():
            res = backtest.run(df, cfg, symbol=s, start_idx=smap[s])
            for t in res.trades:
                t["symbol"] = s
                trades.append(t)
        st = _pool_stats(trades, risk_amt)
        blocks.append({"oos": [a, b], "n": st["n_trades"],
                       "winrate": st["win_rate"], "avgR": st["avg_R"],
                       "pf": st["profit_factor"], "pnl": st["total_pnl"]})
        pooled += trades
        a += oos_len
    return blocks, _pool_stats(pooled, risk_amt)


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
    ap.add_argument("--out", default="revtest_report.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.exchange = args.exchange
    cfg.ccxt.limit = args.limit
    risk_amt = cfg.equity * cfg.risk.risk_per_trade

    print(f"[데이터] {args.exchange} {args.symbols} {args.timeframe} {args.limit}봉")
    data = {}
    for s in args.symbols:
        data[s] = datafeed.load_cached(s, cfg, refresh=args.refresh)
        print(f"  {s:<10} {len(data[s])}봉 "
              f"{data[s].index[0].date()}~{data[s].index[-1].date()}")

    configs = make_configs(cfg)
    print(f"\n[롤링 OOS] warmup={args.warmup} / OOS={args.oos} 고정규칙 비교\n")
    report = {}
    header = (f"  {'config':<16}{'blocks+':>9}{'N':>6}{'WR':>6}"
              f"{'avgR':>8}{'PF':>7}{'PnL':>10}")
    print(header)
    for name, c in configs.items():
        blocks, agg = rolling_oos(data, c, args.warmup, args.oos, risk_amt)
        pos = sum(1 for b in blocks if b["avgR"] > 0)
        report[name] = {"blocks": blocks, "agg": agg,
                        "pos_blocks": pos, "n_blocks": len(blocks)}
        print(f"  {name:<16}{pos:>4}/{len(blocks):<4}{agg['n_trades']:>6}"
              f"{agg['win_rate']*100:>5.0f}%{agg['avg_R']:>8.3f}"
              f"{agg['profit_factor']:>7.2f}{agg['total_pnl']:>10.1f}")

    # 블록별 v1 vs v2 상세
    v1b = report["v1(현재)"]["blocks"]
    v2b = report["v2(≥2요인)"]["blocks"]
    print(f"\n  [블록별 avgR]  {'구간':<14}{'v1':>8}{'v2':>8}{'v2_n':>7}")
    for a, b in zip(v1b, v2b):
        print(f"    {str(a['oos']):<20}{a['avgR']:>8.3f}{b['avgR']:>8.3f}"
              f"{b['n']:>7}")

    v1, v2 = report["v1(현재)"]["agg"], report["v2(≥2요인)"]["agg"]
    print("\n########## 판정 ##########")
    better = (v2["profit_factor"] > v1["profit_factor"] and v2["avg_R"] > v1["avg_R"]
              and report["v2(≥2요인)"]["pos_blocks"]
              >= report["v1(현재)"]["pos_blocks"])
    if v2["n_trades"] < 40:
        print(f"  ⚠️ v2 표본 부족(n={v2['n_trades']}) — 게이트 과도, 통계력 낮음")
    print(f"  v1: avgR {v1['avg_R']:+.3f} PF {v1['profit_factor']} "
          f"(+블록 {report['v1(현재)']['pos_blocks']}/{report['v1(현재)']['n_blocks']})")
    print(f"  v2: avgR {v2['avg_R']:+.3f} PF {v2['profit_factor']} "
          f"(+블록 {report['v2(≥2요인)']['pos_blocks']}/{report['v2(≥2요인)']['n_blocks']})")
    print(f"  → {'✅ v2 개선(채택 검토)' if better else '❌ v2 개선 불충분/악화'}")

    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"\n[저장] {args.out}")


if __name__ == "__main__":
    main()
