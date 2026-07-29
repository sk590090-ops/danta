"""
간단 백테스터 — 롤링 윈도우로 파이프라인 실행, APPROVED 시 페이퍼 진입.
차기 봉들에서 TP/SL 도달로 청산. 성과 통계 산출.
실전 근사용 단순 모델(슬리피지/수수료 옵션).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import scanner, risk as risk_mod, planner, decision
from .signals import evaluate
from .risk import AccountState


@dataclass
class BTResult:
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def run(df: pd.DataFrame, cfg, symbol="SYM", warmup=210, fee=0.0004,
        hold_max=48, start_idx: int | None = None) -> BTResult:
    """
    warmup: MA/스윙 계산용 최소 봉수
    hold_max: 최대 보유 봉수(미도달 시 시장가 청산)
    start_idx: 진입 시작 인덱스(그 앞 데이터는 지표 워밍업용). 워크포워드 OOS에 사용.
    """
    equity = cfg.equity
    peak = equity
    curve, trades = [], []
    n = len(df)
    i = max(warmup, start_idx if start_idx is not None else 0)
    while i < n - 1:
        window = df.iloc[:i + 1]
        feats = scanner.scan(window, cfg)
        signal = evaluate(window, feats, cfg)
        plan = planner.build(window, feats, signal, cfg)
        if plan is None:
            curve.append(equity)
            i += 1
            continue
        acct = AccountState(equity=equity, peak_equity=peak)
        rr = risk_mod.evaluate(acct, plan.entry, plan.stop, feats["atr_pct"], cfg)
        dec = decision.decide(symbol, feats, signal, rr, plan, cfg)

        if dec.status != "APPROVED":
            curve.append(equity)
            i += 1
            continue

        # 진입 후 미래 봉에서 TP/SL 스캔
        entry = plan.entry
        qty = rr.position_qty
        direction = plan.direction
        exit_price, exit_reason, j = None, "TIME", i
        for j in range(i + 1, min(i + 1 + hold_max, n)):
            hi, lo = df["high"].iloc[j], df["low"].iloc[j]
            if direction == "LONG":
                if lo <= plan.stop:
                    exit_price, exit_reason = plan.stop, "STOP"; break
                if hi >= plan.target:
                    exit_price, exit_reason = plan.target, "TARGET"; break
            else:
                if hi >= plan.stop:
                    exit_price, exit_reason = plan.stop, "STOP"; break
                if lo <= plan.target:
                    exit_price, exit_reason = plan.target, "TARGET"; break
        if exit_price is None:
            exit_price = df["close"].iloc[j]
        sgn = 1 if direction == "LONG" else -1
        gross = (exit_price - entry) * sgn * qty
        cost = (entry + exit_price) * qty * fee
        pnl = gross - cost
        equity += pnl
        peak = max(peak, equity)
        trades.append({
            "entry_i": i, "exit_i": j, "setup": signal.setup,
            "direction": direction, "entry": round(entry, 2),
            "exit": round(exit_price, 2), "reason": exit_reason,
            "pnl": round(pnl, 2), "equity": round(equity, 2), "score": signal.score})
        curve.append(equity)
        i = j + 1  # 청산 다음 봉부터 재탐색

    stats = _stats(trades, curve, cfg.equity)
    return BTResult(curve, trades, stats)


def _stats(trades, curve, start_equity):
    if not trades:
        return {"n_trades": 0, "note": "거래 없음"}
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    eq = np.array(curve) if curve else np.array([start_equity])
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    end_eq = float(trades[-1]["equity"])
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    return {
        "n_trades": int(len(trades)),
        "win_rate": round(len(wins) / len(trades), 3),
        "total_return": round(end_eq / start_equity - 1, 4),
        "end_equity": round(end_eq, 2),
        "avg_pnl": round(float(pnls.mean()), 2),
        "profit_factor": round(gross_win / (gross_loss + 1e-9), 2),
        "max_drawdown": round(float(dd.min()), 4) if len(dd) else 0.0,
        "by_setup": _by_setup(trades),
    }


def _by_setup(trades):
    out = {}
    for t in trades:
        s = out.setdefault(t["setup"], {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += int(t["pnl"] > 0)
        s["pnl"] += float(t["pnl"])
    for s in out.values():
        s["win_rate"] = round(s["wins"] / s["n"], 3)
        s["pnl"] = round(s["pnl"], 2)
    return out
