import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, Optional

from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.tls import WorkerTLSIdentityManager

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
    poll_interval_seconds: int = 30


class WorkerHealthState:
    def __init__(self, control_plane_url: str, worker_name: str):
        self._lock = Lock()
        self._snapshot = WorkerHealthSnapshot(
            control_plane_url=control_plane_url,
            worker_name=worker_name,
        )

    def set_runtime(self, run_once: bool, poll_interval_seconds: int):
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

    def record_loop_completed(self):
        with self._lock:
            self._snapshot.last_loop_completed_at = _utcnow()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = asdict(self._snapshot)
        payload["ok"] = True
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


def _worker_tls_paths() -> tuple[str, str, str, str]:
    tls_dir = os.environ.get("WORKER_TLS_DIR", ".worker_tls").strip() or ".worker_tls"
    tls_root = Path(tls_dir)
    ca_file = os.environ.get("WORKER_TLS_CA_FILE", str(tls_root / "ca-cert.pem"))
    cert_file = os.environ.get("WORKER_TLS_CERT_FILE", str(tls_root / "worker-cert.pem"))
    key_file = os.environ.get("WORKER_TLS_KEY_FILE", str(tls_root / "worker-key.pem"))
    return tls_dir, ca_file, cert_file, key_file


def build_service() -> WorkerAgentService:
    tls_dir, tls_ca_file, tls_cert_file, tls_key_file = _worker_tls_paths()
    def _resolve_version():
        wv = (os.environ.get("WORKER_VERSION") or "").strip()
        av = (os.environ.get("APP_VERSION") or "").strip()
        if wv and wv not in ("ghcr", "docker", "dev", "latest"):
            return wv
        if av and av not in ("dev", "latest"):
            return av
        return wv or av or "dev"

    config = WorkerAgentConfig(
        control_plane_url=os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8080"),
        name=os.environ.get("WORKER_NAME", socket.gethostname()),
        host_name=os.environ.get("WORKER_HOST_NAME", socket.gethostname()),
        version=_resolve_version(),
        worker_id=os.environ.get("WORKER_ID") or None,
        labels=_labels_from_env(),
        backup_runtime_image=os.environ.get(
            "BACKUP_RUNTIME_IMAGE",
            "ghcr.io/danielrondongarcia/docker-volume-backup",
        ),
        enrollment_token=os.environ.get("WORKER_ENROLLMENT_TOKEN") or None,
        enrollment_ca_pem=os.environ.get("WORKER_ENROLLMENT_CA_PEM") or None,
        tls_dir=tls_dir,
        tls_ca_file=tls_ca_file,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
    )
    client = ControlPlaneClient(
        config.control_plane_url,
        ca_file=config.tls_ca_file,
        client_cert_file=config.tls_cert_file,
        client_key_file=config.tls_key_file,
    )
    docker_runtime = DockerRuntimeAdapter()
    return WorkerAgentService(config, client, docker_runtime)


def ensure_worker_enrollment(service: WorkerAgentService) -> None:
    config = service.config
    if not config.enrollment_token:
        return
    if not config.tls_ca_file or not config.tls_cert_file or not config.tls_key_file:
        raise RuntimeError("worker TLS file paths are not configured")

    identity = WorkerTLSIdentityManager(
        tls_dir=config.tls_dir,
        ca_file=config.tls_ca_file,
        cert_file=config.tls_cert_file,
        key_file=config.tls_key_file,
    )
    if not Path(config.tls_ca_file).exists():
        if not config.enrollment_ca_pem:
            raise RuntimeError("WORKER_ENROLLMENT_CA_PEM is required to bootstrap TLS enrollment")
        Path(config.tls_ca_file).write_text(config.enrollment_ca_pem, encoding="utf-8")
    if identity.has_client_certificate():
        logger.info("Worker TLS identity already present at %s", config.tls_dir)
        return

    logger.info("Worker enrollment requested; generating CSR for %s", config.name)
    csr_pem = identity.create_csr(name=config.name, host_name=config.host_name)
    enrollment = service.control_plane_client.enroll_worker(
        token=config.enrollment_token,
        csr_pem=csr_pem,
        version=config.version,
        labels=config.labels,
    )
    identity.persist_signed_materials(
        certificate_pem=enrollment["certificate_pem"],
        ca_certificate_pem=enrollment["ca_certificate_pem"],
    )
    config.worker_id = enrollment["worker_id"]
    logger.info("Worker enrolled successfully with id %s", config.worker_id)


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
    ensure_worker_enrollment(service)
    run_once = os.environ.get("WORKER_RUN_ONCE", "true").strip().lower() in ("1", "true", "yes", "on")
    poll_interval = int(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "30"))
    last_logged_worker_id = None
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
            service.send_heartbeat()
            service.sync_inventory()
            processed = service.poll_once()
            health_state.record_control_plane_success(worker_id=worker_id, processed_jobs=len(processed))
            logger.info("Worker processed %s jobs", len(processed))
        except Exception as exc:
            health_state.record_control_plane_failure(str(exc))
            logger.warning("Worker loop could not reach the Control Plane: %s", exc)
            if run_once:
                raise
        finally:
            health_state.record_loop_completed()
        if run_once:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
