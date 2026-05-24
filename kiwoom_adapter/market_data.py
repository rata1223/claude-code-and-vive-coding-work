import logging
from .client import KiwoomClient

logger = logging.getLogger(__name__)


class KiwoomMarketData:
    def __init__(self, client: KiwoomClient):
        self._client = client

    def get_price_kr(self, symbol: str) -> int:
        resp = self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        output = resp.get("output", {})
        price = int(output.get("stck_prpr", 0))
        logger.debug("Kiwoom 시세 %s → %d", symbol, price)
        return price
