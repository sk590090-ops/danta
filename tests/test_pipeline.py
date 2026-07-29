"""파이프라인/지표/백테스트 통합 스모크 테스트."""
import pandas as pd

from ptrader.config import Config
from ptrader import datafeed, scanner, backtest, indicators as ind
from ptrader.pipeline import analyze
from ptrader.risk import AccountState
from ptrader.decision import Decision


def test_synthetic_feed_shape():
    df = datafeed.synthetic_ohlcv(n=300, seed=1)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 300
    # 고가 >= 저가, 고가 >= 시/종가
    assert (df["high"] >= df["low"]).all()
    assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-6).all()


def test_indicators():
    df = datafeed.synthetic_ohlcv(n=300, seed=2)
    a = ind.atr(df, 14)
    assert a.iloc[-1] > 0
    cs = ind.cross_state(df, 5, 20, 200)
    assert cs["cross"] in ("golden", "dead", "none")
    assert cs["trend"] in ("up", "down", "flat", "unknown")


def test_analyze_returns_decision():
    cfg = Config()
    df = datafeed.synthetic_ohlcv(n=400, seed=3)
    acct = AccountState(equity=cfg.equity)
    dec = analyze("BTCUSDT", df, acct, cfg)
    assert isinstance(dec, Decision)
    assert dec.status in ("APPROVED", "WATCHLIST", "REJECTED")
    assert set(dec.memo.keys()) == {
        "1_setup_summary", "2_signal_strength", "3_risk",
        "4_trade_plan", "5_final"}


def test_scan_features():
    cfg = Config()
    df = datafeed.synthetic_ohlcv(n=400, seed=4)
    feats = scanner.scan(df, cfg)
    for k in ("price", "atr", "atr_pct", "cross", "vol_ratio",
              "recent_high", "recent_low"):
        assert k in feats
    assert feats["atr_pct"] > 0


def test_ccxt_symbol_normalization():
    # 네트워크 불필요 — 심볼 포맷 변환만 검증
    assert datafeed.to_ccxt_symbol("BTCUSDT") == "BTC/USDT"
    assert datafeed.to_ccxt_symbol("ETH/USDT") == "ETH/USDT"
    assert datafeed.to_ccxt_symbol("SOLUSDT") == "SOL/USDT"
    assert datafeed.to_ccxt_symbol("XRPKRW") == "XRP/KRW"


def test_backtest_runs():
    cfg = Config()
    df = datafeed.synthetic_ohlcv(n=600, seed=5)
    res = backtest.run(df, cfg, symbol="BTCUSDT")
    assert "n_trades" in res.stats
    # 거래가 있으면 통계 키 존재
    if res.stats["n_trades"] > 0:
        assert "win_rate" in res.stats
        assert "profit_factor" in res.stats
