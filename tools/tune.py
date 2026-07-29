#!/usr/bin/env python
"""
셋업별 성과 분석 + 파라미터 튜닝 하네스.

동작:
  1) 여러 심볼 실데이터 백테스트 → 트레이드 풀링 → 셋업별 성과표
  2) 단계별 그리드서치(손절/목표 geometry → 필터 → 셋업선택)
  3) 최적 파라미터로 재검증 + config.tuned.yaml 저장

사용:
  python tools/tune.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT \
      --limit 800 --timeframe 1h
  python tools/tune.py --refresh          # 캐시 무시하고 재조회
목적함수: 풀링 기대값(평균 R) 최대화, 단 최소 거래수 이상.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

# 프로젝트 루트 import 경로
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from ptrader.config import load_config
from ptrader import datafeed, backtest


# ---- 조합 실행/집계 --------------------------------------------------------
def clone(cfg, **over):
    """cfg 깊은복사 후 'signal.min_score_approve' 형태 override."""
    c = copy.deepcopy(cfg)
    for path, val in over.items():
        obj = c
        parts = path.split(".")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], val)
    return c


def run_combo(data: dict, cfg) -> dict:
    """전 심볼 백테스트 → 트레이드 풀링 → 통계."""
    risk_amt = cfg.equity * cfg.risk.risk_per_trade
    trades = []
    for sym, df in data.items():
        res = backtest.run(df, cfg, symbol=sym)
        for t in res.trades:
            t["symbol"] = sym
            trades.append(t)
    return _pool_stats(trades, risk_amt)


def _pool_stats(trades, risk_amt) -> dict:
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "avg_R": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "total_pnl": 0.0, "by_setup": {}}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(wins)
    gross_loss = -sum(p for p in pnls if p <= 0)
    return {
        "n_trades": n,
        "avg_R": round(sum(pnls) / n / risk_amt, 3),
        "win_rate": round(len(wins) / n, 3),
        "profit_factor": round(gross_win / (gross_loss + 1e-9), 2),
        "total_pnl": round(sum(pnls), 2),
        "by_setup": _by_setup(trades, risk_amt),
    }


def _by_setup(trades, risk_amt) -> dict:
    out = {}
    for t in trades:
        s = out.setdefault(t["setup"], {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += int(t["pnl"] > 0)
        s["pnl"] += t["pnl"]
    for s in out.values():
        s["win_rate"] = round(s["wins"] / s["n"], 3)
        s["avg_R"] = round(s["pnl"] / s["n"] / risk_amt, 3)
        s["pnl"] = round(s["pnl"], 2)
    return dict(sorted(out.items(), key=lambda kv: kv[1]["pnl"], reverse=True))


def _fmt_setup_table(by_setup: dict) -> str:
    rows = [f"  {'SETUP':<20}{'N':>4}{'WIN%':>7}{'avgR':>7}{'PnL':>10}"]
    for name, s in by_setup.items():
        rows.append(f"  {name:<20}{s['n']:>4}{s['win_rate']*100:>6.0f}%"
                    f"{s['avg_R']:>7.2f}{s['pnl']:>10.1f}")
    return "\n".join(rows)


# ---- 그리드서치 ------------------------------------------------------------
MIN_TRADES = 20


def _objective(stats) -> float:
    """최소 거래수 미만이면 강한 페널티, 아니면 평균R."""
    if stats["n_trades"] < MIN_TRADES:
        return -99 + stats["n_trades"] * 0.01
    return stats["avg_R"]


def sweep(data, base_cfg, grid: list[dict], label: str):
    """grid: [{override dict}, ...] → 결과 랭킹."""
    print(f"\n===== {label} ({len(grid)} combos) =====")
    results = []
    for i, over in enumerate(grid, 1):
        cfg = clone(base_cfg, **over)
        st = run_combo(data, cfg)
        results.append((over, st))
        tag = " / ".join(f"{k.split('.')[-1]}={v}" for k, v in over.items())
        print(f"  [{i:>2}/{len(grid)}] {tag:<48} "
              f"n={st['n_trades']:>3} avgR={st['avg_R']:>6.2f} "
              f"WR={st['win_rate']*100:>4.0f}% PF={st['profit_factor']:>5.2f}")
    results.sort(key=lambda r: _objective(r[1]), reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"])
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=2000)  # 짧은 표본 함정 방지
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out", default="tune_report.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.exchange = args.exchange
    cfg.ccxt.limit = args.limit

    print(f"[데이터] {args.exchange} {args.symbols} {args.timeframe} "
          f"{args.limit}봉 로딩...")
    data = {}
    for sym in args.symbols:
        df = datafeed.load_cached(sym, cfg, refresh=args.refresh)
        data[sym] = df
        print(f"  {sym:<10} {len(df)}봉 {df.index[0].date()}~{df.index[-1].date()}")

    # ---------- 0) 베이스라인 ----------
    base = run_combo(data, cfg)
    print("\n########## 베이스라인 (기본 파라미터) ##########")
    print(f"  n={base['n_trades']} avgR={base['avg_R']} "
          f"WR={base['win_rate']*100:.0f}% PF={base['profit_factor']} "
          f"PnL={base['total_pnl']}")
    print("  [셋업별 성과]")
    print(_fmt_setup_table(base["by_setup"]))

    # ---------- 1) 손절/목표 geometry ----------
    grid1 = [{"planner.rr_target": rr, "planner.atr_stop_mult": sm}
             for rr in (1.5, 2.0, 2.5, 3.0) for sm in (1.0, 1.5, 2.0, 2.5)]
    r1 = sweep(data, cfg, grid1, "1단계: 손절폭 × 목표 R:R")
    best1 = r1[0][0]
    print(f"  → 최적 geometry: {best1} (avgR={r1[0][1]['avg_R']})")
    cfg1 = clone(cfg, **best1)

    # ---------- 2) 진입 필터 ----------
    grid2 = [{"signal.min_score_approve": ms,
              "decision.require_trend_alignment": ta}
             for ms in (60, 65, 70, 75) for ta in (False, True)]
    r2 = sweep(data, cfg1, grid2, "2단계: 승인 스코어 × 추세정렬 필터")
    best2 = r2[0][0]
    print(f"  → 최적 필터: {best2} (avgR={r2[0][1]['avg_R']})")
    cfg2 = clone(cfg1, **best2)

    # ---------- 3) 셋업 선택 (손실 셋업 제거) ----------
    cur = run_combo(data, cfg2)
    losing = [name for name, s in cur["by_setup"].items() if s["avg_R"] < 0]
    grid3 = [{}]  # 아무것도 안 끄는 경우 포함
    # 손실 셋업을 나쁜 순으로 누적 제거
    losing_sorted = sorted(losing, key=lambda n: cur["by_setup"][n]["avg_R"])
    for k in range(1, len(losing_sorted) + 1):
        grid3.append({"signal.disabled_setups": tuple(losing_sorted[:k])})
    r3 = sweep(data, cfg2, grid3, "3단계: 손실 셋업 누적 제거")
    best3 = r3[0][0]
    print(f"  → 최적 셋업선택: {best3 or '(제거 없음)'} (avgR={r3[0][1]['avg_R']})")
    cfg_final = clone(cfg2, **best3)

    # ---------- 최종 검증 ----------
    final = run_combo(data, cfg_final)
    print("\n########## 최종 튜닝 결과 ##########")
    all_over = {**best1, **best2, **best3}
    print("  [적용 파라미터]")
    for k, v in all_over.items():
        print(f"    {k} = {v}")
    print(f"\n  베이스라인 → 튜닝:  avgR {base['avg_R']:+.2f} → {final['avg_R']:+.2f}"
          f"  |  WR {base['win_rate']*100:.0f}% → {final['win_rate']*100:.0f}%"
          f"  |  PF {base['profit_factor']} → {final['profit_factor']}"
          f"  |  n {base['n_trades']} → {final['n_trades']}")
    print("  [최종 셋업별 성과]")
    print(_fmt_setup_table(final["by_setup"]))

    # ---------- 저장 ----------
    report = {
        "symbols": args.symbols, "timeframe": args.timeframe,
        "limit": args.limit, "exchange": args.exchange,
        "baseline": base, "final": final, "applied": all_over,
        "stage1_top3": [(o, s["avg_R"]) for o, s in r1[:3]],
        "stage2_top3": [(o, s["avg_R"]) for o, s in r2[:3]],
    }
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"\n[저장] {args.out}")
    _write_tuned_yaml(all_over, cfg_final)


def _write_tuned_yaml(applied: dict, cfg_final):
    """튜닝값을 config.tuned.yaml 조각으로 저장."""
    lines = ["# 튜닝 결과 (tools/tune.py 생성) — config.yaml에 반영하거나 --config로 사용",
             f"symbols: {list(cfg_final.symbols)}",
             f'timeframe: "{cfg_final.timeframe}"',
             "signal:",
             f"  min_score_approve: {cfg_final.signal.min_score_approve}",
             f"  disabled_setups: {list(cfg_final.signal.disabled_setups)}",
             "planner:",
             f"  rr_target: {cfg_final.planner.rr_target}",
             f"  atr_stop_mult: {cfg_final.planner.atr_stop_mult}",
             "decision:",
             f"  require_trend_alignment: {cfg_final.decision.require_trend_alignment}",
             f"  min_rr: {cfg_final.decision.min_rr}"]
    Path("config.tuned.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[저장] config.tuned.yaml")


if __name__ == "__main__":
    main()
