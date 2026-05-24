import logging
from .client import KiwoomClient

logger = logging.getLogger(__name__)


class KiwoomPortfolio:
    def __init__(self, client: KiwoomClient):
        self._client = client

    def get_kr_balance(self) -> dict:
        account = self._client.account_no
        cano = account[:8]
        acnt_prdt_cd = account[9:] if len(account) > 8 else "01"
        resp = self._client.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        output1 = resp.get("output1", [])
        output2 = resp.get("output2", [{}])
        summary = output2[0] if output2 else {}

        positions = []
        for item in output1:
            qty = int(item.get("hldg_qty", 0))
            if qty > 0:
                positions.append({
                    "pdno": item.get("pdno", ""),
                    "prdt_name": item.get("prdt_name", ""),
                    "hldg_qty": qty,
                    "pchs_avg_pric": float(item.get("pchs_avg_pric", 0)),
                    "prpr": float(item.get("prpr", 0)),
                    "evlu_pfls_amt": float(item.get("evlu_pfls_amt", 0)),
                })

        return {
            "positions": positions,
            "cash_balance": float(summary.get("dnca_tot_amt", 0)),
            "total_eval": float(summary.get("tot_evlu_amt", 0)),
        }

    def get_total_asset_krw(self) -> tuple[float, float]:
        try:
            balance = self.get_kr_balance()
            total = balance.get("total_eval", 0)
            return total, 0.0
        except Exception as e:
            logger.warning("Kiwoom 총자산 조회 실패: %s", e)
            return 0.0, 0.0
