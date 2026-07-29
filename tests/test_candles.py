"""캔들 패턴 탐지 단위 테스트 — 합성 봉으로 각 패턴 검증."""
import pandas as pd

from ptrader.config import Config
from ptrader.signals import candles


def _df(rows):
    idx = pd.date_range("2025-01-01", periods=len(rows), freq="1h")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                        index=idx)


CFG = Config().signal
ATR = 10.0  # 고정 ATR로 임계 단순화


def test_big_bull():
    df = _df([[100, 101, 99, 100, 1], [100, 130, 99, 128, 1]])
    assert candles.big_bull(df, 1, ATR, CFG)
    assert not candles.big_bear(df, 1, ATR, CFG)


def test_bullish_engulfing():
    df = _df([[100, 101, 90, 92, 1], [91, 112, 90, 110, 1]])
    assert candles.bullish_engulfing(df, 1, ATR, CFG)


def test_hammer():
    # 몸통 작고 아래꼬리 김
    df = _df([[100, 101, 80, 99, 1]])
    assert candles.hammer(df, 0, ATR, CFG)


def test_shooting_star():
    df = _df([[100, 120, 99, 101, 1]])
    assert candles.shooting_star(df, 0, ATR, CFG)


def test_doji():
    df = _df([[100, 105, 95, 100.2, 1]])
    assert candles.doji(df, 0, ATR, CFG)


def test_morning_star():
    df = _df([
        [130, 131, 100, 102, 1],   # 큰 음선
        [101, 103, 98, 100, 1],    # 작은 별
        [101, 125, 100, 123, 1],   # 큰 양선, 첫봉 중간 위 마감
    ])
    assert candles.morning_star(df, 2, ATR, CFG)


def test_evening_star():
    df = _df([
        [100, 131, 99, 128, 1],    # 큰 양선
        [129, 132, 127, 130, 1],   # 작은 별
        [129, 130, 100, 102, 1],   # 큰 음선
    ])
    assert candles.evening_star(df, 2, ATR, CFG)


def test_detect_all_bias():
    df = _df([[100, 101, 90, 92, 1], [91, 112, 90, 110, 1]])
    res = candles.detect_all(df, ATR, CFG)
    assert res["bias"] == "bull"
    assert "bullish_engulfing" in res["patterns"]
