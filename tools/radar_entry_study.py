#!/usr/bin/env python
"""전조(점화+펀딩) → 진입 방향규칙 연구 — 4규칙 트레이드 시뮬레이션.

질문: 전조 알림(점화+펀딩 동시)이 떴을 때 언제/어느 방향으로 진입해야 하나?
방향 후보 4규칙(모두 전조 시점에 관측 가능한 것만 사용, 룩어헤드 차단):
  A momo   : 최근 24h 수익률 방향 추종 (오르던 놈 롱 / 내리던 놈 숏)
  B contra : 펀딩 역방향 (펀딩+ = 롱 과밀 → 숏 / 펀딩- → 롱) — 스퀴즈 논리
  C with   : 펀딩 순방향 (과밀쪽 지속 베팅)
  D obv    : OBV 20봉 기울기 방향 (돈이 들어오면 롱 / 빠지면 숏)

트레이드 모델(자동매매 엔진과 동일): 다음봉 진입, 손절 1×ATR14, 목표 2R,
최대 48봉, 동시봉 손절우선(보수), 수수료 0.05%×2. 심볼당 순차(중복 미보유).
견고성: 전·후반 반분 성적 병기. 사용: python tools/radar_entry_study.py
"""
from __future__ import annotations

import argparse
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

from daily_scan import FAPI, _get, zscore  # noqa: E402
from radar_backtest import fetch_series, HIST  # noqa: E402

FEE = 0.0005
HOLD = 48
BRACKET_ATR = 0.5           # 브래킷 트리거 폭 (±0.5ATR)
BRACKET_WINDOW = 6          # 트리거 유효시간(봉)
RULES = ("momo", "contra", "with", "obv", "bracket")


def _atr14(highs, lows, closes, i):
    trs = []
    for k in range(i - 13, i + 1):
        trs.append(max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]),
                       abs(lows[k] - closes[k - 1])))
    return sum(trs) / 14


def _obv_slope(closes, vols, i):
    s = 0.0
    for k in range(i - 19, i + 1):
        d = closes[k] - closes[k - 1]
        s += (1 if d > 0 else -1 if d < 0 else 0) * vols[k]
    return s


def sim_symbol(s: dict, rule: str, window: int = BRACKET_WINDOW):
    """한 심볼에서 규칙별 트레이드 목록 [R배수]."""
    ts, closes, highs, lows, vols = (s["ts"], s["closes"], s["highs"],
                                     s["lows"], s["vols"])
    f_times = [t for t, _ in s["fund"]]
    f_vals = [v for _, v in s["fund"]]
    n = len(ts)
    out = []
    i = HIST
    while i < n - 2:
        # 전조: 점화(직전 1h 거래량 z≥3) + 펀딩 극단(|fr|≥5bp)
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
        if rule == "bracket":
            # 방향 무예측: ±0.5ATR 트리거, 먼저 뚫리는 쪽으로 진입.
            up_t = closes[i] + BRACKET_ATR * atr
            dn_t = closes[i] - BRACKET_ATR * atr
            entry = long = None
            for w in range(i + 1, min(i + 1 + window, n)):
                hit_up = highs[w] >= up_t
                hit_dn = lows[w] <= dn_t
                if hit_up and hit_dn:          # 같은 봉 양쪽 관통 = 모호 → 포기
                    break
                if hit_up:
                    entry, long, i = up_t, True, w
                    break
                if hit_dn:
                    entry, long, i = dn_t, False, w
                    break
            if entry is None:
                i += 1
                continue
        else:
            mom = closes[i] / closes[i - 24] - 1
            if rule == "momo":
                long = mom > 0
            elif rule == "contra":
                long = fr < 0
            elif rule == "with":
                long = fr > 0
            else:                              # obv
                long = _obv_slope(closes, vols, i) > 0
            entry = closes[i]                  # 다음봉 시가 근사
        stop = entry - atr if long else entry + atr
        target = entry + 2 * atr if long else entry - 2 * atr
        exit_p, k = None, i
        for k in range(i + 1, min(i + 1 + HOLD, n)):
            if long:
                if lows[k] <= stop:
                    exit_p = stop; break
                if highs[k] >= target:
                    exit_p = target; break
            else:
                if highs[k] >= stop:
                    exit_p = stop; break
                if lows[k] <= target:
                    exit_p = target; break
        if exit_p is None:
            exit_p = closes[k]
        sgn = 1 if long else -1
        gross_r = (exit_p - entry) * sgn / atr
        fee_r = (entry + exit_p) * FEE / atr
        # (진입ts, R, 청산ts, 방향, entry/atr) — 뒤 3개는 포트폴리오 시뮬용
        #   entry/atr = 리스크 1단위당 명목가 배수 → 레버리지 계산에 필요
        out.append((ts[i], gross_r - fee_r, ts[k], "LONG" if long else "SHORT",
                    entry / atr))
        i = k + 1                              # 청산 후 재탐색
    return out


def _stats(rs):
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    gw = sum(wins)
    gl = -sum(r for r in rs if r <= 0)
    return {"n": len(rs), "wr": round(len(wins) / len(rs), 3),
            "avgR": round(sum(rs) / len(rs), 3),
            "pf": round(gw / (gl + 1e-9), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", default="radar_entry_study.json")
    args = ap.parse_args()

    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", args.timeout)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:args.symbols]]
    print(f"▶ 전조 진입규칙 연구: {len(syms)}심볼 × ~10개월 · 4규칙 시뮬")

    trades = {r: [] for r in RULES}
    for k, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, args.timeout, bars=8000, with_oi_taker=False)
        except Exception as e:
            print(f"  {sym} 실패 {type(e).__name__}")
            continue
        if s is None:
            continue
        got = 0
        for rule in RULES:
            tr = sim_symbol(s, rule)
            trades[rule] += tr
            got = len(tr)
        print(f"  [{k}/{len(syms)}] {sym}: 전조 트레이드 {got}건")
        time.sleep(0.1)

    report = {}
    print(f"\n{'규칙':<8}{'n':>6}{'승률':>8}{'avgR':>8}{'PF':>7}"
          f"{'전반PF':>8}{'후반PF':>8}")
    for rule in RULES:
        rs = sorted(trades[rule])               # 정렬 X — 시간순 유지 필요
        rs = trades[rule]
        rs.sort(key=lambda x: x[0])
        vals = [t[1] for t in rs]
        half = len(vals) // 2
        st = _stats(vals)
        a, b = _stats(vals[:half]), _stats(vals[half:])
        report[rule] = {"all": st, "first_half": a, "second_half": b}
        if st["n"]:
            print(f"{rule:<8}{st['n']:>6}{st['wr']*100:>7.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b.get('pf',0):>8.2f}")

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n[저장] {args.out}")
    print("판정 기준: avgR>0 & PF>1.1 & 전·후반 모두 PF>1.0 인 규칙만 채택")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
