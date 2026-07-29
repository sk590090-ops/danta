"""
SIGNAL ENGINE (슬라이드 3/8) — Scan→Detect→Score.
캔들·차트·MA 신호를 5대 셋업으로 종합하고 확률 스코어링.

5대 셋업(슬라이드 3/8):
  BREAKOUT           가격이 핵심 레벨 돌파
  PULLBACK           추세 중 조정 후 재진입
  MOMENTUM           강한 몸통 + MA 기울기 가속
  TREND_CONTINUATION MA 정렬 + 깃발/삼각형 연속 패턴
  REVERSAL           극단에서 반전 캔들/차트 패턴
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import candles, charts


@dataclass
class Signal:
    setup: str                       # 5대 셋업 중 하나 or "NONE"
    direction: str                   # LONG | SHORT | NONE
    score: int                       # 0~100
    reasons: list[str] = field(default_factory=list)
    candles: dict = field(default_factory=dict)
    charts: dict = field(default_factory=dict)

    def as_dict(self):
        return {"setup": self.setup, "direction": self.direction,
                "score": self.score, "reasons": self.reasons}


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, int(round(x))))


def _confluence_factors(sc, feats, direction, confirmed):
    """충족된 컨플루언스 요인 리스트 반환(거래량/RSI/상위TF/확정형)."""
    vol = feats.get("vol_ratio", 1.0)
    rsi = feats.get("rsi", 50.0)
    stretch = feats.get("htf_stretch", 0.0)
    got = []
    if vol >= sc.rev_vol_mult:
        got.append(f"거래량x{vol:.1f}")
    if confirmed:
        got.append("확정형")
    if direction == "LONG":
        if rsi <= sc.rev_rsi_os:
            got.append(f"과매도{rsi:.0f}")
        if stretch <= -sc.rev_stretch_atr:
            got.append(f"하방stretch{stretch:.1f}")
    else:
        if rsi >= sc.rev_rsi_ob:
            got.append(f"과매수{rsi:.0f}")
        if stretch >= sc.rev_stretch_atr:
            got.append(f"상방stretch{stretch:.1f}")
    return got


def _reversal_confluence(sc, feats, direction, confirmed):
    """
    REVERSAL v2 게이트 — 4요인(거래량·RSI·상위TF stretch·확정형) 중
    rev_min_confluence개 이상 충족 시 통과. OFF면 항상 통과(v1).
    반환: (통과여부, 근거 리스트).
    """
    if not sc.reversal_confluence:
        return True, []
    got = _confluence_factors(sc, feats, direction, confirmed)
    ok = len(got) >= sc.rev_min_confluence
    return ok, ([f"✔컨플루언스 {len(got)}개: " + ", ".join(got)] if ok else [])


def _confluence_bonus(sc, feats, direction):
    """충족 요인 수에 비례한 스코어 가산(v2에서만)."""
    if not sc.reversal_confluence:
        return 0
    got = _confluence_factors(sc, feats, direction, True)
    return min(len(got) * 4, 16)


def _convergence_signal(df, sc, feats):
    """
    수렴+거래량 셋업 (bullstory1 "이평선=세력 원가 장부" 번역):
      ① MA(5,20,60) 수렴: (max-min) ≤ ATR×conv_band_atr 가 직전 conv_min_bars 중
         80% 이상 유지 (직전 봉까지 — 이탈봉 자신은 제외)
      ② 수렴 중 거래량 바닥: vol10 ≤ vol50×conv_dry_ratio (직전 봉 기준)
      ③ 이탈: 현재 종가가 수렴구간 고/저 밖 + 거래량 ≥ vol20×conv_break_vol
    방향 = 이탈 방향. 셋업명 CONVERGENCE.
    """
    n_need = max(sc.conv_mas) + sc.conv_min_bars + 2
    if len(df) < n_need:
        return None
    close, vol = df["close"], df["volume"]
    atr_v = feats["atr"]
    mas = [close.rolling(w).mean() for w in sc.conv_mas]
    import pandas as pd
    ma_df = pd.concat(mas, axis=1)
    width = ma_df.max(axis=1) - ma_df.min(axis=1)          # MA 묶음 폭
    band = atr_v * sc.conv_band_atr

    # ① 직전 conv_min_bars(이탈봉 제외)의 80%+ 가 밴드 내
    w = width.iloc[-1 - sc.conv_min_bars:-1]
    if w.isna().any() or (w <= band).mean() < 0.8:
        return None
    # ② 거래량 바닥 (직전 봉 기준 — 이탈봉 거래량 오염 방지)
    vol10 = vol.rolling(10).mean().iloc[-2]
    vol50 = vol.rolling(50).mean().iloc[-2]
    if not (vol10 <= vol50 * sc.conv_dry_ratio):
        return None
    # ③ 이탈봉: 수렴구간 고/저 돌파 + 거래량 클라이맥스
    rng = df.iloc[-1 - sc.conv_min_bars:-1]
    hi, lo = rng["high"].max(), rng["low"].min()
    vol20 = vol.rolling(20).mean().iloc[-2]
    v_ok = vol.iloc[-1] >= vol20 * sc.conv_break_vol
    price = close.iloc[-1]
    if not v_ok:
        return None
    dry = vol10 / (vol50 + 1e-9)
    vx = vol.iloc[-1] / (vol20 + 1e-9)
    why = [f"MA{list(sc.conv_mas)} 수렴 {sc.conv_min_bars}봉+ (폭≤{sc.conv_band_atr}ATR)",
           f"수렴 중 거래량 바닥 {dry:.2f}", f"이탈봉 거래량 x{vx:.1f}"]
    if price > hi:
        return Signal("CONVERGENCE", "LONG", 75, ["수렴 상방 이탈"] + why)
    if price < lo:
        return Signal("CONVERGENCE", "SHORT", 75, ["수렴 하방 이탈"] + why)
    return None


def _obv_divergence_signal(df, sc, feats):
    """
    OBV 다이버전스 (bullstory1 OBV 스레드 번역):
      강세: 가격 스윙로우 LL + 그 시점 OBV는 HL + 현재봉 반등 → LONG
      약세: 가격 스윙하이 HH + OBV LH + 현재봉 하락 → SHORT
    2번째 피벗이 최근 obv_recent_bars 내일 때만(신호 적시성).
    """
    lows, highs = feats.get("swing_lows"), feats.get("swing_highs")
    if lows is None or highs is None or len(df) < 60:
        return None
    import numpy as np
    close, vol = df["close"], df["volume"]
    obv = (np.sign(close.diff().fillna(0.0)) * vol).cumsum()
    n = len(df)

    def _recent_pair(piv):
        if len(piv) < 2:
            return None
        t1, t2 = piv.index[-2], piv.index[-1]
        if n - 1 - df.index.get_loc(t2) > sc.obv_recent_bars:
            return None
        return t1, t2

    p = _recent_pair(lows)
    if p and lows.loc[p[1]] < lows.loc[p[0]] and obv.loc[p[1]] > obv.loc[p[0]] \
            and close.iloc[-1] > close.iloc[-2]:
        return Signal("OBV_DIV", "LONG", 70,
                      ["OBV 강세 다이버전스: 가격 LL + OBV HL (매집)"])
    p = _recent_pair(highs)
    if p and highs.loc[p[1]] > highs.loc[p[0]] and obv.loc[p[1]] < obv.loc[p[0]] \
            and close.iloc[-1] < close.iloc[-2]:
        return Signal("OBV_DIV", "SHORT", 70,
                      ["OBV 약세 다이버전스: 가격 HH + OBV LH (분산)"])
    return None


def _obv_prd_signal(df, sc, feats, timeframe):
    """
    OBV_PRD (Gemini PRD v1.0 변형, 2026-07-28 기획서):
      조건A: 종가 < VWAP (PRD '당일 VWAP' — 크립토 24/7이라 24h 롤링로 대체)
      조건B: 종가 < 직전 obv_prd_lookback봉 최저가 AND 현재 OBV > 그 최저가 시점 OBV
      롱 온리. 기존 OBV_DIV(피벗 기반)와의 차이는 기획서 표 참조.
    """
    lb = sc.obv_prd_lookback
    nbars = max(2, round(sc.vwap_hours / _TF_HOURS.get(timeframe, 1.0)))
    if len(df) < max(lb + 1, nbars) + 2 or nbars < 3:
        return None
    import numpy as np
    close, vol = df["close"], df["volume"]
    tp = (df["high"] + df["low"] + close) / 3
    vwap = ((tp * vol).rolling(nbars).sum() / vol.rolling(nbars).sum()).iloc[-1]
    if not (close.iloc[-1] < vwap):                       # 조건A
        return None
    prev = df.iloc[-1 - lb:-1]                            # 직전 lb봉 (현재봉 제외)
    low_pos = prev["low"].values.argmin()
    local_low = float(prev["low"].iloc[low_pos])
    obv = (np.sign(close.diff().fillna(0.0)) * vol).cumsum()
    obv_at_low = float(obv.iloc[-1 - lb + low_pos])
    if close.iloc[-1] < local_low and obv.iloc[-1] > obv_at_low:   # 조건B
        return Signal("OBV_PRD", "LONG", 70,
                      [f"신저가({lb}봉) + OBV 상방 다이버전스, VWAP 하회"])
    return None


_TF_HOURS = {"1m": 1 / 60, "5m": 1 / 12, "15m": 0.25, "30m": 0.5,
             "1h": 1.0, "4h": 4.0, "1d": 24.0}


def _vwap_signal(df, sc, feats, timeframe):
    """
    롤링 VWAP 탈환/이탈 (kkul_bird VWAP 스레드 번역, 크립토는 24h 롤링로 대체):
      종가가 VWAP을 상향 돌파 + 거래량 확인 → LONG (매수세 우위 전환)
      하향 이탈 + 거래량 → SHORT. 1d 등 창<3봉이면 무의미 → 스킵.
    """
    import pandas as pd
    nbars = max(2, round(sc.vwap_hours / _TF_HOURS.get(timeframe, 1.0)))
    if nbars < 3 or len(df) < nbars + 2:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (tp * df["volume"]).rolling(nbars).sum() / \
        df["volume"].rolling(nbars).sum()
    c0, c1 = df["close"].iloc[-1], df["close"].iloc[-2]
    w0, w1 = vwap.iloc[-1], vwap.iloc[-2]
    if pd.isna(w1) or feats["vol_ratio"] < sc.vwap_vol_mult:
        return None
    if c1 <= w1 and c0 > w0:
        return Signal("VWAP", "LONG", 65,
                      [f"VWAP({sc.vwap_hours:.0f}h) 상향 탈환 + "
                       f"거래량 {feats['vol_ratio']:.1f}x"])
    if c1 >= w1 and c0 < w0:
        return Signal("VWAP", "SHORT", 65,
                      [f"VWAP({sc.vwap_hours:.0f}h) 하향 이탈 + "
                       f"거래량 {feats['vol_ratio']:.1f}x"])
    return None


def evaluate(df, feats, cfg) -> Signal:
    """스캔 피처 + 패턴 → 최고 스코어 셋업 1개 선택."""
    sc = cfg.signal
    cand = candles.detect_all(df, feats["atr"], sc)
    chart = charts.detect_all(df, sc, feats.get("swing_highs"),
                              feats.get("swing_lows"))
    cross = feats["cross"]
    price = feats["price"]

    candidates: list[Signal] = []

    # ---------- BREAKOUT ----------
    if price > feats["recent_high"]:
        s = 55 + 20 * (feats["vol_ratio"] > 1.3) + 15 * (cand["bias"] == "bull")
        s += 10 * cross["allow_long"]
        candidates.append(Signal(
            "BREAKOUT", "LONG", _clamp(s),
            [f"{sc.breakout_lookback}봉 고점 돌파",
             f"거래량비 {feats['vol_ratio']:.2f}",
             f"캔들 바이어스 {cand['bias']}"], cand, chart))
    elif price < feats["recent_low"]:
        s = 55 + 20 * (feats["vol_ratio"] > 1.3) + 15 * (cand["bias"] == "bear")
        s += 10 * cross["allow_short"]
        candidates.append(Signal(
            "BREAKOUT", "SHORT", _clamp(s),
            [f"{sc.breakout_lookback}봉 저점 이탈",
             f"거래량비 {feats['vol_ratio']:.2f}",
             f"캔들 바이어스 {cand['bias']}"], cand, chart))

    # ---------- PULLBACK (추세 중 조정 후 되돌림) ----------
    ma_fast = feats["df_ma"].get(f"ma{sc.fast_ma}")
    ma_slow = feats["df_ma"].get(f"ma{sc.slow_ma}")
    if ma_fast is not None and ma_slow is not None:
        near_ma = abs(price - ma_slow.iloc[-1]) / (price + 1e-9) < 0.01
        if cross["trend"] == "up" and near_ma and cand["bias"] == "bull":
            s = 50 + 20 + 10 * (feats["fast_ma_slope"] > 0)
            candidates.append(Signal(
                "PULLBACK", "LONG", _clamp(s),
                ["상승추세 중 중기MA 지지 조정", "강세 반전 캔들 확인"], cand, chart))
        if cross["trend"] == "down" and near_ma and cand["bias"] == "bear":
            s = 50 + 20 + 10 * (feats["fast_ma_slope"] < 0)
            candidates.append(Signal(
                "PULLBACK", "SHORT", _clamp(s),
                ["하락추세 중 중기MA 저항 반등", "약세 반전 캔들 확인"], cand, chart))

    # ---------- MOMENTUM ----------
    big_bull = "big_bull" in cand["patterns"] or "bullish_engulfing" in cand["patterns"]
    big_bear = "big_bear" in cand["patterns"] or "bearish_engulfing" in cand["patterns"]
    if big_bull and feats["fast_ma_slope"] > 0:
        s = 50 + 15 * (feats["vol_ratio"] > 1.2) + 20 * cross["allow_long"]
        candidates.append(Signal(
            "MOMENTUM", "LONG", _clamp(s),
            ["큰 양선/강세 장악 + MA 상승 가속"], cand, chart))
    if big_bear and feats["fast_ma_slope"] < 0:
        s = 50 + 15 * (feats["vol_ratio"] > 1.2) + 20 * cross["allow_short"]
        candidates.append(Signal(
            "MOMENTUM", "SHORT", _clamp(s),
            ["큰 음선/약세 장악 + MA 하락 가속"], cand, chart))

    # ---------- TREND_CONTINUATION ----------
    cont_bull = {"bull_flag", "ascending_triangle"} & set(chart["found"])
    cont_bear = {"bear_flag", "descending_triangle"} & set(chart["found"])
    if cont_bull and cross["allow_long"]:
        conf = max(chart["found"][k]["confidence"] for k in cont_bull)
        candidates.append(Signal(
            "TREND_CONTINUATION", "LONG", _clamp(45 + 45 * conf),
            [f"연속형 패턴: {', '.join(cont_bull)}"], cand, chart))
    if cont_bear and cross["allow_short"]:
        conf = max(chart["found"][k]["confidence"] for k in cont_bear)
        candidates.append(Signal(
            "TREND_CONTINUATION", "SHORT", _clamp(45 + 45 * conf),
            [f"연속형 패턴: {', '.join(cont_bear)}"], cand, chart))

    # ---------- REVERSAL ----------
    rev_bull_c = {"morning_star", "hammer", "dragonfly_doji",
                  "bullish_engulfing", "bullish_harami"} & set(cand["patterns"])
    rev_bear_c = {"evening_star", "shooting_star", "gravestone_doji",
                  "bearish_engulfing", "bearish_harami"} & set(cand["patterns"])
    rev_bull_ch = {"double_bottom", "triple_bottom",
                   "inverse_head_and_shoulders"} & set(chart["found"])
    rev_bear_ch = {"double_top", "triple_top",
                   "head_and_shoulders"} & set(chart["found"])
    at_low = price <= feats["recent_low"] * 1.02
    at_high = price >= feats["recent_high"] * 0.98
    # 확정형(다봉) 반전 = 자체 확인 내장. 단봉(hammer/doji/harami)은 약함.
    confirmed_bull = {"morning_star", "bullish_engulfing"} & rev_bull_c
    confirmed_bear = {"evening_star", "bearish_engulfing"} & rev_bear_c

    if rev_bull_c and (rev_bull_ch or at_low):
        ok, reasons2 = _reversal_confluence(sc, feats, "LONG", bool(confirmed_bull))
        if ok:
            bonus = _confluence_bonus(sc, feats, "LONG")
            s = 48 + 8 * len(rev_bull_c) + 25 * bool(rev_bull_ch) + bonus
            candidates.append(Signal(
                "REVERSAL", "LONG", _clamp(s),
                [f"바닥 반전 캔들: {', '.join(rev_bull_c)}"]
                + ([f"차트: {', '.join(rev_bull_ch)}"] if rev_bull_ch else [])
                + reasons2, cand, chart))
    if rev_bear_c and (rev_bear_ch or at_high):
        ok, reasons2 = _reversal_confluence(sc, feats, "SHORT", bool(confirmed_bear))
        if ok:
            bonus = _confluence_bonus(sc, feats, "SHORT")
            s = 48 + 8 * len(rev_bear_c) + 25 * bool(rev_bear_ch) + bonus
            candidates.append(Signal(
                "REVERSAL", "SHORT", _clamp(s),
                [f"천장 반전 캔들: {', '.join(rev_bear_c)}"]
                + ([f"차트: {', '.join(rev_bear_ch)}"] if rev_bear_ch else [])
                + reasons2, cand, chart))

    # ---------- CONVERGENCE / OBV_DIV / VWAP (지침 노트 셋업들) ----------
    for extra in (_convergence_signal(df, sc, feats),
                  _obv_divergence_signal(df, sc, feats),
                  _obv_prd_signal(df, sc, feats, cfg.timeframe),
                  _vwap_signal(df, sc, feats, cfg.timeframe)):
        if extra is not None:
            extra.candles, extra.charts = cand, chart
            candidates.append(extra)

    # 비활성화된 셋업 제거 (튜닝: 손실 셋업 차단)
    if sc.disabled_setups:
        candidates = [c for c in candidates if c.setup not in sc.disabled_setups]

    if not candidates:
        return Signal("NONE", "NONE", 0,
                      ["감지된 고확률 셋업 없음"], cand, chart)

    # 장기선 방향 역행 신호 감점 (노트: 매수/매도 금지 규칙)
    for c in candidates:
        if c.direction == "LONG" and not cross["allow_long"]:
            c.score = _clamp(c.score - 20)
            c.reasons.append("⚠ 장기선 하락 → 매수 감점")
        if c.direction == "SHORT" and not cross["allow_short"]:
            c.score = _clamp(c.score - 20)
            c.reasons.append("⚠ 장기선 상승 → 매도 감점")

    best = max(candidates, key=lambda x: x.score)
    return best
