"""라이브 배선 검증 — daily_scan._live 경로로 테스트넷 진입→청산 왕복.

일회성 점검용(실운영 파이프라인과 동일 코드 경로). 소액 명목가로만 실행.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import daily_scan as ds          # noqa: E402
import binance_live as b         # noqa: E402

SYM = sys.argv[1] if len(sys.argv) > 1 else "ETHUSDT"
NOTIONAL = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

ds.LIVE_EXEC = True              # 실제 파이프라인 래퍼 사용
px = float(b._public("/fapi/v1/ticker/price", {"symbol": SYM})["price"])
qty = NOTIONAL / px
print(f"{SYM} 현재가 {px:g} · 수량 {qty:.6f} (약 ${NOTIONAL:,.0f} 명목)")
print(f"모드: {b.env_label()} · 상한 ${b.max_notional():,.0f}\n")

print("[1] 진입 (SHORT, 파이프라인 _live 경로):")
print("   ", ds._live("open_trade", SYM, "SHORT", qty, px * 1.02, px * 0.96))
time.sleep(2)
print("[2] 거래소 포지션:", b.positions(SYM))
print("[3] 청산 (파이프라인 _live 경로):")
print("   ", ds._live("close_trade", SYM))
print("[4] 잔여 포지션:", b.positions(SYM))
print("[5] 잔고:", b.balance())
