import pytest

from bosun.builtins.executors import ScriptExecutor, _render, redact
from bosun.builtins.checks import CommandSucceedsCheck, FileExistsCheck
from bosun.builtins.resolvers import CommandResolver, NowYearMonthResolver
from bosun.contracts import StepContext


def ctx(**params) -> StepContext:
    return StepContext(project="p", pipeline="pl", run_id="r", step_id="s",
                       unit=params.pop("unit", None), attempt=1, idempotency_key="k", params=params)


def test_render_params_and_item():
    c = ctx(yearmonth="202607", unit="us_en")
    assert _render("run {{params.yearmonth}} for {{item}}", c) == "run 202607 for us_en"
    assert _render(["{{item}}", {"a": "{{params.yearmonth}}"}], c) == ["us_en", {"a": "202607"}]


def test_redact_secrets():
    out = redact("api_key=sk-ant-abc12345678 password: hunter2secret ok=fine")
    assert "sk-ant-abc12345678" not in out
    assert "hunter2secret" not in out


async def test_script_executor_success():
    result = await ScriptExecutor().execute(ctx(), {"command": ["echo", "hi"]})
    assert result.status == "succeeded"
    assert "hi" in result.logs


async def test_script_executor_failure():
    result = await ScriptExecutor().execute(ctx(), {"command": ["sh", "-c", "exit 3"]})
    assert result.status == "failed"
    assert result.output["exit_code"] == 3


async def test_file_exists_check(tmp_path):
    f = tmp_path / "x"
    f.write_text("1")
    assert (await FileExistsCheck().run(ctx(), {"path": str(f)})).passed
    assert not (await FileExistsCheck().run(ctx(), {"path": str(f) + "-missing"})).passed


async def test_command_check_evidence():
    r = await CommandSucceedsCheck().run(ctx(), {"command": ["sh", "-c", "echo depth=0; true"]})
    assert r.passed and "depth=0" in r.evidence


async def test_yearmonth_resolver():
    r = await NowYearMonthResolver().resolve(ctx(), {})
    assert len(r.value) == 6 and r.value.isdigit()
    prev = await NowYearMonthResolver().resolve(ctx(), {"offset_months": -1})
    assert prev.value != r.value


async def test_command_resolver():
    r = await CommandResolver().resolve(ctx(), {"command": "echo 31000"})
    assert r.value == "31000"
    assert "command:" in r.provenance


async def test_command_resolver_failure_raises():
    with pytest.raises(RuntimeError, match="resolver command failed"):
        await CommandResolver().resolve(ctx(), {"command": ["sh", "-c", "exit 1"]})
