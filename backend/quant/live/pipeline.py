"""
Deliverable 7: 라이브 트레이딩 파이프라인.
기존 BrokerAdapter + OrderStateMachine + PositionTracker를 그대로 사용.
DataLoader → SignalFusion → PositionSizer → BrokerAdapter 흐름.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from backend.brokers.base import BrokerAdapter
from backend.quant.data.loader import DataLoader
from backend.quant.signals.fusion import SignalFusion, FusionResult, default_fusion
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

    def run_cycle(self) -> dict:
        """
        1사이클:
        1. OHLCV 로드
        2. 신호 평가 (SignalFusion.scan)
        3. 매도 처리 (보유 포지션 중 매도 신호)
        4. 매수 처리 (신규 진입 신호 → 포지션 사이징 → 주문)
        반환: 사이클 요약 dict
        """
        cfg = self.config
        logger.info("라이브 사이클 시작: %d종목", len(cfg.symbols))

        # ── 1. 데이터 로드 ────────────────────────────────────────────
        dfs = self.loader.fetch_multi(cfg.symbols, period=cfg.data_period,
                                      interval=cfg.data_interval)
        if not dfs:
            logger.error("데이터 로드 실패")
            return {"error": "no_data"}

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
                else:
                    try:
                        price = self.broker.get_price(pos.symbol)
                        order = self.broker.place_order(pos.symbol, "sell", pos.qty, price)
                        sells_executed.append({"symbol": pos.symbol, "order_id": order.id})
                        logger.info("매도 주문: %s qty=%d price=%.2f", pos.symbol, pos.qty, price)
                    except Exception as e:
                        logger.error("매도 실패 %s: %s", pos.symbol, e)

        # ── 4. 신규 매수 처리 ─────────────────────────────────────────
        balance = self.broker.get_balance()
        available_cash = balance.cash_krw
        held_symbols = {p.symbol for p in self.broker.get_positions()}

        # 이미 보유 중인 종목 제외
        new_buys = [s for s in buy_candidates if s not in held_symbols]

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
            "dry_run": cfg.dry_run,
        }
        logger.info("사이클 완료: %s", summary)
        return summary
