import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

import yfinance as yf

from kis_adapter import KISClient, KISOrders, KISMarketData, KISPortfolio
from strategy.signals import MultiTimeframeSignals, KR_ETF, EXCD_MAP
from strategy.optimizer import PortfolioOptimizer
from strategy.risk import RiskManager
from bot.notifier import (
    alert_buy, alert_sell, alert_error, alert_emergency, alert_daily_summary, send_alert
)
from bot.scheduler import build_scheduler


def get_usd_krw_rate() -> float:
    """실시간 USD/KRW 환율 조회 (yfinance KRW=X)."""
    try:
        rate = yf.Ticker("KRW=X").fast_info["last_price"]
        if rate and 900 < rate < 2000:
            return float(rate)
    except Exception as e:
        logger.warning("환율 조회 실패: %s — 기본값 1350 사용", e)
    return 1350.0


class TradingEngine:
    def __init__(self):
        self._client = KISClient()
        self._orders = KISOrders(self._client)
        self._market = KISMarketData(self._client)
        self._portfolio = KISPortfolio(self._client)
        self._signals = MultiTimeframeSignals()
        self._optimizer = PortfolioOptimizer()
        self._risk = RiskManager()
        self._account = os.environ["KIS_ACCOUNT_NO"]
        logger.info("TradingEngine 초기화 완료 (env=%s)", os.environ.get("KIS_ENV", "paper"))

    def _get_portfolio_positions(self) -> dict:
        """현재 보유 포지션 {symbol: entry_price} 반환."""
        positions = {}
        try:
            kr = self._portfolio.get_kr_balance()
            for p in kr["positions"]:
                qty = int(p.get("hldg_qty", 0))
                if qty > 0:
                    avg_price = float(p.get("pchs_avg_pric", 0))
                    positions[p["pdno"]] = avg_price
        except Exception as e:
            logger.warning("한국 포지션 조회 실패: %s", e)
        try:
            us = self._portfolio.get_us_balance()
            for p in us["positions"]:
                qty = int(p.get("ovrs_cblc_qty", 0))
                if qty > 0:
                    avg_price = float(p.get("pchs_avg_pric", 0))
                    positions[p["ovrs_pdno"]] = avg_price
        except Exception as e:
            logger.warning("미국 포지션 조회 실패: %s", e)
        return positions

    def _execute_stop_losses(self, positions_map: dict):
        """보유 포지션 손절 강제 실행."""
        def _get_price(symbol):
            if symbol in KR_ETF:
                return float(self._market.get_price_kr(symbol))
            excd = EXCD_MAP.get(symbol, "NASD")
            return self._market.get_price_us(symbol, excd)

        # positions_map → [{"symbol": ..., "entry_price": ..., "qty": ...}] 형태로 변환
        pos_list = []
        try:
            kr = self._portfolio.get_kr_balance()
            for p in kr["positions"]:
                qty = int(p.get("hldg_qty", 0))
                if qty > 0 and p["pdno"] in positions_map:
                    pos_list.append({"symbol": p["pdno"], "entry_price": positions_map[p["pdno"]],
                                     "qty": qty, "market": "KR"})
            us = self._portfolio.get_us_balance()
            for p in us["positions"]:
                qty = int(p.get("ovrs_cblc_qty", 0))
                if qty > 0 and p["ovrs_pdno"] in positions_map:
                    pos_list.append({"symbol": p["ovrs_pdno"], "entry_price": positions_map[p["ovrs_pdno"]],
                                     "qty": qty, "market": "US"})
        except Exception as e:
            logger.warning("포지션 목록 조회 실패: %s", e)
            return

        to_sell = self._risk.enforce_stop_losses(pos_list, _get_price)
        for pos in to_sell:
            try:
                sym = pos["symbol"]
                qty = pos["qty"]
                price = pos["current_price"]
                if pos["market"] == "KR":
                    self._orders.sell_kr(sym, qty, int(price))
                    alert_sell(sym, qty, price, reason=pos["reason"], market="KR")
                else:
                    excd = EXCD_MAP.get(sym, "NASD")
                    self._orders.sell_us(sym, excd, qty, price)
                    alert_sell(sym, qty, price, reason=pos["reason"])
            except Exception as e:
                alert_error(f"손절 주문 실패 {pos['symbol']}: {e}")

    def run_us_session(self):
        if self._risk.is_trading_halted():
            logger.info("당일 매매 중단 상태 — 미국 세션 건너뜀")
            return

        logger.info("미국 세션 시작")
        try:
            portfolio_positions = self._get_portfolio_positions()

            # 손절 먼저 체크
            self._execute_stop_losses(portfolio_positions)

            scan = self._signals.scan_universe(portfolio_positions)

            # 매도 신호 처리
            for symbol, reason in scan["sell"]:
                if symbol in KR_ETF:
                    continue
                try:
                    excd = EXCD_MAP.get(symbol, "NASD")
                    price = self._market.get_price_us(symbol, excd)
                    us = self._portfolio.get_us_balance()
                    qty = next(
                        (int(p.get("ovrs_cblc_qty", 0)) for p in us["positions"] if p["ovrs_pdno"] == symbol), 0
                    )
                    if qty > 0:
                        self._orders.sell_us(symbol, excd, qty, price)
                        alert_sell(symbol, qty, price, reason=reason)
                except Exception as e:
                    alert_error(f"매도 실패 {symbol}: {e}")

            # 매수 신호 처리
            buy_list = [s for s in scan["buy"] if s not in KR_ETF]
            if buy_list:
                kr_equity, us_equity_usd = self._portfolio.get_total_asset_krw()
                fx = get_usd_krw_rate()
                total_krw = kr_equity + us_equity_usd * fx
                weights = self._optimizer.compute_atr_weights(buy_list, total_krw, self._signals)
                for symbol, amount_krw in weights.items():
                    excd = EXCD_MAP.get(symbol, "NASD")
                    price = self._market.get_price_us(symbol, excd)
                    qty = max(1, int(amount_krw / (price * fx)))
                    try:
                        self._orders.buy_us(symbol, excd, qty, price)
                        alert_buy(symbol, qty, price)
                    except Exception as e:
                        alert_error(f"매수 실패 {symbol}: {e}")

            if scan.get("regime") == "bearish":
                send_alert("⚠️ 시장 변동성 급등 — 신규 매수 중단 중")

        except Exception as e:
            alert_error(f"미국 세션 오류: {e}")
            logger.exception("미국 세션 오류")

    def run_kr_session(self):
        if self._risk.is_trading_halted():
            logger.info("당일 매매 중단 상태 — 한국 세션 건너뜀")
            return

        logger.info("한국 세션 시작")
        try:
            portfolio_positions = self._get_portfolio_positions()
            self._execute_stop_losses(portfolio_positions)

            scan = self._signals.scan_universe(portfolio_positions)

            # 매도 신호 처리
            for symbol, reason in scan["sell"]:
                if symbol not in KR_ETF:
                    continue
                try:
                    price = self._market.get_price_kr(symbol)
                    kr = self._portfolio.get_kr_balance()
                    qty = next(
                        (int(p.get("hldg_qty", 0)) for p in kr["positions"] if p["pdno"] == symbol), 0
                    )
                    if qty > 0:
                        self._orders.sell_kr(symbol, qty, price)
                        alert_sell(symbol, qty, float(price), reason=reason, market="KR")
                except Exception as e:
                    alert_error(f"매도 실패 {symbol}: {e}")

            # 매수 신호 처리
            buy_list = [s for s in scan["buy"] if s in KR_ETF]
            if buy_list:
                kr_equity, _ = self._portfolio.get_total_asset_krw()
                weights = self._optimizer.compute_atr_weights(buy_list, kr_equity, self._signals)
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

        if self._risk.is_trading_halted():
            send_alert("⚠️ 당일 매매 중단 상태 — 리밸런싱 취소")
            return

        try:
            kr_balance = self._portfolio.get_kr_balance()
            for pos in kr_balance["positions"]:
                qty = int(pos.get("hldg_qty", 0))
                if qty > 0:
                    symbol = pos["pdno"]
                    price = self._market.get_price_kr(symbol)
                    self._orders.sell_kr(symbol, qty, price)
                    alert_sell(symbol, qty, float(price), reason="월간 리밸런싱", market="KR")

            us_balance = self._portfolio.get_us_balance()
            for pos in us_balance["positions"]:
                qty = int(pos.get("ovrs_cblc_qty", 0))
                if qty > 0:
                    symbol = pos["ovrs_pdno"]
                    excd = EXCD_MAP.get(symbol, "NASD")
                    price = self._market.get_price_us(symbol, excd)
                    self._orders.sell_us(symbol, excd, qty, price)
                    alert_sell(symbol, qty, price, reason="월간 리밸런싱")
        except Exception as e:
            alert_error(f"리밸런싱 오류: {e}")

    def reset_daily_risk(self):
        self._risk.reset_daily_counters()

    def send_daily_summary(self):
        try:
            kr_equity, us_equity_usd = self._portfolio.get_total_asset_krw()
            fx = get_usd_krw_rate()
            total_krw = kr_equity + us_equity_usd * fx

            kr_positions = self._portfolio.get_kr_balance()["positions"]
            us_positions = self._portfolio.get_us_balance()["positions"]
            position_count = (
                sum(1 for p in kr_positions if int(p.get("hldg_qty", 0)) > 0) +
                sum(1 for p in us_positions if int(p.get("ovrs_cblc_qty", 0)) > 0)
            )

            # 당일 PnL: KR 평가손익합
            daily_pnl = sum(
                float(p.get("evlu_pfls_amt", 0))
                for p in kr_positions if int(p.get("hldg_qty", 0)) > 0
            )
            daily_pnl_pct = (daily_pnl / total_krw * 100) if total_krw > 0 else 0

            alert_daily_summary({
                "total_equity": total_krw,
                "daily_pnl_pct": daily_pnl_pct,
                "position_count": position_count,
            })

            # MDD 체크
            if self._risk.check_mdd(total_krw):
                alert_emergency("MDD 15% 초과! 전량 현금화를 실행합니다.")
                self.run_rebalance()

        except Exception as e:
            logger.warning("일일 결산 실패: %s", e)


def main():
    engine = TradingEngine()
    send_alert("🚀 KIS 자동매매 봇 시작")
    scheduler = build_scheduler(engine)
    logger.info("스케줄러 시작")
    scheduler.start()


if __name__ == "__main__":
    main()
