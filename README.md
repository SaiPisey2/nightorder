# nightorder

Long-running operational pipelines, written as declarative specs, interpreted by one
generic Temporal workflow — with human sign-off gates the run genuinely waits on.

The name is the captain's written order for the night watch: proceed on this course,
and wake me if X. That is the shape of the thing. A spec runs unattended until it
reaches a decision a person has to make, and then it stops and asks.

![demo](docs/demo.gif)

## Why

Runbooks rot because the knowledge lives in a wiki page and the execution lives in
someone's shell history. Moving them into a workflow engine usually means writing
orchestration code per pipeline, and the human approval step becomes a Slack message
that nothing actually enforces.

Here a pipeline is a YAML spec. One interpreter workflow runs any spec. A gate is a
first-class step: nothing past it executes until a human resolves it, and the approval
link is a signed, single-use token minted by the kernel.

## Quick start

```bash
uv sync
make infra                    # postgres :5433, qdrant :6333, temporal dev server
cp .env.example .env          # then edit it

make worker                   # terminal 1
make api                      # terminal 2 — http://localhost:8400, UI at /ui/
./scripts/demo.sh             # terminal 3 — the walkthrough in the GIF above
```

## A spec

```yaml
apiVersion: nightorder/v1
kind: Pipeline
name: monthly-rollup
project: batch

parameters:
  - name: yearmonth
    resolver: activity
    activity: now_yearmonth
  - name: cutoff
    resolver: activity          # newest partition actually present
    activity: command
    params: {command: ["sh", "-c", "gsutil ls gs://bucket/ | tail -1"]}

steps:
  - id: aggregate
    executor: argo
    preflight:
      - check: command_succeeds  # blocks kickoff if the input is missing
        params: {command: ["sh", "-c", "gsutil ls gs://bucket/day={{params.cutoff}}/"]}
        on_fail: block
    gate:
      type: sign_off
      prompt: "Submit the rollup for {{params.yearmonth}}? cutoff={{params.cutoff}}"
    config:
      manifest_file: batch/monthly_rollup.yaml
```

Every resolved parameter records where its value came from. Preflight runs before the
gate, so the person approving sees inputs that have already been checked.

`examples/hello_world/` is the full contract test — parameters, preflight, a custom
executor, fan-out with tiers and a quota pool, a gate, and a script step.

## What it does

- **Specs, not code.** Validate, register, version. Teams onboard pipelines without
  touching the kernel.
- **Gates.** Sign-off and approval steps with signed single-use action links, timeouts,
  and a configurable delivery channel (log, webhook, Teams).
- **Fan-out.** Over any list parameter, in tiers, under a shared concurrency quota.
- **Preflight.** Checks that block a run before it burns hours on missing input.
- **Audit.** Every parameter, check, step and decision is an event in Postgres, written
  through a transactional outbox.
- **Advisory (optional).** When a step fails, it assembles an incident bundle, retrieves
  similar past incidents from Qdrant, and runs a LangGraph supervisor to propose a root
  cause and a remediation. It proposes; it never decides.
- **Sandboxed remediation (optional).** Only pre-approved versioned playbooks — fixed
  image, fixed command — run, in a hardened ephemeral Kubernetes Job. Free-form model
  output can never execute.

## Extending it

Four entry-point groups: `step_executors`, `preflight_checks`, `param_resolvers`,
`gate_channels`. Ship a pip-installable package, register against the contract, and the
worker loads it. `examples/hello_world/` is a complete consuming package.

## Security

The control plane is **open by default** — `NIGHTORDER_AUTH=off` means any caller that
can reach it can register specs, start runs and resolve gates. Both processes say so on
boot. It binds `127.0.0.1` unless you set `NIGHTORDER_API_HOST`.

Turning auth on requires a real `NIGHTORDER_GATE_TOKEN_SECRET`: the built-in development
value is in this source, so the process refuses to start with it under
`NIGHTORDER_AUTH=on`.

Spec authors are trusted — a `script` step runs commands. Expressions are narrower:
they are parsed and walked against an allowlist rather than evaluated, so a spec cannot
reach the worker's runtime through one.

Captured output is redacted before it is stored, displayed, or sent to the advisory
model. It is a safety net, not a guarantee.

## Tests

```bash
make test-unit   # 170 tests, no services needed
make test-e2e    # boots worker, api and relay against make infra
```

The e2e suite collects 23 tests. Thirteen run with no credentials and cover the
deterministic backbone. Eight further tests
need `ANTHROPIC_API_KEY` for the advisory half; two more are opt-in against a real
cluster (`NIGHTORDER_E2E_ARGO=1`, `NIGHTORDER_E2E_SANDBOX=1`) and submit real workloads
to whatever context is current — set `NIGHTORDER_KUBE_CONTEXT` deliberately.

## Layout

```
kernel/src/nightorder/   spec schema, interpreter workflow, gates, API, builtins
  spec/                  schema, validation, expression evaluation
  temporal/              the one generic interpreter workflow and its activities
  api/                   FastAPI control plane, signed gate tokens
  ai/                    incident bundle, log reduction, knowledge, agents
ui/                      React SPA served at /ui
examples/                hello_world (contract test), rollup_mini, batch
manifests/               Argo workflows resolved by manifest_file:
k8s/sandbox/             sandbox service account and default-deny NetworkPolicy
```

Configuration is environment only — see `.env.example`. Design rationale is in
`docs/architecture.md`; onboarding a team is `docs/onboarding-guide.md`.
