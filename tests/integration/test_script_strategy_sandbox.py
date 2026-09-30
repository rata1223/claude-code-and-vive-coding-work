"""The legacy ``strategy.ScriptStrategy`` runs user code under RestrictedPython.

``POST /api/strategies/backtest`` executes a user's saved ``script_code``
through ``strategy.script_strategy.ScriptStrategy``. It used to run that code
with a plain ``exec`` — a builtins dict, ``pandas`` and the real ``math`` /
``statistics`` modules, and no guard on attribute access. These tests pin the
restricted environment that replaced it, one guard at a time, and check that
the bundled templates still work (two of them never ran before: every bar
failed with ``__import__ not found`` or ``_bollinger is not defined``).

No network, no broker.
"""
import math
from types import ModuleType

import pytest

from strategy.script_strategy import (
    SCRIPT_TEMPLATES,
    Bar,
    Order,
    ScriptStrategy,
    _restricted_globals,
)


def _compiles(code: str) -> bool:
    return ScriptStrategy(code=code).compile()


def _bar(i: int, px: float) -> Bar:
    return Bar(symbol="AAPL", timestamp=i, open=px, high=px, low=px, close=px,
               volume=1000)


class TestTheEnvironment:
    def test_attribute_reads_are_guarded(self):
        from RestrictedPython.Guards import safer_getattr
        env = _restricted_globals()
        assert env["_getattr_"] is safer_getattr
        assert "_write_" in env and "_getiter_" in env

    def test_no_pandas_and_no_type(self):
        env = _restricted_globals()
        assert "pd" not in env
        assert "type" not in env["__builtins__"]

    def test_math_and_statistics_are_vetted_namespaces_not_modules(self):
        env = _restricted_globals()
        for name in ("math", "statistics"):
            assert not isinstance(env[name], ModuleType)
        assert not hasattr(env["statistics"], "sys")
        assert env["statistics"].mean([1, 2, 3]) == 2


class TestWhatAScriptCannotDo:
    @pytest.mark.parametrize("code", [
        "x = ().__class__",                 # dunder attribute
        "x = [1]._private",                 # underscore attribute
        "_hidden = 1",                      # underscore name
        "import os",                        # any module but the vetted two
        "from statistics import mean",      # from-imports
        "x = pd",                           # pandas is gone
    ])
    def test_rejected(self, code):
        assert _compiles(code) is False

    def test_writes_do_not_reach_objects_passed_in(self):
        st = ScriptStrategy(code="def on_bar(bar, ctx):\n    bar.close = 0\n")
        assert st.compile()
        bar = _bar(0, 100.0)
        assert st.on_bar(bar) is None          # the error is caught and logged
        assert bar.close == 100.0


class TestWhatAScriptCanStillDo:
    def test_context_state_arithmetic_and_vetted_imports(self):
        st = ScriptStrategy(code=(
            "import statistics\n"
            "def on_start(ctx):\n"
            "    ctx['state']['n'] = 0\n"
            "def on_bar(bar, ctx):\n"
            "    n = ctx['state']['n']\n"
            "    n += 1\n"                      # item += is refused by RestrictedPython
            "    ctx['state']['n'] = n\n"
            "    a, b = bar.close, math.sqrt(4)\n"
            "    ctx['state']['m'] = statistics.mean([a, b])\n"
        ))
        assert st.compile(), st.compile_error
        st.on_start()
        st.on_bar(_bar(0, 100.0))
        assert st._context["state"] == {"n": 1, "m": 51.0}

    @pytest.mark.parametrize("key", sorted(SCRIPT_TEMPLATES))
    def test_every_bundled_template_runs(self, key, caplog):
        t = SCRIPT_TEMPLATES[key]
        st = ScriptStrategy(code=t["code"], params=dict(t.get("params") or {}))
        assert st.compile(), st.compile_error
        st.on_start()
        actions = []
        for i in range(300):
            px = 100 + 15 * math.sin(i / 9) + i * 0.05
            sig = st.on_bar(_bar(i, px))
            if sig:
                actions.append(sig.action)
                st.on_order_filled(Order(id=str(i), symbol="AAPL",
                                         side=sig.action, qty=1, price=px))
        assert not [r for r in caplog.records if "실행 오류" in r.getMessage()]
        if key in ("ma_crossover", "mean_reversion"):
            assert actions, f"{key} produced no signal on an oscillating series"
