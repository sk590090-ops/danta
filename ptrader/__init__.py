"""
pattern_trader (ptrader)
========================
24/7 패턴 기반 트레이딩 엔진.

seb.ai "24/7 AI Trader" 아키텍처(Scan → Signal → Risk → Plan → Monitor → Decision)를
뼈대로, 손글씨 캔들/차트패턴 노트(Day1~8 + 차트패턴 치트시트)의 매매 규칙을
실제 파이썬 로직으로 구현한 독립 프로그램.

파이프라인:
    datafeed  → OHLCV 로딩(CSV / 합성 / ccxt 선택)
    scanner   → 피처 계산(추세/변동성/거래량)               [MARKET SCANNER]
    signals   → 캔들·차트·MA 신호 → 5대 셋업 스코어링         [SIGNAL ENGINE]
    risk      → 5중 리스크 체크(사이즈/노출/DD/변동성/최대손실) [RISK MODULE]
    planner   → 진입/목표/손절/무효화 + R:R                   [TRADE PLANNER]
    decision  → 최종 메모 → APPROVED / WATCHLIST / REJECTED   [FINAL DECISION]
    monitor   → 위 과정을 24/7 반복(페이퍼)                    [MONITOR LOOP]
"""

__version__ = "0.1.0"

from .config import Config, load_config  # noqa: F401
