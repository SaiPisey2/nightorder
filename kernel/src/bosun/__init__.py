"""Bosun kernel — Enterprise Agentic Workflow Orchestration Platform.

Kernel components (platform team owns):
  spec/       declarative pipeline spec schema + validation
  contracts/  extension-point contracts + entry-point registry
  temporal/   generic interpreter workflow, activities, worker
  api/        FastAPI control plane
  db/         PostgreSQL system of record (incl. transactional outbox)
  builtins/   built-in executors / checks / resolvers / gate channels
  advisor/    read-only AI failure advisor (Phase 2 seed)
"""

__version__ = "0.1.0"

TASK_QUEUE = "bosun-main"
SPEC_API_VERSION = "bosun/v1"
