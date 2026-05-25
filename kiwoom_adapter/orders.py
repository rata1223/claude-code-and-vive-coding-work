import logging
from .client import KiwoomClient

logger = logging.getLogger(__name__)


class KiwoomOrders:
    def __init__(self, client: KiwoomClient):
        self._client = client

    def buy_kr(self, symbol: str, qty: int, price: int | float) -> dict:
        body = {
            "CANO": self._client.account_no[:8],
            "ACNT_PRDT_CD": self._client.account_no[9:] if len(self._client.account_no) > 8 else "01",
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(price)),
        }
        resp = self._client.post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            body
        )
        logger.info("Kiwoom 매수 %s %d주 @%d", symbol, qty, price)
        return resp

    def sell_kr(self, symbol: str, qty: int, price: int | float) -> dict:
        body = {
            "CANO": self._client.account_no[:8],
            "ACNT_PRDT_CD": self._client.account_no[9:] if len(self._client.account_no) > 8 else "01",
            "PDNO": symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(price)),
            "SLL_TYPE": "01",
        }
        resp = self._client.post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            body
        )
        logger.info("Kiwoom 매도 %s %d주 @%d", symbol, qty, price)
        return resp
