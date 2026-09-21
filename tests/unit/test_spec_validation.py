import pytest
import yaml

from bosun.contracts import load_registry
from bosun.spec.validate import SpecValidationError, validate_spec_dict

BASE = {
    "apiVersion": "bosun/v1",
    "kind": "Pipeline",
    "name": "t",
    "project": "p",
    "steps": [{"id": "a", "executor": "script", "config": {"command": "echo hi"}}],
}


def test_valid_minimal():
    spec = validate_spec_dict(dict(BASE))
    assert spec.name == "t"
    assert spec.steps[0].retry.maximum_attempts == 3


def test_unknown_dependency():
    bad = dict(BASE, steps=[{"id": "a", "depends_on": ["nope"]}])
    with pytest.raises(SpecValidationError, match="unknown step"):
        validate_spec_dict(bad)


def test_cycle_detected():
    bad = dict(BASE, steps=[
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},
    ])
    with pytest.raises(SpecValidationError, match="cycle"):
        validate_spec_dict(bad)


def test_duplicate_ids():
    bad = dict(BASE, steps=[{"id": "a"}, {"id": "a"}])
    with pytest.raises(SpecValidationError, match="duplicate"):
        validate_spec_dict(bad)


def test_unregistered_executor_caught_with_registry():
    bad = dict(BASE, steps=[{"id": "a", "executor": "not-a-real-backend"}])
    with pytest.raises(SpecValidationError, match="unregistered executor"):
        validate_spec_dict(bad, load_registry())


def test_fan_out_unknown_param():
    bad = dict(BASE, steps=[{"id": "a", "fan_out": {"over_param": "missing"}}])
    with pytest.raises(SpecValidationError, match="fans out over unknown parameter"):
        validate_spec_dict(bad)


def test_quota_pool_reference():
    bad = dict(
        BASE,
        parameters=[{"name": "xs", "value": ["1"]}],
        steps=[{"id": "a", "fan_out": {"over_param": "xs", "quota_pool": "nope"}}],
    )
    with pytest.raises(SpecValidationError, match="unknown quota pool"):
        validate_spec_dict(bad)


def test_example_specs_validate():
    registry = load_registry()
    for path in (
        "examples/hello_world/pipeline.yaml",
        "examples/rollup_mini/pipeline.yaml",
        "examples/rollup_mini/argo-smoke.yaml",
    ):
        with open(path) as f:
            validate_spec_dict(yaml.safe_load(f), registry)
