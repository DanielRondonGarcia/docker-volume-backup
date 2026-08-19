import json
import logging
import math
import os
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError

from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.adapters.redis_cache import RedisSnapshotCache
from src.worker_agent.infrastructure.api_client.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneHTTPError,
)
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerHealthSnapshot:
    started_at: str = field(default_factory=_utcnow)
    status: str = "starting"
    control_plane_url: str = ""
    worker_name: str = ""
    worker_id: Optional[str] = None
    registered: bool = False
    control_plane_reachable: bool = False
    last_successful_control_plane_contact_at: Optional[str] = None
    last_loop_completed_at: Optional[str] = None
    last_error: Optional[str] = None
    last_processed_jobs: int = 0
    run_once: bool = False
    poll_interval_seconds: float = 30.0


class WorkerHealthState:
    def __init__(self, control_plane_url: str, worker_name: str):
        self._lock = Lock()
        self._snapshot = WorkerHealthSnapshot(
            control_plane_url=control_plane_url,
            worker_name=worker_name,
        )

    def set_runtime(self, run_once: bool, poll_interval_seconds: float):
        with self._lock:
            self._snapshot.run_once = run_once
            self._snapshot.poll_interval_seconds = poll_interval_seconds

    def record_control_plane_success(self, worker_id: str, processed_jobs: Optional[int] = None):
        with self._lock:
            self._snapshot.worker_id = worker_id
            self._snapshot.registered = True
            self._snapshot.control_plane_reachable = True
            self._snapshot.last_successful_control_plane_contact_at = _utcnow()
            self._snapshot.last_error = None
            self._snapshot.status = "ok"
            if processed_jobs is not None:
                self._snapshot.last_processed_jobs = processed_jobs

    def record_control_plane_failure(self, error: str):
        with self._lock:
            self._snapshot.control_plane_reachable = False
            self._snapshot.last_error = error
            self._snapshot.status = "degraded" if self._snapshot.registered else "starting"

    def record_not_ready(self, error: str):
        with self._lock:
            self._snapshot.control_plane_reachable = False
            self._snapshot.last_error = error
            self._snapshot.status = "not_ready"

    def record_loop_completed(self):
        with self._lock:
            self._snapshot.last_loop_completed_at = _utcnow()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = asdict(self._snapshot)
        payload["ok"] = self._snapshot.status == "ok"
        payload["note"] = (
            "Worker process is alive; control_plane_reachable only indicates the latest known contact with the Control Plane"
        )
        return payload


class WorkerHealthHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, state: WorkerHealthState):
        super().__init__(server_address, RequestHandlerClass)
        self.state = state


class WorkerHealthRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/healthz", "/healthz/", "/health", "/health/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = json.dumps(self.server.state.snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


def _labels_from_env() -> dict:
    raw = os.environ.get("WORKER_LABELS", "{}").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("WORKER_LABELS is not valid JSON; ignoring")
        return {}


def _bounded_interval(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s is invalid; using %.3fs", name, default)
        return default
    if not math.isfinite(value) or value < minimum or value > maximum:
        logger.warning("%s is outside %.3f..%.3fs; using %.3fs", name, minimum, maximum, default)
        return default
    return value


def _runtime_orphan_sweep_interval() -> float:
    return _bounded_interval(
        "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS",
        15.0,
        5.0,
        3600.0,
    )


_WORKER_ERROR_DETAIL_LIMIT = 512


def _bounded_worker_error_detail(error: Exception) -> str:
    detail = " ".join(str(error).split())
    detail = re.sub(
        r"(?i)(?:https?|http\+docker)://[^\s)]+",
        lambda match: (
            "http+docker://<redacted>"
            if match.group(0).lower().startswith("http+docker://")
            else "<url>"
        ),
        detail,
    )
    detail = re.sub(
        r"(?i)\b(?:authorization|bearer|token|secret|password|credential)\b\s*[:=]\s*[^\s,;]+",
        "<redacted>",
        detail,
    )
    if len(detail) <= _WORKER_ERROR_DETAIL_LIMIT:
        return detail
    return f"{detail[:_WORKER_ERROR_DETAIL_LIMIT - 3]}..."


def _is_docker_runtime_error(error: Exception) -> bool:
    qualified_type = f"{error.__class__.__module__}.{error.__class__.__name__}".lower()
    detail = str(error).lower()
    return (
        "docker.errors" in qualified_type
        or "http+docker://" in detail
        or "docker daemon" in detail
        or "docker socket" in detail
        or "no such image" in detail
        or "docker api" in detail
    )


def _is_control_plane_connectivity_error(error: Exception) -> bool:
    if isinstance(error, (URLError, socket.gaierror, ConnectionError, TimeoutError, ssl.SSLError)):
        return True
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in (
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
            "getaddrinfo failed",
            "connection refused",
            "connection reset",
            "connection aborted",
            "tls handshake",
            "ssl error",
            "timed out",
        )
    )


def _classify_worker_loop_error(error: Exception) -> str:
    if isinstance(error, (ControlPlaneHTTPError, HTTPError)):
        return "control_plane_rejection"
    if _is_docker_runtime_error(error):
        return "docker_runtime"
    if _is_control_plane_connectivity_error(error):
        return "control_plane_connectivity"
    return "worker_loop"


def _format_worker_loop_error(error: Exception) -> str:
    category = _classify_worker_loop_error(error)
    if category == "control_plane_rejection":
        return str(error)
    detail = _bounded_worker_error_detail(error) or error.__class__.__name__
    if category == "control_plane_connectivity":
        return f"Worker could not connect to the Control Plane: {detail}"
    if category == "docker_runtime":
        return f"Worker reached the Control Plane but the Docker runtime failed: {detail}"
    return f"Worker loop failed ({error.__class__.__name__}): {detail}"


def _poll_worker(service: WorkerAgentService):
    interactive_poll = getattr(service, "poll_interactive_once", None)
    if callable(interactive_poll):
        try:
            return interactive_poll()
        except (AttributeError, NotImplementedError):
            logger.debug("Interactive worker lane unavailable; falling back to durable polling")
    return service.poll_once()


def build_service() -> WorkerAgentService:
    def _resolve_version():
        wv = (os.environ.get("WORKER_VERSION") or "").strip()
        av = (os.environ.get("APP_VERSION") or "").strip()
        if wv and wv not in ("ghcr", "docker", "dev", "latest"):
            return wv
        if av and av not in ("dev", "latest"):
            return av
        return wv or av or "dev"

    credential_file = os.environ.get("WORKER_CREDENTIAL_FILE", ".worker_credentials.json")
    recovery_file = os.environ.get("WORKER_JOB_RECOVERY_FILE") or str(
        Path(credential_file).with_name("worker_job_recovery.json")
    )
    credential_store = WorkerCredentialStore(credential_file)
    stored = credential_store.load()
    config = WorkerAgentConfig(
        control_plane_url=os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8080"),
        name=os.environ.get("WORKER_NAME", socket.gethostname()),
        host_name=os.environ.get("WORKER_HOST_NAME", socket.gethostname()),
        version=_resolve_version(),
        worker_id=os.environ.get("WORKER_ID") or (stored.worker_id if stored else None),
        labels=_labels_from_env(),
        backup_runtime_image=os.environ.get(
            "BACKUP_RUNTIME_IMAGE",
            "ghcr.io/danielrondongarcia/docker-volume-backup",
        ),
        enrollment_token=os.environ.get("WORKER_ENROLLMENT_TOKEN") or os.environ.get("WORKER_SECRET") or None,
    )
    client = ControlPlaneClient(
        config.control_plane_url,
        ca_file=os.environ.get("CONTROL_PLANE_CA_FILE") or None,
        credential_store=credential_store,
        worker_id=config.worker_id,
        enrollment_secret=config.enrollment_token,
    )
    docker_runtime = DockerRuntimeAdapter()
    snapshot_cache = RedisSnapshotCache.from_env()
    return WorkerAgentService(
        config,
        client,
        docker_runtime,
        snapshot_cache=snapshot_cache,
        recovery_file=recovery_file,
    )


def start_health_server(state: WorkerHealthState):
    host = os.environ.get("WORKER_HEALTH_HOST", "0.0.0.0")
    port = int(os.environ.get("WORKER_HEALTH_PORT", "8081"))
    if port <= 0:
        logger.info("Worker health endpoint disabled because WORKER_HEALTH_PORT=%s", port)
        return None

    try:
        server = WorkerHealthHTTPServer((host, port), WorkerHealthRequestHandler, state=state)
    except OSError as exc:
        logger.warning("Could not bind worker health endpoint on %s:%s: %s", host, port, exc)
        return None
    Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Worker health endpoint listening on %s:%s", host, port)
    return server


def main():
    service = build_service()
    run_once = os.environ.get("WORKER_RUN_ONCE", "true").strip().lower() in ("1", "true", "yes", "on")
    poll_interval = _bounded_interval("WORKER_POLL_INTERVAL_SECONDS", 30.0, 0.1, 86400.0)
    interactive_interval = _bounded_interval("WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS", 0.5, 0.1, 5.0)
    inventory_interval = _bounded_interval("WORKER_INVENTORY_SYNC_INTERVAL_SECONDS", poll_interval, 0.5, 86400.0)
    orphan_sweep_interval = _runtime_orphan_sweep_interval()
    last_logged_worker_id = None
    attempts = 0
    next_orphan_sweep_at = None
    last_heartbeat_at = 0.0
    last_inventory_sync_at = 0.0
    health_state = WorkerHealthState(
        control_plane_url=service.config.control_plane_url,
        worker_name=service.config.name,
    )
    health_state.set_runtime(run_once=run_once, poll_interval_seconds=poll_interval)
    start_health_server(health_state)

    while True:
        try:
            worker_id = service.ensure_registered()
            if last_logged_worker_id != worker_id:
                logger.info("Worker registered with id %s", worker_id)
                last_logged_worker_id = worker_id
            now = time.monotonic()
            if next_orphan_sweep_at is None or now >= next_orphan_sweep_at:
                next_orphan_sweep_at = now + orphan_sweep_interval
                try:
                    sweep = service.cleanup_orphaned_runtime_containers()
                    sweep = sweep if isinstance(sweep, dict) else {}
                    logger.info(
                        "Runtime orphan sweep %s: inspected=%s removed=%s failed=%s skipped=%s; "
                        "expired in-progress leases fail closed as worker_interrupted",
                        "partial" if sweep.get("error") else "complete",
                        sweep.get("inspected", 0),
                        sweep.get("removed", 0),
                        sweep.get("failed", 0),
                        sweep.get("skipped", 0),
                    )
                except Exception as exc:
                    logger.warning(
                        "Runtime orphan sweep failed; expired in-progress leases fail closed as worker_interrupted "
                        "(error_type=%s)",
                        exc.__class__.__name__,
                    )
            now = time.monotonic()
            if run_once or now - last_heartbeat_at >= poll_interval:
                service.send_heartbeat()
                last_heartbeat_at = now
            if run_once or now - last_inventory_sync_at >= inventory_interval:
                service.sync_inventory()
                last_inventory_sync_at = now
            processed = _poll_worker(service)
            health_state.record_control_plane_success(worker_id=worker_id, processed_jobs=len(processed))
            logger.info("Worker processed %s jobs", len(processed))
            attempts = 0
        except Exception as exc:
            attempts += 1
            error_message = _format_worker_loop_error(exc)
            health_state.record_control_plane_failure(error_message)
            logger.warning("%s", error_message)
            if attempts >= 5:
                health_state.record_not_ready(error_message)
                raise RuntimeError("worker startup failed after 5 attempts") from None
        finally:
            health_state.record_loop_completed()
        if run_once and attempts == 0:
            break
        if attempts or not run_once:
            retry_delay = _bounded_interval(
                "WORKER_STARTUP_RETRY_DELAY_SECONDS",
                poll_interval,
                0.0,
                86400.0,
            ) if attempts else interactive_interval
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
