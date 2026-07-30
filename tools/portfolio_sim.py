#!/usr/bin/env python
"""포트폴리오 시뮬 — 슬롯 수 · 리스크% · 레버리지 상한의 실제 영향 측정.

$500 시드로 10개월 브래킷 트레이드를 **시간순**으로 굴린다. 슬롯이 차 있으면
그 신호는 놓친다(실제 봇과 동일). 복리 반영. 측정:
  최종자본 / 총수익률 / 최대낙폭(MDD) / 최악 1일 / 파산근접(-50%) 여부
  + 동시보유 상관(같은 방향 동시 비중) — 슬롯 증설의 숨은 리스크

레버리지 모델(실제 엔진과 동일):
  qty = 리스크금액 / ATR,  명목 = qty × 진입가 = 리스크금액 × (진입가/ATR)
  → 레버리지 = 리스크% × (진입가/ATR).  상한 초과 시 수량 축소 = R도 비례 축소.

⚠️ 한계: 브래킷 셋업 단독(OBV_DIV 제외), 10개월 단일 구간, 슬리피지 미반영.
사용: python tools/portfolio_sim.py
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

from daily_scan import FAPI, _get                 # noqa: E402
from radar_backtest import fetch_series           # noqa: E402
from radar_entry_study import sim_symbol          # noqa: E402

SEED = 500.0
N_SYMBOLS = 30
DAY_MS = 86_400_000


EQUITY_TOKENS = {"SNDKUSDT", "SKHYNIXUSDT", "SOXLUSDT", "SOXSUSDT", "MUUSDT",
                 "KORUUSDT", "SKHYUSDT", "QQQUSDT", "EWYUSDT", "DRAMUSDT",
                 "SAMSUNGUSDT", "INTCUSDT", "NVDAUSDT", "SPCXUSDT"}


def simulate(trades, slots: int, risk_pct: float, lev_cap: float,
             seed: float = SEED, same_dir_cap: int = 0, cluster_cap: int = 0):
    """시간순 포트폴리오 시뮬. trades=[(entry_ts,R,exit_ts,dir,px_atr),...]"""
    eq = seed
    peak = eq
    mdd = 0.0
    open_slots: list = []          # [(exit_ts, direction, symbol)]
    taken = skipped = 0
    daily: dict[int, float] = {}
    conc_same = conc_total = 0
    curve = []
    for ets, r, xts, direction, px_atr, sym in trades:
        open_slots = [o for o in open_slots if o[0] > ets]   # 만료 해제
        if len(open_slots) >= slots:
            skipped += 1
            continue
        if same_dir_cap and sum(1 for o in open_slots
                                if o[1] == direction) >= same_dir_cap:
            skipped += 1
            continue
        if cluster_cap and sym in EQUITY_TOKENS and                 sum(1 for o in open_slots
                    if o[2] in EQUITY_TOKENS) >= cluster_cap:
            skipped += 1
            continue
        # 동시보유 방향 상관 측정
        if open_slots:
            conc_total += 1
            if all(o[1] == direction for o in open_slots):
                conc_same += 1
        risk_amt = eq * risk_pct
        want_lev = risk_pct * px_atr                 # 의도 레버리지
        scale = min(1.0, lev_cap / want_lev) if want_lev > 0 else 1.0
        pnl = r * risk_amt * scale                   # 상한에 걸리면 R도 축소
        eq += pnl
        taken += 1
        open_slots.append((xts, direction, sym))
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        daily[ets // DAY_MS] = daily.get(ets // DAY_MS, 0.0) + pnl
        curve.append(eq)
        if eq <= seed * 0.5:                         # 파산 근접 = 중단
            return {"equity": round(eq, 2), "ruin": True, "taken": taken,
                    "skipped": skipped, "mdd": round(mdd, 4),
                    "worst_day": round(min(daily.values()), 2),
                    "corr": round(conc_same / conc_total, 2) if conc_total else 0.0}
    return {"equity": round(eq, 2), "ruin": False, "taken": taken,
            "skipped": skipped, "mdd": round(mdd, 4),
            "worst_day": round(min(daily.values()), 2) if daily else 0.0,
            "corr": round(conc_same / conc_total, 2) if conc_total else 0.0}


def main() -> int:
    tickers = _get(f"{FAPI}/fapi/v1/ticker/24hr", 15)
    perp = sorted([t for t in tickers if t["symbol"].endswith("USDT")
                   and "_" not in t["symbol"]],
                  key=lambda t: float(t["quoteVolume"]), reverse=True)
    syms = [t["symbol"] for t in perp[:N_SYMBOLS]]
    print(f"▶ 포트폴리오 시뮬: {len(syms)}심볼 × ~10개월 · 시드 ${SEED:,.0f}\n")

    trades = []
    for k, sym in enumerate(syms, 1):
        try:
            s = fetch_series(sym, 15, bars=8000, with_oi_taker=False)
        except Exception:
            continue
        if s:
            trades += [(t[0], t[1], t[2], t[3], t[4], sym)
                       for t in sim_symbol(s, "bracket")]
        print(f"  [{k}/{len(syms)}] {sym}", end="\r")
        time.sleep(0.05)
    trades.sort(key=lambda t: t[0])
    print(f"\n  총 {len(trades)}건 신호 (시간순 정렬)\n")

    lev_used = [t[4] * 0.015 for t in trades]
    lev_used.sort()
    print(f"[참고] 리스크1.5% 기준 '의도 레버리지' 분포: "
          f"중앙 {lev_used[len(lev_used)//2]:.2f}x · "
          f"p90 {lev_used[int(len(lev_used)*0.9)]:.2f}x · "
          f"최대 {lev_used[-1]:.2f}x\n")

    report = {}
    print("═══ A. 슬롯 수 (리스크 1.5%, 레버 상한 2x 고정) ═══")
    print(f"{'슬롯':<6}{'최종자본':>10}{'수익률':>9}{'MDD':>8}"
          f"{'체결':>6}{'놓침':>6}{'최악일':>9}{'동방향비중':>10}")
    for slots in (1, 2, 3, 4, 5, 6, 8):
        r = simulate(trades, slots, 0.015, 2.0)
        report[f"slots_{slots}"] = r
        print(f"{slots:<6}${r['equity']:>9,.0f}{(r['equity']/SEED-1)*100:>8.1f}%"
              f"{r['mdd']*100:>7.1f}%{r['taken']:>6}{r['skipped']:>6}"
              f"${r['worst_day']:>8,.0f}{r['corr']*100:>9.0f}%")

    print("\n═══ B. 리스크% (슬롯 3, 레버 상한 2x) ═══")
    print(f"{'리스크':<8}{'최종자본':>10}{'수익률':>9}{'MDD':>8}{'최악일':>9}{'파산':>6}")
    for rp in (0.005, 0.01, 0.015, 0.02, 0.03, 0.05):
        r = simulate(trades, 3, rp, 2.0)
        report[f"risk_{rp}"] = r
        print(f"{rp*100:<7.1f}%${r['equity']:>9,.0f}{(r['equity']/SEED-1)*100:>8.1f}%"
              f"{r['mdd']*100:>7.1f}%${r['worst_day']:>8,.0f}"
              f"{'  💀' if r['ruin'] else '  —':>6}")

    print("\n═══ C. 레버리지 상한 (슬롯 3, 리스크 1.5%) ═══")
    print(f"{'상한':<7}{'최종자본':>10}{'수익률':>9}{'MDD':>8}{'최악일':>9}")
    for cap in (1.0, 2.0, 3.0, 5.0, 10.0):
        r = simulate(trades, 3, 0.015, cap)
        report[f"lev_{cap}"] = r
        print(f"{cap:<6.0f}x${r['equity']:>9,.0f}{(r['equity']/SEED-1)*100:>8.1f}%"
              f"{r['mdd']*100:>7.1f}%${r['worst_day']:>8,.0f}")

    print("\n═══ E. 쏠림 제한 (슬롯3·리스크1.5%·레버2x) ═══")
    print(f"{'설정':<22}{'최종자본':>10}{'수익률':>9}{'MDD':>8}{'체결':>6}{'놓침':>6}")
    for name, sd, cc in (("기본(제한없음)", 0, 0), ("동방향 최대2", 2, 0),
                         ("주식토큰 최대1", 0, 1), ("주식토큰 최대2", 0, 2),
                         ("동방향2+주식토큰1", 2, 1)):
        r = simulate(trades, 3, 0.015, 2.0, same_dir_cap=sd, cluster_cap=cc)
        report[f"cap_{name}"] = r
        print(f"{name:<22}${r['equity']:>9,.0f}{(r['equity']/SEED-1)*100:>8.1f}%"
              f"{r['mdd']*100:>7.1f}%{r['taken']:>6}{r['skipped']:>6}")

    print("\n═══ D. 조합: 슬롯↑ + 리스크↑ (공격형 위험 확인) ═══")
    print(f"{'설정':<20}{'최종자본':>10}{'수익률':>9}{'MDD':>8}{'파산':>6}")
    for slots, rp in ((3, 0.015), (5, 0.015), (5, 0.03), (8, 0.03), (8, 0.05)):
        r = simulate(trades, slots, rp, 2.0)
        report[f"combo_{slots}_{rp}"] = r
        print(f"{f'슬롯{slots}·리스크{rp*100:.1f}%':<20}${r['equity']:>9,.0f}"
              f"{(r['equity']/SEED-1)*100:>8.1f}%{r['mdd']*100:>7.1f}%"
              f"{'  💀' if r['ruin'] else '  —':>6}")

    Path("portfolio_sim.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[저장] portfolio_sim.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
