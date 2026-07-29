#!/usr/bin/env python
"""
워크포워드 검증 (Walk-Forward Analysis).

각 폴드: 과거 IS구간에서 파라미터 재최적화 → 바로 다음 OOS구간(미지의 미래)에 적용.
OOS 결과만 이어붙인 것이 '진짜 아웃오브샘플 성적' → 과최적화를 걸러냄.
OOS 지표는 직전 IS데이터로 워밍업(backtest start_idx)해 데이터 낭비 없음.

비교군:
  - WFA(적응형): 폴드마다 IS에서 재최적화한 파라미터의 OOS 성적
  - FIXED(고정): 현재 config.yaml 튜닝 파라미터를 동일 OOS에 적용
  - ALL(무튜닝): 전 셋업·기본값을 동일 OOS에 적용

사용:
  python tools/walkforward.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT \
      --limit 5000 --is 1000 --oos 500
"""
from __future__ import annotations

import argparse
import copy
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
from tune import clone, _pool_stats  # 재사용

ALL_SETUPS = {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION", "REVERSAL"}
MIN_IS_TRADES = 15


# ---- 백테스트 헬퍼 ---------------------------------------------------------
def collect_trades(data: dict, cfg, start_map: dict | None = None) -> list:
    """전 심볼 백테스트 트레이드 풀링. start_map[sym]=진입시작인덱스(OOS용)."""
    trades = []
    for sym, df in data.items():
        si = None if start_map is None else start_map.get(sym)
        res = backtest.run(df, cfg, symbol=sym, start_idx=si)
        for t in res.trades:
            t["symbol"] = sym
            trades.append(t)
    return trades


def _slice(data, a, b):
    return {s: df.iloc[a:b] for s, df in data.items()}


# ---- IS 최적화 -------------------------------------------------------------
def optimize_is(data_is, base_cfg, risk_amt):
    """IS구간 소규모 그리드서치 → 최적 (override, 설명)."""
    best = None
    for rr, sm in ((2.0, 1.0), (2.5, 1.5)):
        for ms in (60, 70):
            base_over = {"planner.rr_target": rr, "planner.atr_stop_mult": sm,
                         "signal.min_score_approve": ms}
            # (A) 전 셋업
            st_all = _pool_stats(
                collect_trades(data_is, clone(base_cfg, **base_over,
                               **{"signal.disabled_setups": ()})), risk_amt)
            cands = [("all", (), st_all)]
            # (B) REVERSAL only
            rev_dis = tuple(sorted(ALL_SETUPS - {"REVERSAL"}))
            st_rev = _pool_stats(
                collect_trades(data_is, clone(base_cfg, **base_over,
                               **{"signal.disabled_setups": rev_dis})), risk_amt)
            cands.append(("reversal_only", rev_dis, st_rev))
            # (C) IS 손실셋업 제거
            losers = tuple(sorted(
                (n for n, s in st_all["by_setup"].items() if s["avg_R"] < 0)))
            if losers and set(losers) != ALL_SETUPS:
                st_dl = _pool_stats(
                    collect_trades(data_is, clone(base_cfg, **base_over,
                                   **{"signal.disabled_setups": losers})), risk_amt)
                cands.append(("drop_losers", losers, st_dl))

            for mode, dis, st in cands:
                sc = (st["avg_R"] if st["n_trades"] >= MIN_IS_TRADES
                      else -99 + st["n_trades"] * 0.01)
                over = {**base_over, "signal.disabled_setups": dis}
                desc = f"{mode} rr={rr} sm={sm} ms={ms}"
                if best is None or sc > best[0]:
                    best = (sc, over, st, desc)
    return best


# ---- WFA 실행 --------------------------------------------------------------
def walk_forward(data, base_cfg, is_len, oos_len):
    risk_amt = base_cfg.equity * base_cfg.risk.risk_per_trade
    n = min(len(df) for df in data.values())
    fixed_over = {  # 현재 config.yaml 튜닝값
        "planner.rr_target": base_cfg.planner.rr_target,
        "planner.atr_stop_mult": base_cfg.planner.atr_stop_mult,
        "signal.min_score_approve": base_cfg.signal.min_score_approve,
        "signal.disabled_setups": tuple(base_cfg.signal.disabled_setups)}
    cfg_all = clone(base_cfg, **{"signal.disabled_setups": (),
                                 "signal.min_score_approve": 65,
                                 "planner.rr_target": 2.0,
                                 "planner.atr_stop_mult": 1.0})

    folds = []
    wfa_tr, fixed_tr, all_tr = [], [], []
    start = 0
    fi = 0
    while start + is_len + oos_len <= n:
        fi += 1
        is_a, is_b = start, start + is_len
        oos_a, oos_b = is_b, is_b + oos_len
        data_is = _slice(data, is_a, is_b)

        best = optimize_is(data_is, base_cfg, risk_amt)
        cfg_best = clone(base_cfg, **best[1])

        # OOS: IS를 워밍업으로 포함, 진입은 oos_a부터
        data_ctx = _slice(data, is_a, oos_b)
        smap = {s: (is_b - is_a) for s in data}   # 컨텍스트 내 OOS 시작 오프셋
        oos_wfa = collect_trades(data_ctx, cfg_best, smap)
        oos_fix = collect_trades(data_ctx, clone(base_cfg, **fixed_over), smap)
        oos_all = collect_trades(data_ctx, cfg_all, smap)
        wfa_tr += oos_wfa
        fixed_tr += oos_fix
        all_tr += oos_all

        st = _pool_stats(oos_wfa, risk_amt)
        folds.append({
            "fold": fi, "is": [is_a, is_b], "oos": [oos_a, oos_b],
            "chosen": best[3], "is_avgR": round(best[2]["avg_R"], 3),
            "oos_n": st["n_trades"], "oos_winrate": st["win_rate"],
            "oos_avgR": st["avg_R"], "oos_pf": st["profit_factor"],
            "oos_pnl": st["total_pnl"]})
        start += oos_len

    return {
        "folds": folds,
        "wfa": _pool_stats(wfa_tr, risk_amt),
        "fixed": _pool_stats(fixed_tr, risk_amt),
        "all": _pool_stats(all_tr, risk_amt),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--is", dest="is_len", type=int, default=1000)
    ap.add_argument("--oos", dest="oos_len", type=int, default=500)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out", default="walkforward_report.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.exchange = args.exchange
    cfg.ccxt.limit = args.limit

    print(f"[데이터] {args.exchange} {args.symbols} {args.timeframe} {args.limit}봉")
    data = {}
    for s in args.symbols:
        data[s] = datafeed.load_cached(s, cfg, refresh=args.refresh)
        print(f"  {s:<10} {len(data[s])}봉 "
              f"{data[s].index[0].date()}~{data[s].index[-1].date()}")

    print(f"\n[WFA] IS={args.is_len} / OOS={args.oos_len} 롤링...")
    rep = walk_forward(data, cfg, args.is_len, args.oos_len)

    print(f"\n{'fold':>4} {'chosen(IS 최적)':<26} {'IS_R':>6} "
          f"{'OOS_n':>6} {'WR':>5} {'OOS_R':>7} {'PF':>5} {'PnL':>9}")
    for f in rep["folds"]:
        print(f"{f['fold']:>4} {f['chosen']:<26} {f['is_avgR']:>6.2f} "
              f"{f['oos_n']:>6} {f['oos_winrate']*100:>4.0f}% "
              f"{f['oos_avgR']:>7.3f} {f['oos_pf']:>5.2f} {f['oos_pnl']:>9.1f}")

    def line(tag, st):
        return (f"  {tag:<22} n={st['n_trades']:>4} WR={st['win_rate']*100:>3.0f}% "
                f"avgR={st['avg_R']:>7.3f} PF={st['profit_factor']:>5.2f} "
                f"PnL={st['total_pnl']:>9.1f}")
    print("\n########## 아웃오브샘플 종합(폴드 OOS 이어붙임) ##########")
    print(line("WFA(적응형 재최적화)", rep["wfa"]))
    print(line("FIXED(config.yaml)", rep["fixed"]))
    print(line("ALL(무튜닝·전셋업)", rep["all"]))

    wfa, fixed = rep["wfa"], rep["fixed"]
    pos = sum(1 for f in rep["folds"] if f["oos_avgR"] > 0)
    print(f"\n  · OOS 수익 폴드: {pos}/{len(rep['folds'])}")
    verdict = ("✅ WFA에서도 양의 엣지 유지 — 견고" if wfa["avg_R"] > 0 and wfa["profit_factor"] > 1
               else "⚠️ WFA에서 엣지 소멸/약화 — 과최적화 의심")
    print(f"  · 판정: {verdict}")

    Path(args.out).write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"\n[저장] {args.out}")


if __name__ == "__main__":
    main()
