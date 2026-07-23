import logging
from .auth import KISCredentials
from .client import KISClient
from .dates import inquiry_date_range

logger = logging.getLogger(__name__)

# TR_ID 매핑 (실전 / 모의)
TR = {
    "buy_us":    ("TTTT1002U", "JTTT1002U"),
    "sell_us":   ("TTTT1006U", "JTTT1006U"),
    "cancel_us": ("TTTT1004U", "JTTT1004U"),
    "buy_kr":    ("TTTC0802U", "VTTC0802U"),
    "sell_kr":   ("TTTC0801U", "VTTC0801U"),
    # Read-only single-symbol order inquiry (used by Quick Trade reconciliation to
    # resolve a RESERVED order after an indeterminate submit). Same TRs the
    # Execution-Layer poller uses in backend/brokers/kis.py.
    "inquire_us": ("TTTS3035R", "VTTS3035R"),
    "inquire_kr": ("TTTC8036R", "VTTC8036R"),
}

ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
ORDER_KR_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
INQUIRE_US_PATH = "/uapi/overseas-stock/v1/trading/inquire-order"
INQUIRE_KR_PATH = "/uapi/domestic-stock/v1/trading/inquire-order"


class KISOrders:
    def __init__(self, client: KISClient = None, credentials: "KISCredentials | None" = None):
        self._client = client or KISClient(credentials)
        self._paper = self._client.auth.env == "paper"
        # Account comes from the injected credentials (via the client's auth);
        # only the env-based Execution path may fall back to the process env.
        self._account = self._client.auth.require_account()

    def _tr(self, key: str) -> str:
        real_tr, paper_tr = TR[key]
        return paper_tr if self._paper else real_tr

    def buy_us(self, symbol: str, excd: str, qty: int, price: float) -> dict:
        body = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "OVRS_EXCG_CD": excd,
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        logger.info("BUY_US %s %s qty=%d price=%.2f", excd, symbol, qty, price)
        return self._client.post(ORDER_PATH, self._tr("buy_us"), body)

    def sell_us(self, symbol: str, excd: str, qty: int, price: float) -> dict:
        body = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "OVRS_EXCG_CD": excd,
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        logger.info("SELL_US %s %s qty=%d price=%.2f", excd, symbol, qty, price)
        return self._client.post(ORDER_PATH, self._tr("sell_us"), body)

    def buy_kr(self, symbol: str, qty: int, price: int) -> dict:
        body = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        logger.info("BUY_KR %s qty=%d price=%d", symbol, qty, price)
        return self._client.post(ORDER_KR_PATH, self._tr("buy_kr"), body)

    def sell_kr(self, symbol: str, qty: int, price: int) -> dict:
        body = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        logger.info("SELL_KR %s qty=%d price=%d", symbol, qty, price)
        return self._client.post(ORDER_KR_PATH, self._tr("sell_kr"), body)

    def inquire_orders(self, symbol: str, market: str = "us", excd: str = "NASD") -> list:
        """Read-only inquiry: recent orders for ``symbol``. Returns the raw KIS
        ``output`` list of order rows (each with ``odno``, ``sll_buy_dvsn_cd``,
        qty, price). Never places an order.

        Used by Quick Trade reconciliation to determine whether a RESERVED order
        (submitted with an indeterminate outcome) actually reached the broker.
        """
        strt_dt, end_dt = inquiry_date_range()
        if market.lower() == "kr":
            params = {
                "CANO": self._account[:8],
                "ACNT_PRDT_CD": self._account[8:],
                "INQR_STRT_DT": strt_dt,
                "INQR_END_DT": end_dt,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "01",
                "PDNO": symbol,
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }
            data = self._client.get(INQUIRE_KR_PATH, self._tr("inquire_kr"), params)
            return data.get("output1") or data.get("output", []) or []

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
        data = self._client.get(INQUIRE_US_PATH, self._tr("inquire_us"), params)
        return data.get("output") or []

    def cancel_us(self, org_order_no: str, symbol: str, excd: str, qty: int, price: float) -> dict:
        body = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "OVRS_EXCG_CD": excd,
            "PDNO": symbol,
            "ORGN_ODNO": org_order_no,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        logger.info("CANCEL_US %s order=%s", symbol, org_order_no)
        return self._client.post(CANCEL_PATH, self._tr("cancel_us"), body)
