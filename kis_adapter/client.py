import os
import time
import logging
import requests
from threading import Lock
from .auth import KISAuth, KISCredentials

logger = logging.getLogger(__name__)


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


class KISClient:
    MAX_RETRIES = 3

    def __init__(self, credentials: "KISCredentials | None" = None):
        self.auth = KISAuth(credentials)
        rate = 5 if self.auth.env == "paper" else 15
        self._limiter = RateLimiter(rate)

    @property
    def base_url(self) -> str:
        return self.auth.base_url

    def get(self, path: str, tr_id: str, params: dict = None) -> dict:
        headers = self.auth.get_headers(tr_id)
        url = f"{self.base_url}{path}"

        for attempt in range(self.MAX_RETRIES):
            self._limiter.wait()
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") != "0":
                    raise RuntimeError(f"KIS API error: {data.get('msg1')}")
                return data
            except Exception as e:
                logger.warning("GET %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt == self.MAX_RETRIES - 1:
                    raise
                time.sleep(1)

    def post(self, path: str, tr_id: str, body: dict) -> dict:
        # Retries only on network-level errors (no response received).
        # rt_cd rejections break out immediately — never retry a confirmed API decision.
        hashkey = self.auth.get_hashkey(body)
        headers = self.auth.get_headers(tr_id)
        headers["hashkey"] = hashkey
        url = f"{self.base_url}{path}"

        _CLOSED_CODES = {"-90", "-91", "-100"}
        _CLOSED_TEXTS = ("거래가능시간", "시간외거래", "매매시간이 아님")

        for attempt in range(self.MAX_RETRIES):
            self._limiter.wait()
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("POST %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt == self.MAX_RETRIES - 1:
                    raise
                time.sleep(1)
                continue

            if data.get("rt_cd") != "0":
                code = data.get("rt_cd", "")
                msg = data.get("msg1", "")
                if code in _CLOSED_CODES or any(t in msg for t in _CLOSED_TEXTS):
                    try:
                        from backend.data.calendar import (
                            MarketClosedError, Market, SessionType, BlockReason,
                        )
                        raise MarketClosedError(
                            market=Market.KRX,
                            session=SessionType.CLOSED,
                            reason=BlockReason.WRONG_SESSION,
                            detail=f"KIS rt_cd={code}: {msg}",
                        )
                    except ImportError:
                        raise RuntimeError(f"KIS 시장 미개장 ({code}): {msg}")
                raise RuntimeError(f"KIS API error: {msg}")

            return data
