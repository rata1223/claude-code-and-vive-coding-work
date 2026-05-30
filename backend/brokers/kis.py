import os
import logging
import threading
import time
from .base import BrokerAdapter
from .models import Balance, Order, OrderStatus, Position
from .semantic_mapper import KIS_DOMESTIC_MAPPER, KIS_OVERSEAS_MAPPER
from kis_adapter import KISClient, KISMarketData, KISOrders, KISPortfolio
from backend.execution.circuit_breaker import ConsecutiveFailureBreaker
from backend.quant.data.universe import EXCD_MAP, KR_ETF

logger = logging.getLogger(__name__)

_FX_CACHE_LOCK = threading.Lock()
_FX_CACHE: dict = {"rate": 1350.0, "ts": time.monotonic()}
_FX_TTL = 3600  # 1 hour

# Process-level singleton — rate-limit tracking must be shared across all callers
_KIS_BROKER_INSTANCE: "KISBroker | None" = None
_KIS_BROKER_LOCK = threading.Lock()


def get_kis_broker() -> "KISBroker":
    """Return the process-level KISBroker singleton. Thread-safe."""
    global _KIS_BROKER_INSTANCE
    if _KIS_BROKER_INSTANCE is None:
        with _KIS_BROKER_LOCK:
            if _KIS_BROKER_INSTANCE is None:
                _KIS_BROKER_INSTANCE = KISBroker()
    return _KIS_BROKER_INSTANCE


class KISBroker(BrokerAdapter):

    @staticmethod
    def _is_kr(symbol: str) -> bool:
        """Return True for KR domestic symbols (6-digit code or in KR_ETF list)."""
        return symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())

    def __init__(self):
        self._client = KISClient()
        self._market = KISMarketData(self._client)
        self._orders = KISOrders(self._client)
        self._portfolio = KISPortfolio(self._client)
        self._account = os.environ["KIS_ACCOUNT_NO"]
        self._paper = self._client.auth.env == "paper"
        # Shared breaker for all KIS API calls — trips after 5 consecutive failures
        self._breaker = ConsecutiveFailureBreaker(threshold=5, cooldown_minutes=10)
        logger.info("KISBroker 초기화 (env=%s)", "paper" if self._paper else "real")

    def get_balance(self) -> Balance:
        if self._breaker.is_open():
            raise RuntimeError("KIS circuit breaker open — get_balance 차단")
        try:
            kr = self._portfolio.get_kr_balance()
            us = self._portfolio.get_us_balance()
            kr_cash = float(kr["summary"].get("dnca_tot_amt", 0))
            us_cash = float(us["summary"].get("frcr_dncl_amt_2", 0))
            kr_eval = float(kr["summary"].get("tot_evlu_amt", 0))
            us_eval_usd = float(us["summary"].get("tot_evlu_amt", 0))
            self._breaker.record_success()
            return Balance(
                cash_krw=kr_cash,
                cash_usd=us_cash,
                total_eval_krw=kr_eval + us_eval_usd * self._get_fx(),
            )
        except Exception:
            self._breaker.record_failure()
            raise

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
        if self._breaker.is_open():
            logger.error("주문 차단 — circuit breaker open: %s %s", side, symbol)
            return Order(
                id="", symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.REJECTED, raw={"error": "circuit breaker open"},
            )
        is_kr = symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())
        try:
            if is_kr:
                raw = (self._orders.buy_kr if side == "buy" else self._orders.sell_kr)(symbol, qty, int(price))
            else:
                excd = EXCD_MAP.get(symbol, "NASD")
                raw = (self._orders.buy_us if side == "buy" else self._orders.sell_us)(symbol, excd, qty, price)
            mapper = KIS_DOMESTIC_MAPPER if is_kr else KIS_OVERSEAS_MAPPER
            order_id = mapper.extract_broker_order_id(raw)
            self._breaker.record_success()
            return Order(
                id=order_id, symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.SUBMITTED, raw=raw,
            )
        except Exception as e:
            self._breaker.record_failure()
            logger.error("주문 실패 %s %s: %s", side, symbol, e)
            return Order(
                id="", symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.REJECTED, raw={"error": str(e)},
            )

    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0, price: float = 0.0) -> bool:
        """주문 취소. US 종목은 cancel_us() 라우팅. KR: TTTC0803U/VTTC0803U."""
        is_us = bool(symbol) and not self._is_kr(symbol)
        if is_us:
            excd = EXCD_MAP.get(symbol, "NASD")
            try:
                resp = self._orders.cancel_us(order_id, symbol, excd, qty, price)
                rt_cd = resp.get("rt_cd", "1")
                if rt_cd == "0":
                    logger.info("US 주문 취소 성공: %s %s", order_id, symbol)
                    return True
                logger.warning("US 주문 취소 실패 (rt_cd=%s): %s", rt_cd, resp.get("msg1"))
                return False
            except Exception as e:
                logger.error("US 주문 취소 예외 %s: %s", order_id, e)
                return False

        try:
            tr_id = "VTTC0803U" if self._paper else "TTTC0803U"
            body = {
                "CANO": self._account[:8],
                "ACNT_PRDT_CD": self._account[8:],
                "KRX_FWDG_ORD_ORGNO": "",
                "ORGN_ODNO": order_id,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",  # 취소
                "ORD_QTY": "0",
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
            }
            resp = self._client.post("/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, body)
            rt_cd = resp.get("rt_cd", "1")
            if rt_cd == "0":
                logger.info("주문 취소 성공: %s", order_id)
                return True
            logger.warning("주문 취소 실패 (rt_cd=%s): %s", rt_cd, resp.get("msg1"))
            return False
        except Exception as e:
            logger.error("주문 취소 예외 %s: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str, symbol: str = "") -> Order | None:
        """
        단건 주문 조회. symbol로 KR/US 라우팅.
        반환 None = 조회 실패 또는 주문 미존재.
        """
        is_us = bool(symbol) and not self._is_kr(symbol)
        if is_us:
            return self._get_us_order_status(order_id, symbol)
        return self._get_kr_order_status(order_id)

    def _get_kr_order_status(self, order_id: str) -> Order | None:
        """KIS TR: TTTC8036R (실전) / VTTC8036R (모의)."""
        try:
            tr_id = "VTTC8036R" if self._paper else "TTTC8036R"
            params = {
                "CANO": self._account[:8],
                "ACNT_PRDT_CD": self._account[8:],
                "INQR_STRT_DT": "",
                "INQR_END_DT": "",
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "01",
                "PDNO": "",
                "ORD_GNO_BRNO": "",
                "ODNO": order_id,
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }
            resp = self._client.get("/uapi/domestic-stock/v1/trading/inquire-order", tr_id, params)
            output = resp.get("output1") or resp.get("output", [])
            if not output:
                return None
            row = output[0] if isinstance(output, list) else output
            filled_qty = KIS_DOMESTIC_MAPPER.extract_filled_qty(row)
            ord_qty = KIS_DOMESTIC_MAPPER.extract_order_qty(row)
            avg_price = KIS_DOMESTIC_MAPPER.extract_avg_price(row)
            status = KIS_DOMESTIC_MAPPER.map_status(row, filled_qty, ord_qty)
            sym = row.get("pdno", "")
            side = KIS_DOMESTIC_MAPPER.extract_side(row)
            return Order(
                id=order_id, symbol=sym, side=side, qty=ord_qty,
                price=float(row.get("ord_unpr", 0)), status=status,
                filled_qty=filled_qty, avg_fill_price=avg_price,
            )
        except Exception as e:
            logger.warning("KR 주문 조회 실패 %s: %s", order_id, e)
            return None

    def _get_us_order_status(self, order_id: str, symbol: str) -> Order | None:
        """KIS 해외주식 주문 조회. TR: TTTS3035R (실전) / VTTS3035R (모의)."""
        try:
            tr_id = "VTTS3035R" if self._paper else "TTTS3035R"
            excd = EXCD_MAP.get(symbol, "NASD")
            params = {
                "CANO": self._account[:8],
                "ACNT_PRDT_CD": self._account[8:],
                "OVRS_EXCG_CD": excd,
                "PDNO": symbol,
                "ORD_STRT_DT": "",
                "ORD_END_DT": "",
                "SLL_BUY_DVSN_CD": "00",
                "CCL_NCCS_DVSN": "00",
                "INQR_DVSN": "00",
                "INQR_DVSN_1": "0",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            resp = self._client.get("/uapi/overseas-stock/v1/trading/inquire-order", tr_id, params)
            output = resp.get("output") or []
            if not output:
                return None
            # Match the specific order_id; never fall back to a different order's row.
            row = next((r for r in output if r.get("odno") == order_id), None)
            if row is None:
                logger.warning("US 주문 %s 응답에서 미매칭 — None 반환", order_id)
                return None

            filled_qty = KIS_OVERSEAS_MAPPER.extract_filled_qty(row)
            ord_qty = KIS_OVERSEAS_MAPPER.extract_order_qty(row)
            avg_price = KIS_OVERSEAS_MAPPER.extract_avg_price(row)
            status = KIS_OVERSEAS_MAPPER.map_status(row, filled_qty, ord_qty)
            side = KIS_OVERSEAS_MAPPER.extract_side(row)
            return Order(
                id=order_id, symbol=symbol, side=side, qty=ord_qty,
                price=float(row.get("ft_ord_unpr3", 0)), status=status,
                filled_qty=filled_qty, avg_fill_price=avg_price,
            )
        except Exception as e:
            logger.warning("US 주문 조회 실패 %s: %s", order_id, e)
            return None

    def get_price(self, symbol: str) -> float:
        if self._breaker.is_open():
            raise RuntimeError(f"KIS circuit breaker open — get_price 차단: {symbol}")
        try:
            if symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit()):
                result = float(self._market.get_price_kr(symbol))
            else:
                excd = EXCD_MAP.get(symbol, "NASD")
                result = self._market.get_price_us(symbol, excd)
            self._breaker.record_success()
            return result
        except Exception:
            self._breaker.record_failure()
            raise

    def _get_fx(self) -> float:
        with _FX_CACHE_LOCK:
            if time.monotonic() - _FX_CACHE["ts"] < _FX_TTL:
                return _FX_CACHE["rate"]
        try:
            import yfinance as yf
            rate = yf.Ticker("KRW=X").fast_info["last_price"]
            if rate and 900 < rate < 2000:
                with _FX_CACHE_LOCK:
                    _FX_CACHE["rate"] = float(rate)
                    _FX_CACHE["ts"] = time.monotonic()
                return float(rate)
        except Exception:
            pass
        age_min = (time.monotonic() - _FX_CACHE["ts"]) / 60
        if age_min > 30:
            logger.warning("FX 환율 오래됨 (%.0f분) — 킬스위치 계산 부정확 가능 (fallback=%.0f)", age_min, _FX_CACHE["rate"])
        return _FX_CACHE["rate"]
