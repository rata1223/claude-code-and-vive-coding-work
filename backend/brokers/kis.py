import os
import logging
import threading
import time
from decimal import Decimal, InvalidOperation
from .base import BrokerAdapter
from .capabilities import KIS_LIVE_CAPABILITIES, KIS_PAPER_CAPABILITIES
from .models import Balance, BrokerCapabilities, Order, OrderStatus, Position
from .semantic_mapper import KIS_DOMESTIC_MAPPER, KIS_OVERSEAS_MAPPER
from .validator import BrokerCapabilityValidator, OrderRequest
from kis_adapter import KISClient, KISMarketData, KISOrders, KISPortfolio
from kis_adapter.dates import inquiry_date_range
from kis_adapter.pagination import find_row
from backend.execution.circuit_breaker import ConsecutiveFailureBreaker
from backend.market.symbols import broker_exchange, is_mapped, resolve_exchange, to_quote_excd
from backend.quant.data.universe import KR_ETF

logger = logging.getLogger(__name__)

#: The balance summary fields ``get_balance`` builds its numbers from. None of
#: them has been confirmed against a live KIS response (issue #178).
_BALANCE_FIELDS = {"kr": ("dnca_tot_amt", "tot_evlu_amt"),
                   "us": ("frcr_dncl_amt_2", "tot_evlu_amt")}
_reported_missing: set = set()


def _missing_balance_fields(kr_summary, us_summary) -> list:
    """The fields read as 0 because the response did not have them.

    Each distinct gap is logged once per process, with the **names** of the
    fields the response did carry — no values, which are account balances. One
    paper-trading session then shows what KIS actually sends, which is what
    #178 needs to be closed.
    """
    missing = []
    for side, summary in (("kr", kr_summary), ("us", us_summary)):
        keys = set(summary) if isinstance(summary, dict) else set()
        gap = tuple(k for k in _BALANCE_FIELDS[side] if k not in keys)
        missing.extend(f"{side}.{k}" for k in gap)
        if gap and (side, gap) not in _reported_missing:
            _reported_missing.add((side, gap))
            logger.warning(
                "KIS %s 잔고 응답에 %s 필드 없음 — 0으로 계산됨, 총자산 미검증(#178). "
                "응답 필드: %s", side, list(gap), sorted(keys))
    return missing


def _order_excd(symbol: str) -> str:
    """``OVRS_EXCG_CD`` for a US order or order inquiry.

    Resolution moved out of this module so the bot path and the app path
    (``api/routers/quick_trade``) cannot drift: they used to agree only by
    coincidence, one reading ``EXCD_MAP`` and the other trusting the client.

    Falls back to the historical ``NASD`` when the symbol resolves to nothing,
    which here means a symbol that came from our own universe or from a broker
    position row and should always resolve — hence the warning. The hard
    refusal belongs at the API boundary, where symbols arrive untrusted.
    """
    exchange = resolve_exchange(symbol)
    if exchange is None:
        logger.warning("거래소 미확인 심볼 %s — NASD로 폴백", symbol)
        return "NASD"
    return exchange


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


# P0-07 S2: KIS reports how much of a holding is actually orderable
# (주문가능수량) alongside the held quantity. The balance rows reach us
# untouched, so the field is already in the payload — it was simply never read.
# Absent or unreadable → ``None``, which makes callers fail closed rather than
# fall back to the held quantity.
_ORDERABLE_FIELD = "ord_psbl_qty"


def _orderable_qty(row: dict, held: int):
    """Parse ``ord_psbl_qty`` into an exact share count, or ``None``.

    Parsed with ``Decimal`` rather than ``int(float(...))``: the latter accepts
    ``True``, silently truncates ``"1.9"`` to 1, and lets ``"inf"`` raise
    ``OverflowError`` out of ``get_positions()``. A quantity we cannot read
    exactly is not a quantity — it must fail closed, not authorise a sell.
    """
    raw = row.get(_ORDERABLE_FIELD)
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        return None
    # Never report more orderable than held — the smaller number is the safe one.
    return min(int(value), held)


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

    @property
    def capabilities(self) -> BrokerCapabilities:
        return KIS_PAPER_CAPABILITIES if self._paper else KIS_LIVE_CAPABILITIES

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
            missing = _missing_balance_fields(kr["summary"], us["summary"])
            return Balance(
                cash_krw=kr_cash,
                cash_usd=us_cash,
                total_eval_krw=kr_eval + us_eval_usd * self._get_fx(),
                # Unverified when a field read as 0 because it was absent, or
                # when USD cash exists: the total does not include it, and
                # whether the US evaluation already does is unconfirmed (#178).
                equity_verified=not missing and us_cash == 0,
            )
        except Exception:
            self._breaker.record_failure()
            raise

    # ── US venue: the broker's word for a holding, derived otherwise ─────────

    def _remember_venue(self, symbol: str, reported) -> None:
        """Record where the broker says an unmapped US holding trades.

        ``EXCD_MAP`` knows the universe; a holding outside it (bought by hand,
        say) would otherwise be sold as ``NASD`` and rejected if it is an NYSE
        name — and emergency flatten sells every holding. The balance row's
        ``ovrs_excg_cd`` says where the shares are.
        """
        code = broker_exchange(reported)
        if code is None:
            return
        if is_mapped(symbol):
            # A mapped symbol keeps routing by EXCD_MAP: every order, inquiry and
            # cancel for it has used that venue, and switching mid-flight could
            # look up or cancel an order on a venue other than the one it was
            # sent to. A disagreement is logged once — that is the evidence
            # known issue 4 (SPY/XL* on Arca) is waiting for.
            derived = resolve_exchange(symbol)
            warned = self.__dict__.setdefault("_venue_warned", set())
            if derived != code and symbol not in warned:
                warned.add(symbol)
                logger.warning("거래소 불일치 %s: EXCD_MAP=%s, 브로커 잔고=%s — EXCD_MAP 유지",
                               symbol, derived, code)
            return
        # Unmapped: the derived venue is only the NASD guess, so the broker's
        # word is strictly better — and nothing was routed by the guess before
        # it, since unmapped symbols reach the worker only as holdings.
        self.__dict__.setdefault("_held_venue", {})[symbol] = code

    def _us_order_excd(self, symbol: str) -> str:
        """``OVRS_EXCG_CD`` for ``symbol``: the broker-reported venue of a holding,
        else the derived one (``_order_excd``)."""
        held = (getattr(self, "_held_venue", None) or {}).get(symbol)
        return held or _order_excd(symbol)

    def _us_quote_excd(self, symbol: str) -> str:
        """``EXCD`` for a US quote — a different code set from the order one.

        KIS's own examples: ``order(ovrs_excg_cd="NASD")`` but
        ``price(excd="NAS")``. Passing the order code to the quote endpoint
        names an exchange it does not know.
        """
        return to_quote_excd(self._us_order_excd(symbol)) or "NAS"

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
                    positions.append(Position(symbol=sym, qty=qty, avg_price=avg,
                                              market="KR", current_price=cur,
                                              sellable_qty=_orderable_qty(p, qty)))
        except Exception as e:
            logger.warning("KR 포지션 조회 실패: %s", e)

        try:
            us = self._portfolio.get_us_balance()
            for p in us["positions"]:
                qty = int(p.get("ovrs_cblc_qty", 0))
                if qty > 0:
                    sym = p["ovrs_pdno"]
                    self._remember_venue(sym, p.get("ovrs_excg_cd"))
                    avg = float(p.get("pchs_avg_pric", 0))
                    try:
                        quote_excd = self._us_quote_excd(sym)
                        cur = self._market.get_price_us(sym, quote_excd)
                    except Exception:
                        cur = avg
                    positions.append(Position(symbol=sym, qty=qty, avg_price=avg,
                                              market="US", current_price=cur,
                                              sellable_qty=_orderable_qty(p, qty)))
        except Exception as e:
            logger.warning("US 포지션 조회 실패: %s", e)

        return positions

    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order:
        detected_market = "KR" if self._is_kr(symbol) else "US"
        BrokerCapabilityValidator(self.capabilities).validate(
            OrderRequest(symbol=symbol, side=side, qty=float(qty), price=price,
                         order_type=order_type, market=detected_market)
        )
        if self._breaker.is_open():
            logger.error("주문 차단 — circuit breaker open: %s %s", side, symbol)
            return Order(
                id="", symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.REJECTED, raw={"error": "circuit breaker open"},
            )

        # Calendar gate — raises MarketClosedError (NOT RuntimeError, circuit breaker safe)
        try:
            from backend.data.calendar import get_calendar_service, Market as _Market
            from datetime import datetime as _dt, timezone as _tz
            _mkt = _Market.KRX if self._is_kr(symbol) else _Market.NYSE
            get_calendar_service().assert_tradeable(_mkt, _dt.now(_tz.utc))
        except ImportError:
            pass

        is_kr = self._is_kr(symbol)
        try:
            if is_kr:
                raw = (self._orders.buy_kr if side == "buy" else self._orders.sell_kr)(symbol, qty, int(price))
            else:
                excd = self._us_order_excd(symbol)
                raw = (self._orders.buy_us if side == "buy" else self._orders.sell_us)(symbol, excd, qty, price)
            mapper = KIS_DOMESTIC_MAPPER if is_kr else KIS_OVERSEAS_MAPPER
            order_id = mapper.extract_broker_order_id(raw)
            self._breaker.record_success()
            return Order(
                id=order_id, symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.SUBMITTED, raw=raw,
            )
        except Exception as e:
            # MarketClosedError from KISClient must not penalize the circuit breaker
            try:
                from backend.data.calendar import MarketClosedError as _MCE
                if isinstance(e, _MCE):
                    raise
            except ImportError:
                pass
            self._breaker.record_failure()
            if isinstance(e, RuntimeError):
                # KIS API returned rt_cd != "0" — broker explicitly rejected
                logger.error("주문 거부됨 %s %s: %s", side, symbol, e)
                return Order(
                    id="", symbol=symbol, side=side, qty=qty, price=price,
                    status=OrderStatus.REJECTED, raw={"error": str(e)},
                )
            # Network timeout / connection error — order may have reached the broker
            logger.error("주문 결과 불확실 (UNKNOWN) %s %s: %s", side, symbol, e)
            return Order(
                id="", symbol=symbol, side=side, qty=qty, price=price,
                status=OrderStatus.UNKNOWN, raw={"error": str(e)},
            )

    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0, price: float = 0.0) -> bool:
        """주문 취소. US 종목은 cancel_us() 라우팅. KR: TTTC0803U/VTTC0803U."""
        is_us = bool(symbol) and not self._is_kr(symbol)
        if is_us:
            excd = self._us_order_excd(symbol)
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
            # Replayable: RVSE_CNCL_DVSN_CD=02 keyed to ORGN_ODNO. See KISClient.post.
            resp = self._client.post("/uapi/domestic-stock/v1/trading/order-rvsecncl",
                                     tr_id, body, idempotent=True)
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

        ``None`` = 조회는 성공했고 그 주문이 없다(모든 페이지를 읽고도 미매칭).
        조회 자체가 실패하면(네트워크·오류 응답·페이지네이션·행 파싱) **예외**를
        던진다 — "알 수 없음"이다. 예전엔 둘 다 None이었고, 재조정기는 None을
        "브로커에 없음"으로 읽어 1시간 지난 주문을 분실로 취소 처리했다.
        """
        is_us = bool(symbol) and not self._is_kr(symbol)
        if is_us:
            return self._get_us_order_status(order_id, symbol)
        return self._get_kr_order_status(order_id)

    def _get_kr_order_status(self, order_id: str) -> Order | None:
        """KIS TR: TTTC8036R (실전) / VTTC8036R (모의)."""
        try:
            tr_id = "VTTC8036R" if self._paper else "TTTC8036R"
            strt_dt, end_dt = inquiry_date_range()
            params = {
                "CANO": self._account[:8],
                "ACNT_PRDT_CD": self._account[8:],
                "INQR_STRT_DT": strt_dt,
                "INQR_END_DT": end_dt,
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
            # Match the specific order_id; never fall back to a different
            # order's row (the inquiry can return multiple orders). Stop at the
            # page that has it: a failure on a later page must not hide it —
            # the caller reads None as "absent at the broker".
            row = find_row(self._client, "/uapi/domestic-stock/v1/trading/inquire-order",
                           tr_id, params, ctx="100", list_key=("output1", "output"),
                           match=lambda r: r.get("odno") == order_id)
            if row is None:
                logger.warning("KR 주문 %s 응답에서 미매칭 — None 반환", order_id)
                return None
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
            # Unknown, not absent: the reconciler treats an exception as an error
            # and leaves the order alone, but None as "gone" and cancels it.
            logger.warning("KR 주문 조회 실패 %s: %s", order_id, e)
            raise RuntimeError(f"KR 주문 조회 실패 {order_id}: {e}") from e

    def _get_us_order_status(self, order_id: str, symbol: str) -> Order | None:
        """KIS 해외주식 주문 조회. TR: TTTS3035R (실전) / VTTS3035R (모의)."""
        try:
            tr_id = "VTTS3035R" if self._paper else "TTTS3035R"
            excd = self._us_order_excd(symbol)
            strt_dt, end_dt = inquiry_date_range()
            params = {
                "CANO": self._account[:8],
                "ACNT_PRDT_CD": self._account[8:],
                "OVRS_EXCG_CD": excd,
                "PDNO": symbol,
                "ORD_STRT_DT": strt_dt,
                "ORD_END_DT": end_dt,
                "SLL_BUY_DVSN_CD": "00",
                "CCL_NCCS_DVSN": "00",
                "INQR_DVSN": "00",
                "INQR_DVSN_1": "0",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            # Match the specific order_id; never fall back to a different order's
            # row. Stop at the page that has it (see _get_kr_order_status).
            row = find_row(self._client, "/uapi/overseas-stock/v1/trading/inquire-order",
                           tr_id, params, ctx="200", list_key="output",
                           match=lambda r: r.get("odno") == order_id)
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
            # Unknown, not absent: the reconciler treats an exception as an error
            # and leaves the order alone, but None as "gone" and cancels it.
            logger.warning("US 주문 조회 실패 %s: %s", order_id, e)
            raise RuntimeError(f"US 주문 조회 실패 {order_id}: {e}") from e

    def get_price(self, symbol: str) -> float:
        if self._breaker.is_open():
            raise RuntimeError(f"KIS circuit breaker open — get_price 차단: {symbol}")
        try:
            if self._is_kr(symbol):
                result = float(self._market.get_price_kr(symbol))
            else:
                quote_excd = self._us_quote_excd(symbol)
                result = self._market.get_price_us(symbol, quote_excd)
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
