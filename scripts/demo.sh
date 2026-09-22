#!/usr/bin/env bash
# End-to-end walkthrough against a running control plane.
#
#   make infra && make worker   (terminal 1)
#   make api                    (terminal 2)
#   ./scripts/demo.sh           (terminal 3)
#
# Registers the hello-world pipeline, starts a run, waits at the human gate,
# approves it, and prints the audit trail.
set -euo pipefail

API="${NIGHTORDER_API_BASE_URL:-http://localhost:8400}"
PROJECT="${1:-hello}"
BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

step() { printf '\n%s▸ %s%s\n' "$BOLD" "$1" "$OFF"; }
note() { printf '%s  %s%s\n' "$DIM" "$1" "$OFF"; }

api() { curl -fsS "$@"; }

if ! curl -fsS "$API/health" >/dev/null 2>&1; then
  echo "control plane not reachable at $API — run 'make api' first" >&2
  exit 1
fi

step "Create a project"
api -X POST "$API/projects" -H 'content-type: application/json' \
    -d "{\"id\":\"$PROJECT\",\"display_name\":\"Hello Team\"}" >/dev/null 2>&1 \
  && note "created '$PROJECT'" \
  || note "'$PROJECT' already exists — reusing it"

step "Validate the spec before registering it"
api -X POST "$API/projects/$PROJECT/specs/validate" \
    -H 'content-type: application/yaml' \
    --data-binary @examples/hello_world/pipeline.yaml | jq -c '{valid, errors}'

step "Register it"
VERSION=$(api -X POST "$API/projects/$PROJECT/specs" \
    -H 'content-type: application/yaml' \
    --data-binary @examples/hello_world/pipeline.yaml | jq -r '.version')
note "registered hello-world version $VERSION"

step "Start a run"
RUN=$(api -X POST "$API/projects/$PROJECT/pipelines/hello-world/runs" \
    -H 'content-type: application/json' -d '{}' | jq -r '.run_id')
note "run $RUN"

step "Parameters resolve, each with recorded provenance"
for _ in $(seq 1 30); do
  COUNT=$(api "$API/runs/$RUN" | jq '.parameters | length')
  [ "$COUNT" -ge 3 ] && break
  sleep 1
done
api "$API/runs/$RUN" | jq -r '.parameters[] | "  \(.name) = \(.value|tostring)   [2m\(.provenance)[0m"'

step "Steps run: preflight, then fan-out over regions in tiers"
for _ in $(seq 1 60); do
  GATE=$(api "$API/projects/$PROJECT/gates" | jq -r '.[0].gate_id // empty')
  [ -n "$GATE" ] && break
  sleep 1
done
api "$API/runs/$RUN" | jq -r '.steps[] | "  \(.status|ascii_upcase|.[0:9]) \(.step_id)\(if .unit then " ["+.unit+"]" else "" end)"'

step "The run is paused at a human gate"
api "$API/projects/$PROJECT/gates" | jq -r '.[0] | "  \(.type): \(.prompt)"'
printf '%s  nothing past this point runs until a person decides%s\n' "$YELLOW" "$OFF"

step "Approve it"
api -X POST "$API/gates/$GATE/resolve" -H 'content-type: application/json' \
    -d '{"decision":"approve","actor":"demo@example.com"}' | jq -c '{gate_id, status}'

step "The rest of the pipeline proceeds"
for _ in $(seq 1 60); do
  STATUS=$(api "$API/runs/$RUN" | jq -r '.status')
  case "$STATUS" in completed|failed|rejected) break ;; esac
  sleep 1
done
api "$API/runs/$RUN" | jq -r '.steps[] | "  \(.status|ascii_upcase|.[0:9]) \(.step_id)\(if .unit then " ["+.unit+"]" else "" end)"'
printf '\n  run %s%s%s\n' "$GREEN" "$(api "$API/runs/$RUN" | jq -r '.status')" "$OFF"

step "Every decision is on the audit trail"
api "$API/runs/$RUN/events" | jq -r '.[].type' | sort | uniq -c | sort -rn \
  | awk '{printf "  %2s  %s\n", $1, $2}'
