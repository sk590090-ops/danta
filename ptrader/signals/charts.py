"""
차트 패턴 (손글씨 노트: 사자!/팔자!, 팔아라·기다려·사라 치트시트).
스윙 피벗 기반 휴리스틱 탐지 — 완벽하진 않지만 실전 근사.

지원:
  쌍바닥/쌍봉(double bottom/top), 삼중바닥/삼중천정(triple),
  역헤드앤숄더/헤드앤숄더(inverse H&S / H&S),
  상승깃발/하락깃발(bull/bear flag),
  상승삼각형/하락삼각형(ascending/descending triangle).
각 결과: {"found": bool, "bias": +1/-1, "confidence": 0~1, "note": str}
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import swing_points


def _rel(a, b):  # 상대 오차
    return abs(a - b) / (abs(b) + 1e-9)


def _empty():
    return {"found": False, "bias": 0, "confidence": 0.0, "note": ""}


def double_bottom(highs, lows, price):
    if len(lows) < 2:
        return _empty()
    l1, l2 = lows.iloc[-2], lows.iloc[-1]
    if _rel(l1, l2) < 0.03 and price > max(l1, l2) * 1.005:
        conf = 0.6 + 0.3 * (1 - _rel(l1, l2) / 0.03)
        return {"found": True, "bias": +1, "confidence": min(conf, 0.95),
                "note": "쌍바닥(더블비텀)"}
    return _empty()


def double_top(highs, lows, price):
    if len(highs) < 2:
        return _empty()
    h1, h2 = highs.iloc[-2], highs.iloc[-1]
    if _rel(h1, h2) < 0.03 and price < min(h1, h2) * 0.995:
        conf = 0.6 + 0.3 * (1 - _rel(h1, h2) / 0.03)
        return {"found": True, "bias": -1, "confidence": min(conf, 0.95),
                "note": "쌍봉(더블탑)"}
    return _empty()


def triple_bottom(highs, lows, price):
    if len(lows) < 3:
        return _empty()
    a, b, c = lows.iloc[-3:]
    if _rel(a, b) < 0.03 and _rel(b, c) < 0.03 and price > max(a, b, c) * 1.005:
        return {"found": True, "bias": +1, "confidence": 0.8, "note": "삼중바닥"}
    return _empty()


def triple_top(highs, lows, price):
    if len(highs) < 3:
        return _empty()
    a, b, c = highs.iloc[-3:]
    if _rel(a, b) < 0.03 and _rel(b, c) < 0.03 and price < min(a, b, c) * 0.995:
        return {"found": True, "bias": -1, "confidence": 0.8, "note": "삼중천정"}
    return _empty()


def head_and_shoulders(highs, lows, price):
    """헤드앤숄더(천정) — 좌어깨<머리>우어깨, 어깨 높이 유사."""
    if len(highs) < 3:
        return _empty()
    ls, head, rs = highs.iloc[-3:]
    if head > ls and head > rs and _rel(ls, rs) < 0.05:
        return {"found": True, "bias": -1, "confidence": 0.75, "note": "헤드앤숄더"}
    return _empty()


def inverse_head_and_shoulders(highs, lows, price):
    if len(lows) < 3:
        return _empty()
    ls, head, rs = lows.iloc[-3:]
    if head < ls and head < rs and _rel(ls, rs) < 0.05:
        return {"found": True, "bias": +1, "confidence": 0.75,
                "note": "역헤드앤숄더"}
    return _empty()


def _trend_before(df, window=40):
    """패턴 직전 추세 방향(깃발 판정용)."""
    seg = df["close"].iloc[-window:-window // 3] if len(df) >= window else df["close"]
    if len(seg) < 3:
        return 0
    return np.sign(seg.iloc[-1] - seg.iloc[0])


def bull_flag(df, highs, lows, price):
    """상승깃발 — 강한 상승 후 완만한 하락 조정(연속형)."""
    if len(df) < 40:
        return _empty()
    pre = df["close"].iloc[-40:-15]
    cons = df["close"].iloc[-15:]
    if len(pre) < 5:
        return _empty()
    ran = (pre.iloc[-1] - pre.iloc[0]) / (pre.iloc[0] + 1e-9)
    cons_slope = (cons.iloc[-1] - cons.iloc[0]) / (cons.iloc[0] + 1e-9)
    if ran > 0.05 and -0.04 < cons_slope <= 0.005:
        return {"found": True, "bias": +1, "confidence": 0.6, "note": "상승깃발"}
    return _empty()


def bear_flag(df, highs, lows, price):
    if len(df) < 40:
        return _empty()
    pre = df["close"].iloc[-40:-15]
    cons = df["close"].iloc[-15:]
    if len(pre) < 5:
        return _empty()
    ran = (pre.iloc[-1] - pre.iloc[0]) / (pre.iloc[0] + 1e-9)
    cons_slope = (cons.iloc[-1] - cons.iloc[0]) / (cons.iloc[0] + 1e-9)
    if ran < -0.05 and -0.005 <= cons_slope < 0.04:
        return {"found": True, "bias": -1, "confidence": 0.6, "note": "하락깃발"}
    return _empty()


def ascending_triangle(highs, lows, price):
    """상승삼각형 — 고점 수평 저항 + 저점 상승(강세 연속)."""
    if len(highs) < 2 or len(lows) < 2:
        return _empty()
    flat_top = _rel(highs.iloc[-1], highs.iloc[-2]) < 0.02
    rising_low = lows.iloc[-1] > lows.iloc[-2] * 1.005
    if flat_top and rising_low:
        return {"found": True, "bias": +1, "confidence": 0.6, "note": "상승삼각형"}
    return _empty()


def descending_triangle(highs, lows, price):
    if len(highs) < 2 or len(lows) < 2:
        return _empty()
    flat_bottom = _rel(lows.iloc[-1], lows.iloc[-2]) < 0.02
    falling_high = highs.iloc[-1] < highs.iloc[-2] * 0.995
    if flat_bottom and falling_high:
        return {"found": True, "bias": -1, "confidence": 0.6, "note": "하락삼각형"}
    return _empty()


def detect_all(df: pd.DataFrame, cfg, highs=None, lows=None) -> dict:
    """마지막 봉 기준 차트 패턴 종합. highs/lows 주어지면 재사용(중복계산 방지)."""
    if highs is None or lows is None:
        highs, lows = swing_points(df, cfg.swing_left, cfg.swing_right)
    price = df["close"].iloc[-1]
    checks = {
        "double_bottom": double_bottom(highs, lows, price),
        "double_top": double_top(highs, lows, price),
        "triple_bottom": triple_bottom(highs, lows, price),
        "triple_top": triple_top(highs, lows, price),
        "head_and_shoulders": head_and_shoulders(highs, lows, price),
        "inverse_head_and_shoulders": inverse_head_and_shoulders(highs, lows, price),
        "bull_flag": bull_flag(df, highs, lows, price),
        "bear_flag": bear_flag(df, highs, lows, price),
        "ascending_triangle": ascending_triangle(highs, lows, price),
        "descending_triangle": descending_triangle(highs, lows, price),
    }
    found = {k: v for k, v in checks.items() if v["found"]}
    bull = sum(v["confidence"] for v in found.values() if v["bias"] > 0)
    bear = sum(v["confidence"] for v in found.values() if v["bias"] < 0)
    bias = "bull" if bull > bear else "bear" if bear > bull else "neutral"
    return {
        "found": found,
        "bias": bias,
        "bull_score": round(bull, 3),
        "bear_score": round(bear, 3),
        "n_swings": {"highs": len(highs), "lows": len(lows)},
    }
