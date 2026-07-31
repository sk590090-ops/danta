#!/usr/bin/env python
"""휩쏘(초피니스) 필터 연구 — 6-Why 근본원인 가설의 정면 검증.

가설(2026-07-31 실전 진단): 브래킷 손실은 휩쏘 국면(방향 없이 위아래로 털리는
장세)에 몰린다. 진입 시점에 국면을 재면 나쁜 진입을 걸러낼 수 있다.

지표: Choppiness Index(24봉) = 100·log10(ΣTR / (최고-최저)) / log10(24)
  높음(→100) = 횡보/휩쏘 · 낮음(→0) = 추세. 표준 지표, 튜닝 없음.

측정:
  ① 브래킷 트레이드를 진입 시점 CI 사분위로 나눠 성과 비교 (예측력 확인)
  ② CI 상위 컷(61.8 고전 임계 등) 필터 적용 시 전체 성과 변화 (전/후반 병기)
판정: 최상위 CI 분위 PF가 명확히 나쁘고, 필터 적용이 전후반 일관 개선일 때만 채택.
사용: python tools/chop_study.py
"""
from __future__ import annotations

import json
import math
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
from exit_study import bracket_entries, sim_exit       # noqa: E402
from radar_entry_study import _stats                   # noqa: E402

N_SYMBOLS = 30
CI_LEN = 24


def chop_index(highs, lows, closes, i, n=CI_LEN) -> float | None:
    """진입봉 직전 n봉의 Choppiness Index (0~100)."""
    if i < n + 1:
        return None
    tr_sum = 0.0
    hh, ll = -1e18, 1e18
    for k in range(i - n, i):
        tr_sum += max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]),
                      abs(lows[k] - closes[k - 1]))
        hh = max(hh, highs[k])
        ll = min(ll, lows[k])
    rng = hh - ll
    if rng <= 0 or tr_sum <= 0:
        return None
    return 100.0 * math.log10(tr_sum / rng) / math.log10(n)


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 휩쏘 필터 연구: {len(syms)}심볼 × ~10개월 · CI({CI_LEN}봉)\n")

    rows = []            # (ts, R, ci)
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        for k, long, entry, atr in bracket_entries(s):
            ci = chop_index(s["highs"], s["lows"], s["closes"], k)
            if ci is None:
                continue
            r = sim_exit(s, k, long, entry, atr, "A")
            rows.append((s["ts"][k], r, ci))
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)
    rows.sort(key=lambda x: x[0])
    n = len(rows)
    print(f"\n  브래킷 트레이드 {n}건 (CI 계산 가능분)\n")

    # ① CI 사분위별 성과
    by_ci = sorted(rows, key=lambda x: x[2])
    q = n // 4
    print("═══ ① 진입 시점 CI 사분위별 성과 ═══")
    print(f"{'분위':<16}{'CI범위':>14}{'n':>5}{'승률':>7}{'avgR':>8}{'PF':>7}")
    report = {"quartiles": []}
    for qi in range(4):
        grp = by_ci[qi * q:(qi + 1) * q] if qi < 3 else by_ci[3 * q:]
        vals = [r for _, r, _ in grp]
        st = _stats(vals)
        lo, hi = grp[0][2], grp[-1][2]
        label = ["Q1(추세↑)", "Q2", "Q3", "Q4(휩쏘↑)"][qi]
        report["quartiles"].append({"q": label, "ci": [round(lo, 1), round(hi, 1)], **st})
        print(f"{label:<16}{f'{lo:.0f}~{hi:.0f}':>14}{st['n']:>5}"
              f"{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}{st['pf']:>7.2f}")

    # ② CI 상한 필터 스윕
    print("\n═══ ② CI 상한 필터 (초과 시 진입 스킵) ═══")
    print(f"{'필터':<14}{'n':>5}{'승률':>7}{'avgR':>8}{'PF':>7}{'전반PF':>8}{'후반PF':>8}")
    for cut in (None, 65.0, 61.8, 58.0, 55.0):
        kept = rows if cut is None else [x for x in rows if x[2] <= cut]
        vals = [r for _, r, _ in kept]
        half = len(vals) // 2
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        name = "없음(기준)" if cut is None else f"CI≤{cut}"
        report[name] = {"all": st, "first": a, "second": b}
        if st["n"]:
            print(f"{name:<14}{st['n']:>5}{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b.get('pf',0):>8.2f}")

    Path("chop_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] chop_study.json")
    print("판정: Q4가 명확히 열등 + 필터가 전후반 일관 개선일 때만 채택.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
