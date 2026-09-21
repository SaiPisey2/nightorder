# Bosun — Enterprise Workflow Orchestration Platform

A general-purpose, framework-style orchestration platform: long-running operational
pipelines defined as **declarative specs**, interpreted by **one generic Temporal
workflow**, with human gates, preflight checks, computed parameters, fan-out,
quotas, designed re-runs — and a project-scoped extension model so other teams
onboard their own pipelines **without platform code changes**.

All phases implemented and tested end to end:
**Phase 1** deterministic backbone · **Phase 2** read-only AI advisory
(incident bundles, log reduction, Qdrant memory, LangGraph agents) ·
**Phase 2.5** learning + retrieval metric · **Phase 3** sandboxed remediation
execution (pre-approved playbooks only).

## Architecture at a glance

```
FastAPI (control plane)  →  Temporal (sole orchestration authority)
                                 │
             ┌───────────────────┼─────────────────────┐
        Argo Workflows      RemoteExec (SSH)      Script / custom
        (leaf K8s jobs)     (non-K8s hosts)       executors (extensions)
                                 │
                     PostgreSQL (system of record + transactional outbox)
```

- **Kernel** (`kernel/src/bosun/`): spec schema + validation, generic interpreter
  workflow, gate mechanics + signed action tokens, event emission (outbox), audit,
  extension registry, FastAPI.
- **Extensions** (any pip-installable package): step executors, preflight checks,
  parameter resolvers, gate channels — registered via Python entry points, loaded
  by contract. See `examples/hello_world/` for a complete consuming-team package.
- **Kafka deferred**: events land in Postgres `events` + `outbox` tables
  transactionally; the `bosun-relay` process consumes the outbox (indexes
  knowledge into Qdrant today; Kafka producer slots into the same relay later).
- **Phase 2 AI advisory** (`kernel/src/bosun/ai/`): failed step →
  **Incident Bundle** (executions, events, preflight history, params, reduced
  log, k8s events) → **Qdrant retrieval** of similar incidents → **LangGraph**
  supervisor routing Knowledge Retrieval → Troubleshooting (RCA with evidence)
  → Remediation Planning (propose only) → Human Interaction (drafts gate text)
  → remediation-approval gate. Read-only; never decides pass/fail (P6).
- **Phase 2.5 learning**: `POST /incidents/{id}/outcome` captures what fixed
  it → knowledge record → async Qdrant index; retrieval quality measured as
  hit@k / precision@1 against labeled ground truth (`bosun.ai.evaluate`).
- **Phase 3 execution**: pre-approved versioned **playbooks** (immutable image
  + fixed command) run in a hardened ephemeral K8s Job sandbox (non-root,
  read-only rootfs, no caps, no SA token, default-deny NetworkPolicy —
  `k8s/sandbox/`). Execution requires approved gate + allow-listed params;
  free-form AI output can never run.

Full design rationale: `docs/architecture.md`. Team onboarding: `docs/onboarding-guide.md`.

## Prerequisites

- Docker Desktop (Postgres container)
- `temporal` CLI (`brew install temporal`) — dev server
- `uv` (`brew install uv`)
- Optional (cluster test): `kubectl` with the staging GKE context

## Quick start

```bash
cd bosun
uv sync                 # install workspace (kernel + hello-world example)
make infra              # postgres (docker, port 5433) + temporal dev server
set -a; source .env; set +a   # secrets: gate HMAC, ANTHROPIC_API_KEY, kube context

# terminal 1
make worker             # Temporal worker — loads all installed extensions
# terminal 2
make api                # FastAPI on http://localhost:8400  (docs at /docs)
# terminal 3 (Phase 2+: Qdrant indexing off the outbox)
make relay
```

(`make infra` also starts Qdrant on :6333. `make infra-head` = Temporal dev
server with UI at http://localhost:8233.)

### Web UI

**http://localhost:8400/ui/** — everything below is also doable point-and-click:

- **Dashboard** — live component health (postgres, temporal, worker, relay,
  qdrant, AI advisor), recent runs, pending gates, incidents.
- **Pipelines** — spec editor with validate-before-register (YAML or JSON),
  versioned registration, start runs with parameter overrides.
- **Run detail** — live DAG (React Flow; statuses, gate 🚧 and fan-out ⑂
  badges), per-step/per-unit rows with timings and logs, parameters with
  provenance, gate approve/reject, event stream, AI troubleshoot on failed
  steps, incident RCA/proposal, sandbox playbook execution, outcome capture.
- **Knowledge** — add records, semantic search, click any record/search hit to
  view full content, delete (removes row + Qdrant embedding). The Assistant
  answers "if X happens, what's the fix?" by searching this base first.
- **Gates & Ops** — all pending gates, agent registry, playbook registry.
- **✦ Assistant** — chat with Claude wired to live read-only platform tools
  (runs, steps, events, incidents, knowledge, health, spec lint). Ask "why did
  the last run fail?", "draft a pipeline that…", "fix this yaml". Tool calls
  shown inline; proposed YAML gets an "open in spec editor" button — the
  assistant proposes, you register/approve. API: `POST /projects/{p}/chat`.

Polling-based realtime (2.5–5s). **Open platform**: no API key — first screen
asks for your name (audit trail) and a project; switch projects any time from
the top-bar dropdown. Projects stay strictly separate as namespaces (specs,
runs, gates, knowledge, events all scoped), they're just not locked.

Rebuild after UI changes: `cd ui && npm install && npm run build` — FastAPI
serves `ui/dist` automatically. UI dev loop: `cd ui && npm run dev` (proxies
to :8400).

### Run the hello-world pipeline by hand

```bash
# 1. create a project (open platform — API keys are minted but NOT enforced;
#    set BOSUN_AUTH=on on the API process to enforce them later)
curl -s -X POST localhost:8400/projects -H 'content-type: application/json' \
  -d '{"id":"hello","display_name":"Hello Team"}'
export KEY=anything   # X-API-Key header is optional in open mode

# 2. validate + register the spec (YAML accepted directly)
curl -s -X POST localhost:8400/projects/hello/specs/validate \
  -H "X-API-Key: $KEY" -H 'content-type: application/yaml' \
  --data-binary @examples/hello_world/pipeline.yaml
curl -s -X POST localhost:8400/projects/hello/specs \
  -H "X-API-Key: $KEY" -H 'content-type: application/yaml' \
  --data-binary @examples/hello_world/pipeline.yaml

# 3. start a run
curl -s -X POST localhost:8400/projects/hello/pipelines/hello-world/runs \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{}'
# → {"run_id": "...", ...}

# 4. watch status / steps / timings / params
curl -s localhost:8400/runs/<RUN_ID> -H "X-API-Key: $KEY" | jq .

# 5. the run pauses at the sign-off gate — approve it
curl -s localhost:8400/projects/hello/gates -H "X-API-Key: $KEY" | jq .
curl -s -X POST localhost:8400/gates/<GATE_ID>/resolve \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"decision":"approve","actor":"you@example.com"}'
# (or click the signed single-use Approve link printed in the worker log)

# 6. audit trail
curl -s localhost:8400/runs/<RUN_ID>/events -H "X-API-Key: $KEY" | jq '.[].type'
```

Temporal UI: run `temporal server start-dev` without `--headless` (or open
http://localhost:8233) to watch the interpreter workflow live.

### Run the rollup-shaped demo

`examples/rollup_mini/pipeline.yaml` simulates a monthly rollup runbook shape:
computed `yearmonth`/cost params, feed-file preflight, $27k budget gate, locale
fan-out with phase tiers + shared vendor quota, run-twice-until-queue-drained,
QA sign-off, manual count-sheet step. Register it under a `batch` project and
approve its three gates the same way.

## Testing

```bash
make test-unit     # 27 tests: spec validation, HMAC tokens, builtins,
                   #   log reduction, sandbox-manifest hardening
make test-e2e      # boots worker+API+relay; Phase 1 pipelines (hello-world,
                   #   rollup-mini, preflight blocking, overrides, gate tokens),
                   #   Phase 2 troubleshoot flow + Phase 2.5 learning/metric,
                   #   Phase 3 playbook guardrails

# real-cluster tests (OPT-IN — isolated bosun-e2e namespace, self-cleaning):
./scripts/setup-argo-e2e-namespace.sh          # one-time ns + executor RBAC
BOSUN_E2E_ARGO=1    uv run pytest tests/e2e/test_argo_gke.py -q       # Argo executor
BOSUN_E2E_SANDBOX=1 uv run pytest tests/e2e/test_phase3_sandbox.py -q # sandbox Job
```

E2E logs land in `.e2e-logs/`. Postgres/Qdrant stay up between runs
(`make infra-down` to stop). 41 tests green as of last run, incl. both GKE tests.
First Phase-2 run downloads the local embedding model (~100MB, one-time).

### Phase 2/3 walkthrough (after a step fails)

```bash
# seed knowledge (runbooks, incident notes) — indexed into Qdrant by the relay
curl -X POST localhost:8400/projects/batch/knowledge -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"kind":"incident","title":"PVC undersized → silent dataset corruption","content":"...","source":"INC-1024"}'

# full troubleshooting flow: bundle → retrieval → RCA → proposal → gate
curl -X POST localhost:8400/runs/<RUN_ID>/steps/<STEP_ID>/troubleshoot \
  -H "X-API-Key: $KEY" | jq '{rca, remediation_proposal, similar_incidents, gate_id}'

# approve the remediation gate, then execute a pre-approved playbook in the sandbox
curl -X POST localhost:8400/gates/<GATE_ID>/resolve -H "X-API-Key: $KEY" \
  -d '{"decision":"approve","actor":"you"}' -H 'content-type: application/json'
curl -X POST localhost:8400/projects/batch/playbooks -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"restart-agent","image":"<mirror>/alpine:latest","command":["sh","-c","echo fix $PARAM_HOST"],"allowed_params":{"host":"..."},"approved_by":"lead"}'
curl -X POST localhost:8400/incidents/<INCIDENT_ID>/execute -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' -d '{"playbook":"restart-agent","params":{"host":"ch1"}}'

# close the loop: record the outcome — becomes retrievable knowledge
curl -X POST localhost:8400/incidents/<INCIDENT_ID>/outcome -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' -d '{"outcome":"resized PVC, reran","success":true}'
```

Agent registry: `GET /agents` (toggle with `POST /agents/{id}/toggle`).
Quick one-shot diagnosis without the full flow: `POST /runs/<RUN>/steps/<STEP>/diagnose`.

## Repository layout

```
kernel/                     platform kernel (teams never modify)
  src/bosun/spec/         pipeline spec schema + lint (public API, semver'd)
  src/bosun/contracts/    extension ABCs + entry-point registry (public API)
  src/bosun/temporal/     generic interpreter workflow, activities, worker
  src/bosun/api/          FastAPI control plane + signed gate tokens
  src/bosun/builtins/     built-in executors/checks/resolvers/channels
  src/bosun/advisor/      read-only AI failure advisor (Phase 2 seed)
  src/bosun/ai/           Phase 2: bundle, log reduction, knowledge, agents, metric
  src/bosun/sandbox.py    Phase 3: hardened K8s Job sandbox
  src/bosun/relay.py      outbox consumer (Qdrant indexing; Kafka slot-in)
ui/                         React SPA (Vite + React Flow) — served at /ui
examples/hello_world/       a consuming team's package — the framework contract test
examples/rollup_mini/          rollup-shaped dummy pipeline + real-Argo smoke spec
k8s/sandbox/                sandbox SA + default-deny NetworkPolicy manifests
tests/unit, tests/e2e       41 tests
scripts/                    cluster e2e namespace setup
docs/                       architecture + onboarding guide
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BOSUN_DATABASE_URL` | `postgresql+asyncpg://bosun:bosun@localhost:5433/bosun` | Postgres |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal frontend |
| `BOSUN_API_BASE_URL` | `http://localhost:8400` | Base for gate action links |
| `BOSUN_GATE_TOKEN_SECRET` | dev default — set in prod | HMAC key for gate tokens |
| `BOSUN_KUBE_CONTEXT` | current context | Kube context for the argo executor |
| `BOSUN_ARGO_NAMESPACE` | `argo` | Default Argo namespace |
| `BOSUN_MANIFESTS_DIR` | `manifests` | Folder the argo executor resolves `manifest_file:` against (worker-side) |
| `ANTHROPIC_API_KEY` | — | Enables `/troubleshoot` + `/diagnose` AI endpoints |
| `BOSUN_QDRANT_URL` | `http://localhost:6333` | Qdrant semantic memory |
| `BOSUN_SANDBOX_NAMESPACE` | `bosun-e2e` | Namespace for sandbox Jobs |
| `BOSUN_TEAMS_WEBHOOK_URL` | — | Power Automate webhook; enables the `teams` gate channel default + relay alerts |
| `BOSUN_TEAMS_ALERT_EVENTS` | `WorkflowFailed,GateRequested,RemediationSuggested,GateExpired` | Events the relay forwards to Teams |
| `BOSUN_TEAMS_ALERT_MENTIONS` | — | Emails to @mention on alert cards (csv) |
| `BOSUN_AUTH` | `off` | `on` = enforce per-project API keys (open platform by default) |
| `BOSUN_ADMIN_KEY` | unset (open in dev) | Protects `POST /projects`, agent toggles |

## Scope notes (deliberate)

- Kafka producer not wired yet — relay consumes the outbox and is the slot-in
  point; producers never change.
- **Teams is live**: `teams` gate channel posts Adaptive Cards (Approve/Reject
  buttons = signed single-use links, @mentions via `channel_params.mentions`)
  through a Power Automate webhook; the relay forwards failure/gate/remediation
  events as alert cards with mentions. Spec usage:
  `gate: {channel: teams, channel_params: {mentions: [you@example.com]}}`.
- Preflight checks run at step level (not per fan-out unit).
- Cross-run quota pools not enforced yet (per-run pools work).
- `expression` resolvers evaluate in a builtins-stripped namespace — spec
  authors are trusted project members (same trust level as `script` steps).
- Sandbox admission control (signed images) relies on the cluster's existing
  Gatekeeper allowed-registries policy; per-playbook egress NetworkPolicies
  are templates in `k8s/sandbox/`.
