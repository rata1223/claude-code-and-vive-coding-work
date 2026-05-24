import os
import logging
from .client import KISClient

logger = logging.getLogger(__name__)

PRICE_US_PATH = "/uapi/overseas-price/v1/quotations/price"
PRICE_KR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
PENDING_US_PATH = "/uapi/overseas-stock/v1/trading/inquire-nccs"

TR_PRICE_US = "HHDFS00000300"
TR_PRICE_KR = "FHKST01010100"
TR_PENDING_US = ("TTTS3018R", "VTTS3018R")


class KISMarketData:
    def __init__(self, client: KISClient = None):
        self._client = client or KISClient()
        self._paper = self._client.auth.env == "paper"

    def get_price_us(self, symbol: str, excd: str) -> float:
        params = {"AUTH": "", "EXCD": excd, "SYMB": symbol}
        data = self._client.get(PRICE_US_PATH, TR_PRICE_US, params)
        return float(data["output"]["last"])

    def get_price_kr(self, symbol: str) -> int:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }
        data = self._client.get(PRICE_KR_PATH, TR_PRICE_KR, params)
        return int(data["output"]["stck_prpr"])

    def get_pending_us(self, account_no: str) -> list:
        tr_id = TR_PENDING_US[1] if self._paper else TR_PENDING_US[0]
        params = {
            "CANO": account_no[:8],
            "ACNT_PRDT_CD": account_no[8:],
            "OVRS_EXCG_CD": "NASD",
            "SORT_SQN": "DS",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._client.get(PENDING_US_PATH, tr_id, params)
        return data.get("output", [])
