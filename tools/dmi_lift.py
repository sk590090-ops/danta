#!/usr/bin/env python
"""DMI(ADX/+DI/-DI) 예측력 측정 — dik_chart 인스타 규칙의 정직한 검증.

주장(출처: instagram/dik_chart "DMI 지표 매매법"):
  롱: ADX가 20 상향 돌파 + (+DI가 -DI 상향 교차) + 두 선 동반 상승
  숏: ADX 20 위 강세 + (-DI가 +DI 상향 교차) + 동반 급등

두 가지를 나눠서 잰다 (레이더 방법론과 동일):
  ① lift  — DMI 발동 시 '24h 내 ±8% 폭발' 확률이 기저 대비 몇 배인가
            (= 변동성 예측력. 압축·매집은 여기서 역예측으로 기각됐다)
  ② 방향  — 규칙이 지목한 방향으로 실제 트레이드했을 때 PF/avgR
            (= 방향 예측력. 전조 4규칙이 여기서 전패했다)
비교군: 같은 하니스의 브래킷(방향 무예측, PF 1.52).

⚠️ 한계: 1h봉·바이낸스 선물 상위 30심볼·~10개월. 원 주장은 국내주식 맥락이라
   시장이 다르다. 그래도 '우리 봇에 넣을 값어치'는 이 조건에서 판정해야 한다.

사용: python tools/dmi_lift.py
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
from radar_backtest import fetch_series, HIST, FWD     # noqa: E402
from radar_entry_study import _atr14, _stats, FEE, HOLD  # noqa: E402

N_SYMBOLS = 30
BOOM = 0.08          # 폭발 라벨 (레이더와 동일)
ADX_BASE = 20.0      # 주장의 기준선
DI_LEN = 14


def dmi_series(highs, lows, closes, length: int = DI_LEN):
    """Wilder DMI — (+DI, -DI, ADX) 리스트. 표준 정의, 튜닝 없음."""
    n = len(closes)
    pdm = [0.0] * n
    ndm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    # Wilder 평활
    atr = [0.0] * n
    sp = [0.0] * n
    sn = [0.0] * n
    if n <= length:
        return [0.0] * n, [0.0] * n, [0.0] * n
    atr[length] = sum(tr[1:length + 1])
    sp[length] = sum(pdm[1:length + 1])
    sn[length] = sum(ndm[1:length + 1])
    for i in range(length + 1, n):
        atr[i] = atr[i - 1] - atr[i - 1] / length + tr[i]
        sp[i] = sp[i - 1] - sp[i - 1] / length + pdm[i]
        sn[i] = sn[i - 1] - sn[i - 1] / length + ndm[i]
    pdi = [0.0] * n
    ndi = [0.0] * n
    dx = [0.0] * n
    for i in range(length, n):
        if atr[i] > 0:
            pdi[i] = 100 * sp[i] / atr[i]
            ndi[i] = 100 * sn[i] / atr[i]
        s = pdi[i] + ndi[i]
        dx[i] = 100 * abs(pdi[i] - ndi[i]) / s if s > 0 else 0.0
    adx = [0.0] * n
    start = length * 2
    if n > start:
        adx[start] = sum(dx[length:start]) / length
        for i in range(start + 1, n):
            adx[i] = (adx[i - 1] * (length - 1) + dx[i]) / length
    return pdi, ndi, adx


def dmi_signal(pdi, ndi, adx, i) -> str | None:
    """주장 규칙 그대로. 반환 'LONG' | 'SHORT' | None."""
    if i < DI_LEN * 3 or adx[i] <= 0:
        return None
    rising_adx = adx[i] > adx[i - 1]
    # 롱: +DI가 -DI를 상향 교차 & ADX가 20 위 & ADX 상승
    if (pdi[i] > ndi[i] and pdi[i - 1] <= ndi[i - 1]
            and adx[i] >= ADX_BASE and rising_adx and pdi[i] > pdi[i - 1]):
        return "LONG"
    # 숏: -DI가 +DI를 상향 교차 & ADX 위/상승
    if (ndi[i] > pdi[i] and ndi[i - 1] <= pdi[i - 1]
            and adx[i] >= ADX_BASE and rising_adx and ndi[i] > ndi[i - 1]):
        return "SHORT"
    return None


def eval_symbol(s: dict):
    """(폭발 라벨 샘플, DMI 트레이드) 반환."""
    ts, closes, highs, lows = s["ts"], s["closes"], s["highs"], s["lows"]
    n = len(ts)
    pdi, ndi, adx = dmi_series(highs, lows, closes)
    labels = []          # (발동여부, 폭발여부)
    trades = []          # (ts, R)
    i = HIST
    while i < n - FWD:
        sig = dmi_signal(pdi, ndi, adx, i)
        c0 = closes[i]
        up = max(highs[i + 1:i + 1 + FWD]) / c0 - 1
        dn = 1 - min(lows[i + 1:i + 1 + FWD]) / c0
        labels.append((sig is not None, max(up, dn) >= BOOM))
        if sig is None:
            i += 1
            continue
        # 방향 검증: 엔진과 동일 트레이드 모델 (1ATR 손절 / 2R / 48봉)
        atr = _atr14(highs, lows, closes, i)
        if atr <= 0:
            i += 1
            continue
        long = sig == "LONG"
        entry = c0
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
        r = (exit_p - entry) * sgn / atr - (entry + exit_p) * FEE / atr
        trades.append((ts[i], r))
        i = k + 1
    return labels, trades


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ DMI 검증: {len(syms)}심볼 × ~10개월 1h "
          f"(ADX{DI_LEN}, 기준선 {ADX_BASE:.0f})\n")

    all_labels, all_trades = [], []
    for k, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        lb, tr = eval_symbol(s)
        all_labels += lb
        all_trades += tr
        print(f"  [{k}/{len(syms)}] {sym}: 발동 {len(tr)}건", end="\r")
        time.sleep(0.05)

    N = len(all_labels)
    base = sum(1 for _, b in all_labels if b) / N
    fired = [b for f, b in all_labels if f]
    p = (sum(fired) / len(fired)) if fired else 0.0
    print(f"\n\n═══ ① 변동성 예측력 (lift) ═══")
    print(f"  총 {N:,}샘플 · 기저 폭발확률 {base*100:.1f}%")
    print(f"  DMI 발동 {len(fired):,}건 · 발동 시 폭발률 {p*100:.1f}% "
          f"· **lift {p/base:.2f}**")

    all_trades.sort(key=lambda x: x[0])
    vals = [r for _, r in all_trades]
    half = len(vals) // 2
    st, a, b2 = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
    print(f"\n═══ ② 방향 예측력 (트레이드) ═══")
    print(f"{'규칙':<12}{'n':>6}{'승률':>8}{'avgR':>8}{'PF':>7}{'전반PF':>8}{'후반PF':>8}")
    if st["n"]:
        print(f"{'DMI':<12}{st['n']:>6}{st['wr']*100:>7.0f}%{st['avgR']:>8.3f}"
              f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b2.get('pf',0):>8.2f}")
    print(f"{'브래킷(비교)':<10}{'127':>6}{'46%':>8}{'+0.292':>8}{'1.52':>7}"
          f"{'1.22':>8}{'1.90':>8}")

    ok_lift = (p / base) > 1.3 if base else False
    ok_dir = st.get("avgR", 0) > 0 and st.get("pf", 0) > 1.1 and \
        a.get("pf", 0) > 1.0 and b2.get("pf", 0) > 1.0
    print(f"\n판정: 변동성축 {'✅ 채택 후보' if ok_lift else '❌ 무가치'} "
          f"/ 방향축 {'✅ 채택 후보' if ok_dir else '❌ 기각'}")

    Path("dmi_lift.json").write_text(json.dumps({
        "n_samples": N, "base_rate": round(base, 4),
        "fired": len(fired), "p_boom": round(p, 4),
        "lift": round(p / base, 2) if base else 0,
        "direction": {"all": st, "first": a, "second": b2}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("[저장] dmi_lift.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
