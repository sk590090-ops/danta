"""
RISK MODULE (슬라이드 5/8) — 5중 리스크 체크 → PASS / BLOCK.
  1 POSITION SIZE   위험액 기반 포지션 규모 산출·상한
  2 EXPOSURE LIMIT  총 노출 한도
  3 DRAWDOWN        허용 낙폭 이내
  4 VOLATILITY      변동성(ATR%)이 허용 밴드 내
  5 MAX LOSS        1회 손실 한도 이내
하나라도 실패하면 BLOCK.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AccountState:
    equity: float
    peak_equity: float = 0.0
    open_exposure: float = 0.0       # 현재 열린 포지션 명목가 합
    open_positions: int = 0

    def __post_init__(self):
        self.peak_equity = max(self.peak_equity, self.equity)


@dataclass
class RiskResult:
    passed: bool
    checks: dict = field(default_factory=dict)
    position_notional: float = 0.0
    position_qty: float = 0.0
    risk_amount: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self):
        return {"passed": self.passed, "checks": self.checks,
                "position_notional": round(self.position_notional, 2),
                "position_qty": round(self.position_qty, 8),
                "risk_amount": round(self.risk_amount, 2),
                "reasons": self.reasons}


def evaluate(acct: AccountState, entry: float, stop: float, atr_pct: float,
             cfg) -> RiskResult:
    r = cfg.risk
    checks, reasons = {}, []
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return RiskResult(False, {"position_size": False},
                          reasons=["손절 거리 0 — 계산 불가"])

    # 1) POSITION SIZE — 위험액 = 계좌 * risk_per_trade
    risk_amount = acct.equity * r.risk_per_trade
    qty = risk_amount / stop_dist
    notional = qty * entry
    max_notional = acct.equity * r.max_position_pct
    if notional > max_notional:          # 상한으로 축소
        notional = max_notional
        qty = notional / entry
        risk_amount = qty * stop_dist
        reasons.append("포지션 상한으로 규모 축소")
    checks["position_size"] = True

    # 2) EXPOSURE LIMIT
    new_exposure = acct.open_exposure + notional
    exp_ok = new_exposure <= acct.equity * r.max_total_exposure
    checks["exposure_limit"] = exp_ok
    if not exp_ok:
        reasons.append(
            f"총 노출 초과: {new_exposure:.0f} > {acct.equity * r.max_total_exposure:.0f}")

    # 3) DRAWDOWN
    dd = 0.0 if acct.peak_equity <= 0 else 1 - acct.equity / acct.peak_equity
    dd_ok = dd <= r.max_drawdown
    checks["drawdown"] = dd_ok
    if not dd_ok:
        reasons.append(f"최대낙폭 초과: {dd:.1%} > {r.max_drawdown:.0%}")

    # 4) VOLATILITY
    vol_ok = r.atr_vol_min <= atr_pct <= r.atr_vol_max
    checks["volatility"] = vol_ok
    if not vol_ok:
        reasons.append(
            f"변동성 밴드 이탈: ATR% {atr_pct:.2%} "
            f"(허용 {r.atr_vol_min:.2%}~{r.atr_vol_max:.2%})")

    # 5) MAX LOSS
    loss_ok = risk_amount <= acct.equity * r.max_loss_per_trade
    checks["max_loss"] = loss_ok
    if not loss_ok:
        reasons.append(
            f"1회 손실 한도 초과: {risk_amount:.0f} > "
            f"{acct.equity * r.max_loss_per_trade:.0f}")

    passed = all(checks.values())
    if passed and not reasons:
        reasons.append("모든 리스크 체크 통과")
    return RiskResult(passed, checks, notional, qty, risk_amount, reasons)
