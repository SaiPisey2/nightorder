"""Evaluation of `expression` parameters.

Pipeline specs may compute a parameter from previously resolved ones, e.g.
``params['greeting'] + '!'``. This module evaluates that expression.

It does not call ``eval``. Removing ``__builtins__`` from an ``eval`` namespace
does not confine the expression: attribute access on any literal reaches the
type graph, and from there the import machinery, so a spec author would have
had the worker's full authority. Instead the expression is parsed and walked
node by node, and anything not on the allowlist is refused. Attribute access is
refused outright, which is what removes that traversal.

Specs are still written by trusted project members, exactly like ``script``
steps. This narrows what a mistake — or a spec registered against an
unauthenticated control plane — can reach.
"""
from __future__ import annotations

import ast
import operator
from typing import Any

MAX_EXPRESSION_CHARS = 2_000
MAX_REPEAT = 10_000  # caps 'a' * n and [x] * n so an expression cannot exhaust memory
MAX_POW_EXPONENT = 1_000


class ExpressionError(Exception):
    """Raised when an expression is malformed or uses a refused construct."""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

# Callables a spec may use. Every one is a value transform; none can open a
# file, import a module, or reach an object's type.
_FUNCTIONS = {
    "abs": abs,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "round": round,
    "sorted": sorted,
    "str": str,
    "sum": sum,
}


def evaluate(expression: str, params: dict[str, Any]) -> Any:
    """Evaluate `expression` against `params`. Raises ExpressionError if refused."""
    if not isinstance(expression, str):
        raise ExpressionError("expression must be a string")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ExpressionError(f"expression exceeds {MAX_EXPRESSION_CHARS} characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"could not parse expression: {e}") from e
    return _eval(tree.body, params)


def _refuse(node: ast.AST) -> None:
    raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")


def _eval(node: ast.AST, params: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id != "params":
            raise ExpressionError(f"unknown name '{node.id}': only 'params' is available")
        return params

    if isinstance(node, ast.Subscript):
        target = _eval(node.value, params)
        key = _eval(node.slice, params)
        try:
            return target[key]
        except (KeyError, IndexError, TypeError) as e:
            raise ExpressionError(f"subscript failed: {e}") from e

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            _refuse(node.op)
        left, right = _eval(node.left, params), _eval(node.right, params)
        _check_binop_bounds(type(node.op), left, right)
        try:
            return op(left, right)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            raise ExpressionError(f"operation failed: {e}") from e

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            _refuse(node.op)
        return op(_eval(node.operand, params))

    if isinstance(node, ast.BoolOp):
        values = [_eval(v, params) for v in node.values]
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in values:
                if not v:
                    return v
                result = v
            return result
        if isinstance(node.op, ast.Or):
            for v in values:
                if v:
                    return v
            return values[-1] if values else False
        _refuse(node.op)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, params)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _COMPARE_OPS.get(type(op_node))
            if op is None:
                _refuse(op_node)
            right = _eval(comparator, params)
            try:
                if not op(left, right):
                    return False
            except TypeError as e:
                raise ExpressionError(f"comparison failed: {e}") from e
            left = right
        return True

    if isinstance(node, ast.IfExp):
        return _eval(node.body, params) if _eval(node.test, params) else _eval(node.orelse, params)

    if isinstance(node, ast.List):
        return [_eval(e, params) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, params) for e in node.elts)
    if isinstance(node, ast.Set):
        return {_eval(e, params) for e in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _eval(k, params) if k is not None else None: _eval(v, params)
            for k, v in zip(node.keys, node.values)
        }

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only direct calls to allowed functions are permitted")
        fn = _FUNCTIONS.get(node.func.id)
        if fn is None:
            raise ExpressionError(
                f"function '{node.func.id}' is not allowed; available: "
                + ", ".join(sorted(_FUNCTIONS))
            )
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed in an expression")
        args = [_eval(a, params) for a in node.args]
        try:
            return fn(*args)
        except (TypeError, ValueError) as e:
            raise ExpressionError(f"{node.func.id}() failed: {e}") from e

    # Attribute access is the traversal an escape depends on, so it is refused
    # here rather than filtered. Comprehensions, lambdas, walrus, f-strings,
    # starred args and everything else fall through to the same refusal.
    _refuse(node)


def _check_binop_bounds(op_type: type, left: Any, right: Any) -> None:
    """Refuse operations whose result size is attacker-chosen."""
    if op_type is ast.Pow and isinstance(right, int) and right > MAX_POW_EXPONENT:
        raise ExpressionError(f"exponent exceeds {MAX_POW_EXPONENT}")
    if op_type is ast.Mult:
        for a, b in ((left, right), (right, left)):
            if isinstance(a, (str, bytes, list, tuple)) and isinstance(b, int) and b > MAX_REPEAT:
                raise ExpressionError(f"repeat count exceeds {MAX_REPEAT}")
