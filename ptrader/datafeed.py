"""데이터 피드 — OHLCV 로딩(CSV / 합성 / ccxt 선택)."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: c.lower() for c in df.columns})
    missing = [c for c in OHLCV_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV 컬럼 누락: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        for cand in ("timestamp", "time", "date", "datetime"):
            if cand in df.columns:
                df = df.set_index(pd.to_datetime(df[cand], unit=_guess_unit(df[cand])))
                break
    return df[OHLCV_COLS].astype(float).sort_index()


def _guess_unit(s: pd.Series):
    v = pd.to_numeric(s, errors="coerce").dropna()
    if len(v) and v.iloc[0] > 1e12:
        return "ms"
    if len(v) and v.iloc[0] > 1e9:
        return "s"
    return None


def load_csv(path: str | Path) -> pd.DataFrame:
    return _normalize(pd.read_csv(path))


def synthetic_ohlcv(n: int = 500, seed: int = 7, start_price: float = 30_000.0,
                    freq: str = "1h") -> pd.DataFrame:
    """추세·조정·박스가 섞인 현실적 합성 OHLCV 생성(오프라인 테스트용)."""
    rng = np.random.default_rng(seed)
    # 국면(regime) 전환이 있는 드리프트
    regimes = rng.choice([0.0006, -0.0005, 0.0], size=(n // 60 + 1),
                         p=[0.45, 0.30, 0.25])
    drift = np.repeat(regimes, 60)[:n]
    vol = 0.012
    rets = drift + rng.normal(0, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    hi_noise = np.abs(rng.normal(0, vol, n)) * close
    lo_noise = np.abs(rng.normal(0, vol, n)) * close
    high = np.maximum(open_, close) + hi_noise
    low = np.minimum(open_, close) - lo_noise
    volume = rng.lognormal(mean=6.0, sigma=0.5, size=n) * (1 + np.abs(rets) * 20)
    idx = pd.date_range("2025-01-01", periods=n, freq=_pd_freq(freq))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _pd_freq(tf: str) -> str:
    m = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
         "1h": "1h", "4h": "4h", "1d": "1D"}
    return m.get(tf, "1h")


def to_ccxt_symbol(symbol: str, quote_hint=("USDT", "USDC", "BUSD", "USD",
                                            "BTC", "ETH", "KRW")) -> str:
    """`BTCUSDT` → `BTC/USDT` 변환. 이미 '/' 있으면 그대로."""
    if "/" in symbol:
        return symbol
    up = symbol.upper()
    for q in quote_hint:
        if up.endswith(q) and len(up) > len(q):
            return f"{up[:-len(q)]}/{q}"
    return symbol  # 못 맞추면 원본(거래소가 알아서 처리하거나 에러)


def _make_exchange(cc):
    import ccxt  # type: ignore
    if not hasattr(ccxt, cc.exchange):
        raise ValueError(f"알 수 없는 ccxt 거래소: {cc.exchange}")
    opts = {"enableRateLimit": True}
    if cc.market_type in ("swap", "future", "futures"):
        opts["options"] = {"defaultType": "swap"}
    return getattr(ccxt, cc.exchange)(opts)


def fetch_ccxt(symbol: str, timeframe: str, cc) -> pd.DataFrame:
    """
    실거래소 OHLCV. 페이지네이션으로 cc.limit 봉 확보, 재시도 포함.
    cc: CcxtConfig
    """
    try:
        import ccxt  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "ccxt 미설치. `pip install ccxt` 후 사용하거나 data_source=synthetic 사용."
        ) from e

    ex = _make_exchange(cc)
    market = to_ccxt_symbol(symbol, cc.quote_hint)
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    want = cc.limit
    per = min(cc.max_per_call, want)
    # 최신 want봉을 받기 위한 시작 시각 추정
    now = ex.milliseconds()
    since = now - (want + 5) * tf_ms

    rows: list = []
    guard = 0
    while len(rows) < want and guard < 100:
        guard += 1
        batch = _fetch_with_retry(ex, market, timeframe, since, per, cc)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + tf_ms
        if len(batch) < per:            # 더 이상 없음
            break

    if not rows:
        raise RuntimeError(f"{cc.exchange}:{market} OHLCV 응답 없음")

    df = pd.DataFrame(rows, columns=["timestamp", *OHLCV_COLS])
    df = df.drop_duplicates(subset="timestamp")
    df = df.set_index(pd.to_datetime(df["timestamp"], unit="ms"))
    df = df[OHLCV_COLS].astype(float).sort_index()
    return df.iloc[-want:]              # 최신 want봉만


def _fetch_with_retry(ex, market, timeframe, since, per, cc):
    import ccxt  # type: ignore
    last_err = None
    for attempt in range(cc.retries):
        try:
            return ex.fetch_ohlcv(market, timeframe=timeframe,
                                  since=since, limit=per)
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            last_err = e
            time.sleep(cc.retry_wait * (attempt + 1))
    raise RuntimeError(
        f"{cc.exchange}:{market} 조회 실패({cc.retries}회): "
        f"{type(last_err).__name__} {str(last_err)[:150]}")


def load_cached(symbol: str, cfg, cache_dir: str = "data/cache",
                refresh: bool = False) -> pd.DataFrame:
    """실데이터를 parquet로 캐시. 튜닝 시 반복 조회 방지."""
    ex = cfg.ccxt.exchange if cfg.data_source == "ccxt" else cfg.data_source
    key = f"{ex}_{symbol.replace('/', '')}_{cfg.timeframe}_{cfg.ccxt.limit}.parquet"
    path = Path(cache_dir) / key
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    df = load(symbol, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path)
    except Exception:  # parquet 엔진 없으면 CSV 폴백
        df.to_csv(path.with_suffix(".csv"))
    return df


def load(symbol: str, cfg) -> pd.DataFrame:
    """config에 따라 적절한 소스에서 OHLCV 로드."""
    src = cfg.data_source
    if src == "synthetic":
        seed = abs(hash(symbol)) % 10_000
        return synthetic_ohlcv(n=600, seed=seed, freq=cfg.timeframe)
    if src == "csv":
        path = Path(cfg.data_dir) / f"{symbol}_{cfg.timeframe}.csv"
        return load_csv(path)
    if src == "ccxt":
        return fetch_ccxt(symbol, cfg.timeframe, cfg.ccxt)
    raise ValueError(f"알 수 없는 data_source: {src}")
