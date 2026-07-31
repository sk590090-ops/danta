#!/usr/bin/env python
"""바이낸스 USDT-M 선물 실주문 모듈 — 단타 자동매매의 집행층.

⚠️ 기본은 **테스트넷**(가짜 돈, testnet.binancefuture.com). 실서버는 이중 잠금:
  1) 환경변수 DANTA_LIVE_CONFIRMED=YES        (실서버 전환 — 사용자만)
  2) 주문당 명목가 상한 DANTA_MAX_NOTIONAL    (기본 $200)

키는 환경변수로만 (코드/파일 저장 금지):
  BINANCE_API_KEY / BINANCE_API_SECRET
  테스트넷 키: https://testnet.binancefuture.com 로그인(깃허브 계정 가능) → API Key 탭
  실계정 키(나중에): 선물 거래 권한만, 출금 권한 금지, IP 화이트리스트 필수.

설계: fundarb/live.py(Bybit) 관례 계승 — stdlib만, 돈 경로는 조용한 실패 금지.
  진입 = 시장가 + 거래소측 STOP_MARKET/TAKE_PROFIT_MARKET(closePosition) 동시 설치
  → 봇이 꺼져 있어도 손절/익절은 거래소가 집행한다.
자가검증: python tools/binance_live.py  (서명 벡터 오프라인 테스트)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

TESTNET = "https://testnet.binancefuture.com"
MAINNET = "https://fapi.binance.com"


def is_live() -> bool:
    return os.environ.get("DANTA_LIVE_CONFIRMED") == "YES"


def base_url() -> str:
    return MAINNET if is_live() else TESTNET


def max_notional() -> float:
    """주문당 명목가 상한.

    실서버 기본 $200 = 실탄 초기 소액 원칙(명시적으로 올려야 커진다).
    테스트넷 기본 $1,000 = 가짜 돈이라 손실 방어 목적이 없고, 오히려 전략이
    자연스럽게 내는 사이즈(자본 $500 × 레버 상한 2배)를 그대로 태워야
    파이프라인이 실제로 검증된다. 둘 다 DANTA_MAX_NOTIONAL로 덮어쓸 수 있다.
    """
    env = os.environ.get("DANTA_MAX_NOTIONAL")
    if env:
        return float(env)
    return 200.0 if is_live() else 1000.0


def env_label() -> str:
    return "🔴 실서버" if is_live() else "🧪 테스트넷"


def _keys() -> tuple[str, str]:
    k = os.environ.get("BINANCE_API_KEY", "")
    s = os.environ.get("BINANCE_API_SECRET", "")
    if not k or not s:
        raise RuntimeError(
            "BINANCE_API_KEY / BINANCE_API_SECRET 환경변수가 없습니다.\n"
            "  테스트넷 키: https://testnet.binancefuture.com → API Key 탭\n"
            '  PowerShell:  $env:BINANCE_API_KEY="..."; $env:BINANCE_API_SECRET="..."')
    return k, s


def _sign(secret: str, qs: str) -> str:
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _signed(method: str, path: str, params: dict | None = None,
            timeout: float = 10.0) -> dict | list:
    key, secret = _keys()
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    qs = urllib.parse.urlencode(p)
    qs += "&signature=" + _sign(secret, qs)
    url = base_url() + path + ("?" + qs if method in ("GET", "DELETE") else "")
    data = qs.encode() if method in ("POST", "PUT") else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-MBX-APIKEY": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if '"code":-4411' in body:
            raise RuntimeError(
                "TradFi 퍼프(금·주식토큰) 약관 미서명 — 바이낸스 선물 화면에서 "
                "해당 심볼을 한 번 열어 약관 동의(1회)하면 해제됩니다") from e
        raise RuntimeError(f"Binance {path}: HTTP{e.code} {body[:200]}") from e


def _public(path: str, params: dict | None = None, timeout: float = 10.0):
    url = base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url),
                                timeout=timeout) as r:
        return json.loads(r.read().decode())


# ─────────────── 심볼 필터 (수량/가격 라운딩 — 없으면 주문 거부) ───────────────

_FILTER_CACHE: dict[str, dict] = {}


def filters(symbol: str, timeout: float = 10.0) -> dict:
    if symbol in _FILTER_CACHE:
        return _FILTER_CACHE[symbol]
    # ⚠️ /fapi/v1/exchangeInfo 는 symbol 파라미터를 무시하고 전체를 반환한다.
    #    (첫 항목을 쓰면 엉뚱한 심볼의 stepSize → -1111 Precision 오류)
    info = _public("/fapi/v1/exchangeInfo", None, timeout)
    entry = next((s for s in info.get("symbols") or []
                  if s.get("symbol") == symbol), None)
    if entry is None:
        # 테스트넷은 실서버(851)보다 심볼이 적다(730). 신규 상장·토큰화주식류가
        # 빠지므로 페이퍼 신호가 테스트넷엔 없을 수 있다 — 실서버에선 정상.
        where = "테스트넷에 없는 심볼(실서버엔 존재 가능)" if not is_live() \
            else "거래소에 없는 심볼"
        raise RuntimeError(f"{symbol}: {where} — 실주문 스킵(페이퍼는 계속)")
    # 상장 예정(PENDING_TRADING) 등 거래 불가 상태 — 주문하면 빈 응답/에러.
    st = entry.get("status", "TRADING")
    if st != "TRADING":
        raise RuntimeError(f"{symbol}: 거래 불가 상태({st}) — 실주문 스킵")
    out = {"stepSize": "1", "tickSize": "0.01", "minNotional": 5.0}
    for f in entry.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            out["stepSize"] = f["stepSize"]
        elif f["filterType"] == "PRICE_FILTER":
            out["tickSize"] = f["tickSize"]
        elif f["filterType"] == "MIN_NOTIONAL":
            out["minNotional"] = float(f.get("notional", 5.0))
    _FILTER_CACHE[symbol] = out
    return out


def _mark_price(symbol: str, timeout: float = 10.0) -> float:
    """현재가. ticker/price가 비면 markPrice로 폴백, 둘 다 없으면 명확히 실패.

    (신규 상장 직후엔 ticker가 `{}`로 오는 경우가 있다 — KeyError 대신 안내.)"""
    for path, key in (("/fapi/v1/ticker/price", "price"),
                      ("/fapi/v1/premiumIndex", "markPrice")):
        try:
            r = _public(path, {"symbol": symbol}, timeout)
            if isinstance(r, list):
                r = r[0] if r else {}
            v = r.get(key)
            if v not in (None, "", "0"):
                return float(v)
        except Exception:  # noqa: BLE001 — 다음 소스로 폴백
            continue
    raise RuntimeError(f"{symbol}: 현재가 조회 불가(거래 미개시 추정) — 실주문 스킵")


def _round_step(v: float, step: str) -> str:
    dec = len(step.rstrip("0").split(".")[1]) if "." in step.rstrip("0") else 0
    stepf = float(step)
    floored = int(v / stepf) * stepf
    return f"{floored:.{dec}f}" if dec else str(int(floored))


# ─────────────────────────── 계좌/포지션 ───────────────────────────

def balance(timeout: float = 10.0) -> dict:
    rows = _signed("GET", "/fapi/v2/balance", timeout=timeout)
    for r in rows:
        if r.get("asset") == "USDT":
            return {"balance": float(r["balance"]),
                    "available": float(r["availableBalance"])}
    return {"balance": 0.0, "available": 0.0}


def positions(symbol: str | None = None, timeout: float = 10.0) -> list[dict]:
    p = {"symbol": symbol} if symbol else {}
    rows = _signed("GET", "/fapi/v2/positionRisk", p, timeout=timeout)
    return [{"symbol": r["symbol"], "amt": float(r["positionAmt"]),
             "entry": float(r["entryPrice"]),
             "upnl": float(r["unRealizedProfit"])}
            for r in rows if abs(float(r["positionAmt"])) > 0]


# ─────────────────────────── 주문 ───────────────────────────

def open_trade(symbol: str, direction: str, qty: float,
               stop: float, target: float, timeout: float = 10.0) -> dict:
    """시장가 진입 + 거래소측 손절/익절(closePosition) 설치. 요약 반환."""
    f = filters(symbol, timeout)
    q = _round_step(qty, f["stepSize"])
    if float(q) <= 0:
        raise RuntimeError(f"{symbol}: 수량 라운딩 후 0 (qty={qty})")
    mark = _mark_price(symbol, timeout)
    notional = float(q) * mark
    if notional > max_notional():
        raise RuntimeError(f"{symbol}: 명목 ${notional:,.0f} > 상한 "
                           f"${max_notional():,.0f} (DANTA_MAX_NOTIONAL)")
    if notional < f["minNotional"]:
        raise RuntimeError(f"{symbol}: 명목 ${notional:.1f} < 최소 "
                           f"${f['minNotional']} — 주문 불가")
    side = "BUY" if direction == "LONG" else "SELL"
    opp = "SELL" if direction == "LONG" else "BUY"
    try:  # 레버리지 2배 상한 (실패해도 치명 아님 — 기본값 사용)
        _signed("POST", "/fapi/v1/leverage",
                {"symbol": symbol, "leverage": 2}, timeout=timeout)
    except RuntimeError as e:
        log.warning("레버리지 설정 실패(계속): %s", e)

    entry = _signed("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET", "quantity": q},
        timeout=timeout)
    # 손절/익절은 거래소가 집행 (봇 다운 대비). 2026 API: 조건부 주문은
    # Algo Order 엔드포인트(algoType=CONDITIONAL, triggerPrice) 사용 (-4120 대응).
    sl = _signed("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": opp,
        "type": "STOP_MARKET",
        "triggerPrice": _round_step(stop, f["tickSize"]),
        "closePosition": "true", "workingType": "MARK_PRICE"}, timeout=timeout)
    tp = None
    if target is not None:              # 지표 청산형(일목)은 TP 없이 SL만
        tp = _signed("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": opp,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": _round_step(target, f["tickSize"]),
            "closePosition": "true", "workingType": "CONTRACT_PRICE"},
            timeout=timeout)
    return {"env": env_label(), "symbol": symbol, "qty": q,
            "notional": round(notional, 2),
            "entry_id": entry.get("orderId"),
            "sl_id": sl.get("algoId") or sl.get("orderId"),
            "tp_id": (tp.get("algoId") or tp.get("orderId")) if tp else None}


def update_stops(symbol: str, direction: str, stop: float, target: float,
                 timeout: float = 10.0) -> dict:
    """거래소측 SL/TP 재설치 (본절 이동 등). 기존 algo 주문 취소 후 재설치."""
    f = filters(symbol, timeout)               # 미가용 심볼이면 여기서 예외
    opp = "SELL" if direction == "LONG" else "BUY"
    for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allAlgoOpenOrders"):
        try:
            _signed("DELETE", path, {"symbol": symbol}, timeout=timeout)
            break
        except RuntimeError:
            continue
    sl = _signed("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": opp,
        "type": "STOP_MARKET",
        "triggerPrice": _round_step(stop, f["tickSize"]),
        "closePosition": "true", "workingType": "MARK_PRICE"}, timeout=timeout)
    tp = _signed("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": opp,
        "type": "TAKE_PROFIT_MARKET",
        "triggerPrice": _round_step(target, f["tickSize"]),
        "closePosition": "true", "workingType": "CONTRACT_PRICE"},
        timeout=timeout)
    return {"env": env_label(), "symbol": symbol, "stop": stop,
            "sl_id": sl.get("algoId"), "tp_id": tp.get("algoId")}


def close_trade(symbol: str, timeout: float = 10.0) -> dict:
    """미체결 주문 전부 취소 + 남은 포지션 시장가 청산(reduceOnly)."""
    out: dict = {"env": env_label(), "symbol": symbol}
    try:                      # 진입이 스킵된 심볼이면 청산도 할 게 없다
        filters(symbol, timeout)
    except RuntimeError as e:
        out["note"] = f"거래 대상 아님 — 청산 스킵 ({e})"
        return out
    try:
        _signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol},
                timeout=timeout)
        out["orders_cancelled"] = True
    except RuntimeError as e:
        out["orders_cancelled"] = f"실패: {e}"
    # 조건부(algo) 주문도 취소 — 엔드포인트 후보 순차 시도 (첫 성공에서 중단)
    for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allAlgoOpenOrders"):
        try:
            _signed("DELETE", path, {"symbol": symbol}, timeout=timeout)
            out["algo_cancelled"] = path
            break
        except RuntimeError as e:
            out["algo_cancelled"] = f"실패: {str(e)[:120]}"
    # 청산 후 검증+재시도: 거래소측 TP/SL 부분체결과 시장가 청산이 겹치면
    # 먼지 잔여가 남을 수 있다 (2026-07-30 PAXG 0.001 잔존 실측). 최대 2회.
    closed_total = 0.0
    for attempt in range(2):
        pos = positions(symbol, timeout)
        if not pos:
            break
        amt = pos[0]["amt"]
        side = "SELL" if amt > 0 else "BUY"
        f = filters(symbol, timeout)
        q = _round_step(abs(amt), f["stepSize"])
        if float(q) <= 0:
            out["dust"] = abs(amt)             # 스텝 미만 먼지 — 청산 불가 고지
            break
        r = _signed("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": q, "reduceOnly": "true"}, timeout=timeout)
        closed_total += abs(amt)
        out["order_id"] = r.get("orderId")
        time.sleep(1.0)                        # 체결 반영 대기 후 재검증
    if closed_total:
        out["closed_qty"] = round(closed_total, 8)
    elif "dust" not in out:
        out["note"] = "잔여 포지션 없음"
    leftover = positions(symbol, timeout)
    if leftover:
        out["warning"] = f"잔여 미정리 {leftover[0]['amt']} — 수동 확인 필요"
    return out


if __name__ == "__main__":
    # 오프라인 자가검증: 바이낸스 공식 문서의 HMAC 서명 예제 벡터
    sec = ("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7"
           "H5fATj0j")
    qs = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
          "&price=0.1&recvWindow=5000&timestamp=1499827319559")
    expect = ("c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd"
              "6b71")
    assert _sign(sec, qs) == expect, "서명 벡터 불일치"
    assert _round_step(1.23456, "0.001") == "1.234"
    assert _round_step(1.9, "1") == "1"
    print("selftest OK (HMAC 서명 벡터 + 수량 라운딩)")
    print(f"현재 모드: {env_label()} · 주문 상한 ${max_notional():,.0f}")
