#!/usr/bin/env python
"""OBV_DIV 롱 레짐 필터 검증 (2026-08-06, 실전 원장 롱 7건 -$20.04 복기).

가설: 알트 약세 국면의 OBV 롱(상승 다이버전스)은 떨어지는 칼 잡기다.
레짐 게이트(SMA200 상방일 때만 롱)로 개선되는지 사전 고정 비교.

신호: ptrader.signals.engine._obv_divergence_signal 재현(스윙피벗 3/3,
  2번째 피벗 최근 8봉 내, OBV(base vol) 비교, 반등/하락봉 확인).
  실행 전 소량 구간에서 ptrader 평가와 대조(스모크) — 불일치면 중단.
트레이드: planner 동일 규칙 — 손절 min(스윙저점, 1ATR)/max(스윙고점, 1ATR),
  목표 2R, 48봉, 손절우선, 수수료, 심볼당 순차(중복 미보유).
변형(튜닝 금지):
  A base     : OBV_DIV 롱 전부
  B sym200   : 종가 > 심볼 SMA200(1h) 일 때만 롱
  C btc200   : BTC 종가 > BTC SMA200(1h) 일 때만 롱
  D short    : OBV_DIV 숏 전부 (참고 기준)
판정 게이트: B/C가 A 대비 avgR·PF 모두 개선 + 전/후반 PF 일관 개선 → 채택 후보.
사용: python tools/obv_regime_study.py  (PYTHONIOENCODING=utf-8)
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
from radar_backtest import fetch_klines_long           # noqa: E402
from radar_entry_study import _stats, FEE, HOLD        # noqa: E402

N_SYMBOLS = 30
BARS = 8000
RECENT = 8          # sc.obv_recent_bars
PIV = 3             # sc.swing_left / swing_right
RR = 2.0            # planner.rr_target
STOP_ATR = 1.0      # planner.atr_stop_mult
START = 200         # SMA200 확보 후부터 (전 변형 동일 표본 창)


def fetch_arrays(sym: str, timeout: float = 15):
    """1h klines → (ts, c, h, l, v[base]). engine과 동일한 base volume(k[5])."""
    kl = fetch_klines_long(sym, BARS, timeout)
    if len(kl) < START + 100:
        return None
    return ([int(k[0]) for k in kl], [float(k[4]) for k in kl],
            [float(k[2]) for k in kl], [float(k[3]) for k in kl],
            [float(k[5]) for k in kl])


def wilder_atr(h, l, c, n=14):
    """indicators.atr(ewm alpha=1/14, adjust=False) 재현."""
    out = [h[0] - l[0]]
    for k in range(1, len(c)):
        tr = max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1]))
        out.append(out[-1] + (tr - out[-1]) / n)
    return out


def pivots(h, l):
    """indicators.swing_points(3,3) 재현 — 확정 피벗 인덱스 목록."""
    n = len(h)
    ph, pl = [], []
    for p in range(PIV, n - PIV):
        if h[p] > max(h[p - PIV:p]) and h[p] >= max(h[p + 1:p + 1 + PIV]):
            ph.append(p)
        if l[p] < min(l[p - PIV:p]) and l[p] <= min(l[p + 1:p + 1 + PIV]):
            pl.append(p)
    return ph, pl


def detect(ts, c, h, l, v):
    """engine._obv_divergence_signal 재현. {i: ("LONG"/"SHORT", p2)} 반환.
    i 시점 관측 가능 = 피벗 p ≤ i-3 (우측 3봉 확정)."""
    n = len(c)
    obv = [0.0]
    for k in range(1, n):
        d = c[k] - c[k - 1]
        obv.append(obv[-1] + (1 if d > 0 else -1 if d < 0 else 0) * v[k])
    ph, pl = pivots(h, l)
    sigs = {}
    ih = il = 0
    for i in range(START, n - 1):
        while il < len(pl) and pl[il] <= i - PIV:
            il += 1
        while ih < len(ph) and ph[ih] <= i - PIV:
            ih += 1
        # 강세: 마지막 두 저점 LL + OBV HL + 반등봉 (engine이 롱 먼저 반환)
        if il >= 2:
            p1, p2 = pl[il - 2], pl[il - 1]
            if i - p2 <= RECENT and l[p2] < l[p1] and obv[p2] > obv[p1] \
                    and c[i] > c[i - 1]:
                sigs[i] = ("LONG", p2)
                continue
        # 약세: 마지막 두 고점 HH + OBV LH + 하락봉
        if ih >= 2:
            p1, p2 = ph[ih - 2], ph[ih - 1]
            if i - p2 <= RECENT and h[p2] > h[p1] and obv[p2] < obv[p1] \
                    and c[i] < c[i - 1]:
                sigs[i] = ("SHORT", p2)
    return sigs


def sim(c, h, l, atr, i, long, swing_px):
    """planner 규칙: 손절 min/max(스윙, 1ATR) → 2R → 48봉. (R, 청산봉) 반환."""
    n = len(c)
    entry = c[i]
    a = atr[i]
    if a <= 0:
        return None
    if long:
        stop = min(swing_px, entry - STOP_ATR * a)
    else:
        stop = max(swing_px, entry + STOP_ATR * a)
    dist = abs(entry - stop)
    sgn = 1 if long else -1
    target = entry + sgn * RR * dist
    exit_p, k2 = None, i
    for k2 in range(i + 1, min(i + 1 + HOLD, n)):
        if long:
            if l[k2] <= stop:
                exit_p = stop; break
            if h[k2] >= target:
                exit_p = target; break
        else:
            if h[k2] >= stop:
                exit_p = stop; break
            if l[k2] <= target:
                exit_p = target; break
    if exit_p is None:
        exit_p = c[min(k2, n - 1)]
    return (exit_p - entry) * sgn / dist - (entry + exit_p) * FEE / dist, k2


def smoke(arrays, sigs, sym):
    """재현 신호 vs ptrader 실평가(daily_scan과 동일 cfg·300봉 창) 대조."""
    import pandas as pd
    from ptrader.config import load_config
    from ptrader import scanner
    from ptrader.signals import evaluate

    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = load_config(cfg_path if cfg_path.exists() else None)
    cfg.timeframe = "1h"
    cfg.signal.disabled_setups = tuple(sorted(
        {"BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION",
         "REVERSAL", "CONVERGENCE", "OBV_PRD", "VWAP"}))  # OBV_DIV만

    ts, c, h, l, v = arrays
    n = len(c)
    pos = [i for i in sigs if i >= 500][:25]
    neg = [i for i in range(500, n - 2, max(1, (n - 502) // 75)) if i not in sigs][:75]
    mism = 0
    for i in pos + neg:
        df = pd.DataFrame(
            {"open": [c[k - 1] for k in range(i - 299, i + 1)],
             "high": h[i - 299:i + 1], "low": l[i - 299:i + 1],
             "close": c[i - 299:i + 1], "volume": v[i - 299:i + 1]},
            index=pd.to_datetime(ts[i - 299:i + 1], unit="ms"))
        feats = scanner.scan(df, cfg)
        sig = evaluate(df, feats, cfg)
        got = sig.direction if sig.setup == "OBV_DIV" else None
        exp = sigs[i][0] if i in sigs else None
        if got != exp:
            mism += 1
            print(f"  ✗ {sym} i={i}: 재현={exp} ptrader={got}")
    print(f"  스모크 {sym}: 신호 {len(pos)} + 무신호 {len(neg)} 대조 → "
          f"불일치 {mism}건")
    return mism


def take_sequential(rows):
    """(i, ts, r, exit_i) 목록 → 심볼당 순차(보유 중 신규 무시) 체결."""
    out, free = [], -1
    for i, t, r, ei in rows:
        if i > free:
            out.append((t, r))
            free = ei
    return out


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ OBV_DIV 레짐 필터: {len(syms)}심볼 × ~10개월 · "
          f"planner 동일 규칙(스윙/1ATR·2R·48봉)\n")

    # BTC 레짐 맵 (변형 C)
    btc = fetch_arrays("BTCUSDT")
    if not btc:
        print("BTC 데이터 실패"); return 1
    bts, bc = btc[0], btc[1]
    csum, btc_above = 0.0, {}
    for k, px in enumerate(bc):
        csum += px
        if k >= 200:
            csum -= bc[k - 200]
        if k >= 199:
            btc_above[bts[k]] = px > csum / 200

    # 스모크: 재현 로직 vs ptrader 실평가 (2심볼, 불일치 시 중단)
    print("● 스모크 대조 (재현 vs ptrader.signals)")
    cache = {"BTCUSDT": btc}
    total_mism = 0
    for sym in ("BTCUSDT", "ETHUSDT"):
        arr = cache.get(sym) or fetch_arrays(sym)
        cache[sym] = arr
        total_mism += smoke(arr, detect(*arr), sym)
    if total_mism:
        print("✗ 재현 로직 불일치 — 중단"); return 1
    print("  ✅ 일치 — 본 실험 진행\n")

    variants = {"A base(롱전부)": [], "B sym200(롱)": [],
                "C btc200(롱)": [], "D short(참고)": []}
    for kk, sym in enumerate(syms, 1):
        try:
            arr = cache.get(sym) or fetch_arrays(sym)
        except Exception:
            continue
        if not arr:
            continue
        ts, c, h, l, v = arr
        atr = wilder_atr(h, l, c)
        # 심볼 SMA200 (변형 B)
        sma, csum = [None] * len(c), 0.0
        for k, px in enumerate(c):
            csum += px
            if k >= 200:
                csum -= c[k - 200]
            if k >= 199:
                sma[k] = csum / 200
        rows = {k: [] for k in variants}
        for i, (d, p2) in sorted(detect(ts, c, h, l, v).items()):
            long = d == "LONG"
            swing_px = l[p2] if long else h[p2]
            res = sim(c, h, l, atr, i, long, swing_px)
            if res is None:
                continue
            r, ei = res
            row = (i, ts[i], r, ei)
            if long:
                rows["A base(롱전부)"].append(row)
                if sma[i] is not None and c[i] > sma[i]:
                    rows["B sym200(롱)"].append(row)
                if btc_above.get(ts[i]):
                    rows["C btc200(롱)"].append(row)
            else:
                rows["D short(참고)"].append(row)
        for k in variants:
            variants[k] += take_sequential(rows[k])
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)

    print()
    print(f"\n{'변형':<16}{'n':>6}{'승률':>6}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}  판정")
    report, stats = {}, {}
    for name, tr in variants.items():
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        half = len(vals) // 2
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        stats[name] = (st, a, b)
        report[name] = {"all": st, "first": a, "second": b}
    base = stats["A base(롱전부)"]
    for name, (st, a, b) in stats.items():
        if not st["n"]:
            print(f"{name:<16}{0:>6}  표본 없음")
            continue
        verdict = ""
        if name[0] in "BC" and base[0]["n"]:
            ok = (st["avgR"] > base[0]["avgR"] and st["pf"] > base[0]["pf"]
                  and a.get("pf", 0) > base[1].get("pf", 0)
                  and b.get("pf", 0) > base[2].get("pf", 0))
            verdict = "✅ 채택 후보" if ok else "❌ 미달"
            report[name]["adopt_candidate"] = ok
        print(f"{name:<16}{st['n']:>6}{st['wr']*100:>5.0f}%{st['avgR']:>8.3f}"
              f"{st['pf']:>7.2f}{a.get('pf', 0):>8.2f}{b.get('pf', 0):>8.2f}"
              f"  {verdict}")

    Path("obv_regime_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] obv_regime_study.json")
    print("게이트: B/C가 A 대비 avgR·PF 모두 개선 + 전/후반 PF 일관 개선일 때만 채택 후보.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
