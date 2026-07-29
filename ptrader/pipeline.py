"""단일 심볼 파이프라인 — Scan→Signal→Risk→Plan→Decision을 1회 실행."""
from __future__ import annotations

import pandas as pd

from . import scanner, risk as risk_mod, planner, decision
from .signals import evaluate


def analyze(symbol: str, df: pd.DataFrame, acct: risk_mod.AccountState, cfg):
    """
    OHLCV df에 대해 전체 판단 1회 수행 → Decision 반환.
    df는 시간순 정렬된 OHLCV(마지막 봉이 현재).
    """
    feats = scanner.scan(df, cfg)
    signal = evaluate(df, feats, cfg)
    plan = planner.build(df, feats, signal, cfg)

    if plan is None:
        rr = risk_mod.RiskResult(False, reasons=["플랜 없음"])
    else:
        rr = risk_mod.evaluate(acct, plan.entry, plan.stop, feats["atr_pct"], cfg)

    return decision.decide(symbol, feats, signal, rr, plan, cfg)
