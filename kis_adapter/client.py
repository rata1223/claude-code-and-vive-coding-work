import os
import time
import logging
import requests
from threading import Lock
from .auth import KISAuth, KISCredentials, HTTP_TIMEOUT_ENV, HTTP_TIMEOUT_SECONDS, _http_timeout  # noqa: F401 - re-exported

logger = logging.getLogger(__name__)



class RateLimiter:
    """Paces calls to at most ``calls_per_second``, shared across threads.

    Two properties matter now that a limiter is process-lived and shared by
    every caller on one credential, rather than thrown away with each request:

    * **Monotonic clock.** ``time.time()`` can step backwards (NTP), and a
      backward step of N seconds became an N-second sleep for every caller on
      that credential. ``time.monotonic()`` cannot.
    * **The lock is not held while sleeping.** Holding it would serialise every
      waiter behind the sleeper instead of merely pacing them. These run in
      sync FastAPI handlers on a bounded threadpool — ``GET /health`` included —
      so a throttled credential must not be able to exhaust it.
    """

    def __init__(self, calls_per_second: int):
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self._lock = Lock()

    def wait(self):
        # Claim a slot under the lock, then sleep outside it. Advancing
        # ``_last_call`` to the claimed time is what makes concurrent waiters
        # queue up at one-interval spacing rather than all waking together.
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._last_call + self.min_interval)
            self._last_call = scheduled
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)


#: One limiter per credential, shared process-wide. KIS meters per **app key**
#: (paper 5/s, real 15/s) and answers ``EGW00201`` over the line; a limiter
#: living on the client instance metered nothing, because the API routers build
#: a fresh ``KISClient`` on every request — deliberately, for credential
#: isolation (P0-03). Fresh instance, ``_last_call = 0``, no wait.
#:
#: ``backend/brokers/kis.py`` already keeps a process singleton *because*
#: "rate-limit tracking must be shared across all callers". The bot path
#: honoured that; the API path bypassed it. This puts the sharing where it
#: belongs — on the credential, not on whoever happened to construct a client.
#: ``msg_cd`` values that mean "retry", not "no". ``EGW00201`` is the per-second
#: cap; it is the only rejection that waiting actually fixes. ``EGW00133``
#: (token issuance, once a minute) is deliberately absent — a one-second retry
#: cannot satisfy a one-minute cap, and that path is the auth module's, not this
#: one's.
_TRANSIENT_MSG_CODES = frozenset({"EGW00201"})

_LIMITERS: dict = {}
_LIMITERS_LOCK = Lock()


def _limiter_for(auth: KISAuth) -> RateLimiter:
    """The rate limiter for ``auth``'s app key and environment.

    Keyed on ``KISAuth._fp`` — the sha256 of ``app_key:env`` that the token
    cache already uses. Reusing it means one definition of "which credential is
    this", and keeps the raw app key out of a module-level dict's keys.

    Paper and real are separate entries on purpose: same key, different ceiling
    (5/s vs 15/s) and a different endpoint, so one limiter would apply the wrong
    interval to one of them.
    """
    rate = 5 if auth.env == "paper" else 15
    key = (auth._fp, auth.env)
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = RateLimiter(rate)
            _LIMITERS[key] = limiter
        return limiter


class KISClient:
    MAX_RETRIES = 3

    def __init__(self, credentials: "KISCredentials | None" = None):
        self.auth = KISAuth(credentials)
        self._limiter = _limiter_for(self.auth)

    @property
    def base_url(self) -> str:
        return self.auth.base_url

    def get(self, path: str, tr_id: str, params: dict = None) -> dict:
        headers = self.auth.get_headers(tr_id)
        url = f"{self.base_url}{path}"

        # An ``rt_cd`` rejection is normally the broker's answer, not a failed
        # delivery: asking again returns the same answer and spends three of the
        # app key's rate-limit slots to hear it.
        #
        # ``EGW00201`` is the exception and must stay retryable. It means "you
        # are going too fast", and waiting is precisely the remedy. The limiter
        # above is per *process*, while one app key is driven by the ``api``
        # container, ``kis-worker`` and the bot at once — so a throttle is
        # reachable however well a single process paces itself. Surfacing it
        # would turn a momentary throttle into a failed balance lookup, and
        # ``_live_position_row`` rejects the user's close order on the strength
        # of that.
        for attempt in range(self.MAX_RETRIES):
            self._limiter.wait()
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=_http_timeout())
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("GET %s attempt %d failed: %s", path, attempt + 1, e)
                if attempt == self.MAX_RETRIES - 1:
                    raise
                time.sleep(1)
                continue

            if data.get("rt_cd") != "0":
                msg = data.get("msg1")
                if data.get("msg_cd") in _TRANSIENT_MSG_CODES and attempt < self.MAX_RETRIES - 1:
                    logger.warning("GET %s throttled (%s), retrying: %s",
                                   path, data.get("msg_cd"), msg)
                    time.sleep(1)
                    continue
                raise RuntimeError(f"KIS API error: {msg}")
            return data

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
                resp = requests.post(url, headers=headers, json=body, timeout=_http_timeout())
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
