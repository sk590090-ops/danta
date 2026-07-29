"""설정 (config.yaml 로드 + 기본값)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01      # 1회 거래당 위험(계좌 대비)
    max_position_pct: float = 0.30    # 단일 포지션 최대 비중
    max_total_exposure: float = 1.0   # 총 노출 한도(계좌 배수)
    max_drawdown: float = 0.20        # 허용 최대 낙폭
    max_loss_per_trade: float = 0.02  # 1회 최대 손실 한도
    atr_vol_min: float = 0.002        # 최소 변동성(ATR/price)
    atr_vol_max: float = 0.08         # 최대 변동성(ATR/price)


@dataclass
class SignalConfig:
    ma_windows: tuple[int, ...] = (5, 20, 100, 200)
    fast_ma: int = 5
    slow_ma: int = 20
    trend_ma: int = 200
    atr_period: int = 14
    swing_left: int = 3               # 스윙 피벗 좌측 봉수
    swing_right: int = 3              # 스윙 피벗 우측 봉수
    big_body_atr: float = 1.2         # 큰 양/음선 기준(ATR 배수)
    doji_body_ratio: float = 0.1      # 도지 몸통/전체범위 비율
    pin_wick_ratio: float = 2.0       # 핀바 꼬리/몸통 비율
    breakout_lookback: int = 20       # 돌파 판정용 최근 고저 구간
    min_score_watch: int = 45         # 관찰 최소 스코어
    min_score_approve: int = 65       # 승인 최소 스코어
    # 특정 셋업 비활성화 (튜닝: 손실 셋업 제거). 예: ("REVERSAL","PULLBACK")
    disabled_setups: tuple[str, ...] = ()
    # --- REVERSAL 컨플루언스(신호 질 개선). 고정 규칙(과최적화 방지 위해 튜닝 대상 아님) ---
    rsi_period: int = 14
    reversal_confluence: bool = False  # True면 4요인 중 rev_min_confluence개↑ 충족 필요
    rev_min_confluence: int = 2        # 4요인 중 최소 충족 개수(거래량/RSI/상위TF/확정형)
    rev_vol_mult: float = 1.3          # 반전봉 거래량 클라이맥스(20MA 대비)
    rev_rsi_os: float = 40.0           # 롱: RSI 과매도 임계
    rev_rsi_ob: float = 60.0           # 숏: RSI 과매수 임계
    rev_stretch_atr: float = 1.2       # 장기평균 대비 최소 stretch(ATR 배수)
    # --- CONVERGENCE 셋업(이평선 수렴+거래량, bullstory1 노트). 고정 규칙 ---
    conv_mas: tuple[int, ...] = (5, 20, 60)  # 수렴 판정 이평선
    conv_band_atr: float = 1.0         # 수렴 인정: MA 최대-최소 ≤ ATR×이 값
    conv_min_bars: int = 30            # 수렴 최소 지속 봉수("2~3주+" 번역)
    conv_dry_ratio: float = 0.85       # 수렴 중 거래량 바닥: vol10 ≤ vol50×이 값
    conv_break_vol: float = 2.0        # 이탈봉 거래량 ≥ vol20×이 값("2~3배")
    # --- OBV_DIV / VWAP 셋업 (지표스레드 2차 노트). 고정 규칙 ---
    obv_recent_bars: int = 8           # 다이버전스 2번째 피벗이 최근 N봉 내
    obv_prd_lookback: int = 20         # OBV_PRD: 신저가·OBV 비교 구간(Gemini PRD)
    vwap_hours: float = 24.0           # 롤링 VWAP 창(시간) — 크립토 24/7 일봉리셋 대체
    vwap_vol_mult: float = 1.2         # VWAP 돌파봉 거래량 확인(20MA 대비)


@dataclass
class PlannerConfig:
    rr_target: float = 2.0            # 목표 손익비(진입가 대비 목표까지 = R배수)
    atr_stop_mult: float = 1.0        # ATR 기반 손절 최소폭
    stop_buffer_atr: float = 0.3      # 무효화선 여유(ATR)


@dataclass
class DecisionConfig:
    require_trend_alignment: bool = False  # True면 추세 역행 진입 거부
    min_rr: float = 1.5                    # 최소 손익비


@dataclass
class CcxtConfig:
    exchange: str = "binance"          # ccxt 거래소 id (binance, bybit, okx ...)
    market_type: str = "spot"          # spot | swap(무기한선물)
    limit: int = 500                   # 확보할 총 봉수(페이지네이션)
    max_per_call: int = 1000           # 거래소 1콜 최대(대부분 1000)
    retries: int = 3
    retry_wait: float = 1.5
    quote_hint: tuple[str, ...] = ("USDT", "USDC", "BUSD", "USD",
                                   "BTC", "ETH", "KRW")


@dataclass
class Config:
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    timeframe: str = "1h"
    equity: float = 10_000.0
    data_source: str = "synthetic"     # synthetic | csv | ccxt
    data_dir: str = "data"
    poll_seconds: int = 60
    ccxt: CcxtConfig = field(default_factory=CcxtConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _merge(dc, data: dict):
    """dataclass 인스턴스에 dict 값을 얕게 병합."""
    for k, v in (data or {}).items():
        if hasattr(dc, k):
            cur = getattr(dc, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                _merge(cur, v)
            elif isinstance(cur, tuple) and isinstance(v, list):
                setattr(dc, k, tuple(v))
            else:
                setattr(dc, k, v)
    return dc


def load_config(path: str | Path | None = None) -> Config:
    """config.yaml을 로드. 없으면 기본값. PyYAML 없으면 기본값으로 폴백."""
    cfg = Config()
    if path is None:
        return cfg
    p = Path(path)
    if not p.exists():
        return cfg
    try:
        import yaml  # type: ignore
    except ImportError:
        print("[config] PyYAML 미설치 → 기본 설정 사용")
        return cfg
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    top = {k: v for k, v in data.items()
           if k not in ("risk", "signal", "planner", "decision", "ccxt")}
    _merge(cfg, top)
    _merge(cfg.risk, data.get("risk", {}))
    _merge(cfg.signal, data.get("signal", {}))
    _merge(cfg.planner, data.get("planner", {}))
    _merge(cfg.decision, data.get("decision", {}))
    _merge(cfg.ccxt, data.get("ccxt", {}))
    return cfg
