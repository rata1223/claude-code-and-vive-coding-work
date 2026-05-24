import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from kis_adapter import KISClient, KISOrders, KISMarketData, KISPortfolio
from strategy import TradingSignals, PortfolioOptimizer, RiskManager
from bot.notifier import (
    alert_buy, alert_sell, alert_error, alert_emergency, alert_daily_summary, send_alert
)
from bot.scheduler import build_scheduler
from strategy.signals import EXCD_MAP, KR_ETF


class TradingEngine:
    def __init__(self):
        self._client = KISClient()
        self._orders = KISOrders(self._client)
        self._market = KISMarketData(self._client)
        self._portfolio = KISPortfolio(self._client)
        self._signals = TradingSignals()
        self._optimizer = PortfolioOptimizer()
        self._risk = RiskManager()
        logger.info("TradingEngine 초기화 완료 (env=%s)", os.environ.get("KIS_ENV", "paper"))

    def run_us_session(self):
        if self._risk.is_trading_halted():
            logger.info("당일 매매 중단 상태 — 미국 세션 건너뜀")
            return

        logger.info("미국 세션 시작")
        try:
            candidates = self._signals.scan_universe()
            buy_list = [s for s in candidates["buy"] if s not in KR_ETF]
            if buy_list:
                kr_equity, _ = self._portfolio.get_total_asset_krw()
                weights = self._optimizer.compute_weights(buy_list, kr_equity)
                for symbol, amount_krw in weights.items():
                    excd = EXCD_MAP.get(symbol, "NASD")
                    price = self._market.get_price_us(symbol, excd)
                    qty = max(1, int(amount_krw / (price * 1350)))
                    try:
                        self._orders.buy_us(symbol, excd, qty, price)
                        alert_buy(symbol, qty, price)
                    except Exception as e:
                        alert_error(f"매수 실패 {symbol}: {e}")
        except Exception as e:
            alert_error(f"미국 세션 오류: {e}")
            logger.exception("미국 세션 오류")

    def run_kr_session(self):
        if self._risk.is_trading_halted():
            logger.info("당일 매매 중단 상태 — 한국 세션 건너뜀")
            return

        logger.info("한국 세션 시작")
        try:
            candidates = self._signals.scan_universe()
            buy_list = [s for s in candidates["buy"] if s in KR_ETF]
            if buy_list:
                kr_equity, _ = self._portfolio.get_total_asset_krw()
                weights = self._optimizer.compute_weights(buy_list, kr_equity)
                for symbol, amount_krw in weights.items():
                    price = self._market.get_price_kr(symbol)
                    qty = max(1, int(amount_krw / price))
                    try:
                        self._orders.buy_kr(symbol, qty, price)
                        alert_buy(symbol, qty, float(price), market="KR")
                    except Exception as e:
                        alert_error(f"매수 실패 {symbol}: {e}")
        except Exception as e:
            alert_error(f"한국 세션 오류: {e}")
            logger.exception("한국 세션 오류")

    def run_rebalance(self):
        logger.info("월간 리밸런싱 시작")
        send_alert("🔄 월간 리밸런싱 시작")
        # 현재 포지션 전량 매도 후 재진입 (단순 구현)
        try:
            kr_balance = self._portfolio.get_kr_balance()
            for pos in kr_balance["positions"]:
                qty = int(pos.get("hldg_qty", 0))
                if qty > 0:
                    symbol = pos["pdno"]
                    price = self._market.get_price_kr(symbol)
                    self._orders.sell_kr(symbol, qty, price)
                    alert_sell(symbol, qty, float(price), reason="리밸런싱", market="KR")
        except Exception as e:
            alert_error(f"리밸런싱 오류: {e}")

    def reset_daily_risk(self):
        self._risk.reset_daily_counters()

    def send_daily_summary(self):
        try:
            kr_equity, us_equity_usd = self._portfolio.get_total_asset_krw()
            alert_daily_summary({
                "total_equity": kr_equity,
                "daily_pnl_pct": 0,
                "position_count": 0,
            })
        except Exception as e:
            logger.warning("일일 결산 실패: %s", e)

    def check_mdd_and_halt(self):
        try:
            kr_equity, _ = self._portfolio.get_total_asset_krw()
            if self._risk.check_mdd(kr_equity):
                alert_emergency("MDD 15% 초과! 전량 현금화를 실행합니다.")
                self.run_rebalance()
        except Exception as e:
            logger.warning("MDD 체크 실패: %s", e)


def main():
    engine = TradingEngine()
    send_alert("🚀 KIS 자동매매 봇 시작")
    scheduler = build_scheduler(engine)
    logger.info("스케줄러 시작")
    scheduler.start()


if __name__ == "__main__":
    main()
