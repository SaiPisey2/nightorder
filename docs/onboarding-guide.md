# Onboarding Guide — from runbook to running pipeline

Audience: a team with an operational runbook (manual steps, argo submits,
sign-offs) that wants it on the platform. No platform-team involvement needed.

## 0. Concepts in one minute

- **Pipeline spec** — YAML describing steps, dependencies, parameters, gates,
  checks, fan-out. Registered + versioned via API.
- **Extensions** — your Python package implementing any custom executors /
  checks / resolvers, registered via entry points. Only needed when builtins
  don't cover you.
- **Project** — your isolation boundary: API key, specs, runs, gates, events.

## 1. Translate the runbook

| Runbook pattern | Spec construct |
|---|---|
| "Run step X after Y and Z finish" | `depends_on: [y, z]` |
| "argo submit foo.yaml -p yearmonth=..." | `executor: argo` + `config.manifest_file` (YAML in the worker's `manifests/` dir — recommended) / inline `manifest` / `workflow_template_ref` (installed on cluster) + `parameters` |
| "First check the feed file landed" | `preflight: [{check: ..., on_fail: block}]` |
| "Get QA sign-off before releasing" | `gate: {type: sign_off, ...}` |
| "If cost > budget, get approval" | resolver computes cost → `gate: {type: budget_approval}` |
| "Run per locale, US first, max 4" | `fan_out: {over_param: locales, tiers: [...], max_concurrent: 4}` |
| "Run it again after the queue drains" | `repeat: {until_check: ..., max_iterations: N}` |
| "SSH to the box and run the script" | `executor: remote_exec` |
| "Update the spreadsheet by hand" | `executor: manual` + instructions (tracked, timed, gated) |
| "Kick off on the 8th monthly" | `schedule: {cron: "0 6 8 * *"}` + schedule endpoint |
| Python glue between steps | `executor: script`, or your own executor extension |

Rule from P2: if your Argo YAML says "comment out X before submitting",
convert that to a parameterized `when:` condition during onboarding.

## 2. Start from the example

Copy `examples/hello_world/` — it is a complete consuming-team package:

```
your_project/
  pyproject.toml        # entry-points = your extension registration
  src/your_ext/         # implements nightorder.contracts ABCs
  pipeline.yaml         # your spec
```

Contracts you can implement (from `nightorder.contracts`):
`StepExecutor.execute(ctx, config)`, `PreflightCheck.run(ctx, params)`,
`ParameterResolver.resolve(ctx, params)`, `GateChannel.deliver(notification, params)`.
Each gets a `StepContext` (project, run, step, unit, attempt, idempotency_key,
resolved params). Executors must be idempotent on `ctx.idempotency_key`.

Install your package next to the kernel in the worker environment
(`uv add --editable ./your_project` or bake into the worker image). Restart
the worker; `GET /catalog` must now list your extensions.

## 3. Validate before you register

```bash
curl -X POST $API/projects/<you>/specs/validate -H "X-API-Key: $KEY" \
  -H 'content-type: application/yaml' --data-binary @pipeline.yaml
```

Lint catches: unknown deps, cycles, duplicate ids, unregistered
executors/checks/resolvers/channels, unknown fan-out params and quota pools.

## 4. Register, run, observe

```bash
curl -X POST $API/projects/<you>/specs        ...        # versioned; v1, v2, ...
curl -X POST $API/projects/<you>/pipelines/<name>/runs -d '{"params": {...}}'
curl $API/runs/<run_id>                                   # status, steps, timings, params+provenance, gates
curl $API/runs/<run_id>/events                            # full audit trail
curl $API/projects/<you>/pipelines/<name>/timings         # cross-run timing history
```

Gates: pending list at `GET /projects/<you>/gates`; resolve via
`POST /gates/<id>/resolve` or the signed one-click links delivered by your
gate channel.

## 5. Definition of done (mirror of the platform's Phase-1 exit criteria)

- Zero manual job submissions: every runbook step is a spec step.
- Not-yet-automatable steps are `manual` steps — state stays truthful.
- Every "check X before Y" is a preflight check, not tribal knowledge.
- Sign-offs/budget approvals are gates with audit, not chat messages.
- Timing table in your wiki is retired — the API records it.
