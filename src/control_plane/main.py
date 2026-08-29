import json
import logging
import os
import queue
import re
import ssl
import threading
from collections import deque
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

try:
    from http.server import ThreadingHTTPSServer as NativeThreadingHTTPSServer
except ImportError:
    NativeThreadingHTTPSServer = None

from src.control_plane.auth import AuthService, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER
from src.control_plane.application.services.control_plane_service import ControlPlaneService, WorkerDeletionConflict
from src.control_plane.application.services.live_file_service import (
    LiveCursorError,
    LiveFileService,
    LiveLimitError,
    LiveSessionError,
    LiveSessionKey,
)
from src.control_plane.application.services.scheduler_service import (
    DEFAULT_SCHEDULER_TIMEZONE,
    SCHEDULER_TIMEZONE_ENV,
    SchedulerService,
)
from src.control_plane.domain.models import JobStatus
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryInventoryRepository,
    InMemoryCacheRepository,
    InMemoryIndexRepository,
    InMemoryJobRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetStatsRepository,
    InMemoryTargetRepository,
    InMemoryWorkerRepository,
)
from src.control_plane.infrastructure.repositories.sqlite import (
    SQLiteInventoryRepository,
    SQLiteCacheRepository,
    SQLiteIndexRepository,
    SQLiteJobRepository,
    SQLiteRetentionPolicyRepository,
    SQLiteSecretRepository,
    SQLiteSettingsRepository,
    SQLiteSnapshotRepository,
    SQLiteStorageProfileRepository,
    SQLiteTargetStatsRepository,
    SQLiteTargetRepository,
    SQLiteWorkerRepository,
)
from src.control_plane.infrastructure.security.secret_codec import SecretCodec
from src.control_plane.infrastructure.security.tls import TLSMaterialManager
from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
UI_ROOT = Path(__file__).resolve().parent / "ui"
JOB_EVENT_HEARTBEAT_SECONDS = 15
LIVE_MOUNT_SOURCES_LIMIT = 64
LIVE_MOUNT_FOLDER_LENGTH = 64
LIVE_WORKER_FAILURE_REASONS = {
    "protected_volume",
    "source_unavailable",
    "helper_start_failed",
    "helper_request_failed",
    "live_session_unavailable",
    "invalid_source",
    "invalid_request",
}
LIVE_WORKER_FAILURE_MESSAGES = {
    "protected_volume": "live access denied",
    "source_unavailable": "live source unavailable",
    "helper_start_failed": "live helper unavailable",
    "helper_request_failed": "live helper request unavailable",
    "live_session_unavailable": "live session unavailable",
    "invalid_source": "live source is invalid",
    "invalid_request": "live request is invalid",
}


def _safe_live_log_value(value, fallback="unknown"):
    if not isinstance(value, str) or not value:
        return fallback
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:128] or fallback


class LiveWorkerError(LiveSessionError):
    def __init__(self, status: int, code: str, reason: str, path: str = ""):
        self.status = status
        self.code = code
        self.reason = reason
        self.path = path
        super().__init__(LIVE_WORKER_FAILURE_MESSAGES.get(reason, "live worker operation unavailable"))


class LiveOperationalError(LiveWorkerError):
    def __init__(self, reason="live_session_unavailable", message=None):
        reason = reason if reason in LIVE_WORKER_FAILURE_REASONS else "live_session_unavailable"
        super().__init__(503, "live_worker_unavailable", reason, "")
        if message:
            self.args = (message,)


class LiveAccessDeniedError(LiveWorkerError):
    def __init__(self, path: str):
        super().__init__(403, "live_access_denied", "protected_volume", path)


def _to_jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    if is_dataclass(value):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _secret_to_public(secret):
    payload = _to_jsonable(secret)
    payload.pop("ciphertext", None)
    return payload


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _build_tls_manager():
    if not _env_flag("CONTROL_PLANE_TLS_ENABLED", default=False):
        return None
    configured = os.environ.get("CONTROL_PLANE_TLS_SERVER_HOSTNAMES", "")
    hostnames = [item.strip() for item in configured.split(",") if item.strip()]
    host = os.environ.get("CONTROL_PLANE_HOST", "0.0.0.0").strip()
    if host and host not in ("0.0.0.0", "::"):
        hostnames.append(host)
    hostnames.extend(["localhost", "127.0.0.1", "control-plane"])
    tls_dir = os.environ.get("CONTROL_PLANE_TLS_DIR", ".control_plane_tls")
    return TLSMaterialManager.from_runtime(base_dir=tls_dir, server_hostnames=hostnames)


def _build_service() -> ControlPlaneService:
    repository_mode = os.environ.get("CONTROL_PLANE_REPOSITORY", "sqlite").strip().lower()
    codec = SecretCodec.from_runtime(
        key_file_path=os.environ.get("CONTROL_PLANE_KEY_FILE", ".control_plane.key"),
        env_key=os.environ.get("CONTROL_PLANE_MASTER_KEY"),
    )
    if repository_mode == "memory":
        logger.info("Using in-memory repositories for Control Plane")
        service = ControlPlaneService(
            worker_repository=InMemoryWorkerRepository(),
            inventory_repository=InMemoryInventoryRepository(),
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=codec,
            settings_repository=InMemorySettingsRepository(),
        )
        service.worker_auth = WorkerAuthState()
        return service

    database_path = os.environ.get("CONTROL_PLANE_DB_PATH", "control_plane.db")
    logger.info("Using SQLite repositories for Control Plane at %s", database_path)
    worker_auth = WorkerAuthState(database_path)
    service = ControlPlaneService(
        worker_repository=SQLiteWorkerRepository(database_path),
        inventory_repository=SQLiteInventoryRepository(database_path),
        cache_repository=SQLiteCacheRepository(database_path),
        index_repository=SQLiteIndexRepository(database_path),
        target_repository=SQLiteTargetRepository(database_path),
        job_repository=SQLiteJobRepository(database_path),
        storage_profile_repository=SQLiteStorageProfileRepository(database_path),
        secret_repository=SQLiteSecretRepository(database_path),
        snapshot_repository=SQLiteSnapshotRepository(database_path),
        retention_policy_repository=SQLiteRetentionPolicyRepository(database_path),
        target_stats_repository=SQLiteTargetStatsRepository(database_path),
        secret_codec=codec,
        settings_repository=SQLiteSettingsRepository(database_path),
    )
    service.worker_auth = worker_auth
    return service

@dataclass
class ControlPlaneApplication:
    auth_service: AuthService
    control_plane_service: ControlPlaneService
    tls_manager: TLSMaterialManager | None = None
    scheduler: "SchedulerService | None" = None
    live_file_service: LiveFileService | None = None
    live_lane: "LiveWorkerLane | None" = None


class ControlPlaneHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, application: ControlPlaneApplication):
        super().__init__(server_address, RequestHandlerClass)
        self.application = application


class ControlPlaneHTTPSServer((NativeThreadingHTTPSServer or ThreadingHTTPServer)):
    def __init__(self, server_address, RequestHandlerClass, application: ControlPlaneApplication, tls_manager: TLSMaterialManager):
        if NativeThreadingHTTPSServer is not None:
            super().__init__(
                server_address,
                RequestHandlerClass,
                certfile=str(tls_manager.server_cert_path),
                keyfile=str(tls_manager.server_key_path),
            )
            self.application = application
            self.socket.context.load_verify_locations(cafile=tls_manager.get_ca_certificate_path())
            self.socket.context.verify_mode = ssl.CERT_NONE
            return

        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)
        self.application = application
        context = tls_manager.build_server_ssl_context()
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.server_bind()
        self.server_activate()


class LiveWorkerLane:
    def __init__(self, max_pending=64):
        self.max_pending, self._lock, self._pending, self._operations = max_pending, threading.Condition(), {}, {}
    def submit(self, worker_id, payload, stream=None):
        with self._lock:
            if sum(map(len, self._pending.values())) >= self.max_pending: raise LiveLimitError("live request queue limit reached")
            operation_id = uuid4().hex; self._pending.setdefault(worker_id, deque()).append({**payload, "operation_id": operation_id})
            self._operations[operation_id] = {"worker_id": worker_id, "target_id": payload.get("target_id"), "result": None, "started": threading.Event(), "stream": stream}; return operation_id
    def poll(self, worker_id, limit=4):
        with self._lock:
            pending = self._pending.get(worker_id, deque()); items = [pending.popleft() for _ in range(min(max(1, int(limit)), 16)) if pending]
            if not pending: self._pending.pop(worker_id, None)
            return items
    def respond(self, worker_id, operation_id, result):
        with self._lock:
            operation = self._operations.get(operation_id)
            if not operation or operation["worker_id"] != worker_id: raise LiveSessionError("live operation is unavailable")
            if not isinstance(result, dict) or len(json.dumps(result, default=str)) > 1024 * 1024: raise LiveLimitError("live response exceeds the permitted bound")
            operation["result"] = result; operation["started"].set()
            if operation["stream"] is not None and int(result.get("status", 500)) != 200: operation["stream"].close()
    def wait(self, operation_id, timeout=10.0):
        operation = self._operations.get(operation_id)
        if not operation or not operation["started"].wait(timeout): self.cancel(operation_id); return None
        return operation["result"]
    def chunk(self, worker_id, operation_id, data, final=False):
        operation = self._operations.get(operation_id)
        if not operation or operation["worker_id"] != worker_id or operation["stream"] is None: raise LiveSessionError("live stream is unavailable")
        if data: operation["stream"].push(data)
        if final: operation["stream"].close()
    def cancel(self, operation_id):
        with self._lock:
            operation = self._operations.pop(operation_id, None)
            if operation and operation["stream"] is not None: operation["stream"].cancel()
            for worker_id, pending in list(self._pending.items()):
                self._pending[worker_id] = deque(item for item in pending if item.get("operation_id") != operation_id)
                if not self._pending[worker_id]: self._pending.pop(worker_id, None)
    def finish(self, operation_id):
        with self._lock:
            operation = self._operations.pop(operation_id, None)
            if operation and operation["stream"] is not None: operation["stream"].close()
    def cancel_target(self, target_id):
        for operation_id, operation in list(self._operations.items()):
            if operation.get("target_id") == target_id: self.cancel(operation_id)


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server_version = "docker-volume-backup-control-plane/0.1"

    def _auth_service(self) -> AuthService:
        return self.server.application.auth_service

    def _control_plane_service(self) -> ControlPlaneService:
        return self.server.application.control_plane_service

    def _scheduler(self) -> SchedulerService | None:
        server = getattr(self, "server", None)
        application = getattr(server, "application", None)
        return getattr(application, "scheduler", None)

    def _target_jsonable(self, target):
        payload = _to_jsonable(target)
        service = self._control_plane_service()
        worker = service.worker_repository.get(target.worker_id)
        if worker is None:
            worker_name = None
            worker_status = "missing"
            blocked_reason = "worker_missing"
        else:
            worker_name = worker.name
            worker_status = service._worker_status(worker)
            if worker_status == "disabled":
                blocked_reason = "worker_revoked"
            elif worker_status != "online":
                blocked_reason = "worker_offline"
            else:
                blocked_reason = "target_disabled" if not target.enabled else None
        payload.update(
            {
                "worker_name": worker_name,
                "worker_status": worker_status,
                "execution_blocked": blocked_reason not in (None, "target_disabled"),
                "blocked_reason": blocked_reason,
                "scheduling_disabled": not bool(target.enabled),
            }
        )
        scheduler = self._scheduler()
        if scheduler is not None:
            payload.update(scheduler.preview(target=target))
        return payload

    def _worker_auth(self) -> WorkerAuthState:
        return self._control_plane_service().worker_auth

    def do_GET(self):
        self._handle_get_request(head_only=False)

    def do_HEAD(self):
        self._handle_get_request(head_only=True)

    def _handle_get_request(self, head_only: bool):
        path = urlparse(self.path).path
        parts = self._path_parts(path)
        try:
            if path in ("/login", "/login/"):
                return self._write_file(UI_ROOT / "login.html", "text/html; charset=utf-8", head_only=head_only)
            if path in ("/change-password", "/change-password/"):
                session = self._current_session()
                if session is None:
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not session.get("must_change_password"):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._write_file(UI_ROOT / "change-password.html", "text/html; charset=utf-8", head_only=head_only)
            if path in ("/", "/ui", "/ui/"):
                if path in ("/ui", "/ui/"):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self._require_auth(ROLE_VIEWER, head_only=head_only):
                    return
                return self._write_file(UI_ROOT / "index.html", "text/html; charset=utf-8", head_only=head_only)
            if path.startswith("/styles/"):
                return self._serve_static_file(path, "text/css; charset=utf-8", head_only=head_only)
            if path == "/favicon.ico":
                return self._serve_static_file("/icon.png", "image/png", head_only=head_only)
            if path == "/healthz":
                return self._write_json(200, {"ok": True}, head_only=head_only)
            if path == "/api/v1/version":
                return self._write_json(200, {"version": os.environ.get("APP_VERSION", "dev")}, head_only=head_only)
            if path == "/api/v1/version/latest":
                import urllib.request, json as _json
                try:
                    req = urllib.request.Request(
                        "https://api.github.com/repos/DanielRondonGarcia/docker-volume-backup/releases/latest",
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "docker-volume-backup-control-plane"},
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = _json.loads(resp.read().decode("utf-8"))
                    return self._write_json(200, {"tag_name": data.get("tag_name", ""), "html_url": data.get("html_url", "")}, head_only=head_only)
                except Exception as e:
                    return self._write_json(200, {"tag_name": "", "html_url": "", "error": str(e)}, head_only=head_only)
            if path == "/api/v1/config/public":
                public_url = ""
                try:
                    settings = self._control_plane_service().get_settings()
                    if settings and settings.control_plane_public_url:
                        public_url = settings.control_plane_public_url.strip()
                except Exception:
                    pass
                if not public_url:
                    public_url = os.environ.get("CONTROL_PLANE_PUBLIC_URL", "").strip()
                tls_enabled = os.environ.get("CONTROL_PLANE_TLS_ENABLED", "").strip().lower() in ("1", "true", "yes")
                scheduler = self._scheduler()
                return self._write_json(
                    200,
                    {
                        "public_url": public_url,
                        "tls_enabled": tls_enabled,
                        "scheduler_timezone": scheduler.timezone_name if scheduler else DEFAULT_SCHEDULER_TIMEZONE,
                    },
                    head_only=head_only,
                )
            if path == "/api/v1/scheduler/preview":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                scheduler = self._scheduler()
                if scheduler is None:
                    return self._write_json(503, {"error": "scheduler not configured"}, head_only=head_only)
                parsed = urlparse(self.path)
                from urllib.parse import parse_qs
                qs = parse_qs(parsed.query, keep_blank_values=True)
                target_id = qs.get("target_id", [""])[0].strip() or None
                target = self._control_plane_service().target_repository.get(target_id) if target_id else None
                if target_id and target is None:
                    return self._write_json(404, {"error": "target not found"}, head_only=head_only)
                cron_expression = qs.get("cron_expression", [None])[0]
                target_context = qs.get("target_context", [""])[0].strip().lower() in ("1", "true", "yes")
                return self._write_json(
                    200,
                    scheduler.preview(target=target, cron_expression=cron_expression, target_context=target_context),
                    head_only=head_only,
                )
            if path == "/api/v1/auth/me":
                session = self._current_session()
                if not session:
                    self._write_json(401, {"error": "authentication required"}, head_only=head_only)
                    return
                return self._write_json(
                    200,
                    {
                        "username": session["username"],
                        "role": session["role"],
                        "must_change_password": bool(session.get("must_change_password")),
                    },
                    head_only=head_only,
                )
            if len(parts) == 6 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "live":
                if parts[5] == "events" and not head_only:
                    return self._stream_live_events(parts[3])
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                if parts[5] == "entries":
                    return self._live_entries(parts[3], head_only=head_only)
                if parts[5] == "file" and not head_only:
                    return self._live_file(parts[3])
                return self._write_json(405, {"error": "live operation is not supported"}, head_only=head_only)
            if len(parts) == 5 and parts[:3] == ["api", "v2", "targets"] and parts[4] == "snapshots":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    {
                        "schema_version": 1,
                        "request_id": str(uuid4()),
                        "job_id": None,
                        "status": "succeeded",
                        "source": "metadata",
                        "cache_hit": True,
                        "entries": self._control_plane_service().snapshot_catalog(parts[3]),
                        "b64_content": "",
                        "error": None,
                    },
                    head_only=head_only,
                )
            if len(parts) == 4 and parts[:3] == ["api", "v2", "jobs"]:
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                try:
                    result = self._control_plane_service().snapshot_job_contract(parts[3])
                except ValueError:
                    return self._write_json(404, {"error": "job not found"}, head_only=head_only)
                return self._write_json(200, result, head_only=head_only)
            if path == "/api/v1/workers":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_workers())}, head_only=head_only)
            if path == "/api/v1/jobs":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                parsed = urlparse(self.path)
                from urllib.parse import parse_qs
                qs = parse_qs(parsed.query)
                limit_str = qs.get("limit", [None])[0]
                offset_str = qs.get("offset", ["0"])[0]
                try:
                    limit = int(limit_str) if limit_str else None
                    offset = int(offset_str) if offset_str else 0
                except ValueError:
                    limit, offset = None, 0
                jobs, total = self._control_plane_service().list_job_views(limit=limit, offset=offset)
                return self._write_json(200, {"items": _to_jsonable(jobs), "total": total, "limit": limit, "offset": offset}, head_only=head_only)
            if path == "/api/v1/targets":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    {"items": [self._target_jsonable(target) for target in self._control_plane_service().list_targets()]},
                    head_only=head_only,
                )
            if path == "/api/v1/storage-profiles":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_storage_profiles())}, head_only=head_only)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "storage-profiles"] and parts[4] == "about":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    self._control_plane_service().storage_about(parts[3]),
                    head_only=head_only,
                )
            if path == "/api/v1/retention-policies":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_retention_policies())}, head_only=head_only)
            if path == "/api/v1/secrets":
                if not self._require_auth(ROLE_ADMIN, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    {"items": [_secret_to_public(item) for item in self._control_plane_service().list_secrets()]},
                    head_only=head_only,
                )
            if path == "/api/v1/settings":
                if not self._require_auth(ROLE_ADMIN, head_only=head_only, api_mode=True):
                    return
                settings = self._control_plane_service().get_settings()
                payload = _to_jsonable(settings or {})
                scheduler = self._scheduler()
                payload["scheduler_timezone"] = scheduler.timezone_name if scheduler else DEFAULT_SCHEDULER_TIMEZONE
                return self._write_json(200, payload, head_only=head_only)
            if path == "/api/v1/settings/rclone-remote":
                if not self._require_auth(ROLE_ADMIN, head_only=head_only, api_mode=True):
                    return
                svc = self._control_plane_service()
                settings = svc.get_settings()
                remote_name = None
                if settings and settings.rclone_conf_secret_id:
                    remote_name = svc._extract_rclone_remote_name(settings.rclone_conf_secret_id)
                return self._write_json(200, {"remote_name": remote_name}, head_only=head_only)
            if path == "/api/v1/admin/users":
                if not self._require_auth(ROLE_ADMIN, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    {
                        "items": self._auth_service().list_users_public(),
                        "management": self._auth_service().auth_management_summary(),
                    },
                    head_only=head_only,
                )
            parts = self._path_parts(path)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"] and parts[4] == "logs":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                job_view = self._control_plane_service().get_job_view(parts[3])
                if job_view is None:
                    return self._write_json(404, {"error": "job not found"}, head_only=head_only)
                return self._write_json(200, _to_jsonable(job_view), head_only=head_only)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"] and parts[4] == "events" and not head_only:
                return self._stream_job_events(parts[3])
            if len(parts) == 4 and parts[:3] == ["api", "v1", "jobs"]:
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                job = self._control_plane_service().get_job(parts[3])
                if job is None:
                    return self._write_json(404, {"error": "job not found"}, head_only=head_only)
                return self._write_json(200, _to_jsonable(self._control_plane_service().get_job_view(parts[3])), head_only=head_only)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "inventory":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                snapshot = self._control_plane_service().get_inventory(parts[3])
                if snapshot is None:
                    return self._write_json(404, {"error": "inventory not found"}, head_only=head_only)
                return self._write_json(200, _to_jsonable(snapshot), head_only=head_only)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "snapshots":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    {"items": _to_jsonable(self._control_plane_service().list_snapshots(parts[3]))},
                    head_only=head_only,
                )
            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "stats":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                stats_record = self._control_plane_service().get_target_stats(parts[3])
                if stats_record is None:
                    return self._write_json(404, {"error": "stats not found"}, head_only=head_only)
                return self._write_json(200, _to_jsonable(stats_record), head_only=head_only)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "validate":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, self._control_plane_service().validate_target(parts[3]), head_only=head_only)
            return self._write_json(404, {"error": "not found"}, head_only=head_only)
        except ValueError as exc:
            return self._write_json(404, {"error": str(exc)}, head_only=head_only)
        except Exception as exc:
            logger.exception("Unhandled GET error")
            return self._write_json(500, {"error": str(exc)}, head_only=head_only)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = self._path_parts(path)
        if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4:] == ["live", "chunk"]:
            try:
                return self._handle_live_chunk(parts[3], self._read_raw_body(64 * 1024))
            except ValueError:
                return self._write_json(413, {"error": "live chunk exceeds the permitted bound"})
        body = self._read_json_body()
        try:
            if path in ("/api/v1/workers/register", "/api/v1/worker-enrollments/sign"):
                return self._write_json(404, {"error": "legacy worker trust path is unsupported"})
            if path == "/api/v1/auth/login":
                username = body.get("username", "")
                password = body.get("password", "")
                user = self._auth_service().authenticate(username, password)
                if user is None:
                    return self._write_json(401, {"error": "invalid credentials"})
                token = self._auth_service().issue_session_token(user)
                return self._write_json(
                    200,
                    {
                        "ok": True,
                        "username": user.username,
                        "role": user.role,
                        "must_change_password": user.must_change_password,
                    },
                    extra_headers={"Set-Cookie": self._build_session_cookie(token)},
                )
            if path == "/api/v1/auth/change-password":
                session = self._current_session()
                if session is None:
                    return self._write_json(401, {"error": "authentication required"})
                updated_user = self._auth_service().change_password(
                    username=session["username"],
                    current_password=body.get("current_password", ""),
                    new_password=body.get("new_password", ""),
                )
                token = self._auth_service().issue_session_token(updated_user)
                return self._write_json(
                    200,
                    {
                        "ok": True,
                        "username": updated_user.username,
                        "role": updated_user.role,
                        "must_change_password": updated_user.must_change_password,
                    },
                    extra_headers={"Set-Cookie": self._build_session_cookie(token)},
                )
            if path == "/api/v1/auth/logout":
                return self._write_json(
                    200,
                    {"ok": True},
                    extra_headers={"Set-Cookie": self._build_session_cookie("", max_age=0)},
                )
            if path == "/api/v1/admin/users":
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                user = self._auth_service().create_user(
                    username=body.get("username", ""),
                    password=body.get("password", ""),
                    role=body.get("role", ROLE_VIEWER),
                    must_change_password=bool(body.get("must_change_password", True)),
                )
                return self._write_json(
                    201,
                    {
                        "ok": True,
                        "user": {
                            "username": user.username,
                            "role": user.role,
                            "must_change_password": user.must_change_password,
                            "password_scheme": user.password_scheme,
                        },
                    },
                )
            if path == "/api/v1/admin/worker-enrollments":
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                enrollment = self._worker_auth().create_enrollment(
                    name=body.get("name") or "worker",
                    host_name=body.get("host_name") or "unknown-host",
                    labels=body.get("labels") or {},
                    secret=body.get("secret"),
                    worker_id=body.get("worker_id"),
                    ttl_minutes=int(body.get("ttl_minutes") or 30),
                )
                return self._write_json(201, _to_jsonable(enrollment))
            if path == "/api/v1/worker-enrollments/complete":
                result = self._worker_auth().complete(body.get("secret", ""), body.get("version") or "1", body.get("labels"))
                worker = self._control_plane_service().register_worker(
                    name=result["name"], host_name=result["host_name"], labels=result["labels"], worker_id=result["worker_id"]
                )
                return self._write_json(201, {"worker_id": worker.id, "credential_version": result["credential_version"]})
            if len(parts) == 5 and parts[:3] == ["api", "v2", "targets"] and parts[4] == "about":
                if not self._require_auth(ROLE_VIEWER, api_mode=True):
                    return
                if not isinstance(body, dict):
                    raise ValueError("request body must be an object")
                unsupported_fields = set(body) - {"snapshot_id", "request_id"}
                if unsupported_fields:
                    raise ValueError("unsupported snapshot about fields")
                result = self._control_plane_service().dispatch_snapshot_about(
                    target_id=parts[3],
                    snapshot_id=body.get("snapshot_id"),
                    request_id=body.get("request_id"),
                    requested_by="api",
                )
                return self._write_json(202, result)
            if len(parts) == 5 and parts[:3] == ["api", "v2", "targets"] and parts[4] in {"browse", "search", "dump"}:
                if not self._require_auth(ROLE_VIEWER, api_mode=True):
                    return
                result = self._control_plane_service().dispatch_snapshot_read(
                    target_id=parts[3],
                    operation=parts[4],
                    snapshot_id=body.get("snapshot_id"),
                    path=body.get("path", ""),
                    request_id=body.get("request_id"),
                    max_entries=body.get("max_entries"),
                    query=body.get("query"),
                    archive=body.get("archive") == "zip"
                    or body.get("archive", body.get("format") == "zip" or body.get("zip", False)),
                    max_output_bytes=body.get("max_output_bytes"),
                    requested_by="api",
                )
                return self._write_json(202, result)
            if len(parts) == 5 and parts[:3] == ["api", "v2", "jobs"] and parts[4] == "cancel":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                try:
                    job = self._control_plane_service().cancel_job(parts[3])
                except ValueError as exc:
                    return self._write_json(409, {"error": str(exc)})
                return self._write_json(200, self._control_plane_service().snapshot_job_contract(job.id))
            parts = self._path_parts(path)
            if len(parts) == 6 and parts[:4] == ["api", "v1", "admin", "workers"]:
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                if parts[5] == "enrollment":
                    enrollment = self._control_plane_service().create_worker_enrollment(
                        worker_id=parts[4],
                        secret=body.get("secret"),
                        ttl_minutes=int(body.get("ttl_minutes") or 30),
                    )
                    return self._write_json(201, _to_jsonable(enrollment))
                if parts[5] == "rotate":
                    self._control_plane_service().get_worker(parts[4])
                    return self._write_json(200, self._worker_auth().rotate(parts[4], body.get("secret", "")))
                if parts[5] == "revoke":
                    return self._write_json(
                        200,
                        self._control_plane_service().revoke_worker(parts[4], body.get("version")),
                    )

            if path == "/api/v1/targets":
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                target = self._control_plane_service().register_target(
                    name=body.get("name") or "target",
                    worker_id=body["worker_id"],
                    compose_project=body.get("compose_project"),
                    volume_targets=body.get("volume_targets"),
                    backup_mode=body.get("backup_mode") or "hot",
                    backup_strategy=body.get("backup_strategy") or "restic",
                    runtime_image=body.get("runtime_image"),
                    runtime_command=body.get("runtime_command"),
                    runtime_environment=body.get("runtime_environment") or {},
                    runtime_volumes=body.get("runtime_volumes"),
                    runtime_network_mode=body.get("runtime_network_mode"),
                    storage_profile_id=body.get("storage_profile_id"),
                    path_storage=body.get("path_storage"),
                    retention_policy_id=body.get("retention_policy_id"),
                    restic_password_secret_id=body.get("restic_password_secret_id"),
                    restore_defaults=body.get("restore_defaults") or {},
                    labels=body.get("labels") or {},
                    cron_expression=body.get("cron_expression"),
                    live_access_enabled=body.get("live_access_enabled", False),
                    volume_sources=body.get("volume_sources"),
                )
                return self._write_json(201, self._target_jsonable(target))

            if path == "/api/v1/storage-profiles":
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                profile = self._control_plane_service().create_storage_profile(
                    name=body.get("name") or "profile",
                    backend_type=body["backend_type"],
                    environment=body.get("environment") or {},
                    secret_refs=body.get("secret_refs") or {},
                    file_secret_refs=body.get("file_secret_refs") or {},
                    runtime_volumes=body.get("runtime_volumes") or {},
                    labels=body.get("labels") or {},
                )
                return self._write_json(201, _to_jsonable(profile))

            if path == "/api/v1/retention-policies":
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                policy = self._control_plane_service().create_retention_policy(
                    name=body.get("name") or "policy",
                    keep_last=body.get("keep_last"),
                    keep_hourly=body.get("keep_hourly"),
                    keep_daily=body.get("keep_daily"),
                    keep_weekly=body.get("keep_weekly"),
                    keep_monthly=body.get("keep_monthly"),
                    keep_yearly=body.get("keep_yearly"),
                    prune=bool(body.get("prune", True)),
                    labels=body.get("labels") or {},
                )
                return self._write_json(201, _to_jsonable(policy))

            if path == "/api/v1/secrets":
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                secret = self._control_plane_service().create_secret(
                    name=body.get("name") or "secret",
                    scope=body.get("scope") or "global",
                    secret_type=body.get("secret_type") or "generic",
                    plaintext=body["plaintext"],
                    metadata=body.get("metadata") or {},
                )
                return self._write_json(201, _secret_to_public(secret))

            parts = self._path_parts(path)
            if (
                len(parts) == 6
                and parts[:4] == ["api", "v1", "admin", "users"]
                and parts[5] == "reset-password"
            ):
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                user = self._auth_service().admin_reset_password(
                    username=parts[4],
                    new_password=body.get("new_password", ""),
                    must_change_password=bool(body.get("must_change_password", True)),
                    role=body.get("role"),
                )
                return self._write_json(
                    200,
                    {
                        "ok": True,
                        "user": {
                            "username": user.username,
                            "role": user.role,
                            "must_change_password": user.must_change_password,
                            "password_scheme": user.password_scheme,
                        },
                    },
                )
            if (
                len(parts) == 6
                and parts[:4] == ["api", "v1", "admin", "users"]
                and parts[5] == "update"
            ):
                if not self._require_auth(ROLE_ADMIN, api_mode=True):
                    return
                user = self._auth_service().update_user(
                    username=parts[4],
                    role=body.get("role"),
                    must_change_password=body.get("must_change_password"),
                )
                return self._write_json(
                    200,
                    {
                        "ok": True,
                        "user": {
                            "username": user.username,
                            "role": user.role,
                            "must_change_password": user.must_change_password,
                            "password_scheme": user.password_scheme,
                        },
                    },
                )
            if len(parts) == 5 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "heartbeat":
                if not self._require_worker_identity(parts[3]):
                    return
                worker = self._control_plane_service().heartbeat(
                    worker_id=parts[3],
                    version=body.get("version"),
                    labels=body.get("labels"),
                )
                return self._write_json(200, _to_jsonable(worker))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "inventory":
                if not self._require_worker_identity(parts[3]):
                    return
                snapshot = self._control_plane_service().sync_inventory(parts[3], body.get("inventory") or {})
                return self._write_json(200, _to_jsonable(snapshot))

            if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4:] == ["live", "poll"]:
                if not self._require_worker_identity(parts[3]):
                    return
                lane = self._live_lane()
                if lane is None:
                    return self._write_json(503, {"error": "live worker lane unavailable"})
                return self._write_json(200, {"items": lane.poll(parts[3], body.get("limit", 4))})

            if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4:] == ["live", "response"]:
                if not self._require_worker_identity(parts[3]):
                    return
                lane = self._live_lane()
                if lane is None:
                    return self._write_json(503, {"error": "live worker lane unavailable"})
                lane.respond(parts[3], body.get("operation_id", ""), body.get("result") or {})
                return self._write_json(202, {"ok": True})

            if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4:] == ["live", "change"]:
                if not self._require_worker_identity(parts[3]):
                    return
                try:
                    if not isinstance(body, dict) or body.get("worker_id") != parts[3]:
                        raise ValueError("live change worker identity is invalid")
                    target_id = body.get("target_id")
                    live_service, key, context = self._live_context(target_id)
                    if body.get("config_revision") != context["config_revision"] or key.worker_id != parts[3]:
                        raise ValueError("live change revision is stale")
                    kind, entry_type = body.get("kind"), body.get("entry_type")
                    if kind not in {"created", "modified", "deleted", "resync_required"} or entry_type not in {"file", "dir"}:
                        raise ValueError("live change metadata is invalid")
                    path = self._normalize_live_path(body.get("path"))
                    size, mtime_ns = body.get("size"), body.get("mtime_ns")
                    if any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1) for value in (size, mtime_ns)):
                        raise ValueError("live change metadata is invalid")
                    event = live_service.publish_change(key, kind, path, entry_type, size, mtime_ns)
                    return self._write_json(202, {"ok": True, "event_id": event.event_id})
                except LiveLimitError:
                    return self._write_json(429, {"error": "live resource limit reached"})
                except (LiveSessionError, ValueError, TypeError):
                    return self._write_json(409, {"error": "live change rejected"})

            if len(parts) == 5 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "jobs":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_job(
                    worker_id=parts[3],
                    command=body["command"],
                    payload=body.get("payload") or {},
                    requested_by=body.get("requested_by") or "api",
                    target_id=body.get("target_id"),
                    trigger=body.get("trigger") or "manual",
                )
                return self._write_json(202, self._control_plane_service().public_job_view(job))

            if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "jobs" and parts[5] == "fetch":
                if not self._require_worker_identity(parts[3]):
                    return
                jobs = self._control_plane_service().fetch_jobs_for_worker(parts[3])
                return self._write_json(200, {"items": _to_jsonable(jobs)})

            if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "jobs" and parts[5] == "fetch-interactive":
                if not self._require_worker_identity(parts[3]):
                    return
                jobs = self._control_plane_service().fetch_interactive_jobs_for_worker(parts[3])
                return self._write_json(200, {"items": _to_jsonable(jobs)})

            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "workers"]
                and parts[4] == "jobs"
                and parts[6] == "renew-lease"
            ):
                if not self._require_worker_identity(parts[3]):
                    return
                job = self._control_plane_service().renew_job_lease(
                    worker_id=parts[3],
                    job_id=parts[5],
                    lease_token=body.get("lease_token"),
                )
                return self._write_json(200, _to_jsonable(job))

            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "workers"]
                and parts[4] == "jobs"
                and parts[6] == "cancel-status"
            ):
                if not self._require_worker_identity(parts[3]):
                    return
                canceled = self._control_plane_service().is_job_cancelled(parts[3], parts[5])
                return self._write_json(200, {"canceled": bool(canceled)})

            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "workers"]
                and parts[4] == "jobs"
                and parts[6] == "status"
            ):
                if not self._require_worker_identity(parts[3]):
                    return
                job = self._control_plane_service().update_job_status(
                    worker_id=parts[3],
                    job_id=parts[5],
                    status=body["status"],
                    result_summary=body.get("result_summary"),
                    log_lines=body.get("log_lines"),
                    lease_token=body.get("lease_token"),
                )
                return self._write_json(200, _to_jsonable(job))

            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "workers"]
                and parts[4] == "jobs"
                and parts[6] == "progress"
            ):
                if not self._require_worker_identity(parts[3]):
                    return
                job = self._control_plane_service().update_job_progress(
                    worker_id=parts[3],
                    job_id=parts[5],
                    sequence=body.get("sequence"),
                    progress=body.get("progress"),
                    log_lines=body.get("log_lines"),
                    lease_token=body.get("lease_token"),
                )
                return self._write_json(200, _to_jsonable(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "backup":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_backup_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                    backup_mode=body.get("backup_mode"),
                )
                return self._write_json(202, self._control_plane_service().public_job_view(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"] and parts[4] == "cancel":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                try:
                    job = self._control_plane_service().cancel_job(parts[3])
                except ValueError as exc:
                    return self._write_json(409, {"error": str(exc)})
                return self._write_json(200, self._control_plane_service().public_job_view(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "snapshots-sync":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_snapshot_sync_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                )
                return self._write_json(202, self._control_plane_service().public_job_view(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "snapshot-ls":
                if not self._require_auth(ROLE_VIEWER, api_mode=True):
                    return
                result = self._control_plane_service().dispatch_snapshot_ls(
                    parts[3],
                    snapshot_id=body.get("snapshot_id", ""),
                    path=body.get("path", ""),
                )
                return self._write_json(200, result)

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "snapshot-dump":
                if not self._require_auth(ROLE_VIEWER, api_mode=True):
                    return
                result = self._control_plane_service().dispatch_snapshot_dump(
                    parts[3],
                    snapshot_id=body.get("snapshot_id", ""),
                    path=body.get("path", ""),
                )
                return self._write_json(200, result)

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "stats-sync":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_stats_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                )
                return self._write_json(202, self._control_plane_service().public_job_view(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "retention-run":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_retention_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                )
                return self._write_json(202, self._control_plane_service().public_job_view(job))

            if len(parts) == 6 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "restore":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_restore_for_target(
                    target_id=parts[3],
                    requested_by=body.get("requested_by") or "api",
                    restore_source=body.get("restore_source"),
                    restore_target_path=body.get("restore_target_path"),
                    dry_run=parts[5] == "dry-run",
                    force_overwrite=bool(body.get("force_overwrite", False)),
                    stop_containers=body.get("stop_containers"),
                    chown=body.get("chown"),
                    layout=body.get("layout"),
                    snapshot_id=body.get("snapshot_id"),
                )
                return self._write_json(202, self._control_plane_service().public_job_view(job))

            return self._write_json(404, {"error": "not found"})
        except KeyError as exc:
            return self._write_json(400, {"error": f"missing field: {exc.args[0]}"})
        except ValueError as exc:
            return self._write_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self._write_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled POST error")
            return self._write_json(500, {"error": str(exc)})

    def do_PATCH(self):
        try:
            if not self._require_auth(ROLE_ADMIN, api_mode=True):
                return
            path = urlparse(self.path).path
            body = self._read_json_body() or {}
            parts = self._path_parts(path)
            if len(parts) == 4 and parts[:3] == ["api", "v1", "targets"]:
                target_update = dict(
                    target_id=parts[3],
                    name=body.get("name"),
                    worker_id=body.get("worker_id"),
                    compose_project=body.get("compose_project"),
                    volume_targets=body.get("volume_targets"),
                    backup_mode=body.get("backup_mode"),
                    backup_strategy=body.get("backup_strategy"),
                    runtime_image=body.get("runtime_image"),
                    runtime_command=body.get("runtime_command"),
                    runtime_environment=body.get("runtime_environment"),
                    storage_profile_id=body.get("storage_profile_id"),
                    retention_policy_id=body.get("retention_policy_id"),
                    restic_password_secret_id=body.get("restic_password_secret_id"),
                    restore_defaults=body.get("restore_defaults"),
                    labels=body.get("labels"),
                    cron_expression=body.get("cron_expression"),
                    enabled=body.get("enabled"),
                    live_access_enabled=body.get("live_access_enabled"),
                )
                if "path_storage" in body:
                    target_update["path_storage"] = body.get("path_storage")
                target = self._control_plane_service().update_target(**target_update)
                if self._live_service() is not None:
                    self._live_service().invalidate_target(parts[3], "configuration_changed")
                if self._live_lane() is not None:
                    self._live_lane().cancel_target(parts[3])
                return self._write_json(200, self._target_jsonable(target))
            if path == "/api/v1/settings":
                settings = self._control_plane_service().update_settings(
                    restic_repository_base=body.get("restic_repository_base"),
                    restic_password_secret_id=body.get("restic_password_secret_id"),
                    rclone_conf_secret_id=body.get("rclone_conf_secret_id"),
                    global_cron_expression=body.get("global_cron_expression"),
                    control_plane_public_url=body.get("control_plane_public_url"),
                    snapshot_explorer_listing_max_output_bytes=body.get(
                        "snapshot_explorer_listing_max_output_bytes"
                    ),
                )
                return self._write_json(200, _to_jsonable(settings))
            if len(parts) == 4 and parts[:3] == ["api", "v1", "secrets"]:
                secret = self._control_plane_service().update_secret(
                    secret_id=parts[3],
                    plaintext=body.get("plaintext"),
                    name=body.get("name"),
                )
                return self._write_json(200, _secret_to_public(secret))
            if len(parts) == 4 and parts[:3] == ["api", "v1", "workers"]:
                worker = self._control_plane_service().update_worker(
                    worker_id=parts[3],
                    labels=body.get("labels"),
                )
                return self._write_json(200, _to_jsonable(worker))
            if len(parts) == 4 and parts[:3] == ["api", "v1", "storage-profiles"]:
                profile = self._control_plane_service().update_storage_profile(
                    profile_id=parts[3],
                    name=body.get("name"),
                    backend_type=body.get("backend_type"),
                    environment=body.get("environment"),
                    secret_refs=body.get("secret_refs"),
                    file_secret_refs=body.get("file_secret_refs"),
                    runtime_volumes=body.get("runtime_volumes"),
                    labels=body.get("labels"),
                )
                return self._write_json(200, _to_jsonable(profile))
            if len(parts) == 5 and parts[:3] == ["api", "v1", "secrets"] and parts[4] == "usages":
                usages = self._control_plane_service().find_secret_usages(parts[3])
                return self._write_json(200, {"items": usages})
            return self._write_json(404, {"error": "not found"})
        except ValueError as exc:
            return self._write_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled PATCH error")
            return self._write_json(500, {"error": str(exc)})

    def do_DELETE(self):
        try:
            if not self._require_auth(ROLE_ADMIN, api_mode=True):
                return
            path = urlparse(self.path).path
            parts = self._path_parts(path)
            if len(parts) == 4 and parts[:3] == ["api", "v1", "targets"]:
                deleted = self._control_plane_service().delete_target(parts[3])
                if not deleted:
                    return self._write_json(404, {"error": "target not found"})
                if self._live_service() is not None:
                    self._live_service().invalidate_target(parts[3], "target_deleted")
                if self._live_lane() is not None:
                    self._live_lane().cancel_target(parts[3])
                return self._write_json(204, {})
            if len(parts) == 4 and parts[:3] == ["api", "v1", "secrets"]:
                self._control_plane_service().delete_secret(parts[3])
                return self._write_json(204, {})
            if len(parts) == 4 and parts[:3] == ["api", "v1", "storage-profiles"]:
                deleted = self._control_plane_service().delete_storage_profile(parts[3])
                if not deleted:
                    return self._write_json(404, {"error": "storage profile not found"})
                return self._write_json(204, {})
            if len(parts) == 4 and parts[:3] == ["api", "v1", "workers"]:
                deleted = self._control_plane_service().delete_worker(parts[3])
                if not deleted:
                    return self._write_json(404, {"error": "worker not found"})
                return self._write_json(204, {})
            return self._write_json(404, {"error": "not found"})
        except WorkerDeletionConflict as exc:
            return self._write_json(409, {"error": str(exc), "code": "worker_deletion_conflict"})
        except ValueError as exc:
            return self._write_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled DELETE error")
            return self._write_json(500, {"error": str(exc)})

    def _live_service(self):
        return getattr(self.server.application, "live_file_service", None)
    def _live_lane(self):
        return getattr(self.server.application, "live_lane", None)
    @staticmethod
    def _normalize_live_path(value: str) -> str:
        if value in (None, "", "/"): return "/"
        if not isinstance(value, str) or len(value) > 4096 or not value.startswith("/") or "\\" in value or "\x00" in value: raise ValueError("live path is invalid")
        parts = value.split("/")[1:]
        if any(not part or part in (".", "..") for part in parts): raise ValueError("live path traversal is not allowed")
        return "/" + "/".join(parts)

    @staticmethod
    def _raise_live_worker_error(path: str, result: dict, operation="unknown", target_id="unknown"):
        reason = result.get("reason") if isinstance(result, dict) else None
        code = result.get("code") if isinstance(result, dict) else None
        if reason not in LIVE_WORKER_FAILURE_REASONS:
            raise LiveSessionError("live worker request failed")
        expected_code = "live_access_denied" if reason == "protected_volume" else (
            "live_request_rejected" if reason in {"invalid_source", "invalid_request"} else "live_worker_unavailable"
        )
        if code != expected_code:
            raise LiveSessionError("live worker request failed")
        if reason == "protected_volume":
            raise LiveAccessDeniedError(path)
        status = 400 if reason in {"invalid_source", "invalid_request"} else 503
        raise LiveWorkerError(status, code, reason, path)

    @staticmethod
    def _live_access_denied_payload(path: str):
        return {
            "error": "live access denied",
            "code": "live_access_denied",
            "reason": "protected_volume",
            "path": path,
        }

    @staticmethod
    def _live_worker_error_payload(error: LiveWorkerError):
        if isinstance(error, LiveAccessDeniedError):
            return ControlPlaneRequestHandler._live_access_denied_payload(error.path)
        return {
            "error": LIVE_WORKER_FAILURE_MESSAGES.get(error.reason, "live worker operation unavailable"),
            "code": error.code,
            "reason": error.reason,
        }

    @staticmethod
    def _log_live_worker_error(target_id: str, operation: str, error: LiveWorkerError):
        logger.warning(
            "Live operation failed (operation=%s target_id=%s status=%s code=%s reason=%s)",
            operation if operation in {"watch", "unwatch", "entries", "file"} else "unknown",
            _safe_live_log_value(target_id),
            error.status,
            error.code,
            error.reason,
        )

    def _live_context(self, target_id: str):
        try:
            context = self._control_plane_service().get_live_target_context(target_id)
        except ValueError:
            raise LiveSessionError("live access unavailable") from None
        live_service = self._live_service()
        if not context.get("eligible"): raise LiveSessionError("live access unavailable")
        if live_service is None: raise LiveOperationalError(message="live service unavailable")
        return live_service, LiveSessionKey(context["target_id"], context["config_revision"], context["worker_id"]), context

    @staticmethod
    def _live_mount_folder(destination: str) -> str:
        if (
            not isinstance(destination, str)
            or not destination
            or len(destination) > 4096
            or "\x00" in destination
            or "\\" in destination
            or any(ord(character) < 32 for character in destination)
            or not destination.startswith("/")
        ):
            raise LiveOperationalError(reason="source_unavailable")
        trimmed = destination.strip("/")
        parts = trimmed.split("/") if trimmed else []
        normalized_destination = "/" + trimmed
        if (
            not parts
            or any(not part or part in {".", ".."} for part in parts)
            or normalized_destination in {"/var/lib/docker", "/var/lib/docker/volumes", "/proc", "/sys", "/dev"}
            or normalized_destination.startswith(("/proc/", "/sys/", "/dev/", "/var/lib/docker/"))
            or trimmed.endswith((".sock", ".socket"))
        ):
            raise LiveOperationalError(reason="source_unavailable")
        folder = re.sub(r"[^A-Za-z0-9_-]+", "-", trimmed).strip("-")
        if not folder:
            raise LiveOperationalError(reason="source_unavailable")
        return folder[:LIVE_MOUNT_FOLDER_LENGTH].rstrip("-") or folder[:LIVE_MOUNT_FOLDER_LENGTH]

    def _live_mount_source(self, target_id: str) -> list[dict[str, str]]:
        target = self._control_plane_service().target_repository.get(target_id)
        if target is None: raise LiveSessionError("live access unavailable")
        runtime_volumes = target.runtime_volumes or {}
        if not isinstance(runtime_volumes, dict) or not runtime_volumes:
            raise LiveOperationalError(reason="source_unavailable")
        selected = target.volume_targets or []
        if not isinstance(selected, list):
            raise LiveOperationalError(reason="source_unavailable")
        if (
            len(selected) > LIVE_MOUNT_SOURCES_LIMIT
            or any(not isinstance(destination, str) for destination in selected)
            or len(set(selected)) != len(selected)
        ):
            raise LiveOperationalError(reason="source_unavailable")

        by_destination = {}
        for source, spec in runtime_volumes.items():
            if not isinstance(source, str) or not source or len(source) > 4096 or "\x00" in source:
                continue
            destination = spec.get("bind") if isinstance(spec, dict) else None
            if not isinstance(destination, str):
                continue
            if destination in by_destination:
                by_destination[destination] = None
            else:
                by_destination[destination] = (source, spec)

        if selected:
            ordered = []
            for destination in selected:
                candidate = by_destination.get(destination)
                if candidate is None:
                    raise LiveOperationalError(reason="source_unavailable")
                ordered.append(candidate)
        else:
            ordered = list(runtime_volumes.items())

        if not ordered or len(ordered) > LIVE_MOUNT_SOURCES_LIMIT:
            raise LiveOperationalError(reason="source_unavailable")
        descriptors, used_sources, used_destinations, used_folders = [], set(), set(), set()
        for source, spec in ordered:
            if not isinstance(source, str) or not source or len(source) > 4096 or "\x00" in source:
                raise LiveOperationalError(reason="source_unavailable")
            if not isinstance(spec, dict):
                raise LiveOperationalError(reason="source_unavailable")
            destination = spec.get("bind")
            if not isinstance(destination, str) or source in used_sources or destination in used_destinations:
                raise LiveOperationalError(reason="source_unavailable")
            folder = self._live_mount_folder(destination)
            base_folder, suffix = folder, 2
            while folder in used_folders:
                suffix_text = f"-{suffix}"
                folder = f"{base_folder[:LIVE_MOUNT_FOLDER_LENGTH - len(suffix_text)].rstrip('-')}{suffix_text}"
                suffix += 1
            used_sources.add(source)
            used_destinations.add(destination)
            used_folders.add(folder)
            descriptors.append({"source": source, "folder": folder})
        return descriptors

    def _live_request(self, target_id: str, operation: str):
        live_service, key, context = self._live_context(target_id)
        lane = self._live_lane()
        if lane is None: raise LiveOperationalError(message="live worker lane unavailable")
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        path = self._normalize_live_path(query.get("path", ["/"])[0])
        payload = {"target_id": target_id, "config_revision": context["config_revision"], "worker_id": context["worker_id"], "operation": operation, "path": path, "mount_sources": self._live_mount_source(target_id)}; stream = None
        if operation in {"watch", "unwatch"}:
            operation_id = lane.submit(context["worker_id"], payload)
            result = lane.wait(operation_id)
            lane.finish(operation_id)
            if not result:
                raise LiveOperationalError(message="live watcher request unavailable")
            if int(result.get("status", 500)) != 200:
                self._raise_live_worker_error(path, result, operation=operation, target_id=target_id)
            return None
        if operation == "entries":
            try:
                limit = max(1, min(int(query.get("limit", [100])[0]), 1000))
            except (TypeError, ValueError):
                raise ValueError("live entry limit is invalid") from None
            cursor = query.get("cursor", [""])[0]
            payload.update(limit=limit, cursor=live_service.read_entry_cursor(key, path, cursor) if cursor else None); live_service.attach(key).close()
        else:
            stream = live_service.open_raw_stream(key); payload["max_bytes"] = 64 * 1024 * 1024
        operation_id = lane.submit(context["worker_id"], payload, stream=stream)
        result = lane.wait(operation_id)
        if not result:
            lane.finish(operation_id); raise LiveOperationalError(message="live worker did not respond")
        if operation == "entries":
            lane.finish(operation_id)
            if int(result.get("status", 500)) != 200:
                self._raise_live_worker_error(path, result, operation=operation, target_id=target_id)
            next_cursor = result.get("next_cursor")
            return {"entries": result.get("entries") if isinstance(result.get("entries"), list) else [], "next_cursor": live_service.issue_entry_cursor(key, path, next_cursor) if next_cursor else None}
        if int(result.get("status", 500)) != 200:
            lane.finish(operation_id)
            self._raise_live_worker_error(path, result, operation=operation, target_id=target_id)
        try:
            if not self._control_plane_service().is_live_target_eligible(target_id):
                stream.cancel()
                raise LiveSessionError("live access unavailable")
            self.close_connection = True
            self.send_response(200)
            for name, value in (("Content-Type", "application/octet-stream"), ("Cache-Control", "no-store"), ("Connection", "close"), ("X-Accel-Buffering", "no")): self.send_header(name, value)
            self.end_headers()
            for chunk in stream:
                if not self._control_plane_service().is_live_target_eligible(target_id):
                    stream.cancel()
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            stream.cancel()
        finally:
            lane.finish(operation_id)
        return None

    def _live_entries(self, target_id: str, head_only: bool = False):
        try:
            return self._write_json(200, self._live_request(target_id, "entries"), head_only=head_only)
        except LiveLimitError:
            return self._write_json(429, {"error": "live resource limit reached"}, head_only=head_only)
        except LiveWorkerError as exc:
            self._log_live_worker_error(target_id, "entries", exc)
            return self._write_json(exc.status, self._live_worker_error_payload(exc), head_only=head_only)
        except (LiveCursorError, LiveSessionError, ValueError):
            return self._write_json(404, {"error": "live access unavailable"}, head_only=head_only)

    def _live_file(self, target_id: str):
        try:
            return self._live_request(target_id, "file")
        except LiveLimitError:
            return self._write_json(429, {"error": "live resource limit reached"})
        except LiveWorkerError as exc:
            self._log_live_worker_error(target_id, "file", exc)
            return self._write_json(exc.status, self._live_worker_error_payload(exc))
        except (LiveSessionError, ValueError):
            return self._write_json(404, {"error": "live access unavailable"})

    def _write_live_event(self, event_name: str, payload: dict, event_id: int | None = None):
        prefix = f"id: {event_id}\n" if event_id is not None else ""
        self.wfile.write(f"{prefix}event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()); self.wfile.flush()

    def _write_live_change_event(self, event):
        event_name = "resync_required" if event.get("kind") == "resync_required" else "changed"
        self._write_live_event(event_name, event, event.get("id"))

    def _stream_live_events(self, target_id: str):
        if not self._require_auth(ROLE_VIEWER, api_mode=True):
            return
        try:
            self._live_request(target_id, "watch")
            watching = True
            live_service, key, _ = self._live_context(target_id)
            subscription = live_service.attach(key)
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            cursor = query.get("cursor", [""])[0] or None
            last_id = (self.headers.get("Last-Event-ID", "") or query.get("last_event_id", [""])[0]).strip()
            if not cursor and last_id.isdigit():
                cursor = live_service.issue_cursor(key, int(last_id))
            replay = live_service.replay(key, cursor)
            self.close_connection = True
            self.send_response(200)
            for name, value in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-cache, no-store"), ("Connection", "keep-alive"), ("X-Accel-Buffering", "no")): self.send_header(name, value)
            self.end_headers()
            if replay.get("resync_required"):
                self._write_live_event("resync_required", {"reason": replay.get("reason", "replay_gap")})
            for event in replay.get("events", []):
                self._write_live_change_event(event)
            while self._control_plane_service().is_live_target_eligible(target_id):
                try:
                    item = subscription.get(timeout=15)
                except queue.Empty:
                    self._write_sse_comment("heartbeat")
                    continue
                if item.get("type") == "resync_required":
                    self._write_live_event("resync_required", {"reason": item.get("reason", "replay_gap")})
                    continue
                event = item.get("event") or {}
                if isinstance(event.get("id"), int):
                    subscription.acknowledge(event["id"])
                self._write_live_change_event(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("SSE client disconnected for live target %s", target_id)
        except LiveWorkerError as exc:
            self._log_live_worker_error(target_id, "watch", exc)
            try:
                self._write_json(exc.status, self._live_worker_error_payload(exc))
            except OSError:
                pass
        except (LiveCursorError, LiveSessionError, ValueError):
            try:
                self._write_json(404, {"error": "live access unavailable"})
            except OSError:
                pass
        finally:
            if "subscription" in locals(): subscription.close()
            if "watching" in locals():
                try: self._live_request(target_id, "unwatch")
                except Exception: pass

    def _handle_live_chunk(self, worker_id: str, raw_body: bytes):
        if not self._require_worker_identity(worker_id):
            return
        lane = self._live_lane()
        if lane is None:
            return self._write_json(503, {"error": "live worker lane unavailable"})
        try:
            lane.chunk(worker_id, self.headers.get("X-Live-Operation", ""), raw_body, self.headers.get("X-Live-Final", "0") == "1")
        except (LiveLimitError, LiveSessionError):
            return self._write_json(409, {"error": "live stream unavailable"})
        return self._write_json(202, {"ok": True})

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._raw_body = b""
            return {}
        raw_bytes = self.rfile.read(content_length)
        self._raw_body = raw_bytes
        raw = raw_bytes.decode("utf-8")
        return json.loads(raw) if raw else {}

    def _read_raw_body(self, max_bytes: int) -> bytes:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length < 0 or content_length > max_bytes: raise ValueError("body exceeds the permitted bound")
        raw_bytes = self.rfile.read(content_length)
        if len(raw_bytes) != content_length: raise ValueError("incomplete request body")
        self._raw_body = raw_bytes; return raw_bytes

    def _write_json(self, status_code: int, payload, head_only: bool = False, extra_headers: dict | None = None):
        body = json.dumps(payload, default=_to_jsonable).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    @staticmethod
    def _is_terminal_job_view(view) -> bool:
        return JobStatus.normalize((view or {}).get("status")) in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELED,
        }

    def _write_sse_event(self, view) -> None:
        body = json.dumps(_to_jsonable(view), ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_sse_comment(self, value: str = "heartbeat") -> None:
        self.wfile.write(f": {value}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_job_events(self, job_id: str):
        if not self._require_auth(ROLE_VIEWER, api_mode=True):
            return
        service = self._control_plane_service()
        initial_view = service.get_job_view(job_id)
        if initial_view is None:
            return self._write_json(404, {"error": "job not found"})

        subscription = service.job_event_broker.subscribe(job_id)
        try:
            view = service.get_job_view(job_id) or initial_view
            if view is None:
                return self._write_json(404, {"error": "job not found"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._write_sse_event(view)
            last_updated_at = view.get("updated_at")
            if self._is_terminal_job_view(view):
                return

            while True:
                try:
                    event = subscription.get(timeout=JOB_EVENT_HEARTBEAT_SECONDS)
                except queue.Empty:
                    current_view = service.get_job_view(job_id)
                    if current_view is None:
                        return
                    if current_view.get("updated_at") != last_updated_at or self._is_terminal_job_view(current_view):
                        self._write_sse_event(current_view)
                        last_updated_at = current_view.get("updated_at")
                        if self._is_terminal_job_view(current_view):
                            return
                    self._write_sse_comment()
                    continue

                self._write_sse_event(event)
                last_updated_at = event.get("updated_at")
                if self._is_terminal_job_view(event):
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("SSE client disconnected for job %s", job_id)
        finally:
            subscription.close()

    def _write_file(self, file_path: Path, content_type: str, head_only: bool = False):
        if not file_path.exists():
            return self._write_json(404, {"error": "file not found"}, head_only=head_only)
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve_static_file(self, path: str, content_type: str, head_only: bool = False):
        relative = path.lstrip("/")
        candidate = (UI_ROOT / relative).resolve()
        try:
            candidate.relative_to(UI_ROOT)
        except ValueError:
            return self._write_json(403, {"error": "forbidden"}, head_only=head_only)
        return self._write_file(candidate, content_type, head_only=head_only)

    def _write_empty(self, status_code: int):
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _build_session_cookie(self, token: str, max_age: int | None = None) -> str:
        cookie = cookies.SimpleCookie()
        cookie["cp_session"] = token
        cookie["cp_session"]["path"] = "/"
        cookie["cp_session"]["httponly"] = True
        cookie["cp_session"]["samesite"] = "Lax"
        if _env_flag("CONTROL_PLANE_TLS_ENABLED"): cookie["cp_session"]["secure"] = True
        if max_age is not None:
            cookie["cp_session"]["max-age"] = str(max_age)
        return cookie.output(header="", sep="").strip()

    def _current_session(self):
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        cookie = cookies.SimpleCookie()
        cookie.load(raw_cookie)
        morsel = cookie.get("cp_session")
        if morsel is None:
            return None
        return self._auth_service().parse_session_token(morsel.value)

    def _require_auth(self, required_role: str, head_only: bool = False, api_mode: bool = False):
        session = self._current_session()
        if session is None:
            if api_mode:
                self._write_json(401, {"error": "authentication required"}, head_only=head_only)
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return None
        if session.get("must_change_password"):
            if api_mode:
                self._write_json(
                    403,
                    {"error": "password change required", "code": "password_change_required"},
                    head_only=head_only,
                )
            else:
                self.send_response(302)
                self.send_header("Location", "/change-password")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return None
        if not self._auth_service().can_access(session["role"], required_role):
            self._write_json(403, {"error": "forbidden"}, head_only=head_only)
            return None
        return session

    @staticmethod
    def _path_parts(path: str):
        return [part for part in path.strip("/").split("/") if part]

    def _require_worker_identity(self, worker_id: str) -> bool:
        try:
            self._worker_auth().authenticate(
                worker_id, self.command, self.path, getattr(self, "_raw_body", b""),
                self.headers.get("X-Worker-Timestamp", ""), self.headers.get("X-Worker-Nonce", ""),
                self.headers.get("X-Worker-ID", ""), self.headers.get("X-Worker-Credential-Version", ""),
                self.headers.get("X-Worker-Signature", ""),
            )
        except (ValueError, RuntimeError) as exc:
            self._write_json(401, {"error": str(exc), "code": "worker_auth_failed"})
            return False
        return True


def build_application() -> ControlPlaneApplication:
    control_plane_service = _build_service()
    scheduler_interval = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))
    scheduler_timezone = os.environ.get(SCHEDULER_TIMEZONE_ENV, DEFAULT_SCHEDULER_TIMEZONE)
    scheduler = SchedulerService(
        control_plane_service,
        interval_seconds=scheduler_interval,
        timezone_name=scheduler_timezone,
    )
    app = ControlPlaneApplication(
        auth_service=AuthService.from_runtime(),
        control_plane_service=control_plane_service,
        tls_manager=_build_tls_manager(),
        scheduler=scheduler,
        live_file_service=LiveFileService(),
        live_lane=LiveWorkerLane(),
    )
    return app


def main():
    host = os.environ.get("CONTROL_PLANE_HOST", "0.0.0.0")
    port = int(os.environ.get("CONTROL_PLANE_PORT", "8080"))
    application = build_application()
    if application.scheduler is not None:
        application.scheduler.start()
    if application.tls_manager is not None:
        server = ControlPlaneHTTPSServer(
            (host, port),
            ControlPlaneRequestHandler,
            application=application,
            tls_manager=application.tls_manager,
        )
        logger.info("Control Plane TLS enabled with CA at %s", application.tls_manager.get_ca_certificate_path())
    else:
        server = ControlPlaneHTTPServer((host, port), ControlPlaneRequestHandler, application=application)
        logger.warning("Control Plane HTTP is non-confidential; use HTTPS for remote or confidential deployments")
    logger.info("Control Plane listening on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
