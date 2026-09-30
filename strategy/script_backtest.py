"""User-script backtests run in a child process with a hard time and memory limit (#188).

``POST /api/strategies/backtest`` runs a user's saved script on every bar of the
period. The script is sandboxed (RestrictedPython, strategy/script_strategy.py)
but nothing bounded how long it ran: a loop that never ends, or one long C call
such as ``sum(range(10**12))``, held the request's worker forever. A thread
cannot be stopped from outside and a trace hook cannot interrupt a C call, so
the script runs in its own process, which the parent kills once the budget is
spent. The child also gets an address-space limit, so a runaway allocation ends
the child rather than the API process.

The parent fetches prices (it has the network) and passes the frame in; the
child only compiles, runs and reports ``BacktestResult.to_dict()``. User code
never executes in the API process.

At most ``MAX_CONCURRENT`` backtests run at once per API process; one more is
refused at once rather than queued. Each child may use up to its memory budget,
so without a cap a burst of requests multiplies that budget on the host. The
API runs a single uvicorn worker, so the per-process cap is the global one.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = float(os.environ.get("SCRIPT_BACKTEST_TIMEOUT_SEC", "30"))
DEFAULT_MEMORY_MB = int(os.environ.get("SCRIPT_BACKTEST_MEMORY_MB", "1024"))
MAX_CONCURRENT = max(1, int(os.environ.get("SCRIPT_BACKTEST_MAX_CONCURRENT", "2")))

# The route runs in FastAPI's threadpool, so a thread semaphore bounds it.
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)

# spawn, not fork: the API process runs threads (uvicorn workers, DB pools), and
# forking a threaded process can copy a held lock into the child.
_CTX = mp.get_context("spawn")


class ScriptBacktestError(Exception):
    """The script backtest ended without a result (crash, memory limit, error)."""


class ScriptBacktestTimeout(ScriptBacktestError):
    """The script backtest ran past its time budget and was stopped."""


class ScriptBacktestBusy(ScriptBacktestError):
    """Every backtest slot is taken; nothing was started."""


def _limit_memory(extra_mb: int, cpu_sec: float) -> None:
    """Cap the child's address space at its current size plus ``extra_mb``.

    Set after the imports, so pandas/numpy's own reservations are not counted
    against the script. Best-effort outside Linux.
    """
    try:
        import resource

        with open("/proc/self/statm") as f:
            current = int(f.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
        limit = current + extra_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        cpu = int(cpu_sec) + 5          # backstop behind the parent's kill
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except (ImportError, OSError, ValueError) as e:  # pragma: no cover - non-Linux
        logger.warning("스크립트 백테스트 자원 제한을 걸지 못함: %s", e)


def _child(conn, code: str, params: dict, symbol: str, initial_capital: float,
           period: str, df, memory_mb: int, cpu_sec: float) -> None:
    try:
        from strategy.backtest import Backtester
        from strategy.script_strategy import ScriptStrategy

        _limit_memory(memory_mb, cpu_sec)
        strat = ScriptStrategy(code=code, params=params)
        strat.on_start()
        result = Backtester(strat, symbol, initial_capital=initial_capital,
                            period=period).run(df=df)
        conn.send(("ok", result.to_dict()))
    except MemoryError:
        conn.send(("error", "스크립트가 메모리 한도를 넘었습니다"))
    except BaseException as e:  # noqa: BLE001 - report, never raise out of the child
        conn.send(("error", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def run_script_backtest(code: str, params: dict | None, symbol: str, *,
                        initial_capital: float, period: str, df=None,
                        timeout_sec: float | None = None,
                        memory_mb: int | None = None) -> dict[str, Any]:
    """Backtest ``code`` on ``symbol`` in a child process; the result dict.

    Raises ``ScriptBacktestBusy`` when every slot is taken (nothing starts),
    ``ScriptBacktestTimeout`` when the budget runs out (the child is killed)
    and ``ScriptBacktestError`` when the child ends without a result.
    """
    if not _SLOTS.acquire(blocking=False):
        raise ScriptBacktestBusy(
            f"다른 백테스트가 실행 중입니다(동시 {MAX_CONCURRENT}개) — 잠시 후 다시 시도하세요")
    try:
        return _run(code, params, symbol, initial_capital=initial_capital,
                    period=period, df=df, timeout_sec=timeout_sec,
                    memory_mb=memory_mb)
    finally:
        _SLOTS.release()


def _run(code: str, params: dict | None, symbol: str, *, initial_capital: float,
         period: str, df, timeout_sec: float | None,
         memory_mb: int | None) -> dict[str, Any]:
    timeout = DEFAULT_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    memory = DEFAULT_MEMORY_MB if memory_mb is None else memory_mb
    if df is None:
        from strategy.backtest import Backtester
        df = Backtester(None, symbol, period=period)._fetch()

    recv_conn, send_conn = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(
        target=_child, name="script-backtest", daemon=True,
        args=(send_conn, code, params or {}, symbol, initial_capital, period, df,
              memory, timeout),
    )
    proc.start()
    send_conn.close()          # the child holds the only writer now
    try:
        if not recv_conn.poll(timeout):
            raise ScriptBacktestTimeout(
                f"스크립트 실행 시간 초과({timeout:g}초) — 무한 루프나 과도한 계산이 없는지 확인하세요")
        try:
            status, payload = recv_conn.recv()
        except EOFError:
            raise ScriptBacktestError(
                f"스크립트 실행이 비정상 종료됐습니다(종료 코드 {proc.exitcode})") from None
        if status != "ok":
            raise ScriptBacktestError(f"스크립트 실행 실패: {payload}")
        return payload
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        recv_conn.close()
