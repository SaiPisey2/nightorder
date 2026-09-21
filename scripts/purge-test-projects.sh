#!/usr/bin/env bash
# Purge e2e-* test projects (and all their data) from the local dev DB.
set -euo pipefail

docker exec -i bosun-postgres psql -U bosun <<'SQL'
BEGIN;
CREATE TEMP TABLE doomed AS SELECT id FROM projects WHERE id LIKE 'e2e-%';
DELETE FROM gate_token_nonces WHERE gate_id IN (SELECT id FROM gates WHERE project IN (SELECT id FROM doomed));
DELETE FROM outbox WHERE event_id IN (SELECT id FROM events WHERE project IN (SELECT id FROM doomed));
DELETE FROM events WHERE project IN (SELECT id FROM doomed);
DELETE FROM gates WHERE project IN (SELECT id FROM doomed);
DELETE FROM incidents WHERE project IN (SELECT id FROM doomed);
DELETE FROM knowledge_records WHERE project IN (SELECT id FROM doomed);
DELETE FROM playbooks WHERE project IN (SELECT id FROM doomed);
DELETE FROM resolved_parameters WHERE run_id IN (SELECT id FROM runs WHERE project IN (SELECT id FROM doomed));
DELETE FROM step_executions WHERE run_id IN (SELECT id FROM runs WHERE project IN (SELECT id FROM doomed));
DELETE FROM runs WHERE project IN (SELECT id FROM doomed);
DELETE FROM pipeline_specs WHERE project IN (SELECT id FROM doomed);
DELETE FROM projects WHERE id IN (SELECT id FROM doomed);
COMMIT;
SQL
echo "purged e2e-* projects"
