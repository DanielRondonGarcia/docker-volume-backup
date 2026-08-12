import json
import logging
import os
import ssl
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import cookies
from pathlib import Path
from urllib.parse import urlparse

try:
    from http.server import ThreadingHTTPSServer as NativeThreadingHTTPSServer
except ImportError:
    NativeThreadingHTTPSServer = None

from src.control_plane.auth import AuthService, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER
from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.application.services.scheduler_service import SchedulerService
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryInventoryRepository,
    InMemoryJobRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetStatsRepository,
    InMemoryTargetRepository,
    InMemoryWorkerEnrollmentRepository,
    InMemoryWorkerRepository,
)
from src.control_plane.infrastructure.repositories.sqlite import (
    SQLiteInventoryRepository,
    SQLiteJobRepository,
    SQLiteRetentionPolicyRepository,
    SQLiteSecretRepository,
    SQLiteSettingsRepository,
    SQLiteSnapshotRepository,
    SQLiteStorageProfileRepository,
    SQLiteTargetStatsRepository,
    SQLiteTargetRepository,
    SQLiteWorkerEnrollmentRepository,
    SQLiteWorkerRepository,
)
from src.control_plane.infrastructure.security.secret_codec import SecretCodec
from src.control_plane.infrastructure.security.tls import TLSMaterialManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
UI_ROOT = Path(__file__).resolve().parent / "ui"


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
    tls_manager = _build_tls_manager()
    codec = SecretCodec.from_runtime(
        key_file_path=os.environ.get("CONTROL_PLANE_KEY_FILE", ".control_plane.key"),
        env_key=os.environ.get("CONTROL_PLANE_MASTER_KEY"),
    )
    if repository_mode == "memory":
        logger.info("Using in-memory repositories for Control Plane")
        return ControlPlaneService(
            worker_repository=InMemoryWorkerRepository(),
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            worker_enrollment_repository=InMemoryWorkerEnrollmentRepository(),
            secret_codec=codec,
            tls_manager=tls_manager,
            settings_repository=InMemorySettingsRepository(),
        )

    database_path = os.environ.get("CONTROL_PLANE_DB_PATH", "control_plane.db")
    logger.info("Using SQLite repositories for Control Plane at %s", database_path)
    return ControlPlaneService(
        worker_repository=SQLiteWorkerRepository(database_path),
        inventory_repository=SQLiteInventoryRepository(database_path),
        target_repository=SQLiteTargetRepository(database_path),
        job_repository=SQLiteJobRepository(database_path),
        storage_profile_repository=SQLiteStorageProfileRepository(database_path),
        secret_repository=SQLiteSecretRepository(database_path),
        snapshot_repository=SQLiteSnapshotRepository(database_path),
        retention_policy_repository=SQLiteRetentionPolicyRepository(database_path),
        target_stats_repository=SQLiteTargetStatsRepository(database_path),
        worker_enrollment_repository=SQLiteWorkerEnrollmentRepository(database_path),
        secret_codec=codec,
        tls_manager=tls_manager,
        settings_repository=SQLiteSettingsRepository(database_path),
    )

@dataclass
class ControlPlaneApplication:
    auth_service: AuthService
    control_plane_service: ControlPlaneService
    tls_manager: TLSMaterialManager | None = None
    worker_mtls_required: bool = False
    scheduler: "SchedulerService | None" = None


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
            self.socket.context.verify_mode = ssl.CERT_OPTIONAL if application.worker_mtls_required else ssl.CERT_NONE
            return

        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)
        self.application = application
        context = tls_manager.build_server_ssl_context(require_client_cert=application.worker_mtls_required)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.server_bind()
        self.server_activate()


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server_version = "docker-volume-backup-control-plane/0.1"

    def _auth_service(self) -> AuthService:
        return self.server.application.auth_service

    def _control_plane_service(self) -> ControlPlaneService:
        return self.server.application.control_plane_service

    def _tls_manager(self) -> TLSMaterialManager | None:
        return self.server.application.tls_manager

    def do_GET(self):
        self._handle_get_request(head_only=False)

    def do_HEAD(self):
        self._handle_get_request(head_only=True)

    def _handle_get_request(self, head_only: bool):
        path = urlparse(self.path).path
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
                return self._write_empty(204)
            if path == "/healthz":
                return self._write_json(200, {"ok": True}, head_only=head_only)
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
            if path == "/api/v1/workers":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_workers())}, head_only=head_only)
            if path == "/api/v1/jobs":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_jobs())}, head_only=head_only)
            if path == "/api/v1/targets":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_targets())}, head_only=head_only)
            if path == "/api/v1/storage-profiles":
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                return self._write_json(200, {"items": _to_jsonable(self._control_plane_service().list_storage_profiles())}, head_only=head_only)
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
                return self._write_json(200, _to_jsonable(settings or {}), head_only=head_only)
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
            if path == "/api/v1/admin/worker-enrollments":
                if not self._require_auth(ROLE_ADMIN, head_only=head_only, api_mode=True):
                    return
                return self._write_json(
                    200,
                    {"items": _to_jsonable(self._control_plane_service().list_worker_enrollments())},
                    head_only=head_only,
                )

            parts = self._path_parts(path)
            if len(parts) == 4 and parts[:3] == ["api", "v1", "jobs"]:
                if not self._require_auth(ROLE_VIEWER, head_only=head_only, api_mode=True):
                    return
                job = self._control_plane_service().get_job(parts[3])
                if job is None:
                    return self._write_json(404, {"error": "job not found"}, head_only=head_only)
                return self._write_json(200, _to_jsonable(job), head_only=head_only)
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
        body = self._read_json_body()
        try:
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
                enrollment = self._control_plane_service().create_worker_enrollment(
                    name=body.get("name") or "worker",
                    host_name=body.get("host_name") or "unknown-host",
                    labels=body.get("labels") or {},
                    ttl_minutes=int(body.get("ttl_minutes") or 30),
                )
                return self._write_json(201, _to_jsonable(enrollment))
            if path == "/api/v1/worker-enrollments/sign":
                result = self._control_plane_service().enroll_worker_certificate(
                    token=body.get("token", ""),
                    csr_pem=body.get("csr_pem", ""),
                    version=body.get("version") or "dev",
                    labels=body.get("labels") or {},
                )
                return self._write_json(201, _to_jsonable(result))
            if path == "/api/v1/workers/register":
                requested_worker_id = body.get("worker_id")
                if self.server.application.worker_mtls_required and not requested_worker_id:
                    return self._write_json(400, {"error": "worker_id is required when mTLS is enabled"})
                if requested_worker_id and not self._require_worker_identity(requested_worker_id):
                    return
                worker = self._control_plane_service().register_worker(
                    name=body.get("name") or "worker",
                    host_name=body.get("host_name") or "unknown-host",
                    version=body.get("version") or "dev",
                    labels=body.get("labels") or {},
                    worker_id=requested_worker_id,
                    certificate_fingerprint=self._peer_certificate_fingerprint(),
                )
                return self._write_json(201, _to_jsonable(worker))

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
                    retention_policy_id=body.get("retention_policy_id"),
                    restic_password_secret_id=body.get("restic_password_secret_id"),
                    restore_defaults=body.get("restore_defaults") or {},
                    labels=body.get("labels") or {},
                    cron_expression=body.get("cron_expression"),
                )
                return self._write_json(201, _to_jsonable(target))

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
                return self._write_json(202, _to_jsonable(job))

            if len(parts) == 6 and parts[:3] == ["api", "v1", "workers"] and parts[4] == "jobs" and parts[5] == "fetch":
                if not self._require_worker_identity(parts[3]):
                    return
                jobs = self._control_plane_service().fetch_jobs_for_worker(parts[3])
                return self._write_json(200, {"items": _to_jsonable(jobs)})

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
                )
                return self._write_json(200, _to_jsonable(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "backup":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_backup_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                )
                return self._write_json(202, _to_jsonable(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "snapshots-sync":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_snapshot_sync_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                )
                return self._write_json(202, _to_jsonable(job))

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
                return self._write_json(202, _to_jsonable(job))

            if len(parts) == 5 and parts[:3] == ["api", "v1", "targets"] and parts[4] == "retention-run":
                if not self._require_auth(ROLE_OPERATOR, api_mode=True):
                    return
                job = self._control_plane_service().dispatch_retention_for_target(
                    parts[3],
                    requested_by=body.get("requested_by") or "api",
                )
                return self._write_json(202, _to_jsonable(job))

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
                )
                return self._write_json(202, _to_jsonable(job))

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
                target = self._control_plane_service().update_target(
                    target_id=parts[3],
                    name=body.get("name"),
                    worker_id=body.get("worker_id"),
                    compose_project=body.get("compose_project"),
                    volume_targets=body.get("volume_targets"),
                    backup_mode=body.get("backup_mode"),
                    backup_strategy=body.get("backup_strategy"),
                    runtime_command=body.get("runtime_command"),
                    runtime_environment=body.get("runtime_environment"),
                    storage_profile_id=body.get("storage_profile_id"),
                    retention_policy_id=body.get("retention_policy_id"),
                    restic_password_secret_id=body.get("restic_password_secret_id"),
                    restore_defaults=body.get("restore_defaults"),
                    labels=body.get("labels"),
                    cron_expression=body.get("cron_expression"),
                )
                return self._write_json(200, _to_jsonable(target))
            if path == "/api/v1/settings":
                settings = self._control_plane_service().update_settings(
                    restic_repository_base=body.get("restic_repository_base"),
                    restic_password_secret_id=body.get("restic_password_secret_id"),
                    rclone_conf_secret_id=body.get("rclone_conf_secret_id"),
                )
                return self._write_json(200, _to_jsonable(settings))
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
                return self._write_json(204, {})
            return self._write_json(404, {"error": "not found"})
        except ValueError as exc:
            return self._write_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled DELETE error")
            return self._write_json(500, {"error": str(exc)})

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        return json.loads(raw) if raw else {}

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

    def _peer_certificate_fingerprint(self) -> str | None:
        tls_manager = self._tls_manager()
        if tls_manager is None:
            return None
        peer_certificate = getattr(self.connection, "getpeercert", lambda **kwargs: None)(binary_form=True)
        return tls_manager.fingerprint_from_peer_certificate(peer_certificate)

    def _require_worker_identity(self, worker_id: str) -> bool:
        if not self.server.application.worker_mtls_required:
            return True
        fingerprint = self._peer_certificate_fingerprint()
        if not fingerprint:
            self._write_json(401, {"error": "worker client certificate required"})
            return False
        worker = self._control_plane_service().get_worker(worker_id)
        if not worker.certificate_fingerprint:
            self._write_json(403, {"error": "worker has no enrolled certificate"})
            return False
        if worker.certificate_fingerprint != fingerprint:
            self._write_json(403, {"error": "worker certificate fingerprint mismatch"})
            return False
        return True


def build_application() -> ControlPlaneApplication:
    control_plane_service = _build_service()
    scheduler_interval = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))
    scheduler = SchedulerService(control_plane_service, interval_seconds=scheduler_interval)
    app = ControlPlaneApplication(
        auth_service=AuthService.from_runtime(),
        control_plane_service=control_plane_service,
        tls_manager=control_plane_service.tls_manager,
        worker_mtls_required=_env_flag("CONTROL_PLANE_WORKER_MTLS_REQUIRED", default=False),
        scheduler=scheduler,
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
    logger.info("Control Plane listening on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
