"""
MONITOR LOOP (슬라이드 6/8) — 24/7 반복 감시(페이퍼).
run_once: 전 심볼 1회 스캔→결정, 승인 건은 페이퍼 진입.
run_loop: poll_seconds 간격으로 무한 반복(Ctrl+C 종료).
실주문 없음 — 모든 실행은 사람 승인/실계좌 연동 시에만.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import datafeed
from .decision import format_memo
from .pipeline import analyze
from .risk import AccountState


@dataclass
class PaperPosition:
    symbol: str
    direction: str
    entry: float
    stop: float
    target: float
    qty: float
    opened_ts: str

    def unrealized(self, price):
        s = 1 if self.direction == "LONG" else -1
        return (price - self.entry) * s * self.qty


@dataclass
class PaperBook:
    equity: float
    peak_equity: float = 0.0
    positions: list = field(default_factory=list)
    closed: list = field(default_factory=list)

    def state_path(self, d="."):
        return Path(d) / "paper_state.json"

    def save(self, d="."):
        self.state_path(d).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    @classmethod
    def load(cls, d=".", default_equity=10_000.0):
        """paper_state.json에서 복원 — 예약작업식 1회 실행(bot) 간 상태 유지."""
        p = Path(d) / "paper_state.json"
        if not p.exists():
            return cls(equity=default_equity)
        raw = json.loads(p.read_text(encoding="utf-8"))
        book = cls(equity=raw.get("equity", default_equity),
                   peak_equity=raw.get("peak_equity", 0.0),
                   closed=raw.get("closed", []))
        book.positions = [PaperPosition(**q) for q in raw.get("positions", [])]
        return book


def _open_exposure(book: PaperBook, prices: dict) -> float:
    return sum(abs(p.qty) * prices.get(p.symbol, p.entry) for p in book.positions)


def run_once(cfg, book: PaperBook, verbose=True) -> list:
    """전 심볼 1회 판단. 승인 & 미보유 심볼은 페이퍼 진입."""
    decisions = []
    prices = {}
    for sym in cfg.symbols:
        df = datafeed.load(sym, cfg)
        prices[sym] = float(df["close"].iloc[-1])

    # 보유 포지션 TP/SL 관리
    _manage_positions(book, prices, verbose)

    for sym in cfg.symbols:
        df = datafeed.load(sym, cfg)
        acct = AccountState(
            equity=book.equity, peak_equity=book.peak_equity,
            open_exposure=_open_exposure(book, prices),
            open_positions=len(book.positions))
        dec = analyze(sym, df, acct, cfg)
        decisions.append(dec)
        if verbose:
            print(format_memo(dec))
            print()

        held = any(p.symbol == sym for p in book.positions)
        if dec.status == "APPROVED" and not held:
            p = dec.memo["4_trade_plan"]
            r = dec.memo["3_risk"]
            book.positions.append(PaperPosition(
                symbol=sym, direction=p["direction"], entry=p["entry"],
                stop=p["stop"], target=p["target"], qty=r["position_qty"],
                opened_ts=dec.ts))
            if verbose:
                print(f"  → 📝 페이퍼 진입: {sym} {p['direction']} "
                      f"qty={r['position_qty']}\n")
    book.peak_equity = max(book.peak_equity, book.equity)
    return decisions


def _manage_positions(book: PaperBook, prices: dict, verbose=True):
    still = []
    for p in book.positions:
        price = prices.get(p.symbol)
        if price is None:
            still.append(p)
            continue
        hit_stop = (p.direction == "LONG" and price <= p.stop) or \
                   (p.direction == "SHORT" and price >= p.stop)
        hit_tp = (p.direction == "LONG" and price >= p.target) or \
                 (p.direction == "SHORT" and price <= p.target)
        if hit_stop or hit_tp:
            pnl = p.unrealized(p.stop if hit_stop else p.target)
            book.equity += pnl
            book.closed.append({"symbol": p.symbol, "direction": p.direction,
                                "pnl": round(pnl, 2),
                                "exit": "STOP" if hit_stop else "TARGET"})
            if verbose:
                tag = "🛑 손절" if hit_stop else "🎯 익절"
                print(f"  → {tag} {p.symbol} PnL={pnl:+.2f} → equity={book.equity:.2f}")
        else:
            still.append(p)
    book.positions = still


def run_loop(cfg, book: PaperBook, max_iters: int | None = None, save_dir="."):
    print(f"[MONITOR LOOP] 시작 — {len(cfg.symbols)}개 심볼, "
          f"{cfg.poll_seconds}s 간격 (Ctrl+C 종료)")
    i = 0
    try:
        while max_iters is None or i < max_iters:
            i += 1
            print(f"\n===== iteration {i} =====")
            run_once(cfg, book)
            book.save(save_dir)
            if max_iters is not None and i >= max_iters:
                break
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        print("\n[MONITOR LOOP] 사용자 종료")
    book.save(save_dir)
    print(f"[MONITOR LOOP] 종료. equity={book.equity:.2f}, "
          f"보유={len(book.positions)}, 청산={len(book.closed)}")
