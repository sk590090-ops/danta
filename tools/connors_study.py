#!/usr/bin/env python
"""Connors RSI(2) 검증 — 지침 폴더 @lol_gini 규칙 + 문헌(Larry Connors) 전략.

규칙(스레드 명세 그대로, 무튜닝):
  4h봉 · 종가 > EMA200 (장기추세 생존) · RSI(2) < 10 (극단 과매도) → 롱 온리
  손절 3.5% 고정 · 익절 4%(라이브값) 또는 12%(파일값) 두 변형 · 96h(24봉) 시간청산
데이터: 1h를 4h로 리샘플(기존 페치 재사용). 30심볼 × ~10개월. 수수료 0.05%×2.
판정: avgR>0 & PF>1.1 & 전후반 모두 PF>1.0 (기존 게이트와 동일).
사용: python tools/connors_study.py
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
from radar_entry_study import _stats                   # noqa: E402

N_SYMBOLS = 30
FEE = 0.0005
SL = 0.035
HOLD_4H = 24            # 96시간


def resample_4h(s: dict) -> dict:
    """1h → 4h (UTC 4h 격자, 완결 그룹만)."""
    ts, o, h, l, c = s["ts"], s.get("opens"), s["highs"], s["lows"], s["closes"]
    out = {"ts": [], "highs": [], "lows": [], "closes": []}
    bucket = None
    bh, bl, bc = -1e18, 1e18, 0.0
    cnt = 0
    for i in range(len(ts)):
        b = ts[i] // (4 * 3600_000)
        if b != bucket:
            if bucket is not None and cnt == 4:
                out["ts"].append(bucket * 4 * 3600_000)
                out["highs"].append(bh)
                out["lows"].append(bl)
                out["closes"].append(bc)
            bucket, bh, bl, cnt = b, -1e18, 1e18, 0
        bh = max(bh, h[i]); bl = min(bl, l[i]); bc = c[i]; cnt += 1
    return out


def rsi2(closes, i) -> float | None:
    """Wilder RSI(2) — 표준 정의."""
    if i < 20:
        return None
    # 짧은 기간이라 국소 재계산(20봉 워밍업)으로 충분히 수렴
    g = l = 1e-12
    ag = al = None
    for k in range(i - 19, i + 1):
        d = closes[k] - closes[k - 1]
        up, dn = max(d, 0.0), max(-d, 0.0)
        if ag is None:
            ag, al = up, dn
        else:
            ag = (ag + up) / 2
            al = (al + dn) / 2
    if al <= 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def ema200(closes, i, prev=None) -> float:
    k = 2 / 201
    if prev is None:
        return sum(closes[max(0, i - 199):i + 1]) / min(200, i + 1)
    return closes[i] * k + prev * (1 - k)


def sim(s4: dict, tp: float):
    """롱 온리 시뮬. R = pnl% / SL% (리스크 단위 통일)."""
    c, h, l, ts = s4["closes"], s4["highs"], s4["lows"], s4["ts"]
    n = len(c)
    if n < 220:
        return []
    ema = None
    out = []
    i = 0
    while i < n - 1:
        ema = ema200(c, i, ema)
        if i < 205:
            i += 1
            continue
        r2 = rsi2(c, i)
        if r2 is None or c[i] <= ema or r2 >= 10:
            i += 1
            continue
        entry = c[i]
        stop, target = entry * (1 - SL), entry * (1 + tp)
        exit_p, k = None, i
        for k in range(i + 1, min(i + 1 + HOLD_4H, n)):
            if l[k] <= stop:
                exit_p = stop; break
            if h[k] >= target:
                exit_p = target; break
        if exit_p is None:
            exit_p = c[k]
        pnl = (exit_p - entry) / entry - 2 * FEE
        out.append((ts[i], pnl / SL))
        # ema 따라잡기 (건너뛴 봉들)
        for j in range(i + 1, k + 1):
            ema = ema200(c, j, ema)
        i = k + 1
    return out


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ Connors RSI(2) 검증: {len(syms)}심볼 × ~10개월 4h · "
          f"EMA200↑ & RSI2<10 롱, SL {SL*100:.1f}%\n")

    trades = {"TP4%": [], "TP12%": []}
    for kk, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if not s:
            continue
        s4 = resample_4h(s)
        trades["TP4%"] += sim(s4, 0.04)
        trades["TP12%"] += sim(s4, 0.12)
        print(f"  [{kk}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)

    print()
    print(f"\n{'변형':<10}{'n':>6}{'승률':>7}{'avgR':>8}{'PF':>7}{'전반PF':>8}{'후반PF':>8}")
    report = {}
    for name, tr in trades.items():
        tr.sort(key=lambda x: x[0])
        vals = [r for _, r in tr]
        half = len(vals) // 2
        st, a, b = _stats(vals), _stats(vals[:half]), _stats(vals[half:])
        report[name] = {"all": st, "first": a, "second": b}
        if st["n"]:
            print(f"{name:<10}{st['n']:>6}{st['wr']*100:>6.0f}%{st['avgR']:>8.3f}"
                  f"{st['pf']:>7.2f}{a.get('pf',0):>8.2f}{b.get('pf',0):>8.2f}")

    Path("connors_study.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] connors_study.json")
    print("판정: avgR>0 & PF>1.1 & 전후반 모두 PF>1.0 이면 페이퍼 셋업 후보.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
