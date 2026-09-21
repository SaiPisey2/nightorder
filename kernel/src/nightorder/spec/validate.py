"""Spec validation/linting — fail loudly at registration, never at runtime.

Structural validation via pydantic, then semantic lints:
DAG integrity, unique step ids, referenced params/quotas exist, and (when a
registry is supplied) referenced executors/checks/resolvers/channels exist.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from nightorder.spec.model import PipelineSpec


class SpecValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _dag_errors(spec: PipelineSpec) -> list[str]:
    errors: list[str] = []
    ids = [s.id for s in spec.steps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"duplicate step ids: {sorted(dupes)}")
    id_set = set(ids)
    for s in spec.steps:
        for dep in s.depends_on:
            if dep not in id_set:
                errors.append(f"step '{s.id}' depends on unknown step '{dep}'")
    # cycle detection (Kahn)
    indeg = {i: 0 for i in id_set}
    for s in spec.steps:
        for dep in s.depends_on:
            if dep in indeg:
                indeg[s.id] += 1
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    adj: dict[str, list[str]] = {i: [] for i in id_set}
    for s in spec.steps:
        for dep in s.depends_on:
            if dep in adj:
                adj[dep].append(s.id)
    while queue:
        n = queue.pop()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if seen != len(id_set):
        errors.append("dependency graph contains a cycle")
    return errors


def _reference_errors(spec: PipelineSpec, registry: Any | None) -> list[str]:
    errors: list[str] = []
    param_names = {p.name for p in spec.parameters}
    quota_names = {q.name for q in spec.quotas}
    for s in spec.steps:
        if s.fan_out and s.fan_out.over_param not in param_names:
            errors.append(f"step '{s.id}' fans out over unknown parameter '{s.fan_out.over_param}'")
        if s.fan_out and s.fan_out.quota_pool and s.fan_out.quota_pool not in quota_names:
            errors.append(f"step '{s.id}' references unknown quota pool '{s.fan_out.quota_pool}'")
    for p in spec.parameters:
        if p.resolver == "activity" and registry is not None:
            if p.activity not in registry.param_resolvers:
                errors.append(f"parameter '{p.name}' uses unregistered resolver '{p.activity}'")
    if registry is not None:
        for s in spec.steps:
            if s.executor not in ("manual",) and s.executor not in registry.step_executors:
                errors.append(f"step '{s.id}' uses unregistered executor '{s.executor}'")
            for c in s.preflight:
                if c.check not in registry.preflight_checks:
                    errors.append(f"step '{s.id}' preflight uses unregistered check '{c.check}'")
            if s.repeat and s.repeat.until_check not in registry.preflight_checks:
                errors.append(f"step '{s.id}' repeat uses unregistered check '{s.repeat.until_check}'")
            if s.gate and s.gate.channel not in registry.gate_channels:
                errors.append(f"step '{s.id}' gate uses unregistered channel '{s.gate.channel}'")
    return errors


def validate_spec_dict(raw: dict, registry: Any | None = None) -> PipelineSpec:
    """Parse + lint a raw spec dict. Raises SpecValidationError with all findings."""
    try:
        spec = PipelineSpec.model_validate(raw)
    except ValidationError as e:
        raise SpecValidationError([f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]) from e
    errors = _dag_errors(spec) + _reference_errors(spec, registry)
    if errors:
        raise SpecValidationError(errors)
    return spec
