"""이벤트 감시 루프 — 첫 이벤트 발생 시 요약 출력 후 종료 (세션 감시용).

감지: 일목 신호 / 신규 복기 / 실체결 / 본절 이동 / 포지션 변동 / 스캔 정지.
사용: python tools/_watch_events.py   (백그라운드 실행 전제, 4분 주기)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "futures_paper.json"
LOG = ROOT / "logs" / "daily_scan.log"


def _led() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"open": [], "closed": []}


def _log_count(needle: str) -> int:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").count(needle)
    except Exception:
        return 0


def snapshot() -> dict:
    d = _led()
    return {
        "ichi": _log_count("일목4h"),
        "fill": _log_count("실주문 체결"),
        "be": _log_count("본절 이동"),
        "rv": sum(1 for t in d["closed"] if t.get("review")),
        "closed": len(d["closed"]),
        "open": len(d["open"]),
    }


def main() -> int:
    base = snapshot()
    while True:
        time.sleep(240)
        cur = snapshot()
        d = _led()
        if cur["ichi"] != base["ichi"]:
            print("ICHIMOKU: 일목 신호 발생")
            for ln in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
                if "일목4h" in ln:
                    last = ln
            print(" ", last.strip())
            return 0
        if cur["rv"] != base["rv"]:
            print("REVIEW: 신규 복기 판정")
            revs = [t for t in d["closed"] if t.get("review")]
            for t in revs[base["rv"]:]:
                print(f"  {t['symbol']} [{t['reason']}] ${t['pnl']:+.2f} → {t['review']}")
            return 0
        if cur["fill"] != base["fill"]:
            print("LIVE_FILL: 실체결 발생")
            return 0
        if cur["be"] != base["be"]:
            print("BE_MOVE: 본절 이동 발동")
            return 0
        if (cur["closed"], cur["open"]) != (base["closed"], base["open"]):
            if cur["closed"] > base["closed"]:
                for t in d["closed"][base["closed"]:]:
                    print(f"EXIT {t['symbol']} [{t['reason']}] "
                          f"${t['pnl']:+.2f} ({t.get('r', 0):+.2f}R)")
            if cur["open"] > base["open"]:
                p = d["open"][-1]
                print(f"ENTRY {p['symbol']} {p['direction']} "
                      f"{p.get('setup', 'OBV')} 진입 {p['entry']}")
            return 0
        try:
            age = time.time() - LOG.stat().st_mtime
            if age > 5400:
                print(f"STALE: 스캔 {age:.0f}s 정지 — 예약작업 점검 필요")
                return 1
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
