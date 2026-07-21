import logging
from .auth import KISCredentials
from .client import KISClient

logger = logging.getLogger(__name__)

# TR_ID 매핑 (실전 / 모의)
TR = {
    "buy_us":    ("TTTT1002U", "JTTT1002U"),
    "sell_us":   ("TTTT1006U", "JTTT1006U"),
    "cancel_us": ("TTTT1004U", "JTTT1004U"),
    "buy_kr":    ("TTTC0802U", "VTTC0802U"),
    "sell_kr":   ("TTTC0801U", "VTTC0801U"),
}

ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
ORDER_KR_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"


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
