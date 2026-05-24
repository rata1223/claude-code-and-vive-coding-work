import os
import logging
from .client import KISClient

logger = logging.getLogger(__name__)

BALANCE_US_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
BALANCE_KR_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"

TR_BALANCE_US = ("TTTS3012R", "VTTS3012R")
TR_BALANCE_KR = ("TTTC8434R", "VTTC8434R")


class KISPortfolio:
    def __init__(self, client: KISClient = None):
        self._client = client or KISClient()
        self._paper = self._client.auth.env == "paper"
        self._account = os.environ["KIS_ACCOUNT_NO"]

    def _tr(self, key: str) -> str:
        mapping = {
            "balance_us": TR_BALANCE_US,
            "balance_kr": TR_BALANCE_KR,
        }
        real_tr, paper_tr = mapping[key]
        return paper_tr if self._paper else real_tr

    def get_us_balance(self) -> dict:
        params = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._client.get(BALANCE_US_PATH, self._tr("balance_us"), params)
        positions = data.get("output1", [])
        summary = data.get("output2", {})
        return {"positions": positions, "summary": summary}

    def get_kr_balance(self) -> dict:
        params = {
            "CANO": self._account[:8],
            "ACNT_PRDT_CD": self._account[8:],
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = self._client.get(BALANCE_KR_PATH, self._tr("balance_kr"), params)
        positions = data.get("output1", [])
        summary = data.get("output2", {})
        return {"positions": positions, "summary": summary}

    def get_total_asset_krw(self) -> float:
        kr = self.get_kr_balance()
        us = self.get_us_balance()
        kr_eval = float(kr["summary"].get("tot_evlu_amt", 0))
        us_eval_usd = float(us["summary"].get("tot_evlu_amt", 0))
        # USD→KRW 환산은 시세 조회가 필요하므로 별도 처리
        return kr_eval, us_eval_usd
