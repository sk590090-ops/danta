#!/usr/bin/env python
"""6-way 전조 레이더 과거 검증 — 관측별 '폭발 선행' 예측력(lift) 측정.

방법:
  - 심볼별 최근 ~30일 1h 데이터(가격·거래량·OI·테이커·펀딩)를 시간 정렬.
  - 매 시점 t마다 (t까지의 데이터만으로) 6-way 플래그 계산  → 룩어헤드 차단.
  - 라벨: 이후 24시간 내 최대 변동(고가 상방/저가 하방) ≥ 8% = '폭발'.
  - 산출: 기저확률 대비 플래그별·점수별 적중률과 lift.

한계(정직): OI·테이커 이력은 바이낸스가 최근 ~30일만 제공 → 검증 창이 짧고
한 달짜리 시장 국면에 종속된다. lift는 '이번 달 기준' 캘리브레이션이지
영구 진리가 아니다. 유니버스는 '현재' 상위 거래대금(생존편향 소지).

사용: python tools/radar_backtest.py [--symbols 30] [--boom-pct 8] [--out radar_bt.json]
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

from daily_scan import FAPI, _get, _radar_eval, _FLAG_KR  # noqa: E402

H = 3600_000
FWD = 24            # 라벨 창(시간)
HIST = 121          # 플래그 계산에 필요한 과거 봉수


def fetch_klines_long(sym: str, bars: int, timeout: float) -> list:
    """endTime 페이지네이션으로 1h 봉 bars개 확보 (신규상장은 있는 만큼)."""
    out: list = []
    end = None
    while len(out) < bars:
        url = (f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1h&limit=1000"
               + (f"&endTime={end}" if end else ""))
        rows = _get(url, timeout)
        if not rows:
            break
        out = rows + out
        end = int(rows[0][0]) - 1
        if len(rows) < 1000:
            break
        time.sleep(0.05)
    return out[-bars:]


def fetch_series(sym: str, timeout: float, bars: int = 1000,
                 with_oi_taker: bool = True):
    """1h 정렬 시계열 dict 반환. 실패 시 None.

    bars>1000이면 페이지네이션. with_oi_taker=False면 OI·테이커 생략
    (30일 제공 한계를 넘는 장기 검증용 — 연료·수급 관측은 평가 불가)."""
    kl = fetch_klines_long(sym, bars, timeout) if bars > 1000 else \
        _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1h&limit=1000",
             timeout)
    if len(kl) < HIST + FWD + 50:
        return None
    ts = [int(k[0]) for k in kl]
    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    vols = [float(k[7]) for k in kl]

    oi_map: dict[int, float] = {}
    tk_map: dict[int, float] = {}
    if with_oi_taker:
        end = None
        for _ in range(2):                   # 500×2 ≈ 30일 상한
            url = (f"{FAPI}/futures/data/openInterestHist?symbol={sym}"
                   f"&period=1h&limit=500" + (f"&endTime={end}" if end else ""))
            rows = _get(url, timeout)
            if not rows:
                break
            for r in rows:
                oi_map[int(r["timestamp"])] = float(r["sumOpenInterestValue"])
            end = min(int(r["timestamp"]) for r in rows) - 1
            time.sleep(0.05)
        end = None
        for _ in range(2):
            url = (f"{FAPI}/futures/data/takerlongshortRatio?symbol={sym}"
                   f"&period=1h&limit=500" + (f"&endTime={end}" if end else ""))
            rows = _get(url, timeout)
            if not rows:
                break
            for r in rows:
                tk_map[int(r["timestamp"])] = float(r["buySellRatio"])
            end = min(int(r["timestamp"]) for r in rows) - 1
            time.sleep(0.05)

    fr = _get(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}&limit=1000", timeout)
    f_ts = sorted((int(r["fundingTime"]), float(r["fundingRate"])) for r in fr)
    return {"ts": ts, "closes": closes, "highs": highs, "lows": lows,
            "vols": vols, "oi": oi_map, "tk": tk_map, "fund": f_ts}


def eval_symbol(s: dict, boom: float):
    """한 심볼의 (플래그들, 라벨) 샘플 목록."""
    ts, closes, highs, lows, vols = (s["ts"], s["closes"], s["highs"],
                                     s["lows"], s["vols"])
    f_times = [t for t, _ in s["fund"]]
    f_vals = [v for _, v in s["fund"]]
    out = []
    n = len(ts)
    has_oi = bool(s["oi"])
    for i in range(HIST, n - FWD):
        t = ts[i]
        oi_vals = [s["oi"][t - k * H] for k in range(24, -1, -1)
                   if (t - k * H) in s["oi"]]
        if has_oi and len(oi_vals) < 20:
            continue                 # OI 모드에선 결측 시점 제외(플래그 왜곡 방지)
        tks = [s["tk"][t - k * H] for k in range(5, -1, -1)
               if (t - k * H) in s["tk"]]
        taker = sum(tks) / len(tks) if len(tks) >= 4 else None
        j = bisect_right(f_times, t) - 1
        funding = f_vals[j] if j >= 0 else None
        r = _radar_eval(closes[i - HIST + 1:i + 1], vols[i - HIST + 1:i + 1],
                        oi_vals, funding, taker)
        c0 = closes[i]
        up = max(highs[i + 1:i + 1 + FWD]) / c0 - 1
        dn = 1 - min(lows[i + 1:i + 1 + FWD]) / c0
        out.append((r["flags"], r["score"], max(up, dn) >= boom))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=30)
    ap.add_argument("--boom-pct", type=float, default=8.0)
    ap.add_argument("--long", action="store_true",
                    help="장기 모드: 1h봉 ~10개월(8000봉), OI·테이커 생략 "
                         "(펀딩·점화·압축·매집만 검증)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", default="radar_bt.json")
    args = ap.parse_args()
    boom = args.boom_pct / 100
    bars = 8000 if args.long else 1000

    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", args.timeout)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:args.symbols]]
    span = "~10개월(펀딩·점화·압축·매집만)" if args.long else "~30일(6-way 전부)"
    print(f"▶ {len(syms)}심볼 × {span} 1h · 폭발={args.boom_pct:.0f}%/24h")

    samples = []
    for k, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, args.timeout, bars=bars,
                             with_oi_taker=not args.long)
        except Exception as e:
            print(f"  {sym} 수집 실패 {type(e).__name__}")
            continue
        if s is None:
            continue
        got = eval_symbol(s, boom)
        samples += got
        print(f"  [{k}/{len(syms)}] {sym}: {len(got)}샘플")
        time.sleep(0.1)

    N = len(samples)
    base = sum(1 for _, _, b in samples if b) / N
    print(f"\n총 {N:,}샘플 · 기저 폭발확률 {base*100:.1f}%\n")
    print(f"{'관측':<8}{'발동수':>8}{'발동시 폭발률':>12}{'lift':>7}")
    flags_out = {}
    for key in ("ign", "fuel", "sq", "acc", "fund", "flow"):
        hit = [(f, b) for f, _, b in samples if f[key]]
        nf = len(hit)
        p = (sum(1 for _, b in hit if b) / nf) if nf else 0.0
        lift = p / base if base else 0.0
        flags_out[key] = {"n": nf, "p_boom": round(p, 4),
                          "lift": round(lift, 2)}
        print(f"{_FLAG_KR[key]:<8}{nf:>8,}{p*100:>11.1f}%{lift:>7.2f}")
    print(f"\n{'점수':<8}{'샘플수':>8}{'폭발률':>10}{'lift':>7}")
    score_out = {}
    for sc in range(0, 7):
        grp = [b for _, s2, b in samples if s2 == sc]
        ng = len(grp)
        p = (sum(grp) / ng) if ng else 0.0
        score_out[sc] = {"n": ng, "p_boom": round(p, 4),
                         "lift": round(p / base, 2) if base else 0}
        if ng:
            print(f"{sc}/6{'':<5}{ng:>8,}{p*100:>9.1f}%{p/base:>7.2f}")

    Path(args.out).write_text(json.dumps(
        {"n": N, "base_rate": round(base, 4), "boom_pct": args.boom_pct,
         "flags": flags_out, "scores": score_out, "symbols": syms},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[저장] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
