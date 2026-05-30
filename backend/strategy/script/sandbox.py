"""
ScriptStrategy 샌드박스.
AST 화이트리스트 검사 + 스레드 타임아웃으로 위험 코드 차단.

signal.alarm/SIGALRM은 사용하지 않는다 — daemon 스레드에서 호출하면
SIGALRM이 메인 스레드로 전달되어 Worker 프로세스 전체가 크래시된다.
대신 daemon=True 스레드 + join(timeout)으로 실행 시간을 제한한다.
스레드는 완전히 종료되지 않을 수 있으나 메인 프로세스는 보호된다.
"""
import ast
import logging
import textwrap
import threading
from typing import Any

from RestrictedPython import compile_restricted, safe_builtins
from RestrictedPython import safe_globals as _rp_safe_globals

logger = logging.getLogger(__name__)

# 허용된 AST 노드 유형
_ALLOWED_NODES = {
    # 모듈/표현식
    ast.Module, ast.Expr, ast.Interactive,
    # 리터럴
    ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.comprehension,
    # 연산
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    # 이름/속성/인덱스
    ast.Name, ast.Attribute, ast.Subscript, ast.Slice,
    ast.Load, ast.Store, ast.Del,
    # 흐름 제어
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign,
    # 함수/클래스
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.arguments, ast.arg, ast.keyword, ast.Call,
    # 기타
    ast.IfExp, ast.NamedExpr, ast.Starred, ast.FormattedValue, ast.JoinedStr,
    ast.With, ast.withitem, ast.AsyncWith,
    ast.Try, ast.ExceptHandler, ast.Raise,
    ast.Global, ast.Nonlocal,
}
# Python 3.8 이하 호환 (ast.Index는 3.9+에서 제거)
if hasattr(ast, "Index"):
    _ALLOWED_NODES.add(ast.Index)
# Python 3.11+ TryStar 지원
if hasattr(ast, "TryStar"):
    _ALLOWED_NODES.add(ast.TryStar)

# 완전 금지 AST 노드 (import, exec, 파일 I/O 등)
_FORBIDDEN_NODES = {
    ast.Import, ast.ImportFrom,
    # exec/eval은 Call에서 이름 검사로 차단
}

# 금지 내장함수/이름
_FORBIDDEN_NAMES = {
    "exec", "eval", "compile", "open", "__import__", "breakpoint",
    "input", "memoryview", "__builtins__", "__loader__", "__spec__",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "object", "type", "super",
}

# 허용 내장함수
_ALLOWED_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter",
    "float", "format", "int", "isinstance", "len", "list", "map",
    "max", "min", "print", "range", "reversed", "round", "set",
    "sorted", "str", "sum", "tuple", "zip",
}


class SandboxViolation(Exception):
    pass


class _ASTChecker(ast.NodeVisitor):
    def generic_visit(self, node):
        node_type = type(node)
        if node_type in _FORBIDDEN_NODES:
            raise SandboxViolation(f"금지된 구문: {node_type.__name__}")
        if node_type not in _ALLOWED_NODES:
            raise SandboxViolation(f"허용되지 않은 구문: {node_type.__name__}")
        super().generic_visit(node)

    def visit_Name(self, node):
        if node.id in _FORBIDDEN_NAMES:
            raise SandboxViolation(f"금지된 이름: {node.id}")
        self.generic_visit(node)

    def visit_Call(self, node):
        # exec(), eval() 직접 호출 차단
        if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
            raise SandboxViolation(f"금지된 함수 호출: {node.func.id}")
        # __dunder__ 메서드 직접 호출 차단
        if isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith("__") and node.func.attr.endswith("__"):
                raise SandboxViolation(f"__dunder__ 호출 차단: {node.func.attr}")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        # Block any dunder attribute access — prevents subclass traversal attacks
        # e.g. ().__class__.__bases__[0].__subclasses__()
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise SandboxViolation(f"__dunder__ 속성 접근 차단: {node.attr}")
        # os.*, sys.*, subprocess.* 등 모듈 속성 접근 차단
        BLOCKED_MODULES = {"os", "sys", "subprocess", "socket", "shutil", "pathlib", "io"}
        if isinstance(node.value, ast.Name) and node.value.id in BLOCKED_MODULES:
            raise SandboxViolation(f"금지된 모듈 접근: {node.value.id}.{node.attr}")
        self.generic_visit(node)


def validate_script(source: str) -> None:
    """
    소스 코드 정적 검사. 위반 시 SandboxViolation 발생.
    실행 전 반드시 호출.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError as e:
        raise SandboxViolation(f"문법 오류: {e}")
    _ASTChecker().visit(tree)


def execute_script(
    source: str,
    context: dict[str, Any],
    timeout_sec: int = 5,
) -> dict[str, Any]:
    """
    검증된 스크립트를 제한된 환경에서 실행.
    context: 전략에서 사용할 변수 (self=strategy, bar=bar_data 등)
    반환: 실행 후 context 상태

    타임아웃은 daemon 스레드 + join(timeout)으로 구현한다.
    스레드가 종료되지 않으면 TimeoutError를 발생시키고 메인 스레드는 계속 진행한다.
    """
    validate_script(source)

    try:
        code = compile_restricted(textwrap.dedent(source), "<strategy>", "exec")
    except SyntaxError as e:
        raise SandboxViolation(f"RestrictedPython 컴파일 오류: {e}")
    if code is None:
        raise SandboxViolation("RestrictedPython compilation failed")

    # Build execution environment from RestrictedPython's safe_globals (provides _getattr_,
    # _getiter_, _write_, etc.) then restrict builtins further to our explicit allowlist.
    safe_env = dict(_rp_safe_globals)
    safe_env["__builtins__"] = {k: safe_builtins[k] for k in _ALLOWED_BUILTINS if k in safe_builtins}
    safe_env.update(context)

    result: dict[str, Any] = {"globals": None, "exc": None}

    def _run():
        try:
            exec(code, safe_env)
            result["globals"] = safe_env
        except Exception as e:
            result["exc"] = e

    t = threading.Thread(target=_run, daemon=True, name="sandbox-exec")
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        raise TimeoutError(f"스크립트 실행 시간 초과 ({timeout_sec}s)")
    if result["exc"] is not None:
        raise result["exc"]
    return result["globals"]
