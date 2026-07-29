"""기술 지표 — 이동평균, ATR, 스윙 피벗, 골든/데드크로스 (노트: 이동평균선 페이지)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_moving_averages(df: pd.DataFrame, windows=(5, 20, 100, 200)) -> pd.DataFrame:
    out = df.copy()
    for w in windows:
        out[f"ma{w}"] = sma(out["close"], w)
    return out


def slope(s: pd.Series, n: int = 5) -> pd.Series:
    """최근 n봉 선형 기울기(정규화)."""
    return (s - s.shift(n)) / (s.shift(n).abs() + 1e-9)


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3):
    """
    스윙 고점/저점(피벗) 탐지 — 벡터화. 확정 피벗만(우측 right봉 확인 필요).
    반환: (highs, lows) — 각 index→price 인 pandas Series (피벗만).
    """
    h = df["high"]
    l = df["low"]
    # 좌측 left봉의 최대/최소 (i-left .. i-1)
    left_max = h.rolling(left).max().shift(1)
    left_min = l.rolling(left).min().shift(1)
    # 우측 right봉의 최대/최소 (i+1 .. i+right)
    right_max = h.rolling(right).max().shift(-right)
    right_min = l.rolling(right).min().shift(-right)
    is_high = (h > left_max) & (h >= right_max)
    is_low = (l < left_min) & (l <= right_min)
    highs = h[is_high.fillna(False)]
    lows = l[is_low.fillna(False)]
    return highs, lows


def cross_state(df: pd.DataFrame, fast: int = 5, slow: int = 20, trend: int = 200):
    """
    골든/데드크로스 + 장기선 방향 필터 상태를 마지막 봉 기준으로 반환.
    노트 규칙:
      - 골든크로스 + 장기선 상승 → 매수
      - 데드크로스 + 장기선 하락 → 매도
      - 장기선 방향과 반대 크로스 → 매수/매도 금지
    """
    c = df["close"]
    f, s = sma(c, fast), sma(c, slow)
    t = sma(c, trend)
    if f.isna().iloc[-1] or s.isna().iloc[-1]:
        return {"cross": "none", "trend": "unknown", "allow_long": False,
                "allow_short": False}
    diff = f - s
    prev, now = diff.iloc[-2], diff.iloc[-1]
    cross = "golden" if prev <= 0 < now else "dead" if prev >= 0 > now else "none"
    if t.notna().iloc[-1]:
        tslope = t.iloc[-1] - t.iloc[-min(len(t), trend // 4 + 1)]
        trend_dir = "up" if tslope > 0 else "down" if tslope < 0 else "flat"
    else:  # 장기선 데이터 부족 → 중기선 방향으로 대체
        tslope = s.iloc[-1] - s.iloc[-min(len(s), slow)]
        trend_dir = "up" if tslope > 0 else "down" if tslope < 0 else "flat"
    ma_up = f.iloc[-1] > s.iloc[-1]
    return {
        "cross": cross,
        "trend": trend_dir,
        "fast_over_slow": bool(ma_up),
        # 장기선 방향과 정렬될 때만 허용 (매수/매도 금지 규칙)
        "allow_long": trend_dir != "down",
        "allow_short": trend_dir != "up",
    }
