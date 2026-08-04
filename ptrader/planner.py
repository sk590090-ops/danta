"""
TRADE PLANNER (슬라이드 4/8) — 진입/목표/손절/무효화 + R:R.
구조 기반 손절(스윙 저/고점 or ATR), 목표는 R 배수 + 최근 레벨.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .indicators import swing_points


@dataclass
class TradePlan:
    direction: str
    entry: float
    stop: float
    target: float
    invalidation: float
    rr: float
    timeframe: str
    confidence: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self):
        # 가격은 반올림하지 않는다 — 고정 4자리 반올림이 초저가 코인(예: ZIL
        # 0.0025)의 손절·목표를 뭉갠 사고(2026-08-04). 틱 반올림은 주문 시
        # binance_live.filters()가 담당.
        return {"direction": self.direction, "entry": self.entry,
                "stop": self.stop, "target": self.target,
                "invalidation": self.invalidation, "rr": round(self.rr, 2),
                "timeframe": self.timeframe, "confidence": self.confidence,
                "notes": self.notes}


def _confidence_label(score: int) -> str:
    return "HIGH" if score >= 70 else "MODERATE" if score >= 55 else "LOW"


def build(df, feats, signal, cfg, rr_target: float | None = None) -> TradePlan | None:
    if signal.direction == "NONE":
        return None
    pc = cfg.planner
    rr_target = pc.rr_target if rr_target is None else rr_target
    stop_mult = pc.atr_stop_mult
    buf = pc.stop_buffer_atr
    price = feats["price"]
    atr_v = feats["atr"]
    highs = feats.get("swing_highs")
    lows = feats.get("swing_lows")
    if highs is None or lows is None:
        highs, lows = swing_points(df, cfg.signal.swing_left, cfg.signal.swing_right)
    notes = []

    if signal.direction == "LONG":
        # 손절: 최근 스윙 저점 or ATR 하단 중 더 먼(넓은) 구조
        swing_stop = float(lows.iloc[-1]) if len(lows) else price - 1.5 * atr_v
        stop = min(swing_stop, price - stop_mult * atr_v)
        risk = price - stop
        target = price + rr_target * risk
        # 최근 스윙 고점이 목표보다 가까우면 1차 목표로 참고
        if len(highs) and price < highs.iloc[-1] < target:
            notes.append(f"1차 저항 {highs.iloc[-1]:.2f} 부근 부분익절 고려")
        invalidation = stop - buf * atr_v
    else:  # SHORT
        swing_stop = float(highs.iloc[-1]) if len(highs) else price + 1.5 * atr_v
        stop = max(swing_stop, price + stop_mult * atr_v)
        risk = stop - price
        target = price - rr_target * risk
        if len(lows) and target < lows.iloc[-1] < price:
            notes.append(f"1차 지지 {lows.iloc[-1]:.2f} 부근 부분익절 고려")
        invalidation = stop + buf * atr_v

    rr = abs(target - price) / (abs(price - stop) + 1e-9)
    return TradePlan(
        direction=signal.direction, entry=price, stop=stop, target=target,
        invalidation=invalidation, rr=rr, timeframe=cfg.timeframe,
        confidence=_confidence_label(signal.score), notes=notes)
