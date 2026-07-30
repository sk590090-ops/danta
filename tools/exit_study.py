#!/usr/bin/env python
"""출구(청산) 설계 비교 — 브래킷 트레이드에 4가지 청산 방식을 적용해 실측.

배경: 현행은 '2R 전량 익절'. SKHYNIX처럼 익절 후 추세가 이어지는 비용과,
+1R 갔다가 손절로 되돌아오는 비용 중 무엇이 큰지는 데이터만 안다.

  A fixed2R : 손절 1ATR / 목표 2R 전량 (현행 기준선)
  B partial : 2R에서 절반 익절 → 손절 본절 이동 → 나머지 최근 3봉 저/고가 트레일
  C be1R    : +1R 도달 시 손절을 본절로 이동, 목표 2R 유지
  D 시간청산 : A에서 최대보유 24/48/96봉 비교

전조·브래킷 진입은 radar_entry_study와 동일(점화+펀딩 → ±0.5ATR 브래킷).
수수료 0.05%×2, 부분청산은 가중 R로 합산. 전·후반 반분 병기.
사용: python tools/exit_study.py
"""
from __future__ import annotations

import json
import sys
import time
from bisect import bisect_right
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from daily_scan import FAPI, _get, zscore                     # noqa: E402
from radar_backtest import fetch_series, HIST                 # noqa: E402
from radar_entry_study import _atr14, _stats, FEE, BRACKET_ATR, \
    BRACKET_WINDOW                                            # noqa: E402

N_SYMBOLS = 30


def bracket_entries(s: dict):
    """전조→브래킷 진입 지점만 추출: (진입idx, long, entry, atr)."""
    ts, closes, highs, lows, vols = (s["ts"], s["closes"], s["highs"],
                                     s["lows"], s["vols"])
    f_times = [t for t, _ in s["fund"]]
    f_vals = [v for _, v in s["fund"]]
    n = len(ts)
    out = []
    i = HIST
    while i < n - 2:
        j = bisect_right(f_times, ts[i]) - 1
        fr = f_vals[j] if j >= 0 else None
        if fr is None or abs(fr) < 0.0005 or \
                zscore(vols[i], vols[max(0, i - 100):i]) < 3.0:
            i += 1
            continue
        atr = _atr14(highs, lows, closes, i)
        if atr <= 0:
            i += 1
            continue
        up_t = closes[i] + BRACKET_ATR * atr
        dn_t = closes[i] - BRACKET_ATR * atr
        hit = None
        for w in range(i + 1, min(i + 1 + BRACKET_WINDOW, n)):
            hu, hd = highs[w] >= up_t, lows[w] <= dn_t
            if hu and hd:
                break
            if hu:
                hit = (w, True, up_t); break
            if hd:
                hit = (w, False, dn_t); break
        if hit is None:
            i += 1
            continue
        k, long, entry = hit
        out.append((k, long, entry, atr))
        i = k + 24              # 재탐색 간격(변형 간 동일 조건 유지, 보유와 무관)
    return out


def sim_exit(s, k, long, entry, atr, mode, hold_max=48):
    """한 트레이드를 mode 규칙으로 청산해 R 반환 (부분청산은 가중 합)."""
    highs, lows, closes = s["highs"], s["lows"], s["closes"]
    n = len(closes)
    sgn = 1 if long else -1
    stop = entry - sgn * atr
    target = entry + sgn * 2 * atr
    be_armed = False            # C: 본절 이동됨
    runner = 1.0                # 남은 물량 비율
    banked_r = 0.0              # 부분익절로 확정한 R
    trail_on = False            # B: 트레일 단계
    end = min(k + 1 + hold_max, n)
    for w in range(k + 1, end):
        hi, lo = highs[w], lows[w]
        # 트레일 갱신 (B, 2R 이후): 최근 3봉 반대편 극값
        if trail_on:
            if long:
                stop = max(stop, min(lows[max(k + 1, w - 3):w] or [stop]))
            else:
                stop = min(stop, max(highs[max(k + 1, w - 3):w] or [stop]))
        # 손절/트레일 터치 (보수: 동시면 손절 우선)
        if (long and lo <= stop) or (not long and hi >= stop):
            exit_r = (stop - entry) * sgn / atr
            return banked_r + runner * exit_r - _fee_r(entry, stop, atr)
        # 목표/부분익절
        if (long and hi >= target) or (not long and lo <= target):
            if mode == "A" or mode == "D":
                return 2.0 * runner + banked_r - _fee_r(entry, target, atr)
            if mode == "B" and not trail_on:
                banked_r += 0.5 * 2.0          # 절반을 2R에 확정
                runner = 0.5
                stop = entry                    # 본절
                trail_on = True
                continue
            if mode == "C":
                return 2.0 * runner + banked_r - _fee_r(entry, target, atr)
        # C: +1R 도달 시 본절 이동
        if mode == "C" and not be_armed:
            r1 = entry + sgn * atr
            if (long and hi >= r1) or (not long and lo <= r1):
                stop = entry
                be_armed = True
    # 시간청산
    exit_p = closes[min(end, n) - 1]
    exit_r = (exit_p - entry) * sgn / atr
    return banked_r + runner * exit_r - _fee_r(entry, exit_p, atr)


def _fee_r(entry, exit_p, atr):
    return (entry + exit_p) * FEE / atr


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 출구 설계 비교: {len(syms)}심볼 × ~10개월 브래킷 진입 고정\n")

    series = {}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if s:
            series[sym] = s
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)
    print(f"\n  {len(series)}심볼 확보\n")

    variants = [("A 고정2R(현행)", "A", 48), ("B 절반+트레일", "B", 48),
                ("C +1R본절", "C", 48), ("D 24h청산", "D", 24),
                ("D 96h청산", "D", 96)]
    print(f"{'방식':<14}{'n':>5}{'승률':>7}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}")
    report = {}
    for name, mode, hold in variants:
        rs = []
        for sym, s in series.items():
            for k, long, entry, atr in bracket_entries(s):
                rs.append((s["ts"][k], sim_exit(s, k, long, entry, atr,
                                                mode, hold)))
        rs.sort(key=lambda x: x[0])
        vals = [r for _, r in rs]
        half = len(vals) // 2
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        report[name] = {"all": st, "first": a, "second": b}
        if st["n"]:
            print(f"{name:<14}{st['n']:>5}{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b.get('pf',0):>8.2f}")

    Path("exit_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] exit_study.json")
    print("판정: 현행(A) 대비 avgR·PF가 전후반 일관되게 좋아야만 교체.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
