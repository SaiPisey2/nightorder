"""Entry-point based extension registry.

Teams register extensions in their package's pyproject.toml:

    [project.entry-points."bosun.step_executors"]
    my_backend = "my_pkg.executors:MyBackendExecutor"

    [project.entry-points."bosun.preflight_checks"]
    my_check = "my_pkg.checks:MyCheck"

`pip install my-pkg` into the worker image is the whole registration story.
A bad extension cannot corrupt kernel state: it is validated at load time
(must subclass the contract ABC), executed inside Temporal Activities with
timeouts, and its failures surface as step/check failures — never as
interpreter failures.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points

from bosun.contracts.base import GateChannel, ParameterResolver, PreflightCheck, StepExecutor

log = logging.getLogger(__name__)

GROUPS = {
    "step_executors": ("bosun.step_executors", StepExecutor),
    "preflight_checks": ("bosun.preflight_checks", PreflightCheck),
    "param_resolvers": ("bosun.param_resolvers", ParameterResolver),
    "gate_channels": ("bosun.gate_channels", GateChannel),
}


@dataclass
class Registry:
    step_executors: dict[str, StepExecutor] = field(default_factory=dict)
    preflight_checks: dict[str, PreflightCheck] = field(default_factory=dict)
    param_resolvers: dict[str, ParameterResolver] = field(default_factory=dict)
    gate_channels: dict[str, GateChannel] = field(default_factory=dict)

    def catalog(self) -> dict[str, list[str]]:
        return {kind: sorted(getattr(self, kind).keys()) for kind in GROUPS}


_registry: Registry | None = None


def load_registry(force: bool = False) -> Registry:
    """Load all registered extensions from entry points (cached)."""
    global _registry
    if _registry is not None and not force:
        return _registry
    reg = Registry()
    for kind, (group, base_cls) in GROUPS.items():
        target: dict = getattr(reg, kind)
        for ep in entry_points(group=group):
            try:
                cls = ep.load()
                if not (isinstance(cls, type) and issubclass(cls, base_cls)):
                    log.error("extension %s (%s) does not implement %s — skipped", ep.name, group, base_cls.__name__)
                    continue
                target[ep.name] = cls()
            except Exception:
                log.exception("failed to load extension %s (%s) — skipped", ep.name, group)
    _registry = reg
    return reg
