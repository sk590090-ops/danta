"""
캔들 패턴 (손글씨 노트 Day1~8).
  Day1 K선 구조 / Day2 큰 양선·큰 음선 / Day3 십자선(도지)·묘비·잠자리
  Day4 하라미·목봉선(핀바) / Day5 포선(장악형) / Day7 아침의 별 / Day8 저녁의 별

각 detector는 (df, i, atr, cfg)를 받아 bool 반환. i 기본값 = 마지막 봉.
detect_all()이 마지막 봉의 모든 패턴을 dict로 종합.
"""
from __future__ import annotations

import pandas as pd


# ---- 기본 봉 요소 ---------------------------------------------------------
def _o(df, i): return df["open"].iloc[i]
def _h(df, i): return df["high"].iloc[i]
def _l(df, i): return df["low"].iloc[i]
def _c(df, i): return df["close"].iloc[i]


def body(df, i): return abs(_c(df, i) - _o(df, i))
def rng(df, i): return max(_h(df, i) - _l(df, i), 1e-9)
def upper_wick(df, i): return _h(df, i) - max(_o(df, i), _c(df, i))
def lower_wick(df, i): return min(_o(df, i), _c(df, i)) - _l(df, i)
def is_bull(df, i): return _c(df, i) >= _o(df, i)
def is_bear(df, i): return _c(df, i) < _o(df, i)


# ---- Day2: 큰 양선 / 큰 음선 ---------------------------------------------
def big_bull(df, i, atr_v, cfg):
    return is_bull(df, i) and body(df, i) >= cfg.big_body_atr * atr_v \
        and body(df, i) / rng(df, i) > 0.6


def big_bear(df, i, atr_v, cfg):
    return is_bear(df, i) and body(df, i) >= cfg.big_body_atr * atr_v \
        and body(df, i) / rng(df, i) > 0.6


# ---- Day3: 도지 계열 ------------------------------------------------------
def doji(df, i, atr_v, cfg):
    return body(df, i) / rng(df, i) <= cfg.doji_body_ratio


def gravestone_doji(df, i, atr_v, cfg):  # 묘비 — 위꼬리 길고 종가 저가 부근 (천장 신호)
    return doji(df, i, atr_v, cfg) and upper_wick(df, i) >= 0.6 * rng(df, i) \
        and lower_wick(df, i) <= 0.15 * rng(df, i)


def dragonfly_doji(df, i, atr_v, cfg):  # 잠자리 — 아래꼬리 길고 종가 고가 부근 (바닥 신호)
    return doji(df, i, atr_v, cfg) and lower_wick(df, i) >= 0.6 * rng(df, i) \
        and upper_wick(df, i) <= 0.15 * rng(df, i)


# ---- Day4: 하라미 / 목봉선(핀바) -----------------------------------------
def bullish_harami(df, i, atr_v, cfg):
    if i < 1:
        return False
    prev_big = is_bear(df, i - 1) and body(df, i - 1) >= cfg.big_body_atr * atr_v
    inside = max(_o(df, i), _c(df, i)) < max(_o(df, i - 1), _c(df, i - 1)) \
        and min(_o(df, i), _c(df, i)) > min(_o(df, i - 1), _c(df, i - 1))
    return prev_big and inside


def bearish_harami(df, i, atr_v, cfg):
    if i < 1:
        return False
    prev_big = is_bull(df, i - 1) and body(df, i - 1) >= cfg.big_body_atr * atr_v
    inside = max(_o(df, i), _c(df, i)) < max(_o(df, i - 1), _c(df, i - 1)) \
        and min(_o(df, i), _c(df, i)) > min(_o(df, i - 1), _c(df, i - 1))
    return prev_big and inside


def hammer(df, i, atr_v, cfg):  # 목봉선(강세 핀바) — 아래꼬리 = 몸통 2배 이상
    return lower_wick(df, i) >= cfg.pin_wick_ratio * body(df, i) \
        and upper_wick(df, i) <= body(df, i) and body(df, i) > 0


def shooting_star(df, i, atr_v, cfg):  # 약세 핀바 — 위꼬리 길다
    return upper_wick(df, i) >= cfg.pin_wick_ratio * body(df, i) \
        and lower_wick(df, i) <= body(df, i) and body(df, i) > 0


# ---- Day5: 포선(장악형, engulfing) ---------------------------------------
def bullish_engulfing(df, i, atr_v, cfg):
    if i < 1:
        return False
    return is_bear(df, i - 1) and is_bull(df, i) \
        and _c(df, i) >= _o(df, i - 1) and _o(df, i) <= _c(df, i - 1) \
        and body(df, i) > body(df, i - 1)


def bearish_engulfing(df, i, atr_v, cfg):
    if i < 1:
        return False
    return is_bull(df, i - 1) and is_bear(df, i) \
        and _o(df, i) >= _c(df, i - 1) and _c(df, i) <= _o(df, i - 1) \
        and body(df, i) > body(df, i - 1)


# ---- Day7: 아침의 별 / Day8: 저녁의 별 (3봉) ------------------------------
def morning_star(df, i, atr_v, cfg):
    if i < 2:
        return False
    c1 = is_bear(df, i - 2) and body(df, i - 2) >= cfg.big_body_atr * atr_v
    c2 = body(df, i - 1) <= 0.5 * body(df, i - 2)  # 작은 중간봉(별)
    c3 = is_bull(df, i) and _c(df, i) > (_o(df, i - 2) + _c(df, i - 2)) / 2
    return c1 and c2 and c3


def evening_star(df, i, atr_v, cfg):
    if i < 2:
        return False
    c1 = is_bull(df, i - 2) and body(df, i - 2) >= cfg.big_body_atr * atr_v
    c2 = body(df, i - 1) <= 0.5 * body(df, i - 2)
    c3 = is_bear(df, i) and _c(df, i) < (_o(df, i - 2) + _c(df, i - 2)) / 2
    return c1 and c2 and c3


# ---- 종합 -----------------------------------------------------------------
# (이름, 함수, 방향)  방향: +1 강세, -1 약세
_BULL = [
    ("big_bull", big_bull, +1),
    ("dragonfly_doji", dragonfly_doji, +1),
    ("bullish_harami", bullish_harami, +1),
    ("hammer", hammer, +1),
    ("bullish_engulfing", bullish_engulfing, +1),
    ("morning_star", morning_star, +1),
]
_BEAR = [
    ("big_bear", big_bear, -1),
    ("gravestone_doji", gravestone_doji, -1),
    ("bearish_harami", bearish_harami, -1),
    ("shooting_star", shooting_star, -1),
    ("bearish_engulfing", bearish_engulfing, -1),
    ("evening_star", evening_star, -1),
]
_NEUTRAL = [("doji", doji, 0)]
ALL_PATTERNS = _BULL + _BEAR + _NEUTRAL


def detect_all(df: pd.DataFrame, atr_v: float, cfg, i: int | None = None) -> dict:
    """마지막 봉(또는 i)에서 감지된 캔들 패턴과 종합 바이어스 반환."""
    if i is None:
        i = len(df) - 1
    found, bull, bear = [], 0, 0
    for name, fn, direction in ALL_PATTERNS:
        try:
            if fn(df, i, atr_v, cfg):
                found.append(name)
                bull += direction > 0
                bear += direction < 0
        except (IndexError, ZeroDivisionError):
            continue
    bias = "bull" if bull > bear else "bear" if bear > bull else "neutral"
    return {"patterns": found, "bull": bull, "bear": bear, "bias": bias}
