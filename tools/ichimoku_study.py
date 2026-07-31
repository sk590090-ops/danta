#!/usr/bin/env python
"""일목균형표 3조건 검증 — 지침 폴더 마지막 미검증 항목 (bullstory 계열 스레드).

규칙(스레드 명세 그대로, 표준 파라미터 9/26/52, 무튜닝):
  진입(롱): ① 전환선(9)이 기준선(26) 상향 교차
           ② 종가가 구름(선행스팬1·2, 26봉 선행) 위에서 마감
           ③ 후행스팬(현재 종가) > 26봉 전 가격
           — 3조건 동시 충족 시. 하나라도 빠지면 "그냥 반등"으로 간주(스레드 원문).
  청산:   기준선 아래 종가 마감 또는 구름 안 재진입. (+안전장치: 96봉 상한)
  숏은 대칭 규칙으로 병행 검증(스레드는 롱 중심이나 대칭성 확인 가치).

R 정의: 진입 시점 ATR14 기준 (기존 연구들과 통일). 수수료 0.05%×2.
TF: 1h 기본 + 4h 병행. 판정: avgR>0 & PF>1.1 & 전후반 모두 PF>1.0.
사용: python tools/ichimoku_study.py
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
from radar_backtest import fetch_series                # noqa: E402
from radar_entry_study import _stats, _atr14           # noqa: E402
from connors_study import resample_4h                  # noqa: E402

N_SYMBOLS = 30
FEE = 0.0005
HOLD_MAX = 96


def _mid(highs, lows, i, n):
    """직전 n봉(현재 포함) 고저 중간값."""
    hh = max(highs[i - n + 1:i + 1])
    ll = min(lows[i - n + 1:i + 1])
    return (hh + ll) / 2


def ichimoku(highs, lows, i):
    """(전환선, 기준선, 선행스팬A@i, 선행스팬B@i) — 스팬은 26봉 전 계산값."""
    tenkan = _mid(highs, lows, i, 9)
    kijun = _mid(highs, lows, i, 26)
    j = i - 26                       # 구름은 26봉 전 시점 값이 현재 위에 그려짐
    if j < 52:
        return tenkan, kijun, None, None
    span_a = (_mid(highs, lows, j, 9) + _mid(highs, lows, j, 26)) / 2
    span_b = _mid(highs, lows, j, 52)
    return tenkan, kijun, span_a, span_b


def sim(s: dict, side: str, stop_atr: float | None = None):
    """3조건 동시충족 진입 → 규칙 청산(+선택적 재난손절). [(ts, R)] 반환."""
    h, l, c, ts = s["highs"], s["lows"], s["closes"], s["ts"]
    n = len(c)
    sgn = 1 if side == "LONG" else -1
    out = []
    i = 80
    while i < n - 1:
        tk, kj, sa, sb = ichimoku(h, l, i)
        if sa is None:
            i += 1
            continue
        tk_p, kj_p, _, _ = ichimoku(h, l, i - 1)
        cloud_hi, cloud_lo = max(sa, sb), min(sa, sb)
        if side == "LONG":
            cross = tk > kj and tk_p <= kj_p          # ① 전환>기준 교차
            above = c[i] > cloud_hi                    # ② 구름 위 종가
            chikou = c[i] > c[i - 26]                  # ③ 후행스팬 조건
        else:
            cross = tk < kj and tk_p >= kj_p
            above = c[i] < cloud_lo
            chikou = c[i] < c[i - 26]
        if not (cross and above and chikou):
            i += 1
            continue
        entry = c[i]
        atr = _atr14(h, l, c, i)
        if atr <= 0:
            i += 1
            continue
        hard = entry - sgn * (stop_atr * atr) if stop_atr else None
        exit_p, k = None, i
        for k in range(i + 1, min(i + 1 + HOLD_MAX, n)):
            if hard is not None:               # 재난 손절 (지표 청산보다 우선)
                if (side == "LONG" and l[k] <= hard) or                         (side == "SHORT" and h[k] >= hard):
                    exit_p = hard; break
            _, kj_k, sa_k, sb_k = ichimoku(h, l, k)
            if sa_k is None:
                continue
            chi_, clo_ = max(sa_k, sb_k), min(sa_k, sb_k)
            if side == "LONG":
                if c[k] < kj_k or c[k] < chi_:         # 기준선 하회 or 구름 재진입
                    exit_p = c[k]; break
            else:
                if c[k] > kj_k or c[k] > clo_:
                    exit_p = c[k]; break
        if exit_p is None:
            exit_p = c[k]
        pnl_r = (exit_p - entry) * sgn / atr - (entry + exit_p) * FEE / atr
        out.append((ts[i], pnl_r))
        i = k + 1
    return out


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 일목 3조건 검증: {len(syms)}심볼 × ~10개월 · 9/26/52 무튜닝\n")

    buckets = {"1h 롱": [], "1h 숏": [], "4h 롱": [], "4h 숏": []}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        s4 = resample_4h(s)
        buckets["1h 롱"] += sim(s, "LONG")
        buckets["1h 숏"] += sim(s, "SHORT")
        buckets["4h 롱"] += sim(s4, "LONG")
        buckets["4h 숏"] += sim(s4, "SHORT")
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)

    print()
    print(f"\n{'변형':<10}{'n':>6}{'승률':>7}{'avgR':>8}{'PF':>7}{'전반PF':>8}{'후반PF':>8}")
    report = {}
    for name, tr in buckets.items():
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        half = len(vals) // 2
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        report[name] = {"all": st, "first": a, "second": b}
        if st["n"]:
            print(f"{name:<10}{st['n']:>6}{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b.get('pf',0):>8.2f}")

    Path("ichimoku_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] ichimoku_study.json")
    print("판정: avgR>0 & PF>1.1 & 전후반 모두 PF>1.0 이면 페이퍼 셋업 후보.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
