"""
FINAL DECISION (슬라이드 7/8) — 신호+리스크+계획 → 최종 메모.
상태: APPROVED(실행) / WATCHLIST(관찰) / REJECTED(거래안함).
슬라이드 원칙: "사람이 최종 결정" → 이 메모는 사람 승인용 산출물.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Decision:
    symbol: str
    status: str                      # APPROVED | WATCHLIST | REJECTED
    setup: str
    direction: str
    score: int
    memo: dict = field(default_factory=dict)
    ts: str = ""

    def as_dict(self):
        return {"symbol": self.symbol, "status": self.status, "setup": self.setup,
                "direction": self.direction, "score": self.score,
                "memo": self.memo, "ts": self.ts}


def decide(symbol, feats, signal, risk_result, plan, cfg) -> Decision:
    d = cfg.decision
    s = cfg.signal
    reasons = []
    status = "REJECTED"

    if signal.setup == "NONE" or plan is None:
        reasons.append("유효 셋업 없음")
        status = "REJECTED"
    elif not risk_result.passed:
        reasons.append("리스크 체크 실패 → 차단")
        status = "REJECTED"
    elif plan.rr < d.min_rr:
        reasons.append(f"손익비 부족 R:R {plan.rr:.2f} < {d.min_rr}")
        status = "WATCHLIST"
    elif d.require_trend_alignment and (
        (signal.direction == "LONG" and not feats["cross"]["allow_long"]) or
        (signal.direction == "SHORT" and not feats["cross"]["allow_short"])
    ):
        reasons.append("추세 정렬 요구 위반 → 관찰")
        status = "WATCHLIST"
    elif signal.score >= s.min_score_approve:
        reasons.append("스코어·리스크·손익비 충족 → 승인")
        status = "APPROVED"
    elif signal.score >= s.min_score_watch:
        reasons.append("조건 근접 → 관찰(감시)")
        status = "WATCHLIST"
    else:
        reasons.append(f"스코어 미달({signal.score})")
        status = "REJECTED"

    memo = {
        "1_setup_summary": {
            "setup": signal.setup,
            "trend": feats["cross"]["trend"],
            "cross": feats["cross"]["cross"],
            "timeframe": cfg.timeframe,
        },
        "2_signal_strength": {
            "score": signal.score,
            "confidence": plan.confidence if plan else "N/A",
            "reasons": signal.reasons,
            "candles": signal.candles.get("patterns", []),
            "charts": list(signal.charts.get("found", {}).keys()),
        },
        "3_risk": risk_result.as_dict(),
        "4_trade_plan": plan.as_dict() if plan else None,
        "5_final": {"status": status, "decision_reasons": reasons},
    }
    return Decision(
        symbol=symbol, status=status, setup=signal.setup,
        direction=signal.direction, score=signal.score, memo=memo,
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def format_memo(dec: Decision) -> str:
    """사람이 읽는 텍스트 메모(슬라이드 7/8 FINAL DECISION MEMO 스타일)."""
    m = dec.memo
    icon = {"APPROVED": "✅", "WATCHLIST": "👁", "REJECTED": "✕"}[dec.status]
    lines = [
        "┌─ FINAL DECISION MEMO ────────────────────────",
        f"│ SYMBOL : {dec.symbol}    TIME: {dec.ts}",
        f"│ 1. SETUP    : {m['1_setup_summary']['setup']} "
        f"/ trend={m['1_setup_summary']['trend']} "
        f"/ cross={m['1_setup_summary']['cross']}",
        f"│ 2. STRENGTH : score={dec.score} "
        f"({m['2_signal_strength']['confidence']})  "
        f"candles={m['2_signal_strength']['candles']}",
        f"│              charts={m['2_signal_strength']['charts']}",
        f"│ 3. RISK     : {'PASS' if m['3_risk']['passed'] else 'BLOCK'} "
        f"| size={m['3_risk']['position_notional']} "
        f"| risk={m['3_risk']['risk_amount']}",
    ]
    if m["4_trade_plan"]:
        p = m["4_trade_plan"]
        lines += [
            f"│ 4. PLAN     : {p['direction']} entry={p['entry']} "
            f"stop={p['stop']} target={p['target']} R:R={p['rr']}",
        ]
    else:
        lines.append("│ 4. PLAN     : —")
    lines += [
        f"│ 5. STATUS   : {icon} {dec.status}  "
        f"— {'; '.join(m['5_final']['decision_reasons'])}",
        "└──────────────────────────────────────────────",
        "  ↳ HUMAN REVIEW REQUIRED — 최종 실행은 사람이 결정",
    ]
    return "\n".join(lines)
