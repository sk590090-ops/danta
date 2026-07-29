# pattern_trader (ptrader) v0.1

**패턴 기반 24/7 트레이딩 판단 엔진** — 손글씨 캔들/차트패턴 노트(Day1~8 + 치트시트)의
매매 규칙을, seb.ai "24/7 AI Trader with Fable 5" 아키텍처
(Scan → Signal → Risk → Plan → Monitor → Decision)에 얹어 파이썬으로 구현한 **독립 프로그램**.

> ⚠️ 현재 **페이퍼/분석 전용**. 실주문 코드 없음. 최종 실행 결정은 항상 **사람**이 합니다
> (슬라이드 7/8 "HUMAN REVIEW REQUIRED" 원칙).

---

## 왜 만들었나 / 기존과의 관계
- `01_trading/`의 기존 프로젝트(`funding_arb`, `coin`, `Tradebot` 등)와 **분리된 신규 코드베이스**.
- 병합 여부는 검증 후 결정 (예: `funding_arb`의 실행/대시보드 계층에 신호 소스로 편입 가능).

## 파이프라인 (= 인스타 8슬라이드 매핑)

| 슬라이드 | 모듈 | 역할 |
|---|---|---|
| 2/8 MARKET SCANNER | `scanner.py` | 추세·변동성(ATR)·거래량·MA 피처 계산 |
| 3/8 SIGNAL ENGINE | `signals/engine.py` | 캔들+차트+MA → **5대 셋업** 스코어링 |
| — 캔들 | `signals/candles.py` | Day1~8 캔들패턴 |
| — 차트 | `signals/charts.py` | 쌍바닥/쌍봉·H&S·깃발·삼각형 |
| — 지표 | `indicators.py` | MA(5/20/100/200)·ATR·스윙·골든/데드크로스 |
| 5/8 RISK MODULE | `risk.py` | 5중 체크(사이즈/노출/DD/변동성/최대손실) → PASS/BLOCK |
| 4/8 TRADE PLANNER | `planner.py` | 진입·목표·손절·무효화 + R:R |
| 7/8 FINAL DECISION | `decision.py` | 메모 → APPROVED / WATCHLIST / REJECTED |
| 6/8 MONITOR LOOP | `monitor.py` | 24/7 반복 감시 + 페이퍼 북 |

**5대 셋업** (슬라이드 3/8): `BREAKOUT` · `PULLBACK` · `MOMENTUM` · `TREND_CONTINUATION` · `REVERSAL`

## 구현된 패턴 (손글씨 노트 대응)
- **캔들 (Day1~8):** 큰 양선/음선, 도지·묘비·잠자리, 하라미, 목봉선(핀바/해머·슈팅스타),
  포선(장악형 engulfing), 아침의 별(모닝스타), 저녁의 별(이브닝스타)
- **차트 (치트시트):** 쌍바닥/쌍봉, 삼중바닥/삼중천정, 역헤드앤숄더/헤드앤숄더,
  상승깃발/하락깃발, 상승삼각형/하락삼각형
- **이동평균:** 골든/데드크로스 + 장기선(200MA) 방향 필터 →
  "매수 금지/매도 금지" 규칙(추세 역행 신호 감점)

## 설치 & 실행
```bash
# 의존성: pandas, numpy 만 있으면 동작 (PyYAML·ccxt는 선택)
pip install -r requirements.txt

python run.py demo                    # 합성데이터로 1회 시연 + JSON 메모
python run.py scan                    # config 심볼 전체 1회 판단
python run.py monitor --iters 3       # 24/7 루프(테스트 3회) — 페이퍼 진입
python run.py backtest --symbol BTCUSDT

# 옵션
python run.py scan --source synthetic         # 오프라인(기본)
python run.py scan --source ccxt --symbol BTC/USDT   # 실거래소(ccxt 설치 필요)
```

## 데이터 소스 (`data_source`)
- `synthetic` (기본): 국면전환 포함 랜덤워크 OHLCV — 오프라인 즉시 실행
- `csv`: `data/<SYMBOL>_<TF>.csv` (컬럼: open,high,low,close,volume [,timestamp])
- `ccxt`: **실거래소 실시간** (`pip install ccxt`) — ✅ 연동 완료(binance 검증)
  - 심볼 자동변환: `BTCUSDT` → `BTC/USDT` (또는 `BTC/USDT` 직접 입력)
  - **페이지네이션**으로 `ccxt.limit`(기본 500)봉 확보, 실패 시 재시도
  - `config.yaml`의 `ccxt:` 블록에서 거래소/마켓(spot·swap)/봉수 지정
  - 지원 거래소: ccxt가 지원하는 모든 거래소 id (binance/bybit/okx/upbit …)
  ```bash
  python run.py scan     --source ccxt --symbol BTCUSDT   # 실시간 BTC 판단
  python run.py backtest --source ccxt --symbol ETHUSDT   # 실데이터 백테스트
  python run.py monitor  --source ccxt --iters 5          # 실데이터 24/7 루프
  ```

## 설정 (`config.yaml`)
심볼·타임프레임·자본금, 리스크 파라미터(위험비율/노출/DD/변동성밴드),
신호 임계(스코어 승인/관찰 기준), 손익비 최소치 등. 파일 없으면 코드 기본값.

## 셋업 성과 분석 & 파라미터 튜닝 (`tools/tune.py`)
```bash
python tools/tune.py --limit 2000                 # 5심볼 3개월 실데이터 튜닝
python tools/tune.py --symbols BTCUSDT ETHUSDT --timeframe 4h --refresh
```
단계별 그리드서치: ① 손절폭×목표R:R → ② 승인스코어×추세필터 → ③ 손실셋업 누적제거.
목적함수 = 풀링 기대값(평균 R), 최소 거래수 조건. 결과는 `tune_report.json` +
`config.tuned.yaml`로 저장.

### 튜닝 결과 (binance 5심볼 1h, 2026-04~07 ≈ 3개월, fee 0.04%)
| 구간 | 설정 | N | 승률 | 평균R | PF | 손익 |
|---|---|---|---|---|---|---|
| 베이스라인(800봉·1개월) | 전 셋업 | 135 | 24% | **-0.16** | 0.42 | 손실 |
| 베이스라인(2000봉·3개월) | 전 셋업 | 359 | 37% | -0.01 | 0.98 | ≈본전 |
| **튜닝(2000봉)** | **REVERSAL만** | 292 | 42% | **+0.065** | **1.25** | **+$1,893** |
| ↳ out-of-sample 전반부 | 〃 | 126 | 46% | +0.175 | 1.79 | +$2,205 |
| ↳ out-of-sample 후반부 | 〃 | 138 | 45% | +0.063 | 1.26 | +$874 |

**핵심 발견**
1. **표본 길이가 결정적** — 1개월은 순손실로 보였으나, 3개월에선 본전권. 짧은 백테스트에 속지 말 것.
2. **REVERSAL 셋업만 양(+)의 엣지** (PF 1.25). MOMENTUM·BREAKOUT·PULLBACK·TREND_CONTINUATION은
   현재 로직상 순손실 → **비활성화**가 최적. (`disabled_setups`에 반영)
3. **out-of-sample 전·후반 모두 수익** → 단일 구간 우연은 아님. 단, 아래 한계 유의.
4. 최적 파라미터: `rr_target=2.0, atr_stop_mult=1.0, min_score_approve=60` → `config.yaml`에 반영 완료.

## 워크포워드 검증 (`tools/walkforward.py`) — ⚠️ 엄정 검증 결과
```bash
python tools/walkforward.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT \
    --limit 5000 --is 1000 --oos 500
```
각 폴드: 과거 IS(1000봉)에서 재최적화 → 직후 OOS(500봉, 미지의 미래)에 적용.
OOS만 이어붙인 게 '진짜 성적'. (OOS 지표는 직전 IS로 워밍업)

### 결과 (binance 4심볼 1h, 2025-12~2026-07 ≈ 7개월, 8폴드)
| 구성 | OOS N | 승률 | 평균R | PF | 손익 |
|---|---|---|---|---|---|
| WFA(폴드마다 재최적화) | 546 | 36% | **−0.017** | **0.94** | −$915 |
| FIXED(config.yaml 고정) | 537 | 41% | +0.043 | 1.16 | +$2,323 |
| ALL(무튜닝·전셋업) | 681 | 40% | +0.019 | 1.08 | +$1,300 |
- **OOS 수익 폴드: 4/8** (폴드별 PF 0.50~1.86로 편차 극심)

### 판정: **엣지는 견고하지 않다 (과최적화 확인)**
1. **적응형 재최적화가 오히려 손실**(WFA PF 0.94 < FIXED 1.16 < 무튜닝조차 1.08). IS에서
   좋던 파라미터(IS_R +0.31 등)가 OOS로 안 넘어감 → **파라미터 그리드서치는 노이즈를 학습**.
   → 파라미터 자유도를 늘릴수록 나빠짐. **단순 고정 규칙이 더 낫다.**
2. **앞선 3개월 "REVERSAL PF 1.25"는 국면 운(luck)이 상당**. 7개월·워크포워드로 늘리면
   FIXED조차 PF 1.16(수수료 前)·4/8폴드로 **약하고 불안정**. 슬리피지 감안 시 사실상 본전권.
3. **결론: 현 상태로 실전 배포 부적합.** 워크포워드가 과최적화를 걸러줌 — 검증의 목적 달성.

### 이 검증이 알려주는 실행 방향
- ❌ 파라미터 추가 튜닝(수익률 짜내기)은 역효과 → 중단.
- ✅ **신호 자체의 질**을 높여야 함: REVERSAL에 컨플루언스(거래량·상위TF 추세소진·확인봉) 추가 후
  **다시 워크포워드**로 검증. 통과 못 하면 배포 안 함.
- ✅ 타임프레임 변경(4h/1d — 노이즈↓) 재검증.
- ✅ 자유도 최소화: 심볼·기간 무관 **단일 고정 규칙** 지향.

## REVERSAL 신호 질 개선 시도 (`tools/revtest.py`) — ❌ 실패
WFA 교훈(자유도↑=과최적화)을 지켜, 컨플루언스를 **고정 규칙**으로 넣고 v1 vs v2를
롤링 OOS 비교(재최적화 없음). 추가 필터: 거래량 클라이맥스·RSI 극단·상위TF stretch(200MA
대비 ATR 정규화)·확정형(다봉) 반전 — 4요인 중 K개 이상 충족 시 발동.
```bash
python tools/revtest.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT --limit 5000
```
### 결과 (동일 7개월·4심볼·9블록 롤링 OOS)
| 규칙 | N | 승률 | 평균R | PF | +블록 |
|---|---|---|---|---|---|
| v1 (컨플루언스 없음) | 598 | 40% | **+0.032** | **1.12** | 6/9 |
| v2 (≥2요인) | 595 | 38% | +0.003 | 1.01 | 5/9 |
| v2 (≥3요인) | 296 | 34% | −0.029 | 0.87 | 5/9 |

**판정: 컨플루언스가 REVERSAL을 개선하지 못함.** 오히려 필터를 강하게 걸수록(≥3) 악화.
블록별로 v1·v2가 서로 엇갈리며(예: 한 블록 v1+0.28/v2−0.09, 다른 블록 반대) 일관성이 없음
= **엣지가 아니라 노이즈**라는 신호. 거래량·RSI·200MA-stretch는 이 데이터의 반전 예측력에
기여하지 않음. → `reversal_confluence`는 기본 OFF 유지(코드·툴은 재사용 가능하게 보존).

**두 독립 검증(워크포워드 + 컨플루언스)이 일치**: 현 패턴 로직은 1h 암호화폐에서
견고한 수익 엣지가 없다. 파라미터/필터를 더 만지는 것은 과최적화일 뿐. 다음은
**타임프레임 전환(4h/1d, 노이즈↓)** 또는 **접근법 자체 재설계**가 정직한 경로.

## 타임프레임 재검증 (1h / 4h / 1d) — 롤링 OOS
```bash
python tools/revtest.py --timeframe 4h --limit 5000 --warmup 300 --oos 500
python tools/revtest.py --timeframe 1d --limit 2000 --warmup 250 --oos 250
```
REVERSAL-only(v1) 고정 규칙, R:R=2:1, 재최적화 없음.

| TF | 기간 | v1 N | 승률 | 평균R | PF | +블록 | 판정 |
|---|---|---|---|---|---|---|---|
| 1h | 7개월 | 598 | 40% | +0.032 | 1.12 | 6/9 | 얇음·불안정 |
| 4h | 2.3년 | 630 | 42% | +0.041 | 1.10 | 5/9 | 1h와 동일 수준 |
| 1d | 5.5년 | 202 | 35% | −0.080 | 0.86 | 4/7 | **손실** |

- **4h ≈ 1h** (PF~1.10, 승률 42%): 느린 TF도 개선 없음. 블록별 avgR이 +0.35~−0.17로 마구 엇갈림 = 노이즈.
- **1d는 v1 손실**(PF 0.86). 컨플루언스 강화(v2≥3)가 PF 1.38로 좋아 보이나 **5.5년간 51건뿐** —
  표본이 너무 작아 통계적으로 무의미(신뢰구간 과대). 채택 불가.
- 컨플루언스가 1h/4h선 해롭고 1d선 도움되는 등 **TF마다 방향이 뒤집힘** = 안정적 기전 없음(노이즈 확증).

### R:R 2:1 손익분기 승률
`기대값(R) = 승률×2 − (1−승률)×1 = 3×승률 − 1` → **0이 되는 승률 = 33.3%**.
- 수수료·슬리피지 ≈0.1R 반영 시 **~36~37%** 필요. 여유 있으려면 40%+.
- 우리 실측 승률 35~42%는 **손익분기 언저리** → PF가 0.86~1.12로 겨우 본전 부근인 이유.
- 승률(42%)이 이론상 +0.26R을 줘야 하나 실측 +0.04R인 격차는 **시간청산(hold_max)·수수료** 탓 →
  진입만큼 **청산 설계(목표 도달률·트레일링)** 가 관건.

**최종 결론: 1h·4h·1d 어느 것도 견고한 고표본 엣지를 주지 못함.** 패턴+고정 R:R 접근은
암호화폐에서 한계 확인. 추가 파라미터 조정은 과최적화. → 접근법 재설계 or 연구종료 권장.

## 테스트
```bash
python -m pytest tests/ -q     # 14 passed
```

## 현재 한계 (정직한 상태 — 과대해석 금지)
- **엣지는 얇고 표본이 제한적**: REVERSAL PF 1.25는 유망하나 ①약 3개월 ②대형코인 5종
  ③단일 시장국면 ④in-sample에서 사후적으로 REVERSAL 선택(과최적화 여지). **워크포워드/추가기간
  검증 전 실전 신뢰 금물.**
- **비용 모델 단순**: fee 0.04%만 반영, 슬리피지·펀딩·호가공백 미반영 → 실거래 성과는 더 낮을 수 있음.
- **차트패턴 탐지는 스윙피벗 휴리스틱** → 노이즈 과탐지(scan 메모에 동시 다수 패턴). 정교화 여지 큼.
- 멀티 타임프레임(H1/H4 동시확인, 슬라이드 7/8) 미구현 — 단일 TF.
- ccxt는 **읽기 전용**(공개 OHLCV). API 키·주문 기능 없음. 실주문·부분청산 미구현.

## 다음 단계 후보
1. **워크포워드 검증** — 롤링 in/out-of-sample으로 REVERSAL 엣지 견고성 재확인(과최적화 배제)
2. **REVERSAL 집중 개선** — 유일하게 먹히는 셋업. 진입확인봉·거래량·추세소진 컨플루언스로 질 향상
3. 차트패턴 정교화(넥라인 돌파·거래량 필터) → 과탐지 감소
4. 멀티 타임프레임 정렬(H4 추세 + H1 트리거), 타 타임프레임(4h/1d) 튜닝
5. `funding_arb` 대시보드/실행계층과 병합 여부 결정
6. 텔레그램/알림 연동(슬라이드 6/8 TRIGGER ALERTS)

## 구조
```
pattern_trader/
├─ run.py              # CLI (demo/scan/monitor/backtest)
├─ config.yaml
├─ requirements.txt
├─ ptrader/
│  ├─ config.py        datafeed.py     scanner.py    indicators.py
│  ├─ pipeline.py      risk.py         planner.py    decision.py   monitor.py   backtest.py
│  └─ signals/         candles.py      charts.py     engine.py
└─ tests/              test_candles.py test_pipeline.py
```
