"""ScriptStrategy sandbox — RestrictedPython advisories and our own AST checker.

The sandbox runs user-written strategy code. Two layers guard it:
RestrictedPython (`compile_restricted` + guard hooks) and
``_ASTChecker`` in ``backend/strategy/script/sandbox.py``. These tests pin
both against the advisories that reached 8.0:

* **PYSEC-2026-3917** (patched 8.3): a positional-only argument named after a
  guard hook replaced that hook inside the function. Reachable here — the
  script below changed a context object's attribute on 8.0, although every
  attribute write is otherwise refused (the sandbox never defines ``_write_``).
* **CVE-2026-76825** (patched 8.4): ``string.Formatter`` field traversal. Not
  reachable — imports are rejected — and pinned so it stays that way.

No network, no broker.
"""
import pytest
from RestrictedPython import compile_restricted

from backend.strategy.script.sandbox import (
    SandboxViolation,
    execute_script,
    validate_script,
)


class _Target:
    x = 1


_SHADOW_WRITE = '''
def w(o):
    return o
def f(_write_=w, /):
    target.x = 99
f()
'''


class TestGuardShadowingThroughPositionalOnlyArguments:
    def test_a_script_cannot_replace_the_write_guard(self):
        target = _Target()
        with pytest.raises(SandboxViolation):
            execute_script(_SHADOW_WRITE, {"target": target})
        assert target.x == 1

    def test_restrictedpython_itself_rejects_it(self):
        """The pinned RestrictedPython must be a patched one, independent of
        our checker: 8.0 compiled this without complaint."""
        with pytest.raises(SyntaxError, match="_write_"):
            compile_restricted(_SHADOW_WRITE, "<strategy>", "exec")

    @pytest.mark.parametrize("signature", [
        "_getattr_=g, /", "_x, /", "_x", "*, _x=1", "*_x", "**_x",
    ])
    def test_our_checker_rejects_underscore_argument_names_of_every_kind(self, signature):
        with pytest.raises(SandboxViolation, match="인자 이름"):
            validate_script(f"def g(o, n):\n    return o\ndef f({signature}):\n    pass\n")


class TestStringFormatterIsUnreachable:
    @pytest.mark.parametrize("source", [
        "import string", "from string import Formatter", "s = string.Formatter()",
    ])
    def test_no_way_to_the_string_module(self, source):
        with pytest.raises((SandboxViolation, NameError)):
            execute_script(source, {})


class TestOrdinaryScriptsStillRun:
    def test_functions_with_plain_arguments_work(self):
        env = execute_script(
            "def score(a, b, /, c=1, *, d=2):\n    return a + b + c + d\n"
            "result = score(1, 2)\n", {})
        assert env["result"] == 6

    def test_attribute_writes_on_context_objects_stay_refused(self):
        target = _Target()
        with pytest.raises(NameError, match="_write_"):
            execute_script("target.x = 7", {"target": target})
        assert target.x == 1
