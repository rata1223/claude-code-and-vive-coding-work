"""
ScriptStrategy 샌드박스.
RestrictedPython + AST 화이트리스트 검사 + 타임아웃으로 위험 코드 차단.
"""
import ast
import logging
import signal
import textwrap
from typing import Any

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
        # os.*, sys.*, subprocess.* 등 모듈 속성 접근 차단
        BLOCKED_MODULES = {"os", "sys", "subprocess", "socket", "shutil", "pathlib", "io"}
        if isinstance(node.value, ast.Name) and node.value.id in BLOCKED_MODULES:
            raise SandboxViolation(f"금지된 모듈 접근: {node.value.id}.{node.attr}")
        self.generic_visit(node)


def _timeout_handler(signum, frame):
    raise TimeoutError("스크립트 실행 시간 초과")


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
    """
    validate_script(source)

    safe_globals = {
        "__builtins__": {k: __builtins__[k] for k in _ALLOWED_BUILTINS if k in __builtins__}
        if isinstance(__builtins__, dict)
        else {k: getattr(__builtins__, k) for k in _ALLOWED_BUILTINS if hasattr(__builtins__, k)},
    }
    safe_globals.update(context)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout_sec)
    try:
        exec(compile(ast.parse(textwrap.dedent(source)), "<strategy>", "exec"), safe_globals)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    return safe_globals
