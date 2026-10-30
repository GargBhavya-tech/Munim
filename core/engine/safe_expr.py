"""A tiny, safe expression language for playbook done-checks, step conditions and preconditions.

The source is parsed with ``ast.parse`` (parsing never runs code) and then walked by OUR evaluator over a
whitelist of node types. Python's built-in evaluators are never used, so there is nothing to inject into.

Allowed: comparisons (== != < <= > >=), ``in`` / ``not in``, ``and`` / ``or`` / ``not``, string / number /
bool / None literals, list / tuple / set literals of those, and dotted names that appear in the caller's
whitelist of fields. Everything else (calls, attribute tricks, lambdas, comprehensions, f-strings, walrus,
subscripts, arithmetic...) fails at compile time.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

MAX_SOURCE_LEN = 500


class ExpressionError(ValueError):
    """The expression is not in the safe language."""


class UnknownFieldError(ExpressionError):
    """The expression names a field that is not in the whitelist."""

    def __init__(self, field: str):
        super().__init__(f"unknown field {field!r}")
        self.field = field


_CMP = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_LITERAL_TYPES = (str, int, float, bool, type(None))


def _dotted(node: ast.AST) -> str | None:
    """'a.b.c' for a pure Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


@dataclass(frozen=True)
class Expr:
    source: str
    fields: frozenset[str]
    _tree: ast.AST

    def evaluate(self, namespace: dict) -> Any:
        """Evaluate over a namespace keyed by full dotted name. A field missing from the namespace is None;
        a comparison that cannot be made (None >= 1) is False, never an exception."""
        try:
            return _ev(self._tree, namespace)
        except TypeError:
            return False

    def __bool__(self) -> bool:  # an Expr is truthy; call .evaluate to get the answer
        return True


def _ev(node: ast.AST, ns: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _ev(node.body, ns)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ns.get(_dotted(node))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_ev(e, ns) for e in node.elts]
        return frozenset(items) if isinstance(node, ast.Set) else items
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _ev(v, ns)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = _ev(v, ns)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):  # only Not is admitted at compile time
        return not _ev(node.operand, ns)
    if isinstance(node, ast.Compare):
        left = _ev(node.left, ns)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, ns)
            try:
                ok = _CMP[type(op)](left, right)
            except TypeError:
                return False
            if not ok:
                return False
            left = right
        return True
    raise ExpressionError(f"cannot evaluate {type(node).__name__}")  # unreachable after compile


def _check(node: ast.AST, allowed: set[str], used: set[str], depth: int = 0) -> None:
    if depth > 30:
        raise ExpressionError("expression is nested too deeply")
    d = depth + 1
    if isinstance(node, ast.Expression):
        _check(node.body, allowed, used, d)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, _LITERAL_TYPES):
            raise ExpressionError(f"literal of type {type(node.value).__name__} is not allowed")
    elif isinstance(node, (ast.Name, ast.Attribute)):
        name = _dotted(node)
        if name is None:
            raise ExpressionError("only plain dotted names are allowed, not attribute access on other expressions")
        if any(part.startswith("_") for part in name.split(".")):
            raise ExpressionError(f"names starting with an underscore are not allowed: {name!r}")
        if name not in allowed:
            raise UnknownFieldError(name)
        used.add(name)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for e in node.elts:
            if not isinstance(e, ast.Constant):
                raise ExpressionError("list / tuple / set literals may contain only constants")
            _check(e, allowed, used, d)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            _check(v, allowed, used, d)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            raise ExpressionError("only 'not' is allowed as a unary operator")
        _check(node.operand, allowed, used, d)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMP:
                raise ExpressionError(f"operator {type(op).__name__} is not allowed")
        _check(node.left, allowed, used, d)
        for c in node.comparators:
            _check(c, allowed, used, d)
    else:
        raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")


def compile_expr(src: str, allowed_fields: set[str] | frozenset[str]) -> Expr:
    """Parse and validate ``src``. Raises ExpressionError (UnknownFieldError for a non-whitelisted name)."""
    if not isinstance(src, str) or not src.strip():
        raise ExpressionError("expression must be a non-empty string")
    if len(src) > MAX_SOURCE_LEN:
        raise ExpressionError("expression is too long")
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        raise ExpressionError(f"cannot parse expression: {e}") from None
    used: set[str] = set()
    try:
        _check(tree, set(allowed_fields), used)
    except RecursionError:
        raise ExpressionError("expression is nested too deeply") from None
    return Expr(src, frozenset(used), tree)
