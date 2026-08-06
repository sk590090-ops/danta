#!/usr/bin/env python
"""
FASTWF — OBV_DIV 워크포워드 벡터화 가속판 (VectorBT 방식, 새 의존성 없음).

obvwf.py와 폴드/그리드/판정 로직 동일. 차이는 백테스트 코어 하나:
원본은 봉마다 전체 파이프라인 재계산(O(n²)), 여기는 신호·지표를 슬라이스당
1회 전역 벡터화(O(n)) 후 얇은 순차 루프로 체결만 재현한다.
OBV_DIV 단독 경로 전용 — 다른 셋업은 원본 obvwf.py를 쓴다.

  python tools/fastwf.py --verify          # 원본 backtest.run과 트레이드 일치 검증
  python tools/fastwf.py --timeframe 1h --limit 5000   # WFA 본실행 (obvwf와 동일 CLI)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from ptrader.config import load_config
from ptrader import datafeed, backtest, indicators as ind
from tune import clone, _pool_stats

SETUPS = {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION",
          "REVERSAL", "CONVERGENCE", "OBV_DIV", "OBV_PRD", "VWAP"}
GRID = [{"planner.rr_target": rr, "planner.atr_stop_mult": sm,
         "signal.min_score_approve": ms}
        for rr in (2.0, 2.5) for sm in (1.0, 1.5) for ms in (60, 70)]
# --wide: OBV_DIV에서 ms는 무의미(점수가 70 아니면 50)해서 rr×sm만 확장 (20콤보)
WIDE_GRID = [{"planner.rr_target": rr, "planner.atr_stop_mult": sm,
              "signal.min_score_approve": 60}
             for rr in (1.5, 2.0, 2.5, 3.0) for sm in (0.75, 1.0, 1.25, 1.5, 2.0)]
FIXED = {"planner.rr_target": 2.0, "planner.atr_stop_mult": 1.0,
         "signal.min_score_approve": 60}
MIN_IS_TRADES = 10
WARMUP = 300


# ── 슬라이스당 1회: 파라미터 무관 배열 전부 벡터화 ─────────────────────────
def precompute(df, sc):
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    n = len(c)
    atr = ind.atr(df, sc.atr_period).to_numpy(float)
    obv = np.cumsum(np.sign(np.diff(c, prepend=c[:1])) * v)

    # cross_state 벡터화 (indicators.cross_state와 동일 규칙)
    f5 = df["close"].rolling(sc.fast_ma, min_periods=sc.fast_ma).mean().to_numpy(float)
    s20 = df["close"].rolling(sc.slow_ma, min_periods=sc.slow_ma).mean().to_numpy(float)
    t200 = df["close"].rolling(sc.trend_ma, min_periods=sc.trend_ma).mean().to_numpy(float)
    ii = np.arange(n)
    tslope = t200 - t200[np.maximum(ii - sc.trend_ma // 4, 0)]
    sslope = s20 - s20[np.maximum(ii - (sc.slow_ma - 1), 0)]
    slope = np.where(~np.isnan(t200), tslope, sslope)
    invalid = np.isnan(f5) | np.isnan(s20)
    allow_long = ~(slope < 0) & ~invalid
    allow_short = ~(slope > 0) & ~invalid

    # 스윙 피벗 (indicators.swing_points와 동일). 봉 i에서 보이는 피벗 = 위치 ≤ i-right
    left, right = sc.swing_left, sc.swing_right
    hs, ls_ = df["high"], df["low"]
    is_high = ((hs > hs.rolling(left).max().shift(1))
               & (hs >= hs.rolling(right).max().shift(-right))).fillna(False).to_numpy()
    is_low = ((ls_ < ls_.rolling(left).min().shift(1))
              & (ls_ <= ls_.rolling(right).min().shift(-right))).fillna(False).to_numpy()
    hp, lp = np.flatnonzero(is_high), np.flatnonzero(is_low)
    kh = np.searchsorted(hp, ii - right, side="right")
    kl = np.searchsorted(lp, ii - right, side="right")

    def _pair_sig(piv, series, cmp2_lt_1, obv_gt):
        """최근 2피벗 다이버전스 마스크 + 마지막 피벗 가격(손절 참조)."""
        k = np.searchsorted(piv, ii - right, side="right")
        last_px = np.full(n, np.nan)
        if len(piv):
            last_px[k >= 1] = series[piv[np.clip(k - 1, 0, len(piv) - 1)]][k >= 1]
        if len(piv) < 2:
            return np.zeros(n, bool), last_px
        p2 = piv[np.clip(k - 1, 0, len(piv) - 1)]
        p1 = piv[np.clip(k - 2, 0, len(piv) - 1)]
        pair = (k >= 2) & (ii - p2 <= sc.obv_recent_bars)
        v2, v1 = series[p2], series[p1]
        div = (v2 < v1) if cmp2_lt_1 else (v2 > v1)
        odiv = (obv[p2] > obv[p1]) if obv_gt else (obv[p2] < obv[p1])
        return pair & div & odiv, last_px

    close_up = np.zeros(n, bool)
    close_up[1:] = c[1:] > c[:-1]
    close_dn = np.zeros(n, bool)
    close_dn[1:] = c[1:] < c[:-1]
    min_len = ii + 1 >= 60                      # engine: len(df) < 60 → None

    bull, low_last = _pair_sig(lp, l, True, True)
    bear, high_last = _pair_sig(hp, h, False, False)
    long_raw = bull & close_up & min_len
    short_raw = bear & close_dn & min_len & ~long_raw   # engine은 bull 우선 반환

    with np.errstate(invalid="ignore"):
        vol_ok = (atr / c >= 0.002) & (atr / c <= 0.08)   # risk 4) VOLATILITY
    return {"c": c, "h": h, "l": l, "atr": atr, "n": n,
            "allow_long": allow_long, "allow_short": allow_short,
            "long_raw": long_raw, "short_raw": short_raw,
            "low_last": low_last, "high_last": high_last, "vol_ok": vol_ok}


# ── 콤보당: 손절/목표/승인 마스크 벡터화 + 얇은 체결 루프 ─────────────────
def simulate(P, cfg, start, symbol, hold_max=48, fee=0.0004, warmup=210):
    c, h, l, atr, n = P["c"], P["h"], P["l"], P["atr"], P["n"]
    sm = cfg.planner.atr_stop_mult
    rr = cfg.planner.rr_target
    ms = cfg.signal.min_score_approve
    r = cfg.risk

    swingL = np.where(np.isnan(P["low_last"]), c - 1.5 * atr, P["low_last"])
    stopL = np.minimum(swingL, c - sm * atr)
    tgtL = c + rr * (c - stopL)
    swingS = np.where(np.isnan(P["high_last"]), c + 1.5 * atr, P["high_last"])
    stopS = np.maximum(swingS, c + sm * atr)
    tgtS = c - rr * (stopS - c)
    scL = 70 - 20 * (~P["allow_long"])
    scS = 70 - 20 * (~P["allow_short"])
    long_ok = P["long_raw"] & (scL >= ms)
    short_ok = P["short_raw"] & (scS >= ms)

    eq = cfg.equity
    peak = eq
    trades = []
    i = max(warmup, start)
    while i < n - 1:
        d = 1 if long_ok[i] else (-1 if short_ok[i] else 0)
        if d == 0 or not P["vol_ok"][i] or 1 - eq / peak > r.max_drawdown:
            i += 1
            continue
        entry = c[i]
        stop = stopL[i] if d > 0 else stopS[i]
        target = tgtL[i] if d > 0 else tgtS[i]
        sd = (entry - stop) * d
        qty = eq * r.risk_per_trade / sd
        if qty * entry > eq * r.max_position_pct:
            qty = eq * r.max_position_pct / entry
        exit_p, reason, j = None, "TIME", i
        for j in range(i + 1, min(i + 1 + hold_max, n)):
            if d > 0:
                if l[j] <= stop:
                    exit_p, reason = stop, "STOP"; break
                if h[j] >= target:
                    exit_p, reason = target, "TARGET"; break
            else:
                if h[j] >= stop:
                    exit_p, reason = stop, "STOP"; break
                if l[j] <= target:
                    exit_p, reason = target, "TARGET"; break
        if exit_p is None:
            exit_p = c[j]
        pnl = (exit_p - entry) * d * qty - (entry + exit_p) * qty * fee
        eq += pnl
        peak = max(peak, eq)
        trades.append({"entry_i": i, "exit_i": j, "setup": "OBV_DIV",
                       "direction": "LONG" if d > 0 else "SHORT",
                       "entry": round(entry, 2), "exit": round(exit_p, 2),
                       "reason": reason, "pnl": round(pnl, 2),
                       "equity": round(eq, 2),
                       "score": int(scL[i] if d > 0 else scS[i]),
                       "symbol": symbol})
        i = j + 1
    return trades


_pre_cache: dict = {}


def run_slice(data, cfg, a, b, start_off):
    trades = []
    for sym, df in data.items():
        key = (sym, a, b)
        if key not in _pre_cache:
            _pre_cache[key] = precompute(df.iloc[a:b], cfg.signal)
        trades += simulate(_pre_cache[key], cfg, start_off, sym)
    return trades


def orig_run_slice(data, cfg, a, b, start_off):
    """검증용 — 원본 파이프라인 (obvwf.run_slice와 동일)."""
    trades = []
    for sym, df in data.items():
        res = backtest.run(df.iloc[a:b], cfg, symbol=sym, start_idx=start_off)
        for t in res.trades:
            t["symbol"] = sym
            trades.append(t)
    return trades


def verify(data, cfg, base, a, b):
    """전 그리드 콤보에서 원본과 트레이드 단위 일치 확인 + 속도 비교."""
    print(f"[VERIFY] 슬라이스 [{a}:{b}] · {len(data)}심볼 · {len(GRID)+1}콤보")
    t_orig = t_fast = 0.0
    for over in GRID + [FIXED]:
        c = clone(cfg, **{**base, **over})
        t0 = time.perf_counter()
        o = orig_run_slice(data, c, a, b, WARMUP)
        t1 = time.perf_counter()
        f = run_slice(data, c, a, b, WARMUP)
        t2 = time.perf_counter()
        t_orig += t1 - t0
        t_fast += t2 - t1
        keyf = lambda t: (t["symbol"], t["entry_i"], t["exit_i"], t["direction"],
                          t["entry"], t["exit"], t["reason"], t["pnl"])
        so, sf = sorted(map(keyf, o)), sorted(map(keyf, f))
        assert so == sf, (f"불일치 rr={over['planner.rr_target']} "
                          f"sm={over['planner.atr_stop_mult']} "
                          f"ms={over['signal.min_score_approve']}: "
                          f"orig {len(o)}건 vs fast {len(f)}건\n"
                          f"orig-only: {[x for x in so if x not in sf][:3]}\n"
                          f"fast-only: {[x for x in sf if x not in so][:3]}")
        print(f"  ✓ rr={over['planner.rr_target']} sm={over['planner.atr_stop_mult']} "
              f"ms={over['signal.min_score_approve']} — {len(o)}건 일치")
    print(f"[VERIFY] ✅ 전 콤보 일치 · 원본 {t_orig:.1f}s → 벡터화 {t_fast:.2f}s "
          f"(x{t_orig / max(t_fast, 1e-9):.0f} 가속)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--is-len", type=int, default=1000)
    ap.add_argument("--oos-len", type=int, default=500)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="fastwf_report.json")
    ap.add_argument("--verify", action="store_true",
                    help="첫 IS 슬라이스에서 원본 backtest.run과 일치 검증만 실행")
    ap.add_argument("--wide", action="store_true",
                    help="확장 그리드(rr 4 × sm 5 = 20콤보)로 IS 탐색")
    args = ap.parse_args()
    grid = WIDE_GRID if args.wide else GRID
    only = tuple(sorted(SETUPS - {"OBV_DIV"}))

    cfg = load_config(args.config)
    cfg.data_source = "ccxt"
    cfg.timeframe = args.timeframe
    cfg.ccxt.limit = args.limit
    risk_amt = cfg.equity * cfg.risk.risk_per_trade
    base = {"signal.disabled_setups": only}

    data = {s: datafeed.load_cached(s, cfg) for s in args.symbols}
    n = min(len(df) for df in data.values())
    print(f"[{args.timeframe}] {len(data)}심볼 {n}봉 · "
          f"IS={args.is_len}/OOS={args.oos_len} · OBV_DIV-only FASTWF")

    if args.verify:
        verify(data, cfg, base, 0, min(args.is_len, n))
        return

    t0 = time.perf_counter()
    folds, wfa_pool, fix_pool = [], [], []
    a, fi = 0, 0
    while a + args.is_len + args.oos_len <= n:
        fi += 1
        is_b = a + args.is_len
        oos_b = is_b + args.oos_len
        best = None
        for over in grid:
            c = clone(cfg, **{**base, **over})
            st = _pool_stats(run_slice(data, c, a, is_b, WARMUP), risk_amt)
            score = st["avg_R"] if st["n_trades"] >= MIN_IS_TRADES \
                else -9 + st["n_trades"] * 0.001
            if best is None or score > best[0]:
                best = (score, over, st)
        oa = is_b - WARMUP
        wfa = run_slice(data, clone(cfg, **{**base, **best[1]}), oa, oos_b, WARMUP)
        fix = run_slice(data, clone(cfg, **{**base, **FIXED}), oa, oos_b, WARMUP)
        wfa_pool += wfa
        fix_pool += fix
        ws, fs = _pool_stats(wfa, risk_amt), _pool_stats(fix, risk_amt)
        p = best[1]
        folds.append({"fold": fi, "chosen": p, "is_avgR": best[2]["avg_R"],
                      "wfa": ws, "fix": fs})
        print(f"  f{fi} IS[{a}:{is_b}] rr={p['planner.rr_target']} "
              f"sm={p['planner.atr_stop_mult']} ms={p['signal.min_score_approve']} "
              f"(IS avgR {best[2]['avg_R']:+.2f}) → OOS "
              f"n={ws['n_trades']} avgR={ws['avg_R']:+.3f} PF={ws['profit_factor']}"
              f"  | FIX n={fs['n_trades']} avgR={fs['avg_R']:+.3f}")
        a += args.oos_len

    W = _pool_stats(wfa_pool, risk_amt)
    F = _pool_stats(fix_pool, risk_amt)
    pos = sum(1 for f in folds if f["wfa"]["avg_R"] > 0)
    print(f"\n########## OOS 종합 ({len(folds)}폴드 · {time.perf_counter()-t0:.1f}s) ##########")
    print(f"  WFA(적응형): n={W['n_trades']} WR={W['win_rate']*100:.0f}% "
          f"avgR={W['avg_R']:+.3f} PF={W['profit_factor']} PnL={W['total_pnl']}")
    print(f"  FIXED     : n={F['n_trades']} WR={F['win_rate']*100:.0f}% "
          f"avgR={F['avg_R']:+.3f} PF={F['profit_factor']} PnL={F['total_pnl']}")
    print(f"  OOS 수익 폴드: {pos}/{len(folds)}")
    ok = W["avg_R"] > 0 and W["profit_factor"] > 1.1 and pos >= (len(folds) + 1) // 2
    print(f"  판정: {'✅ 워크포워드 통과' if ok else '❌ 확증 실패 — 과최적화/불안정'}")

    Path(args.out).write_text(json.dumps(
        {"folds": folds, "wfa": W, "fixed": F}, ensure_ascii=False,
        indent=2, default=str), encoding="utf-8")
    print(f"[저장] {args.out}")


if __name__ == "__main__":
    main()
