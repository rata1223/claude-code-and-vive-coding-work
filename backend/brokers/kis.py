import os
import logging
from .base import BrokerAdapter
from .models import Balance, Order, OrderStatus, Position
from kis_adapter import KISClient, KISMarketData, KISOrders, KISPortfolio
from strategy.signals import KR_ETF, EXCD_MAP

logger = logging.getLogger(__name__)


class KISBroker(BrokerAdapter):
    def __init__(self):
        self._client = KISClient()
        self._market = KISMarketData(self._client)
        self._orders = KISOrders(self._client)
        self._portfolio = KISPortfolio(self._client)
        self._account = os.environ["KIS_ACCOUNT_NO"]
        self._paper = self._client.auth.env == "paper"
        logger.info("KISBroker 초기화 (env=%s)", "paper" if self._paper else "real")

    def get_balance(self) -> Balance:
        kr = self._portfolio.get_kr_balance()
        us = self._portfolio.get_us_balance()
        kr_cash = float(kr["summary"].get("dnca_tot_amt", 0))
        us_cash = float(us["summary"].get("frcr_dncl_amt_2", 0))
        kr_eval = float(kr["summary"].get("tot_evlu_amt", 0))
        us_eval_usd = float(us["summary"].get("tot_evlu_amt", 0))
        return Balance(
            cash_krw=kr_cash,
            cash_usd=us_cash,
            total_eval_krw=kr_eval + us_eval_usd * self._get_fx(),
        )

    def get_positions(self) -> list[Position]:
        positions: list[Position] = []
        try:
            kr = self._portfolio.get_kr_balance()
            for p in kr["positions"]:
                qty = int(p.get("hldg_qty", 0))
                if qty > 0:
                    sym = p["pdno"]
                    avg = float(p.get("pchs_avg_pric", 0))
                    try:
                        cur = float(self._market.get_price_kr(sym))
                    except Exception:
                        cur = avg
                    positions.append(Position(symbol=sym, qty=qty, avg_price=avg, market="KR", current_price=cur))
        except Exception as e:
            logger.warning("KR 포지션 조회 실패: %s", e)

        try:
            us = self._portfolio.get_us_balance()
            for p in us["positions"]:
                qty = int(p.get("ovrs_cblc_qty", 0))
                if qty > 0:
                    sym = p["ovrs_pdno"]
                    avg = float(p.get("pchs_avg_pric", 0))
                    try:
                        excd = EXCD_MAP.get(sym, "NASD")
                        cur = self._market.get_price_us(sym, excd)
                    except Exception:
                        cur = avg
                    positions.append(Position(symbol=sym, qty=qty, avg_price=avg, market="US", current_price=cur))
        except Exception as e:
            logger.warning("US 포지션 조회 실패: %s", e)

        return positions

    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order:
        is_kr = symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())
        try:
            if is_kr:
                raw = (self._orders.buy_kr if side == "buy" else self._orders.sell_kr)(symbol, qty, int(price))
            else:
                excd = EXCD_MAP.get(symbol, "NASD")
                raw = (self._orders.buy_us if side == "buy" else self._orders.sell_us)(symbol, excd, qty, price)
            order_id = raw.get("output", {}).get("ODNO", "")
            return Order(
                id=order_id, symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.SUBMITTED, raw=raw,
            )
        except Exception as e:
            logger.error("주문 실패 %s %s: %s", side, symbol, e)
            return Order(
                id="", symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.REJECTED, raw={"error": str(e)},
            )

    def cancel_order(self, order_id: str) -> bool:
        logger.warning("KIS cancel_order: 개별 취소는 cancel_us만 지원 (order_id=%s)", order_id)
        return False

    def get_price(self, symbol: str) -> float:
        if symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit()):
            return float(self._market.get_price_kr(symbol))
        excd = EXCD_MAP.get(symbol, "NASD")
        return self._market.get_price_us(symbol, excd)

    def _get_fx(self) -> float:
        try:
            import yfinance as yf
            rate = yf.Ticker("KRW=X").fast_info["last_price"]
            if rate and 900 < rate < 2000:
                return float(rate)
        except Exception:
            pass
        return 1350.0
