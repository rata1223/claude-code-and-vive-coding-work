import os
import time
import logging
import requests
from threading import Lock

logger = logging.getLogger(__name__)

PAPER_BASE = "https://openapi.koreainvestment.com:9443"
REAL_BASE = "https://openapi.koreainvestment.com:9443"

KIWOOM_BASE = "https://openapi.kiwoom.com:10000"


class RateLimiter:
    def __init__(self, calls_per_second: int):
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self._lock = Lock()

    def wait(self):
        with self._lock:
            elapsed = time.time() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.time()


class KiwoomClient:
    """키움증권 Open API+ REST 클라이언트."""

    MAX_RETRIES = 3

    def __init__(self, app_key: str, account_no: str, is_paper: bool = True):
        self.app_key = app_key
        self.account_no = account_no
        self.is_paper = is_paper
        self.base_url = KIWOOM_BASE
        self._limiter = RateLimiter(5)
        self._token: str | None = None
        self._token_expires: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 3600:
            return self._token

        url = f"{self.base_url}/oauth2/token"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._token = data["token"]
        self._token_expires = time.time() + int(data.get("expires_in", 86400))
        return self._token

    def _headers(self) -> dict:
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
        }

    def get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(self.MAX_RETRIES):
            self._limiter.wait()
            try:
                resp = requests.get(url, headers=self._headers(), params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") not in (None, "0", 0):
                    raise RuntimeError(f"Kiwoom API error: {data.get('msg1')}")
                return data
            except Exception as e:
                logger.warning("Kiwoom GET %s attempt %d: %s", path, attempt + 1, e)
                if attempt == self.MAX_RETRIES - 1:
                    raise
                time.sleep(1)

    def post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(self.MAX_RETRIES):
            self._limiter.wait()
            try:
                resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") not in (None, "0", 0):
                    raise RuntimeError(f"Kiwoom API error: {data.get('msg1')}")
                return data
            except Exception as e:
                logger.warning("Kiwoom POST %s attempt %d: %s", path, attempt + 1, e)
                if attempt == self.MAX_RETRIES - 1:
                    raise
                time.sleep(1)
