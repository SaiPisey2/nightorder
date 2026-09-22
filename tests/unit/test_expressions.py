"""The expression resolver evaluates a node allowlist, not eval()."""
import pytest

from nightorder.spec.expressions import (
    MAX_EXPRESSION_CHARS,
    MAX_POW_EXPONENT,
    MAX_REPEAT,
    ExpressionError,
    evaluate,
)


# --- what specs are allowed to do -----------------------------------------

def test_the_expression_shipped_in_the_hello_world_example():
    assert evaluate("params['greeting'] + '!'", {"greeting": "hello"}) == "hello!"


@pytest.mark.parametrize(
    "expression, params, expected",
    [
        ("1 + 2 * 3", {}, 7),
        ("(1 + 2) * 3", {}, 9),
        ("10 / 4", {}, 2.5),
        ("10 // 4", {}, 2),
        ("10 % 4", {}, 2),
        ("2 ** 8", {}, 256),
        ("-5", {}, -5),
        ("not 0", {}, True),
        ("params['a'] > params['b']", {"a": 2, "b": 1}, True),
        ("1 < params['a'] < 10", {"a": 5}, True),
        ("1 < params['a'] < 3", {"a": 5}, False),
        ("'x' in params['items']", {"items": ["x", "y"]}, True),
        ("params['a'] if params['flag'] else params['b']", {"a": 1, "b": 2, "flag": True}, 1),
        ("params['a'] and params['b']", {"a": 1, "b": 2}, 2),
        ("params['a'] or params['b']", {"a": 0, "b": 2}, 2),
        ("[1, 2, 3]", {}, [1, 2, 3]),
        ("{'k': params['v']}", {"v": 9}, {"k": 9}),
        ("len(params['items'])", {"items": [1, 2, 3]}, 3),
        ("str(params['n']) + 'x'", {"n": 7}, "7x"),
        ("int('42')", {}, 42),
        ("max(1, params['a'])", {"a": 5}, 5),
        ("sorted([3, 1, 2])", {}, [1, 2, 3]),
        ("sum([1, 2, 3])", {}, 6),
        ("round(2.567, 1)", {}, 2.6),
        ("params['nested']['inner']", {"nested": {"inner": "v"}}, "v"),
        ("params['items'][1]", {"items": ["a", "b"]}, "b"),
    ],
)
def test_allowed_expressions(expression, params, expected):
    assert evaluate(expression, params) == expected


# --- what is refused -------------------------------------------------------

@pytest.mark.parametrize(
    "expression",
    [
        "''.join(['a'])",            # attribute call on a literal
        "params.keys()",             # attribute call on params
        "params['s'].upper()",       # attribute call on a resolved value
        # Bare attribute reads: these reach the attribute guard directly. The
        # three cases above are refused by the call guard first, so without
        # these the attribute guard is not actually pinned by any test.
        "''.join",                   # attribute read on a literal
        "params.keys",               # attribute read on params
        "params['s'].upper",         # attribute read on a resolved value
        "(1).real",                  # attribute read on an int literal
        "params['s'][0].join",       # attribute read after a subscript
        "__import__('os')",          # function not on the allowlist
        "open('/etc/passwd')",       # ditto
        "print(1)",                  # ditto
        "lambda: 1",                 # lambda
        "[x for x in [1, 2]]",       # comprehension
        "{k: 1 for k in [1]}",       # dict comprehension
        "(y := 2)",                  # walrus
        "f\"{params['a']}\"",        # f-string
        "max(*[1, 2])",              # starred argument
        "min(1, 2, key=None)",       # keyword argument
        "os",                        # unknown bare name
        "1 if False else os",        # unknown name in the branch actually taken
    ],
)
def test_refused_constructs(expression):
    with pytest.raises(ExpressionError):
        evaluate(expression, {"a": 1, "s": "x"})


def test_refusal_names_the_construct():
    with pytest.raises(ExpressionError) as e:
        evaluate("params.items", {})
    assert "Attribute" in str(e.value)


def test_a_branch_that_is_not_taken_is_not_evaluated():
    """Matches Python: the untaken branch of a ternary is never reached, so an
    unknown name there is not an error. Pinned so the behaviour is deliberate."""
    assert evaluate("1 if True else os", {}) == 1


def test_unknown_name_is_reported_clearly():
    with pytest.raises(ExpressionError) as e:
        evaluate("something", {})
    assert "only 'params' is available" in str(e.value)


def test_unknown_function_lists_what_is_available():
    with pytest.raises(ExpressionError) as e:
        evaluate("frobnicate(1)", {})
    assert "not allowed" in str(e.value) and "len" in str(e.value)


# --- resource bounds -------------------------------------------------------

def test_pow_exponent_is_bounded():
    with pytest.raises(ExpressionError):
        evaluate(f"2 ** {MAX_POW_EXPONENT + 1}", {})


def test_string_repeat_is_bounded():
    with pytest.raises(ExpressionError):
        evaluate(f"'a' * {MAX_REPEAT + 1}", {})


def test_list_repeat_is_bounded_in_either_operand_order():
    with pytest.raises(ExpressionError):
        evaluate(f"{MAX_REPEAT + 1} * [0]", {})


def test_expression_length_is_bounded():
    with pytest.raises(ExpressionError):
        evaluate("1 + " * MAX_EXPRESSION_CHARS + "1", {})


# --- error handling --------------------------------------------------------

def test_syntax_error_is_an_expression_error():
    with pytest.raises(ExpressionError):
        evaluate("1 +", {})


def test_missing_param_is_an_expression_error_not_a_keyerror():
    with pytest.raises(ExpressionError):
        evaluate("params['absent']", {})


def test_division_by_zero_is_an_expression_error():
    with pytest.raises(ExpressionError):
        evaluate("1 / 0", {})


def test_non_string_expression_is_refused():
    with pytest.raises(ExpressionError):
        evaluate(123, {})
