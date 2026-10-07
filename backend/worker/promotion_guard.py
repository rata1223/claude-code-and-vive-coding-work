"""
실전 매매 전환 체크리스트.

KIS_ENV=real + ENABLE_LIVE_TRADING=true 로 전환하기 전에
모든 항목이 통과해야 한다. StartupRecovery._step_enable_trading()에서 호출.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

#: 실전 전환 전에 모의투자로 채워야 하는 연속 실행 기간.
PAPER_RUN_MIN = timedelta(days=28)


def paper_run_qualifies(run, now: datetime, uptime: timedelta) -> bool:
    """이 실행이 28일 동안 중지되지 않고 **실제로 돌고 있었는가** — 기간만 본다.

    ``uptime``은 :func:`uptime_by_run`이 센, 실행 구간 중 그 실행이 실제로 돌던
    시간이다. 달력 날수가 아니다 — 워커가 6시간 내려가 있었거나 복원이 실패해 아무도
    돌리지 않았으면 6시간을 더 채워야 한다(워커 재시작은 실행을 멈추지 않는다:
    ``is_active`` 유지, 기동 시 복원). 환경(모의/실전)과 실제 체결은
    :func:`paper_gate_status`가 함께 본다.

    - 시작 기록이 없으면 불통과.
    - 중지 요청은 됐는데(``is_active=False``) 종료 기록(``stopped_at``)이 없는 행은
      언제 멈췄는지 알 수 없으므로 불통과(fail-closed).
    - 그 밖에는 ``uptime``이 28일 이상이어야 한다.
    """
    if getattr(run, "started_at", None) is None:
        return False
    if getattr(run, "stopped_at", None) is None and not getattr(run, "is_active", False):
        return False
    return uptime >= PAPER_RUN_MIN


def run_window(run, now: datetime) -> tuple[datetime, datetime] | None:
    """실행이 차지한 구간: 시작부터 종료 기록(없으면 지금)까지."""
    started = getattr(run, "started_at", None)
    if started is None:
        return None
    end = getattr(run, "stopped_at", None) or now
    return started, max(end, started)


def covered_time(intervals, start: datetime, end: datetime) -> timedelta:
    """``[start, end]`` 안에서 ``intervals``가 덮는 시간. 겹친 구간은 한 번만 센다."""
    clipped = sorted((max(a, start), min(b, end)) for a, b in intervals)
    total = timedelta(0)
    cur_a = cur_b = None
    for a, b in clipped:
        if b <= a:
            continue
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def uptime_by_run(db, runs, now: datetime) -> dict:
    """실행별로 실제로 돌고 있던 시간(``run_uptime``). 한 번의 쿼리.

    실행의 세션이 ``strategy.start()``에 성공한 뒤부터 기록한다(복원이 실패해 아무도
    돌리지 않는 활성 실행은 기록이 없다). 한 행은 ``boot_at``부터 깨끗한 종료
    (``ended_at``)까지, 종료 기록이 없으면(죽었거나 아직 도는 중) 마지막 박동 +
    :data:`~backend.worker.uptime.UPTIME_GRACE`까지(지금을 넘지 않게) 덮는다.
    기록이 없으면 0 — 이 기능 이전의 실행은 관문에 세지 않는다.
    """
    from collections import defaultdict
    from backend.database.models import RunUptime
    from backend.worker.uptime import UPTIME_GRACE
    windows = {r.id: w for r in runs if (w := run_window(r, now)) is not None}
    result = {r.id: timedelta(0) for r in runs}
    if not windows:
        return result
    intervals = defaultdict(list)
    for row in db.query(RunUptime).filter(RunUptime.run_id.in_(list(windows))).all():
        end = (row.ended_at if row.ended_at is not None
               else min(row.last_beat_at + UPTIME_GRACE, now))
        intervals[row.run_id].append((row.boot_at, end))
    for rid, (a, b) in windows.items():
        result[rid] = covered_time(intervals[rid], a, b)
    return result


#: kis-api가 실행을 만들 때 ``config``에 찍는 환경 키(서버의 ``KIS_ENV``).
#: 클라이언트가 보낸 값은 덮어쓴다 — 실행이 어느 환경에서 돌았는지의 기록이다.
RUN_ENV_KEY = "kis_env"


#: 워커가 실행을 시작·복원할 때 찍는 주문 제출 여부(``ENABLE_LIVE_TRADING``).
#: ``False``면 섀도(신호만, 주문 없음)라 체결이 생기지 않는다 — 화면 설명용이고,
#: 관문은 체결 자체를 본다.
RUN_ORDERS_KEY = "orders_enabled"


def orders_enabled() -> bool:
    """이 프로세스가 주문을 실제로 제출하는가(``backend/strategy/base.py``의 섀도 게이트와 같은 규칙)."""
    return os.environ.get("ENABLE_LIVE_TRADING", "false").lower() == "true"


def current_kis_env() -> str:
    """이 프로세스의 KIS 환경. compose 기본값과 같이 없으면 ``paper``."""
    return os.environ.get("KIS_ENV", "paper")


def _run_config(run) -> dict:
    raw = getattr(run, "config", None)
    if isinstance(raw, dict):
        return raw
    try:
        cfg = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def run_kis_env(run) -> str | None:
    """실행에 찍힌 환경. 찍히지 않았거나(이 기록 이전의 행) 읽을 수 없으면 ``None``."""
    env = _run_config(run).get(RUN_ENV_KEY)
    return env if isinstance(env, str) and env else None


def run_orders_enabled(run) -> bool | None:
    """워커가 마지막으로 찍은 주문 제출 여부. 기록이 없으면 ``None``."""
    value = _run_config(run).get(RUN_ORDERS_KEY)
    return value if isinstance(value, bool) else None


def paper_gate_status(run, filled_orders: int, now: datetime,
                      uptime: timedelta) -> tuple[bool, str | None]:
    """4주 관문을 이 실행이 채우는가, 아니면 왜 못 채우는가.

    관문(:meth:`LivePromotionGuard._check_paper_run`)과 운영 화면이 같은 규칙을
    쓰도록 한곳에 둔다. 세 가지가 모두 필요하다.

    - 환경: 모의(``paper``)로 찍힌 실행. 실전으로 찍혔거나 기록이 없으면 불통과.
    - 기간: :func:`paper_run_qualifies` — 실제로 돌던 시간(``uptime``)이 28일.
    - 체결: 이 실행에 귀속된 주문(``orders.strategy_run_id``) 중 체결 수량이 있는
      것이 하나 이상(``filled_orders``). 시간만 흐르고 아무것도 체결되지 않은
      실행은 주문→체결→기록 경로를 검증하지 못한다.

    사유는 고칠 수 없는 것(환경)부터: ``env_unknown``, ``env_not_paper``,
    ``duration``, ``no_fills``.
    """
    env = run_kis_env(run)
    if env is None:
        return False, "env_unknown"
    if env != "paper":
        return False, "env_not_paper"
    if not paper_run_qualifies(run, now, uptime):
        return False, "duration"
    if not filled_orders:
        return False, "no_fills"
    return True, None


def filled_order_counts(db, run_ids) -> dict:
    """실행별로 체결 수량이 있는 귀속 주문 수. 한 번의 쿼리."""
    ids = [i for i in run_ids if i is not None]
    if not ids:
        return {}
    from sqlalchemy import func
    from backend.database.models import Order
    rows = (db.query(Order.strategy_run_id, func.count(Order.id))
            .filter(Order.strategy_run_id.in_(ids), Order.filled_qty > 0)
            .group_by(Order.strategy_run_id)
            .all())
    return {rid: n for rid, n in rows}


class LivePromotionGuard:
    """
    실전 전환 전 필수 체크리스트.

    미달 항목이 있으면 (False, [failed_items]) 반환.
    SAFE_MODE를 enable하지 않고 Worker를 SafeMode로 유지.
    """

    def __init__(self, db_factory: Callable, redis_client=None):
        self._factory = db_factory
        self._redis = redis_client

    def check(self) -> tuple[bool, list[str]]:
        checks: list[tuple[str, Callable[[], bool]]] = [
            ("KIS_ENV=real", self._check_kis_env),
            ("ENABLE_LIVE_TRADING=true", self._check_live_flag),
            ("Telegram 설정됨", self._check_telegram),
            ("DB 연결 가능", self._check_db),
            ("Redis 연결 가능", self._check_redis),
            ("4주 모의투자 완료", self._check_paper_run),
        ]
        failed = []
        for name, fn in checks:
            try:
                if not fn():
                    failed.append(name)
                    logger.warning("실전 전환 체크 실패: %s", name)
            except Exception as e:
                failed.append(f"{name} (오류: {e})")
                logger.warning("실전 전환 체크 예외 %s: %s", name, e)

        if failed:
            logger.error("실전 전환 불가 — 미달 항목: %s", failed)
        else:
            logger.info("실전 전환 체크리스트 전항목 통과")
        return len(failed) == 0, failed

    def _check_kis_env(self) -> bool:
        return os.environ.get("KIS_ENV") == "real"

    def _check_live_flag(self) -> bool:
        return os.environ.get("ENABLE_LIVE_TRADING", "false").lower() == "true"

    def _check_telegram(self) -> bool:
        token = os.environ.get("TELEGRAM_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        return (bool(token) and not token.startswith("여기에") and
                bool(chat_id) and not chat_id.startswith("여기에"))

    def _check_db(self) -> bool:
        try:
            from sqlalchemy import text
            db = self._factory()
            db.execute(text("SELECT 1"))
            db.close()
            return True
        except Exception:
            return False

    def _check_redis(self) -> bool:
        if self._redis is None:
            return False
        try:
            self._redis.ping()
            return True
        except Exception:
            return False

    def _check_paper_run(self) -> bool:
        """모의 환경에서 28일(4주) 동안 중지되지 않고 실제로 돌았으며 체결이
        있었던 실행이 있는지.

        예전에는 ``started_at <= now-28d`` 인 행이 **있기만** 하면 통과했다 — 28일
        전에 시작해 1분 뒤 중지한 실행도. 그다음엔 기간만 봤다. 이제 환경과 체결까지
        :func:`paper_gate_status`로 본다.
        """
        try:
            from backend.database.models import StrategyRun
            db = self._factory()
            try:
                now = datetime.utcnow()
                cutoff = now - PAPER_RUN_MIN
                candidates = (db.query(StrategyRun)
                              .filter(StrategyRun.started_at <= cutoff)
                              .all())
                fills = filled_order_counts(db, [r.id for r in candidates])
                uptime = uptime_by_run(db, candidates, now)
                statuses = {r.id: paper_gate_status(r, fills.get(r.id, 0), now, uptime[r.id])
                            for r in candidates}
            finally:
                db.close()
            qualified = [rid for rid, (ok, _) in statuses.items() if ok]
            if not qualified:
                logger.warning("4주 모의투자 미완료: 조건(모의 환경·28일 무중지 가동·체결 1건 이상)을 "
                               "채운 실행 없음 — 28일 전에 시작한 행의 사유: %s",
                               {rid: reason for rid, (_, reason) in statuses.items()} or "없음")
                return False
            logger.info("4주 모의투자 확인: run_id=%s", qualified)
            return True
        except Exception as e:
            logger.warning("모의투자 기간 확인 실패: %s", e)
            return False
