"""
ScriptStrategy: 사용자가 Python 스크립트로 직접 작성하는 이벤트 기반 전략.
RestrictedPython 기반 샌드박스 실행.
"""
import logging
import math
import operator
import statistics
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from RestrictedPython import compile_restricted
from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    guarded_unpack_sequence,
    safer_getattr,
)
from RestrictedPython.PrintCollector import PrintCollector

logger = logging.getLogger(__name__)


@dataclass
class Bar:
    symbol: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str = "1d"


@dataclass
class Signal:
    action: str  # "buy" | "sell" | "hold"
    symbol: str
    qty: int = 1
    price: float | None = None
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Order:
    id: str
    symbol: str
    side: str
    qty: int
    price: float
    status: str = "filled"
    filled_at: float = field(default_factory=time.time)


SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "print": print, "range": range,
    "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
}


def _public_functions(module, names):
    """A namespace holding only the named callables — never the module itself.

    A module object hands out everything it imported: ``statistics.sys`` is a
    public attribute, and from ``sys`` every other module is one lookup away.
    """
    return SimpleNamespace(**{n: getattr(module, n) for n in names})


SAFE_MODULES = {
    "math": SimpleNamespace(
        pi=math.pi, e=math.e, inf=math.inf, nan=math.nan,
        **vars(_public_functions(math, [
            "sqrt", "exp", "log", "log10", "log2", "pow", "fabs", "floor", "ceil",
            "isnan", "isinf", "isfinite", "copysign", "fsum", "prod", "tanh",
            "sin", "cos", "tan", "atan", "atan2", "hypot",
        ]))),
    "statistics": _public_functions(statistics, [
        "mean", "fmean", "geometric_mean", "harmonic_mean", "median",
        "median_low", "median_high", "mode", "multimode", "stdev", "pstdev",
        "variance", "pvariance", "quantiles",
    ]),
}

_INPLACE_OPS = {
    "+=": operator.iadd, "-=": operator.isub, "*=": operator.imul,
    "/=": operator.itruediv, "//=": operator.ifloordiv, "%=": operator.imod,
    "**=": operator.ipow,
}


def _inplacevar(op, x, y):
    """RestrictedPython routes ``x += y`` here; only arithmetic is supported."""
    fn = _INPLACE_OPS.get(op)
    if fn is None:
        raise SyntaxError(f"지원하지 않는 복합 대입: {op}")
    return fn(x, y)


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    """``import math`` / ``import statistics`` resolve to the vetted namespaces
    above; every other import is refused."""
    if level == 0 and name in SAFE_MODULES and not fromlist:
        return SAFE_MODULES[name]
    raise ImportError(f"허용되지 않은 import: {name}")


def _restricted_globals() -> dict[str, Any]:
    """The globals a script runs under: RestrictedPython's guard hooks, our
    builtin allowlist, and the vetted modules.

    User scripts used to run through plain ``exec`` with this module's builtins,
    ``pandas`` and the real ``math``/``statistics`` modules — no guard at all.
    Every attribute read now goes through ``safer_getattr`` (no ``_``-prefixed
    names), writes only reach dicts and lists (``full_write_guard``), and
    ``pandas`` is gone: its I/O functions carry no underscore to stop them.
    """
    builtins = dict(SAFE_BUILTINS)
    builtins["__import__"] = _guarded_import
    return {
        "__builtins__": builtins,
        "__name__": "strategy_script",
        "_getattr_": safer_getattr,
        "_getitem_": default_guarded_getitem,
        "_getiter_": default_guarded_getiter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_unpack_sequence_": guarded_unpack_sequence,
        "_write_": full_write_guard,
        "_inplacevar_": _inplacevar,
        "_print_": PrintCollector,
        "_apply_": lambda f, *a, **kw: f(*a, **kw),
        **SAFE_MODULES,
        "Signal": Signal,
        "Bar": Bar,
    }


class ScriptStrategy:
    """
    사용자 제공 Python 코드를 실행하는 이벤트 기반 전략.

    스크립트는 다음 함수를 정의해야 합니다:
      def on_bar(bar, context) -> Signal | None

    선택적으로:
      def on_start(context)
      def on_order_filled(order, context)
      def on_stop(context)
    """

    def __init__(self, code: str, params: dict | None = None):
        self.code = code
        self.params = params or {}
        self._ns: dict[str, Any] = {}
        self._context: dict[str, Any] = {"params": self.params, "state": {}}
        self._compiled = False
        self._error: str | None = None

    def compile(self) -> bool:
        try:
            code = compile_restricted(self.code, "<strategy>", "exec")
            env = _restricted_globals()
            exec(code, env)
            self._ns = env
            self._compiled = True
            return True
        except Exception as e:
            self._error = str(e)
            logger.error("ScriptStrategy 컴파일 실패: %s", e)
            return False

    def on_start(self) -> None:
        if not self._compiled:
            self.compile()
        fn = self._ns.get("on_start")
        if callable(fn):
            try:
                fn(self._context)
            except Exception as e:
                logger.error("on_start 실행 오류: %s", e)

    def on_bar(self, bar: Bar) -> Signal | None:
        if not self._compiled:
            if not self.compile():
                return None
        fn = self._ns.get("on_bar")
        if not callable(fn):
            logger.warning("on_bar 함수가 정의되지 않았습니다")
            return None
        try:
            result = fn(bar, self._context)
            if result is not None and not isinstance(result, Signal):
                logger.warning("on_bar가 Signal이 아닌 값을 반환했습니다: %s", type(result))
                return None
            return result
        except Exception as e:
            logger.error("on_bar 실행 오류: %s", e)
            return None

    def on_order_filled(self, order: Order) -> None:
        fn = self._ns.get("on_order_filled")
        if callable(fn):
            try:
                fn(order, self._context)
            except Exception as e:
                logger.error("on_order_filled 실행 오류: %s", e)

    def on_stop(self) -> None:
        fn = self._ns.get("on_stop")
        if callable(fn):
            try:
                fn(self._context)
            except Exception as e:
                logger.error("on_stop 실행 오류: %s", e)

    @property
    def compile_error(self) -> str | None:
        return self._error


SCRIPT_TEMPLATES = {
    "momentum": {
        "name": "모멘텀 전략",
        "description": "3개월 수익률 + RSI + SMA200 기반 매수, 손절/추세 이탈 시 매도",
        "code": """\
def on_start(ctx):
    ctx['state']['entry_price'] = {}

def on_bar(bar, ctx):
    import statistics
    state = ctx['state']
    params = ctx['params']

    stop_loss = params.get('stop_loss_pct', 0.07)

    # 진입가 기준 손절
    ep = state.get('entry_price', {}).get(bar.symbol)
    if ep and bar.close <= ep * (1 - stop_loss):
        return Signal(action='sell', symbol=bar.symbol, reason='손절')

    return None
""",
        "params": {"stop_loss_pct": 0.07, "take_profit_pct": 0.15},
    },
    "ma_crossover": {
        "name": "이동평균 크로스오버",
        "description": "단기 EMA가 장기 EMA를 상향 돌파 시 매수, 하향 이탈 시 매도",
        "code": """\
def on_start(ctx):
    ctx['state'] = {'fast_ema': [], 'slow_ema': [], 'position': {}}

def on_bar(bar, ctx):
    state = ctx['state']
    params = ctx['params']
    fast_len = int(params.get('fast_length', 12))
    slow_len = int(params.get('slow_length', 26))

    fast_list = state['fast_ema']
    slow_list = state['slow_ema']

    alpha_f = 2 / (fast_len + 1)
    alpha_s = 2 / (slow_len + 1)

    if not fast_list:
        fast_list.append(bar.close)
        slow_list.append(bar.close)
        return None

    fast_list.append(fast_list[-1] * (1 - alpha_f) + bar.close * alpha_f)
    slow_list.append(slow_list[-1] * (1 - alpha_s) + bar.close * alpha_s)

    if len(fast_list) < 2:
        return None

    prev_fast, curr_fast = fast_list[-2], fast_list[-1]
    prev_slow, curr_slow = slow_list[-2], slow_list[-1]

    has_position = state['position'].get(bar.symbol, False)

    if prev_fast < prev_slow and curr_fast >= curr_slow and not has_position:
        state['position'][bar.symbol] = True
        return Signal(action='buy', symbol=bar.symbol, reason='골든크로스')

    if prev_fast > prev_slow and curr_fast <= curr_slow and has_position:
        state['position'][bar.symbol] = False
        return Signal(action='sell', symbol=bar.symbol, reason='데드크로스')

    return None

def on_order_filled(order, ctx):
    if order.side == 'sell':
        ctx['state']['position'][order.symbol] = False
""",
        "params": {"fast_length": 12, "slow_length": 26},
    },
    "mean_reversion": {
        "name": "평균 회귀 전략",
        "description": "볼린저 밴드 하단 터치 시 매수, 상단 터치 시 매도",
        "code": """\
def on_start(ctx):
    ctx['state'] = {'prices': {}, 'position': {}}

def bollinger_bands(prices, length=20, std_mult=2.0):
    import statistics
    if len(prices) < length:
        return None, None, None
    window = prices[-length:]
    mid = sum(window) / length
    variance = sum((x - mid) ** 2 for x in window) / length
    std = variance ** 0.5
    return mid - std_mult * std, mid, mid + std_mult * std

def on_bar(bar, ctx):
    state = ctx['state']
    params = ctx['params']
    length = int(params.get('bb_length', 20))
    std_mult = float(params.get('bb_std', 2.0))

    if bar.symbol not in state['prices']:
        state['prices'][bar.symbol] = []
    state['prices'][bar.symbol].append(bar.close)

    prices = state['prices'][bar.symbol]
    lower, mid, upper = bollinger_bands(prices, length, std_mult)
    if lower is None:
        return None

    has_position = state['position'].get(bar.symbol, False)

    if bar.close <= lower and not has_position:
        state['position'][bar.symbol] = True
        return Signal(action='buy', symbol=bar.symbol, reason='BB 하단')

    if bar.close >= upper and has_position:
        state['position'][bar.symbol] = False
        return Signal(action='sell', symbol=bar.symbol, reason='BB 상단')

    return None

def on_order_filled(order, ctx):
    if order.side == 'sell':
        ctx['state']['position'][order.symbol] = False
""",
        "params": {"bb_length": 20, "bb_std": 2.0},
    },
}
