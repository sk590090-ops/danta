"""MARKET SCANNER (슬라이드 2/8) — OHLCV에서 추세·변동성·거래량 피처 계산."""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


def scan(df: pd.DataFrame, cfg) -> dict:
    """마지막 봉 기준 시장 상태 피처 dict."""
    sc = cfg.signal
    dfx = ind.add_moving_averages(df, sc.ma_windows)
    atr_series = ind.atr(df, sc.atr_period)
    atr_v = float(atr_series.iloc[-1])
    price = float(df["close"].iloc[-1])
    cross = ind.cross_state(df, sc.fast_ma, sc.slow_ma, sc.trend_ma)

    vol = df["volume"]
    vol_ma = vol.rolling(20, min_periods=5).mean()
    vol_ratio = float(vol.iloc[-1] / (vol_ma.iloc[-1] + 1e-9))

    recent = df.iloc[-sc.breakout_lookback:]
    hi = float(recent["high"].iloc[:-1].max()) if len(recent) > 1 else price
    lo = float(recent["low"].iloc[:-1].min()) if len(recent) > 1 else price

    ma_fast_slope = float(ind.slope(dfx[f"ma{sc.fast_ma}"], 3).iloc[-1]) \
        if f"ma{sc.fast_ma}" in dfx else 0.0

    # RSI(과매수/과매도) + 상위TF 소진 근사(장기MA 대비 ATR 정규화 stretch)
    rsi_v = float(ind.rsi(df["close"], sc.rsi_period).iloc[-1])
    trend_ma_col = f"ma{sc.trend_ma}"
    if trend_ma_col in dfx and not pd.isna(dfx[trend_ma_col].iloc[-1]):
        htf_ref = float(dfx[trend_ma_col].iloc[-1])
    else:  # 장기MA 데이터 부족 시 중기선 대체
        htf_ref = float(dfx[f"ma{sc.slow_ma}"].iloc[-1]) \
            if f"ma{sc.slow_ma}" in dfx and not pd.isna(dfx[f"ma{sc.slow_ma}"].iloc[-1]) \
            else price
    htf_stretch = (price - htf_ref) / (atr_v + 1e-9)   # +면 장기평균 위, -면 아래

    # 스윙 피벗은 여기서 1회만 계산 → engine/charts/planner가 재사용
    swing_highs, swing_lows = ind.swing_points(df, sc.swing_left, sc.swing_right)

    return {
        "price": price,
        "atr": atr_v,
        "atr_pct": atr_v / (price + 1e-9),
        "cross": cross,               # 골든/데드 + 장기선 방향 + allow_long/short
        "vol_ratio": vol_ratio,       # 최근 거래량 / 20MA
        "recent_high": hi,
        "recent_low": lo,
        "fast_ma_slope": ma_fast_slope,
        "rsi": rsi_v,
        "htf_stretch": htf_stretch,   # 장기평균 대비 ATR 배수 (상위TF 소진 근사)
        "df_ma": dfx,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
    }
