import os
import time
import json
import hashlib
import requests
import redis
from datetime import datetime, timedelta


PAPER_BASE = "https://openapivts.koreainvestment.com:9443"
REAL_BASE = "https://openapi.koreainvestment.com:9443"

TOKEN_CACHE_KEY = "kis:access_token"
TOKEN_EXPIRY_KEY = "kis:token_expiry"


class KISAuth:
    def __init__(self):
        self.app_key = os.environ["KIS_APP_KEY"]
        self.app_secret = os.environ["KIS_APP_SECRET"]
        self.env = os.environ.get("KIS_ENV", "paper")
        self.base_url = PAPER_BASE if self.env == "paper" else REAL_BASE
        self._redis = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))

    def get_token(self) -> str:
        cached = self._redis.get(TOKEN_CACHE_KEY)
        if cached:
            expiry = self._redis.get(TOKEN_EXPIRY_KEY)
            if expiry and float(expiry) - time.time() > 3600:
                return cached.decode()

        token, expires_in = self._issue_token()
        self._redis.set(TOKEN_CACHE_KEY, token)
        self._redis.set(TOKEN_EXPIRY_KEY, time.time() + expires_in)
        return token

    def _issue_token(self) -> tuple[str, int]:
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 86400))

    def revoke_token(self):
        token = self._redis.get(TOKEN_CACHE_KEY)
        if not token:
            return
        url = f"{self.base_url}/oauth2/revokeP"
        body = {
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "token": token.decode(),
        }
        requests.post(url, json=body, timeout=10)
        self._redis.delete(TOKEN_CACHE_KEY, TOKEN_EXPIRY_KEY)

    def get_hashkey(self, body: dict) -> str:
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()["HASH"]

    def get_headers(self, tr_id: str, include_token: bool = True) -> dict:
        headers = {
            "content-type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        if include_token:
            headers["authorization"] = f"Bearer {self.get_token()}"
        return headers
