#!/usr/bin/env python
"""
pattern_trader CLI 진입점.

사용법:
  python run.py demo                  # 합성데이터로 파이프라인 1회 시연
  python run.py scan                  # config 심볼 1회 스캔→결정 메모
  python run.py monitor --iters 3     # 24/7 루프(테스트로 3회)
  python run.py backtest --symbol BTCUSDT
옵션:
  --config config.yaml  --source synthetic|csv|ccxt
"""
from __future__ import annotations

import argparse
import json
import sys

# Windows 콘솔(cp949)에서 한글/이모지 출력 깨짐 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from ptrader.config import load_config
from ptrader import datafeed, backtest
from ptrader.monitor import PaperBook, run_once, run_loop
from ptrader.pipeline import analyze
from ptrader.risk import AccountState
from ptrader.decision import format_memo


def _apply_overrides(cfg, args):
    if getattr(args, "source", None):
        cfg.data_source = args.source
    if getattr(args, "symbol", None):
        cfg.symbols = [args.symbol]
    return cfg


def cmd_demo(cfg, args):
    sym = cfg.symbols[0]
    df = datafeed.load(sym, cfg)
    print(f"[DEMO] {sym} 합성 OHLCV {len(df)}봉, "
          f"현재가 {df['close'].iloc[-1]:.2f}\n")
    acct = AccountState(equity=cfg.equity)
    dec = analyze(sym, df, acct, cfg)
    print(format_memo(dec))
    print("\n[JSON 메모]")
    print(json.dumps(dec.as_dict(), ensure_ascii=False, indent=2))


def cmd_scan(cfg, args):
    book = PaperBook(equity=cfg.equity)
    run_once(cfg, book, verbose=True)


def cmd_monitor(cfg, args):
    book = PaperBook(equity=cfg.equity)
    run_loop(cfg, book, max_iters=args.iters, save_dir=".")


def cmd_bot(cfg, args):
    """예약작업용 1사이클 — 상태 로드→판단/페이퍼 진입·청산→저장."""
    book = PaperBook.load(".", default_equity=cfg.equity)
    run_once(cfg, book, verbose=True)
    book.peak_equity = max(book.peak_equity, book.equity)
    book.save(".")
    print(f"[BOT] equity={book.equity:.2f} 보유={len(book.positions)} "
          f"청산누적={len(book.closed)}")


def cmd_backtest(cfg, args):
    sym = cfg.symbols[0]
    df = datafeed.load(sym, cfg)
    print(f"[BACKTEST] {sym} {len(df)}봉 ({cfg.data_source})")
    res = backtest.run(df, cfg, symbol=sym)
    print("\n[성과 통계]")
    print(json.dumps(res.stats, ensure_ascii=False, indent=2))
    if res.trades:
        print(f"\n최근 거래 {min(5, len(res.trades))}건:")
        for t in res.trades[-5:]:
            print(f"  {t['setup']:<18} {t['direction']:<5} "
                  f"{t['reason']:<7} pnl={t['pnl']:+.2f} eq={t['equity']:.2f}")


def main():
    ap = argparse.ArgumentParser(description="pattern_trader CLI")
    ap.add_argument("command",
                    choices=["demo", "scan", "monitor", "backtest", "bot"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--source", default=None,
                    choices=["synthetic", "csv", "ccxt"])
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--iters", type=int, default=3)
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg = _apply_overrides(cfg, args)

    {"demo": cmd_demo, "scan": cmd_scan, "monitor": cmd_monitor,
     "backtest": cmd_backtest, "bot": cmd_bot}[args.command](cfg, args)


if __name__ == "__main__":
    main()
