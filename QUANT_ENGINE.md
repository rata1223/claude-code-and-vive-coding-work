# QuantDinger Quant Engine — 아키텍처 & MVP 로드맵

## Deliverable 1: System Architecture

```
[Mobile App (Vue3+Capacitor)]
        ↕ REST / WebSocket
[QuantDinger Backend :5000]  ←→  [KIS API Server :5001]
                                          ↕
                              [KIS Worker (Redis Pub/Sub)]
                                          ↕
                          ┌───────────────────────────────┐
                          │     backend/quant/ Engine      │
                          │                               │
                          │  DataLoader → Indicators      │
                          │      ↓                        │
                          │  SignalFusion (Score 모델)     │
                          │      ↓                        │
                          │  PositionSizer / Allocator    │
                          │      ↓                        │
                          │  BrokerAdapter (KIS/Sim)      │
                          │      ↓                        │
                          │  OrderStateMachine            │
                          └───────────────────────────────┘
                                          ↕
                              [BacktestEngine + WalkForward]
```

## Deliverable 2: Repo Folder Structure

```
backend/quant/
├── __init__.py               # 공개 API
├── data/
│   └── loader.py             # yfinance + PyKRX + broker fallback
├── indicators/
│   ├── base.py               # no_lookahead() 보장
│   ├── trend.py              # SMA/EMA/MACD/Ichimoku
│   ├── volatility.py         # ATR/BB/Squeeze/KC
│   ├── momentum.py           # RSI/Stoch/12-1/ROC
│   └── pattern.py            # Fibonacci/Harmonic(Gartley)
├── signals/
│   ├── base.py               # SignalBase ABC, SignalOutput
│   ├── trend_following.py    # 3중 추세 확인 (SMA+MACD+Ichimoku)
│   ├── momentum.py           # 12-1 Factor + RSI 필터
│   ├── volatility_breakout.py# BB Squeeze 돌파
│   ├── mean_reversion.py     # Pairs Trading + 공적분
│   └── fusion.py             # 가중합산 스코어링 모델
├── risk/
│   ├── position_sizer.py     # ATR/Kelly/Fixed-Frac + 비용 모델
│   └── portfolio.py          # PyPortfolioOpt + Risk Parity
├── backtest/
│   ├── engine.py             # 벡터화 백테스터 (Backtrader 호환)
│   ├── metrics.py            # Sharpe/Sortino/Calmar/MDD/CAGR
│   └── walk_forward.py       # WFO + OOS + Monte Carlo
├── live/
│   └── pipeline.py           # 라이브 트레이딩 1사이클 실행기
└── tests/
    ├── test_indicators.py
    ├── test_signals.py
    ├── test_backtest.py
    └── test_risk.py
```

## Deliverable 3: Core Module Interfaces

| 인터페이스 | 위치 | 핵심 메서드 |
|-----------|------|------------|
| `SignalBase` | signals/base.py | `compute(df, symbol) → SignalOutput` |
| `BacktestEngine` | backtest/engine.py | `run(df, signal_series) → BacktestResult` |
| `WalkForwardOptimizer` | backtest/walk_forward.py | `run(df, is_bars, oos_bars) → WFOResult` |
| `PositionSizer` | risk/position_sizer.py | `atr_based/kelly_based/fixed_fraction(df) → dict` |
| `PortfolioAllocator` | risk/portfolio.py | `allocate(symbols, price_history) → dict` |
| `SignalFusion` | signals/fusion.py | `evaluate(df, symbol) → FusionResult` |
| `LivePipeline` | live/pipeline.py | `run_cycle() → dict` |
| `DataLoader` | data/loader.py | `fetch(symbol, ...) → pd.DataFrame` |

## Deliverable 4: 전략 스코어링 모델

```python
score = Σ (weight_i / Σweights) × signal_i × strength_i
# signal_i ∈ {-1, 0, 1}, strength_i ∈ [0, 1]
# score ∈ [-1, 1]
# buy if score ≥ 0.25, sell if score ≤ -0.25
```

기본 앙상블: 추세추종(40%) + 모멘텀(40%) + 변동성돌파(20%)
+ Ichimoku 레짐 필터 (약세 구름 아래 = 신규 매수 차단)

## Deliverable 5: Risk Management Rules

| 규칙 | 파라미터 | 위치 |
|------|---------|------|
| ATR 손절 | 진입가 - 2×ATR(14) | position_sizer.py |
| 일손실 한도 | 자본의 3% | strategy/risk.py (RiskManager) |
| MDD 한도 | 15% → 전량 청산 | strategy/risk.py (RiskManager) |
| 종목당 최대 | 자본의 5% | position_sizer.py |
| 포트폴리오 열 | 최대 10종목 | live/pipeline.py |
| 레짐 킬스위치 | SPY 변동성 > 25% | signals/fusion.py |

## Deliverable 9: MVP 로드맵

### Phase 1 — 완료 (현재)
- [x] 지표 라이브러리 (no-lookahead 보장)
- [x] 신호 모듈 5종 + 퓨전 스코어링
- [x] 포지션 사이징 (ATR/Kelly/Fixed)
- [x] 백테스트 엔진 (벡터화)
- [x] Walk-Forward + Monte Carlo
- [x] 라이브 파이프라인 골격
- [x] 단위/통합 테스트 40개+

### Phase 2 — 2주 (다음 스프린트)
- [ ] `backend/api/server.py`에 `/api/quant/backtest`, `/api/quant/scan` 엔드포인트 추가
- [ ] KIS 실시간 가격 → `feed_price()` 연동
- [ ] LivePipeline → `backend/worker/scheduler.py` 매일 09:05 KR, 22:35 US 등록
- [ ] 펀더멘털 필터 (CAPM + Fama-French) 추가

### Phase 3 — 4주
- [ ] Mobile UI: 백테스트 결과 차트 표시 (KlineChart에 equity curve 추가)
- [ ] TA-Lib 지표 확장 (현재는 pandas-ta만 사용)
- [ ] Fundamental overlay (yfinance financials)
- [ ] Riskfolio-Lib 포트폴리오 최적화 대체

### Phase 4 — 8주 (실전 전환 전)
- [ ] 4주 모의 운용 검증
- [ ] OOS 샤프비율 > 0.8 달성 후 실전 전환
- [ ] 텔레그램 알림 → LivePipeline 결과 연동
