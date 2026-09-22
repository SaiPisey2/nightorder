"""Temporal worker: loads the extension registry (kernel + every installed
extension package) and serves the generic interpreter."""
from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from nightorder import TASK_QUEUE
from nightorder.config import log_runtime_config, settings
from nightorder.contracts import load_registry
from nightorder.db import init_db
from nightorder.temporal.activities import ALL_ACTIVITIES
from nightorder.temporal.workflows import PipelineRunWorkflow, RemediationWorkflow, StepUnitWorkflow

log = logging.getLogger("nightorder.worker")


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = settings()
    await init_db()
    registry = load_registry()
    log.info("extension catalog: %s", registry.catalog())
    client = await Client.connect(cfg.temporal_address, namespace=cfg.temporal_namespace)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PipelineRunWorkflow, StepUnitWorkflow, RemediationWorkflow],
        activities=ALL_ACTIVITIES,
    )
    log.info("worker started on task queue %s (temporal %s)", TASK_QUEUE, cfg.temporal_address)
    await worker.run()


def main() -> None:
    log_runtime_config()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
