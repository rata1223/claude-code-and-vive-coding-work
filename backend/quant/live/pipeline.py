"""
라이브 트레이딩 파이프라인 (프로덕션).

DataLoader → BinaryRegimeEngine(SPY) → SignalFusion → PositionSizer → BrokerAdapter

킬스위치:
- 일일 손실이 daily_loss_limit_pct(기본 3%) 초과 시 당일 매수 전면 차단
- 연속 손실 max_consecutive_losses(기본 5) 초과 시 운영 중단

안티오버트레이딩:
- 동일 종목 재진입은 cooldown_days(기본 3) 이후만 허용
"""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from backend.brokers.base import BrokerAdapter
from backend.quant.data.loader import DataLoader
from backend.quant.signals.fusion import SignalFusion, FusionResult, default_fusion
from backend.quant.signals.regime import BinaryRegimeEngine, RegimeOutput
from backend.quant.risk.position_sizer import PositionSizer
from backend.quant.risk.portfolio import PortfolioAllocator

logger = logging.getLogger(__name__)


@dataclass
class LiveConfig:
    symbols: list[str]
    initial_capital: float = 2_000_000.0
    max_position_pct: float = 0.05
    risk_pct_per_trade: float = 0.01       # ATR 사이징: 자본의 1%
    data_period: str = "2y"
    data_interval: str = "1d"
    sizing_method: str = "atr"             # atr | kelly | fixed
    portfolio_method: str = "max_sharpe"   # max_sharpe | equal | risk_parity
    dry_run: bool = True                   # True = 실제 주문 없이 로그만
    daily_loss_limit_pct: float = 0.03     # 일일 손실 한도 3%
    max_consecutive_losses: int = 5        # 연속 손실 → 운영 중단
    cooldown_days: int = 3                 # 동일 종목 재진입 금지 기간
    regime_symbol: str = "SPY"            # 레짐 탐지용 시장 프록시


@dataclass
class RiskState:
    """일내 리스크 상태 추적."""
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    kill_switch_active: bool = False
    kill_reason: str = ""
    trade_date: date = field(default_factory=date.today)
    last_sell_date: dict = field(default_factory=dict)  # {symbol: date}


class LivePipeline:
    """
    라이브 1사이클 실행기.
    스케줄러(APScheduler)에서 매일 장 시작 후 호출.

    예:
        pipeline = LivePipeline(broker=kis_broker, config=cfg)
        pipeline.run_cycle()
    """

    def __init__(self, broker: BrokerAdapter, config: LiveConfig,
                 fusion: Optional[SignalFusion] = None):
        self.broker = broker
        self.config = config
        self.fusion = fusion or default_fusion()
        self.loader = DataLoader()
        self.risk_state = RiskState()
        self._regime_engine = BinaryRegimeEngine()
        self._prev_regime_state: str = "risk_on"  # 히스테리시스 유지

    def run_cycle(self) -> dict:
        """
        1사이클:
        1. 킬스위치 점검
        2. OHLCV 로드
        3. 레짐 탐지 (선택적)
        4. 신호 평가
        5. 매도 처리
        6. 매수 처리 (쿨다운·킬스위치 적용)
        반환: 사이클 요약 dict
        """
        cfg = self.config
        state = self.risk_state

        # 날짜가 바뀌면 일일 PnL 리셋
        today = date.today()
        if state.trade_date != today:
            state.daily_pnl = 0.0
            state.trade_date = today
            logger.info("새 거래일 — 일일 PnL 리셋")

        # ── 킬스위치 점검 ──────────────────────────────────────────────
        if state.kill_switch_active:
            logger.warning("킬스위치 활성: %s — 사이클 중단", state.kill_reason)
            return {"error": "kill_switch", "reason": state.kill_reason,
                    "kill_switch": True}

        logger.info("라이브 사이클 시작: %d종목", len(cfg.symbols))

        # ── 1. 데이터 로드 ────────────────────────────────────────────
        dfs = self.loader.fetch_multi(cfg.symbols, period=cfg.data_period,
                                      interval=cfg.data_interval)
        if not dfs:
            logger.error("데이터 로드 실패")
            return {"error": "no_data"}

        # ── 1-B. 레짐 탐지 (SPY 시장 프록시) ─────────────────────────
        regime: Optional[RegimeOutput] = None
        try:
            df_spy = self.loader.fetch(cfg.regime_symbol, period="1y")
            regime = self._regime_engine.detect(df_spy, prev_state=self._prev_regime_state)
            self._prev_regime_state = regime.meta.get("state", self._prev_regime_state)
            logger.info(
                "레짐: %s (risk_on=%s, vol=%.1f%%, sma_pct=%.2f%%)",
                regime.regime, regime.risk_on,
                regime.vol_ann * 100, regime.sma_pct * 100,
            )
        except Exception as e:
            logger.warning("레짐 탐지 실패 — risk_on 가정: %s", e)

        # STRESS 레짐: 신규 매수 전면 차단
        buy_blocked_by_regime = regime is not None and not regime.risk_on

        # ── 2. 신호 평가 ──────────────────────────────────────────────
        scan_results: list[FusionResult] = self.fusion.scan(dfs)
        buy_candidates = [r.symbol for r in scan_results if r.signal == 1]
        sell_candidates = [r.symbol for r in scan_results if r.signal == -1]

        logger.info("신호: 매수=%s 매도=%s", buy_candidates, sell_candidates)

        # ── 3. 기존 포지션 매도 처리 ──────────────────────────────────
        sells_executed = []
        positions = self.broker.get_positions()
        for pos in positions:
            if pos.symbol in sell_candidates:
                if cfg.dry_run:
                    logger.info("[DRY RUN] SELL %s qty=%d", pos.symbol, pos.qty)
                    # 드라이런에서도 쿨다운 기록
                    state.last_sell_date[pos.symbol] = today
                else:
                    try:
                        price = self.broker.get_price(pos.symbol)
                        order = self.broker.place_order(pos.symbol, "sell", pos.qty, price)
                        sells_executed.append({"symbol": pos.symbol, "order_id": order.id})
                        state.last_sell_date[pos.symbol] = today
                        logger.info("매도 주문: %s qty=%d price=%.2f", pos.symbol, pos.qty, price)
                    except Exception as e:
                        logger.error("매도 실패 %s: %s", pos.symbol, e)

        # ── 4. 일일 손실 한도 점검 ────────────────────────────────────
        try:
            balance = self.broker.get_balance()
            available_cash = balance.cash_krw
        except Exception as e:
            logger.error("잔고 조회 실패: %s", e)
            return {"error": "balance_unavailable"}

        daily_loss_limit = cfg.initial_capital * cfg.daily_loss_limit_pct
        if state.daily_pnl <= -daily_loss_limit:
            logger.warning("일일 손실 한도 초과 (%.0f원) — 당일 매수 차단", daily_loss_limit)
            return {
                "buy_signals": buy_candidates,
                "sell_signals": sell_candidates,
                "sells_executed": sells_executed,
                "buys_executed": [],
                "available_cash": available_cash,
                "blocked_reason": "daily_loss_limit",
                "regime": regime.regime if regime else "unknown",
                "dry_run": cfg.dry_run,
            }

        if buy_blocked_by_regime:
            logger.warning("레짐 차단 (%s) — 신규 매수 전면 금지", regime.regime)
            return {
                "buy_signals": buy_candidates,
                "sell_signals": sell_candidates,
                "sells_executed": sells_executed,
                "buys_executed": [],
                "available_cash": available_cash,
                "blocked_reason": f"regime_{regime.regime}",
                "regime": regime.regime,
                "dry_run": cfg.dry_run,
            }

        # ── 5. 신규 매수 처리 ─────────────────────────────────────────
        held_symbols = {p.symbol for p in self.broker.get_positions()}

        # 보유 중 + 쿨다운 적용
        cooldown_cutoff = today - timedelta(days=cfg.cooldown_days)
        blocked_by_cooldown = {
            sym for sym, sell_date in state.last_sell_date.items()
            if sell_date > cooldown_cutoff
        }

        new_buys = [
            s for s in buy_candidates
            if s not in held_symbols and s not in blocked_by_cooldown
        ]
        if blocked_by_cooldown:
            logger.info("쿨다운 차단: %s", sorted(blocked_by_cooldown))

        # 포트폴리오 비중 계산
        allocator = PortfolioAllocator(
            total_capital=available_cash,
            method=cfg.portfolio_method
        )
        allocations = allocator.allocate(new_buys, price_history=dfs)

        buys_executed = []
        sizer = PositionSizer(
            capital=available_cash,
            max_position_pct=cfg.max_position_pct
        )

        for symbol in new_buys:
            if symbol not in dfs:
                continue
            df = dfs[symbol]
            try:
                price = self.broker.get_price(symbol)
            except Exception:
                price = df["Close"].iloc[-1]

            if cfg.sizing_method == "atr":
                sizing = sizer.atr_based(df, risk_pct=cfg.risk_pct_per_trade)
            elif cfg.sizing_method == "fixed":
                alloc = allocations.get(symbol, 0)
                qty = int(alloc / price) if price > 0 and alloc > 0 else 0
                sizing = {"qty": qty, "entry_price": price}
            else:
                sizing = sizer.fixed_fraction(df)

            qty = sizing.get("qty", 0)
            if qty <= 0:
                continue

            if cfg.dry_run:
                logger.info("[DRY RUN] BUY %s qty=%d @ %.2f", symbol, qty, price)
                buys_executed.append({"symbol": symbol, "qty": qty, "price": price, "dry": True})
            else:
                try:
                    order = self.broker.place_order(symbol, "buy", qty, price)
                    buys_executed.append({"symbol": symbol, "qty": qty,
                                          "price": price, "order_id": order.id})
                    logger.info("매수 주문: %s qty=%d price=%.2f", symbol, qty, price)
                except Exception as e:
                    logger.error("매수 실패 %s: %s", symbol, e)

        summary = {
            "buy_signals": buy_candidates,
            "sell_signals": sell_candidates,
            "buys_executed": buys_executed,
            "sells_executed": sells_executed,
            "available_cash": available_cash,
            "regime": regime.regime if regime else "unknown",
            "regime_risk_on": regime.risk_on if regime else True,
            "dry_run": cfg.dry_run,
            "kill_switch": False,
        }
        logger.info("사이클 완료: %s", summary)
        return summary

    def record_trade_pnl(self, pnl: float) -> None:
        """
        거래 완료 후 PnL 기록 및 킬스위치 평가.
        손실 거래 연속 max_consecutive_losses 초과 시 킬스위치 발동.
        """
        state = self.risk_state
        cfg = self.config
        state.daily_pnl += pnl

        if pnl < 0:
            state.consecutive_losses += 1
            logger.warning("손실 거래 기록: pnl=%.0f, 연속손실=%d",
                           pnl, state.consecutive_losses)
            if state.consecutive_losses >= cfg.max_consecutive_losses:
                state.kill_switch_active = True
                state.kill_reason = (
                    f"연속 손실 {state.consecutive_losses}회 초과 — 수동 재활성화 필요"
                )
                logger.error("킬스위치 발동: %s", state.kill_reason)
        else:
            state.consecutive_losses = 0

    def reset_kill_switch(self) -> None:
        """수동으로 킬스위치 해제 (운영자 승인 후 호출)."""
        self.risk_state.kill_switch_active = False
        self.risk_state.kill_reason = ""
        self.risk_state.consecutive_losses = 0
        logger.info("킬스위치 수동 해제")
