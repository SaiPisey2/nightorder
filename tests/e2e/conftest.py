"""E2E harness: boots the full local stack (Postgres via docker compose,
Temporal dev server, bosun worker, bosun API) and tears down what it
started. Requires docker + temporal CLI on PATH."""
from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
API = "http://localhost:8400"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _load_dotenv() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k, v)
    return env


def _wait(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for {what}")


@pytest.fixture(scope="session")
def stack():
    env = _load_dotenv()
    procs: list[subprocess.Popen] = []
    started_temporal = False

    # 1. postgres + qdrant
    subprocess.run(["docker", "compose", "up", "-d", "postgres", "qdrant"], cwd=ROOT, check=True, capture_output=True)
    _wait(lambda: subprocess.run(
        ["docker", "exec", "bosun-postgres", "pg_isready", "-U", "bosun"],
        capture_output=True).returncode == 0, 60, "postgres")
    _wait(lambda: _port_open(6333), 60, "qdrant")

    # 2. temporal dev server
    if not _port_open(7233):
        procs.append(subprocess.Popen(
            ["temporal", "server", "start-dev", "--headless", "--log-level", "warn"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        started_temporal = True
        _wait(lambda: _port_open(7233), 60, "temporal dev server")

    # 3. worker + api
    log_dir = ROOT / ".e2e-logs"
    log_dir.mkdir(exist_ok=True)
    worker_log = open(log_dir / "worker.log", "w")
    api_log = open(log_dir / "api.log", "w")
    relay_log = open(log_dir / "relay.log", "w")
    procs.append(subprocess.Popen(["uv", "run", "bosun-worker"], cwd=ROOT, env=env,
                                  stdout=worker_log, stderr=subprocess.STDOUT))
    procs.append(subprocess.Popen(["uv", "run", "bosun-api"], cwd=ROOT, env=env,
                                  stdout=api_log, stderr=subprocess.STDOUT))
    procs.append(subprocess.Popen(["uv", "run", "bosun-relay"], cwd=ROOT, env=env,
                                  stdout=relay_log, stderr=subprocess.STDOUT))

    def api_up() -> bool:
        try:
            return httpx.get(f"{API}/health", timeout=2).status_code == 200
        except Exception:
            return False

    _wait(api_up, 90, "bosun api")
    time.sleep(2)  # worker registration grace

    yield {"api": API, "env": env}

    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    worker_log.close()
    api_log.close()
    relay_log.close()
    # postgres left running for fast re-runs (make infra-down cleans up);
    # temporal dev server killed only if we started it (it's in procs).
    _ = started_temporal


@pytest.fixture()
def client(stack):
    with httpx.Client(base_url=stack["api"], timeout=30) as c:
        yield c


@pytest.fixture()
def project(client):
    """Fresh project per test — logical isolation keeps tests independent."""
    pid = f"e2e-{uuid.uuid4().hex[:8]}"
    resp = client.post("/projects", json={"id": pid, "display_name": "e2e"})
    assert resp.status_code == 201, resp.text
    return {"id": pid, "key": resp.json()["api_key"]}


def poll_run(client: httpx.Client, run_id: str, key: str, timeout: float = 180,
             until: tuple[str, ...] = ("completed", "failed")) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/runs/{run_id}", headers={"X-API-Key": key}).json()
        if last.get("status") in until:
            return last
        time.sleep(2)
    raise TimeoutError(f"run {run_id} still {last.get('status')} after {timeout}s: {last}")


def approve_pending_gates(client: httpx.Client, project: dict, run_id: str,
                          expect: int, timeout: float = 120) -> list[str]:
    """Approve gates for a run as they appear, until `expect` approved."""
    approved: list[str] = []
    deadline = time.monotonic() + timeout
    while len(approved) < expect and time.monotonic() < deadline:
        gates = client.get(f"/projects/{project['id']}/gates",
                           headers={"X-API-Key": project["key"]}).json()
        for g in gates:
            if g["run_id"] == run_id and g["gate_id"] not in approved:
                r = client.post(f"/gates/{g['gate_id']}/resolve",
                                json={"decision": "approve", "actor": "e2e-test"},
                                headers={"X-API-Key": project["key"]})
                assert r.status_code == 200, r.text
                approved.append(g["gate_id"])
        time.sleep(1.5)
    assert len(approved) == expect, f"approved {len(approved)}/{expect} gates"
    return approved
