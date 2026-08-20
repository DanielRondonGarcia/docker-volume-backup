import base64
import inspect
import json
import logging
import os
import posixpath
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError

from src.control_plane.domain.models import JobStatus
from src.worker_agent.domain.models import WorkerAgentConfig, WorkerJobExecutionResult
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.adapters.live_file_runtime import LiveAccessDeniedError, LiveFileRuntime, LiveFileSource, LiveRuntimeError
from src.worker_agent.infrastructure.adapters.redis_cache import RedisSnapshotCache
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.application.services.live_target_session_manager import LiveTargetSessionKey, LiveTargetSessionManager
from src.worker_agent.infrastructure.security.job_recovery_journal import WorkerJobRecoveryJournal

logger = logging.getLogger(__name__)
LIVE_MOUNT_SOURCES_LIMIT = 64
LIVE_MOUNT_FOLDER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
LIVE_FAILURE_REASONS = {
    "protected_volume",
    "source_unavailable",
    "helper_start_failed",
    "helper_request_failed",
    "live_session_unavailable",
    "invalid_source",
    "invalid_request",
}


class _LiveRequestFailure(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason if reason in LIVE_FAILURE_REASONS else "live_session_unavailable"
        super().__init__(self.reason)


def _safe_live_log_value(value: Any, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not value:
        return fallback
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:128] or fallback


def _phase_from_line(line: str) -> Optional[str]:
    text = " ".join(str(line or "").split())[:2048].casefold()
    phase_markers = (
        ("pruning finished successfully", "finalizing"),
        ("pruning old snapshots", "pruning"),
        ("repository not initialized", "initializing"),
        ("repository initialized successfully", "initializing"),
        ("running restic backup", "backup"),
        ("performing backup strategy", "preparing"),
        ("backup starting", "preparing"),
    )
    for marker, phase in phase_markers:
        if marker in text:
            return phase
    return None


def _restic_progress_from_line(line: str) -> Optional[Dict[str, Any]]:
    phase = _phase_from_line(line)
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return {"phase": phase} if phase else None
    if not isinstance(record, dict) or record.get("message_type") != "status":
        return {"phase": phase} if phase else None
    progress: Dict[str, Any] = {"phase": "backup"}
    numeric_fields = ("percent_done", "files_done", "total_files", "bytes_done", "total_bytes")
    for field in numeric_fields:
        value = record.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            progress[field] = value
    current_file = record.get("current_file")
    if not isinstance(current_file, str):
        current_files = record.get("current_files")
        if isinstance(current_files, list) and current_files and isinstance(current_files[-1], str):
            current_file = current_files[-1]
    if isinstance(current_file, str):
        progress["current_file"] = current_file[:512]
    percent = progress.get("percent_done")
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        progress["percent_done"] = min(99.9, max(0.0, float(percent)))
    return progress


class _JobProgressReporter:
    """Coalesce runtime output into non-fatal, leased progress updates."""

    # Keep live updates responsive without issuing one request for every runtime line.
    FLUSH_INTERVAL_SECONDS = 0.25
    MAX_PENDING_LINES = 32

    def __init__(self, client, worker_id: str, job: Dict[str, Any]):
        self.client = client
        self.worker_id = worker_id
        self.job_id = job.get("id")
        self.lease_token = job.get("lease_token")
        self.sequence = 0
        self.latest_progress: Dict[str, Any] = {}
        self._pending_lines: List[str] = []
        self._partial_line = ""
        self._last_inferred_phase: Optional[str] = None
        self._last_sent = 0.0
        self._lock = threading.Lock()
        method = getattr(type(client), "update_job_progress", None)
        if callable(method):
            self._send_method = getattr(client, "update_job_progress")
        else:
            values = getattr(client, "__dict__", {})
            configured = values.get("update_job_progress") if isinstance(values, dict) else None
            self._send_method = configured if callable(configured) else None

    @property
    def callback(self) -> Optional[Callable[[str], None]]:
        return self.emit if self._send_method is not None else None

    def start(self) -> None:
        self.emit("", {"phase": "starting"}, force=True)

    def emit(self, chunk: Any, progress: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk or "")
        with self._lock:
            if text:
                text = self._partial_line + text
                parts = text.splitlines(keepends=True)
                self._partial_line = ""
                if parts and not parts[-1].endswith(("\n", "\r")):
                    self._partial_line = parts.pop()
                for part in parts:
                    line = part.rstrip("\r\n")
                    if line:
                        self._pending_lines.append(line[:16 * 1024])
                self._pending_lines = self._pending_lines[-self.MAX_PENDING_LINES :]
                phase_changed = False
                for line in parts:
                    parsed = _restic_progress_from_line(line.rstrip("\r\n"))
                    if parsed:
                        inferred_phase = parsed.get("phase")
                        if inferred_phase and inferred_phase != self._last_inferred_phase:
                            self._last_inferred_phase = inferred_phase
                            phase_changed = True
                        self.latest_progress = parsed
            else:
                phase_changed = False
            if progress:
                self.latest_progress = dict(progress)
            now = time.monotonic()
            should_flush = (
                force
                or phase_changed
                or len(self._pending_lines) >= self.MAX_PENDING_LINES
                or now - self._last_sent >= self.FLUSH_INTERVAL_SECONDS
            )
            if not should_flush:
                return
            lines = self._pending_lines
            self._pending_lines = []
            self.sequence += 1
            sequence = self.sequence
            progress_payload = dict(self.latest_progress)
            self._last_sent = now
        self._send(sequence, progress_payload, lines)

    def finish(self) -> None:
        with self._lock:
            if self._partial_line:
                self._pending_lines.append(self._partial_line[:16 * 1024])
                self._partial_line = ""
        self.emit("", force=True)

    def _send(self, sequence: int, progress: Dict[str, Any], lines: List[str]) -> None:
        if not callable(self._send_method) or not self.job_id or not isinstance(self.lease_token, str):
            return
        try:
            self._send_method(
                worker_id=self.worker_id,
                job_id=self.job_id,
                sequence=sequence,
                progress=progress,
                log_lines=lines,
                lease_token=self.lease_token,
            )
        except Exception as exc:
            logger.warning(
                "Job progress update failed for %s (error_type=%s)",
                self.job_id,
                exc.__class__.__name__,
            )


class WorkerAgentService:
    MAX_LOG_LINES = 1000
    MAX_LOG_CHARS = 4 * 1024 * 1024
    MAX_SNAPSHOT_METADATA_LOG_CHARS = DockerRuntimeAdapter.MAX_SNAPSHOT_METADATA_LOG_BYTES
    MAX_SNAPSHOT_ENTRIES = 10_000
    MAX_LIVE_POLL_REQUESTS = 16
    MAX_LIVE_FILE_BYTES = 64 * 1024 * 1024
    MISSING_RESTIC_REPOSITORY_ERROR = (
        "Restic repository is not initialized or RESTIC_REPOSITORY points to the wrong path. "
        "Verify the target repository configuration before running restic init."
    )
    UNCONFIGURED_RESTIC_REPOSITORY_ERROR = (
        "Restic repository is not configured. Set RESTIC_REPOSITORY on the target, storage profile, or Settings before running this job."
    )
    JOB_LEASE_RENEWAL_INTERVAL_SECONDS = 60.0
    INTERACTIVE_COMMANDS = frozenset(
        {"snapshots.list", "snapshot.ls", "snapshot.search", "snapshot.find", "snapshot.dump", "stats.get", "storage.about"}
    )

    def __init__(
        self,
        config: WorkerAgentConfig,
        control_plane_client: ControlPlaneClient,
        docker_runtime: DockerRuntimeAdapter,
        snapshot_cache: Optional[RedisSnapshotCache] = None,
        recovery_file: Optional[str] = None,
        recovery_journal: Optional[WorkerJobRecoveryJournal] = None,
        live_session_manager: Optional[LiveTargetSessionManager] = None,
    ):
        self.config = config
        self.control_plane_client = control_plane_client
        self.docker_runtime = docker_runtime
        self.snapshot_cache = snapshot_cache
        self.recovery_journal = recovery_journal or (
            WorkerJobRecoveryJournal(recovery_file) if recovery_file else None
        )
        self.live_sessions = live_session_manager or LiveTargetSessionManager(runtime_factory=self._live_runtime_factory, change_publisher=self._publish_live_change)

    def ensure_registered(self) -> str:
        if self.config.worker_id and self.control_plane_client.credential_store and self.control_plane_client.credential_store.load():
            return self.config.worker_id
        response = self.control_plane_client.register_worker(
            name=self.config.name,
            host_name=self.config.host_name,
            version=self.config.version,
            labels=self.config.labels,
            worker_id=self.config.worker_id,
        )
        self.config.worker_id = response["worker_id"]
        return self.config.worker_id

    def cleanup_orphaned_runtime_containers(self) -> Dict[str, Any]:
        result = self.docker_runtime.cleanup_orphaned_runtime_containers(
            recover_callback=self._recover_orphaned_runtime_container
        )
        client = getattr(self.docker_runtime, "client", None)
        if client is not None:
            try:
                result["live_helpers"] = self.live_sessions.cleanup_orphaned(client)
            except Exception:
                result["live_helpers"] = {"error": "live helper sweep failed"}
        return result

    def _live_runtime_factory(self, root: Any) -> LiveFileRuntime:
        store = getattr(self.control_plane_client, "credential_store", None)
        credential = store.load() if store and callable(getattr(store, "load", None)) else None
        secret = getattr(credential, "secret", None)
        if not isinstance(secret, (str, bytes)) or len(secret) < 32: raise RuntimeError("live worker credential is unavailable")
        return LiveFileRuntime(root, secret)

    def _resolve_live_source(self, source: Any, folder: str = "") -> Any:
        if not isinstance(source, str) or not source or len(source) > 4096 or "\x00" in source: raise ValueError("live mount source is invalid")
        if not isinstance(folder, str) or (folder and not LIVE_MOUNT_FOLDER_PATTERN.fullmatch(folder)): raise ValueError("live mount folder is invalid")
        helper_image = getattr(self.config, "live_helper_image", "docker-volume-backup-worker-local:dev")
        if os.path.isabs(source):
            normalized_source = source.rstrip("/") or "/"
            if DockerRuntimeAdapter._is_ignored_bind_source(normalized_source) or normalized_source in {"/var/lib/docker", "/var/lib/docker/volumes"}:
                raise ValueError("live mount source is unsafe")
            return LiveFileSource("bind", source, getattr(self.docker_runtime, "client", None), helper_image, folder=folder)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", source): raise ValueError("live mount source is invalid")
        client = getattr(self.docker_runtime, "client", None)
        volumes = getattr(client, "volumes", None)
        volume = volumes.get(source) if volumes and callable(getattr(volumes, "get", None)) else None
        if volume is None: raise ValueError("live mount source is unavailable")
        return LiveFileSource("volume", source, client, helper_image, folder=folder)

    def _resolve_live_sources(self, descriptors: Any) -> list[LiveFileSource]:
        if not isinstance(descriptors, list) or not descriptors or len(descriptors) > LIVE_MOUNT_SOURCES_LIMIT:
            raise ValueError("live mount sources are invalid")
        resolved, source_keys, folders = [], set(), set()
        for descriptor in descriptors:
            if not isinstance(descriptor, dict) or set(descriptor) != {"source", "folder"}:
                raise ValueError("live mount sources are invalid")
            source = descriptor.get("source")
            folder = descriptor.get("folder")
            if not isinstance(folder, str) or not LIVE_MOUNT_FOLDER_PATTERN.fullmatch(folder):
                raise ValueError("live mount folder is invalid")
            resolved_source = self._resolve_live_source(source, folder)
            source_key = (resolved_source.kind, resolved_source.source)
            if source_key in source_keys or folder in folders:
                raise ValueError("live mount sources are ambiguous")
            source_keys.add(source_key)
            folders.add(folder)
            resolved.append(resolved_source)
        return resolved

    def _publish_live_change(self, key, kind, path, entry_type, size, mtime_ns):
        try:
            self.control_plane_client.send_live_change(
                worker_id=key.worker_id, target_id=key.target_id, config_revision=key.config_revision,
                kind=kind, path=path, entry_type=entry_type, size=size, mtime_ns=mtime_ns,
            )
            return True
        except Exception as exc:
            logger.debug("Live change publication stopped (error_type=%s)", exc.__class__.__name__)
            return False

    def _process_live_request(self, worker_id: str, command: Dict[str, Any]) -> None:
        operation_id = command.get("operation_id") if isinstance(command, dict) else None
        operation = command.get("operation") if isinstance(command, dict) else None
        target_id = command.get("target_id") if isinstance(command, dict) else None
        try:
            if (
                not isinstance(command, dict)
                or not isinstance(operation_id, str)
                or not operation_id
                or command.get("worker_id") != worker_id
                or not isinstance(target_id, str)
                or not target_id
                or not isinstance(command.get("config_revision"), str)
                or not command.get("config_revision")
                or operation not in {"watch", "unwatch", "entries", "file"}
            ):
                raise _LiveRequestFailure("invalid_request")
            key = LiveTargetSessionKey(target_id, command["config_revision"], worker_id)
            descriptors = command.get("mount_sources")
            try:
                if descriptors is None:
                    legacy_source = command.get("mount_source")
                    root = self._resolve_live_source(legacy_source)
                else:
                    root = self._resolve_live_sources(descriptors)
            except ValueError as exc:
                reason = "source_unavailable" if "unavailable" in str(exc).casefold() else "invalid_source"
                raise _LiveRequestFailure(reason) from None
            if operation == "watch":
                self.live_sessions.begin_watch(key, target_root=root)
                self.control_plane_client.send_live_response(worker_id, operation_id, {"status": 200})
                return
            if operation == "unwatch":
                self.live_sessions.end_watch(key)
                self.control_plane_client.send_live_response(worker_id, operation_id, {"status": 200})
                return
            handle = self.live_sessions.attach(key, target_root=root)
            try:
                if operation == "entries":
                    limit = max(1, min(int(command.get("limit", 100)), 1000))
                    result = handle.list_entries(command.get("path", "/"), limit, command.get("cursor"))
                    self.control_plane_client.send_live_response(worker_id, operation_id, {"status": 200, **result})
                elif operation == "file":
                    reader = iter(
                        handle.read_file(
                            command.get("path", "/"),
                            max_bytes=min(int(command.get("max_bytes", self.MAX_LIVE_FILE_BYTES)), self.MAX_LIVE_FILE_BYTES),
                        )
                    )
                    first_chunk = next(reader, None)
                    self.control_plane_client.send_live_response(worker_id, operation_id, {"status": 200, "content_type": "application/octet-stream"})
                    try:
                        if first_chunk is not None:
                            self.control_plane_client.send_live_chunk(worker_id, operation_id, first_chunk)
                        for chunk in reader:
                            self.control_plane_client.send_live_chunk(worker_id, operation_id, chunk)
                    finally:
                        self.control_plane_client.send_live_chunk(worker_id, operation_id, b"", final=True)
                else:
                    raise ValueError("live operation is not supported")
            finally:
                handle.release()
        except LiveAccessDeniedError:
            self._reject_live_request(worker_id, operation_id, operation, target_id, "protected_volume")
        except _LiveRequestFailure as exc:
            self._reject_live_request(worker_id, operation_id, operation, target_id, exc.reason)
        except LiveRuntimeError as exc:
            self._reject_live_request(worker_id, operation_id, operation, target_id, getattr(exc, "reason", "live_session_unavailable"))
        except (KeyError, TypeError, ValueError):
            self._reject_live_request(worker_id, operation_id, operation, target_id, "invalid_request")
        except PermissionError:
            self._reject_live_request(worker_id, operation_id, operation, target_id, "protected_volume")
        except Exception:
            self._reject_live_request(worker_id, operation_id, operation, target_id, "live_session_unavailable")

    def _reject_live_request(self, worker_id, operation_id, operation, target_id, reason):
        reason = reason if reason in LIVE_FAILURE_REASONS else "live_session_unavailable"
        if reason == "protected_volume":
            result = {"status": 403, "code": "live_access_denied", "reason": reason}
        elif reason in {"invalid_source", "invalid_request"}:
            result = {"status": 400, "code": "live_request_rejected", "reason": reason}
        else:
            result = {"status": 503, "code": "live_worker_unavailable", "reason": reason}
        response_sent = True
        try:
            self.control_plane_client.send_live_response(worker_id, operation_id, result)
        except Exception:
            response_sent = False
        safe_operation = operation if operation in {"watch", "unwatch", "entries", "file"} else "unknown"
        logger.warning(
            "Live operation rejected (operation=%s target_id=%s code=%s reason=%s response_sent=%s)",
            safe_operation,
            _safe_live_log_value(target_id),
            result["code"],
            result["reason"],
            response_sent,
        )

    def poll_live_once(self, limit: int = 4):
        worker_id = self.ensure_registered()
        self.live_sessions.cleanup()
        requests = self.control_plane_client.fetch_live_requests(worker_id, min(limit, self.MAX_LIVE_POLL_REQUESTS))
        for command in requests or []:
            if isinstance(command, dict): self._process_live_request(worker_id, command)
        return requests or []

    def _persist_recovery_record(self, worker_id: str, job: Dict[str, Any]) -> None:
        if self.recovery_journal is None:
            return
        self.recovery_journal.write(
            job_id=job.get("id"),
            worker_id=worker_id,
            command=job.get("command"),
            lease_token=job.get("lease_token"),
        )

    def _clear_recovery_record(self, job_id: Any) -> None:
        if self.recovery_journal is not None and isinstance(job_id, str):
            self.recovery_journal.clear(expected_job_id=job_id)

    @staticmethod
    def _is_definitive_control_plane_rejection(error: Exception) -> bool:
        if isinstance(error, HTTPError):
            return error.code in (400, 404, 409, 410, 422)
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return False
        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "canceled",
                "cancelled",
                "expired",
                "not found",
                "unknown job",
                "not in progress",
                "does not own",
                "invalid or stale",
                "lease is invalid",
            )
        )

    @classmethod
    def _parse_recovery_json(cls, logs: Any, default: Any) -> tuple[Any, bool]:
        text = str(logs or "")[: cls.MAX_LOG_CHARS]
        if not text.strip():
            return default, True
        try:
            return json.loads(text), True
        except json.JSONDecodeError:
            candidates = []
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("[") or line.startswith("{"):
                    candidates.append(line)
            if not candidates:
                return default, False
            try:
                return json.loads("".join(candidates)), True
            except json.JSONDecodeError:
                return default, False

    def _recovery_log_lines(
        self,
        command: str,
        inspection: Dict[str, Any],
        status: str,
        error: Optional[str] = None,
    ) -> List[str]:
        lines = ["Worker restart recovered runtime (recovery=worker_restart_recovered)."]
        logs = self._safe_job_text({}, inspection.get("logs", ""))
        if logs.strip():
            lines.extend(logs[: self.MAX_LOG_CHARS].strip().splitlines()[-self.MAX_LOG_LINES :])
        if error:
            lines.append(error)
        if status == JobStatus.FAILED and not logs.strip() and not error:
            code = inspection.get("status_code")
            code_text = str(code) if isinstance(code, int) else "unavailable"
            lines.append(f"{command} runtime exited with status code {code_text} after worker restart.")
        return lines[-self.MAX_LOG_LINES :]

    def _build_recovery_result(
        self,
        command: str,
        inspection: Dict[str, Any],
    ) -> WorkerJobExecutionResult:
        status_code = inspection.get("status_code")
        runtime_ok = status_code == 0
        recovery = "worker_restart_recovered"

        if command == "snapshot.dump":
            error = "Binary snapshot output unavailable after worker restart."
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"status_code": status_code, "recovery": recovery, "error": error},
                log_lines=self._recovery_log_lines(command, inspection, JobStatus.FAILED, error),
            )

        if command == "snapshots.list":
            parsed, valid = self._parse_recovery_json(inspection.get("logs"), [])
            snapshots = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
            snapshots = [item for item in snapshots if isinstance(item, dict)]
            if len(snapshots) > self.MAX_SNAPSHOT_ENTRIES:
                valid = False
                snapshots = []
            error = None
            if not runtime_ok:
                error = f"snapshots.list runtime exited with status code {status_code}."
            elif not valid:
                error = "Snapshot catalog output was unavailable after worker restart."
            status = JobStatus.SUCCEEDED if runtime_ok and valid else JobStatus.FAILED
            summary = {"status_code": status_code, "recovery": recovery, "snapshots": snapshots}
            if error:
                summary["error"] = error
            return WorkerJobExecutionResult(
                status=status,
                result_summary=summary,
                log_lines=self._recovery_log_lines(command, inspection, status, error),
            )

        if command == "snapshot.ls":
            error = "Snapshot listing was unavailable after worker restart."
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={
                    "status_code": status_code,
                    "recovery": recovery,
                    "entries": [],
                    "listing_mode": "direct",
                    "listing_complete": False,
                    "listing_entry_count": 0,
                    "listing_error_code": "runtime_failure",
                    "error": error,
                },
                log_lines=self._recovery_log_lines(command, inspection, JobStatus.FAILED, error),
            )

        if command in ("snapshot.search", "snapshot.find"):
            entries = self._parse_snapshot_ls_entries(inspection.get("logs", ""))
            error = None if runtime_ok else f"{command} runtime exited with status code {status_code}."
            status = JobStatus.SUCCEEDED if runtime_ok else JobStatus.FAILED
            summary = {"status_code": status_code, "recovery": recovery, "entries": entries}
            if error:
                summary["error"] = error
            return WorkerJobExecutionResult(
                status=status,
                result_summary=summary,
                log_lines=self._recovery_log_lines(command, inspection, status, error),
            )

        if command == "stats.get":
            parsed, valid = self._parse_recovery_json(inspection.get("logs"), {})
            stats = parsed if isinstance(parsed, dict) else {}
            error = None
            if not runtime_ok:
                error = f"stats.get runtime exited with status code {status_code}."
            elif not valid or not isinstance(parsed, dict):
                error = "Snapshot statistics output was unavailable after worker restart."
            status = JobStatus.SUCCEEDED if runtime_ok and valid and isinstance(parsed, dict) else JobStatus.FAILED
            summary = {"status_code": status_code, "recovery": recovery, "stats": stats}
            if error:
                summary["error"] = error
            return WorkerJobExecutionResult(
                status=status,
                result_summary=summary,
                log_lines=self._recovery_log_lines(command, inspection, status, error),
            )

        if command in ("backup.run", "retention.run", "restore.dry_run", "restore.run"):
            error = None if runtime_ok else (
                f"{command} runtime exited with status code {status_code}."
                if isinstance(status_code, int)
                else f"{command} runtime exit status unavailable after worker restart."
            )
            status = JobStatus.SUCCEEDED if runtime_ok else JobStatus.FAILED
            summary = {"status_code": status_code, "recovery": recovery}
            if error:
                summary["error"] = error
            return WorkerJobExecutionResult(
                status=status,
                result_summary=summary,
                log_lines=self._recovery_log_lines(command, inspection, status, error),
            )

        error = f"{command} cannot be recovered after worker restart."
        return WorkerJobExecutionResult(
            status=JobStatus.FAILED,
            result_summary={"status_code": status_code, "recovery": recovery, "error": error},
            log_lines=self._recovery_log_lines(command, inspection, JobStatus.FAILED, error),
        )

    def _recover_orphaned_runtime_container(self, job_id: Any, inspection: Dict[str, Any]) -> str:
        journal = self.recovery_journal
        if journal is None:
            return "remove"
        if not isinstance(job_id, str) or not job_id or job_id == "unknown":
            return "remove"
        record = journal.load()
        if not record or record["job_id"] != job_id or record["worker_id"] != self.config.worker_id:
            return "remove"

        execution = self._build_recovery_result(record["command"], inspection)
        execution.log_lines = [
            DockerRuntimeAdapter._redact_text(line, {record["lease_token"]})
            for line in execution.log_lines
        ]
        try:
            self.control_plane_client.update_job_status(
                worker_id=record["worker_id"],
                job_id=record["job_id"],
                status=execution.status,
                result_summary=execution.result_summary,
                log_lines=execution.log_lines,
                lease_token=record["lease_token"],
            )
        except Exception as exc:
            if self._is_definitive_control_plane_rejection(exc):
                journal.clear(expected_job_id=record["job_id"])
                return "remove"
            logger.warning(
                "Runtime orphan recovery deferred (job_id=%s error_type=%s)",
                record["job_id"],
                exc.__class__.__name__,
            )
            return "retain"
        journal.clear(expected_job_id=record["job_id"])
        return "remove"

    def send_heartbeat(self):
        worker_id = self.ensure_registered()
        return self.control_plane_client.send_heartbeat(
            worker_id=worker_id,
            version=self.config.version,
            labels=self.config.labels,
        )

    @staticmethod
    def _parse_snapshot_ls_entries(
        logs: str,
        max_entries: Optional[int] = None,
        path: Optional[str] = None,
        max_log_bytes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(logs, str) or not logs:
            return []

        limit = max_entries if max_entries is not None else WorkerAgentService.MAX_SNAPSHOT_ENTRIES
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("snapshot entry limit must be positive")
        limit = min(limit, WorkerAgentService.MAX_SNAPSHOT_ENTRIES)
        log_limit = max_log_bytes if max_log_bytes is not None else WorkerAgentService.MAX_LOG_CHARS
        if (
            isinstance(log_limit, bool)
            or not isinstance(log_limit, int)
            or log_limit <= 0
            or log_limit > WorkerAgentService.MAX_SNAPSHOT_METADATA_LOG_CHARS
        ):
            raise ValueError("snapshot metadata log limit is outside the permitted bounds")
        logs = logs[:log_limit]
        requested_path = None
        if path is not None:
            requested_path = posixpath.normpath(path or "/")
            if not requested_path.startswith("/"):
                requested_path = f"/{requested_path}"

        decoder = json.JSONDecoder()
        entries: List[Dict[str, Any]] = []
        offset = 0
        while offset < len(logs):
            while offset < len(logs) and logs[offset].isspace():
                offset += 1
            if offset >= len(logs):
                break
            try:
                value, end = decoder.raw_decode(logs, offset)
            except json.JSONDecodeError:
                newline = logs.find("\n", offset)
                if newline == -1:
                    break
                offset = newline + 1
                continue
            offset = end

            values = value if isinstance(value, list) else (value,) if isinstance(value, dict) else ()
            for entry in values:
                if not isinstance(entry, dict):
                    continue
                if entry.get("struct_type") != "node" and entry.get("type") not in ("file", "dir"):
                    continue
                if requested_path is not None:
                    entry_path = entry.get("path")
                    if not isinstance(entry_path, str) or not entry_path.startswith("/"):
                        continue
                    normalized_entry_path = posixpath.normpath(entry_path)
                    if normalized_entry_path == "/" or posixpath.dirname(normalized_entry_path) != requested_path:
                        continue
                entries.append(entry)
                if len(entries) >= limit:
                    return entries
        return entries

    @staticmethod
    def _parse_snapshot_tree_entries(
        logs: str,
        path: Optional[str] = None,
        max_entries: Optional[int] = None,
        max_log_bytes: Optional[int] = None,
    ) -> tuple[List[Dict[str, Any]], bool, Optional[str], int]:
        limit = max_entries if max_entries is not None else WorkerAgentService.MAX_SNAPSHOT_ENTRIES
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("snapshot entry limit must be positive")
        limit = min(limit, WorkerAgentService.MAX_SNAPSHOT_ENTRIES)
        log_limit = max_log_bytes if max_log_bytes is not None else DockerRuntimeAdapter.MAX_LOG_BYTES
        if (
            isinstance(log_limit, bool)
            or not isinstance(log_limit, int)
            or log_limit <= 0
            or log_limit > WorkerAgentService.MAX_SNAPSHOT_METADATA_LOG_CHARS
        ):
            raise ValueError("snapshot metadata log limit is outside the permitted bounds")

        try:
            requested_path = DockerRuntimeAdapter.normalize_snapshot_path(path or "/")
        except (TypeError, ValueError):
            return [], False, "snapshot tree path is invalid", 0
        if not isinstance(logs, str) or not logs.strip():
            return [], False, "snapshot tree JSON is empty or incomplete", 0
        try:
            tree = json.loads(logs[:log_limit])
        except json.JSONDecodeError:
            return [], False, "snapshot tree JSON is malformed or incomplete", 0
        if not isinstance(tree, dict) or not isinstance(tree.get("nodes"), list):
            return [], False, "snapshot tree JSON is malformed or incomplete", 0

        nodes = tree["nodes"]
        if len(nodes) > WorkerAgentService.MAX_SNAPSHOT_ENTRIES:
            return [], False, "snapshot tree listing exceeded the permitted entry limit", WorkerAgentService.MAX_SNAPSHOT_ENTRIES + 1

        entries: List[Dict[str, Any]] = []
        base_path = requested_path.rstrip("/") or "/"
        for node in nodes:
            if not isinstance(node, dict):
                return [], False, "snapshot tree JSON is malformed or incomplete", 0
            name = node.get("name")
            node_type = node.get("type")
            if (
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or "\x00" in name
                or any(ord(character) < 32 for character in name)
                or len(name) > DockerRuntimeAdapter.MAX_SNAPSHOT_PATH_LENGTH
                or node_type not in {"file", "dir", "symlink"}
            ):
                return [], False, "snapshot tree JSON is malformed or incomplete", 0

            child_path = posixpath.normpath(posixpath.join(base_path, name))
            prefix = "/" if base_path == "/" else f"{base_path}/"
            if not child_path.startswith(prefix) or child_path == requested_path:
                return [], False, "snapshot tree JSON is malformed or incomplete", 0

            entry: Dict[str, Any] = {
                "struct_type": "node",
                "type": node_type,
                "path": child_path,
            }
            size = node.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= (1 << 63) - 1:
                entry["size"] = size
            subtree = node.get("subtree")
            if isinstance(subtree, str) and re.fullmatch(r"[0-9a-f]{64}", subtree, re.IGNORECASE):
                entry["subtree"] = subtree
            for metadata_name in ("mtime", "atime", "ctime"):
                metadata_value = node.get(metadata_name)
                if isinstance(metadata_value, str) and len(metadata_value) <= 128:
                    entry[metadata_name] = metadata_value
            if len(entries) < limit:
                entries.append(entry)

        return entries, True, None, len(nodes)

    @staticmethod
    def _filter_snapshot_entries(entries: List[Dict[str, Any]], query: Optional[str], max_entries: Optional[int]) -> List[Dict[str, Any]]:
        if query is not None:
            needle = query.casefold()
            entries = [entry for entry in entries if needle in str(entry.get("path", "")).casefold()]
        if max_entries is not None:
            entries = entries[:max_entries]
        return entries

    @staticmethod
    def _is_missing_restic_repository_error(text: str) -> bool:
        normalized = text.casefold()
        return all(
            marker in normalized
            for marker in ("unable to open config file", "repository", "does not exist")
        )

    def _classify_snapshot_runtime_error(
        self,
        command: str,
        payload: Dict[str, Any],
        summary: Dict[str, Any],
    ) -> str:
        safe_sources = []
        for source in (summary.get("error"), summary.get("stderr"), summary.get("logs")):
            safe_source = self._safe_job_text(payload, source).strip()
            if safe_source:
                safe_sources.append(safe_source[: self.MAX_LOG_CHARS])

        safe_text = "\n".join(safe_sources)[: self.MAX_LOG_CHARS]
        if self._is_missing_restic_repository_error(safe_text):
            return self.MISSING_RESTIC_REPOSITORY_ERROR

        for source in safe_sources:
            lines = [line.strip() for line in source.splitlines() if line.strip()]
            if lines:
                return lines[-1]

        status_code = summary.get("status_code")
        code_text = str(status_code) if isinstance(status_code, int) else "unavailable"
        return f"{command} runtime exited with status code {code_text}."

    @staticmethod
    def _snapshot_listing_output_limit(payload: Dict[str, Any]) -> int:
        value = payload.get("max_log_bytes")
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 < value <= DockerRuntimeAdapter.MAX_SNAPSHOT_METADATA_LOG_BYTES
        ):
            return value
        return DockerRuntimeAdapter.MAX_LOG_BYTES

    @staticmethod
    def _snapshot_listing_error_code(status_code: Any, error: Any) -> str:
        text = str(error or "").casefold()
        if "malformed" in text or "incomplete" in text or "tree json" in text:
            return "malformed_tree"
        if "entry limit" in text:
            return "entry_limit"
        if status_code == 413 or any(marker in text for marker in ("exceed", "limit", "too large", "truncat")):
            return "output_limit"
        if status_code == 124 or "timed out" in text or "timeout" in text:
            return "timeout"
        if "repository" in text and ("not initialized" in text or "not configured" in text):
            return "repository"
        if any(marker in text for marker in ("path", "snapshot", "tree target")) and status_code not in (None, 0):
            return "path_failure"
        return "runtime_failure"

    @staticmethod
    def _snapshot_cache_context(command: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        environment = payload.get("environment") or {}
        if not isinstance(environment, dict):
            return None
        repository = environment.get("RESTIC_REPOSITORY")
        target_id = payload.get("target_id")
        if not isinstance(repository, str) or not repository or target_id is None:
            return None
        try:
            repository_fingerprint = DockerRuntimeAdapter.repository_fingerprint(repository)
        except (TypeError, ValueError):
            return None
        max_log_bytes = payload.get("max_log_bytes")
        if max_log_bytes is None:
            max_log_bytes = DockerRuntimeAdapter.MAX_LOG_BYTES
        return {
            "target_id": target_id,
            "repository": repository,
            "repository_fingerprint": repository_fingerprint,
            "cache_generation": payload.get("cache_generation", 0),
            "operation": command,
            "snapshot_id": payload.get("snapshot_id"),
            "path": payload.get("path") or "/",
            "query": payload.get("query"),
            "max_entries": payload.get("max_entries"),
            "max_log_bytes": max_log_bytes,
        }

    def _execute_snapshot_metadata(
        self,
        command: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None,
        progress_reporter: _JobProgressReporter | None = None,
    ) -> WorkerJobExecutionResult:
        image = payload.get("image") or self.config.backup_runtime_image
        context_line = self._storage_context_log_line(payload)
        direct_log_lines: List[str] = [context_line] if context_line else []

        if self._storage_repository_unconfigured(payload):
            error = self.UNCONFIGURED_RESTIC_REPOSITORY_ERROR
            result_summary = {
                "target_id": payload.get("target_id"),
                "storage_context": self._storage_context(payload),
                "error": error,
            }
            if command == "snapshot.ls":
                result_summary.update(
                    {
                        "listing_mode": "direct",
                        "listing_complete": False,
                        "listing_entry_count": 0,
                        "listing_output_limit_bytes": self._snapshot_listing_output_limit(payload),
                        "listing_error_code": "repository",
                    }
                )
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary=result_summary,
                log_lines=direct_log_lines + [error],
            )

        def compute() -> Dict[str, Any]:
            if command == "snapshots.list":
                summary = self._invoke_runtime(
                    self.docker_runtime.list_restic_snapshots,
                    image,
                    payload,
                    cancel_check,
                    progress_reporter.callback if progress_reporter else None,
                )
                status = self._runtime_status(summary)
                error = (
                    self._classify_snapshot_runtime_error(command, payload, summary)
                    if status != JobStatus.SUCCEEDED
                    else None
                )
                value = {
                    "schema_version": 1,
                    "status": status,
                    "status_code": summary.get("status_code"),
                    "target_id": payload.get("target_id"),
                    "snapshots": summary.get("snapshots", []),
                }
                if error:
                    value["error"] = error
                if error == self.MISSING_RESTIC_REPOSITORY_ERROR:
                    direct_log_lines[:] = ([context_line] if context_line else []) + [error]
                else:
                    direct_log_lines.extend(
                        self._bounded_log_lines(
                            payload,
                            summary.get("logs"),
                            summary.get("stderr"),
                        )
                    )
                    if error and not direct_log_lines:
                        direct_log_lines.append(error)
                return value

            summary = self._invoke_runtime(
                self.docker_runtime.run_runtime_job,
                image,
                payload,
                cancel_check,
                progress_reporter.callback if progress_reporter else None,
            )
            status = self._runtime_status(summary)
            error = (
                self._classify_snapshot_runtime_error(command, payload, summary)
                if status != JobStatus.SUCCEEDED
                else None
            )
            listing_fields: Dict[str, Any] = {}
            if command == "snapshot.ls":
                listing_limit = self._snapshot_listing_output_limit(payload)
                listing_fields = {
                    "listing_mode": "direct",
                    "listing_complete": False,
                    "listing_entry_count": 0,
                    "listing_output_limit_bytes": listing_limit,
                }
                entries: List[Dict[str, Any]] = []
                if status == JobStatus.SUCCEEDED:
                    entries = self._parse_snapshot_ls_entries(
                        summary.get("logs", ""),
                        payload.get("max_entries"),
                        path=payload.get("path"),
                        max_log_bytes=listing_limit,
                    )
                    listing_fields["listing_complete"] = True
                    listing_fields["listing_entry_count"] = len(entries)
                else:
                    listing_fields["listing_error_code"] = self._snapshot_listing_error_code(
                        summary.get("status_code"), error
                    )
            else:
                entries = self._parse_snapshot_ls_entries(
                    summary.get("logs", ""),
                    None if command in ("snapshot.search", "snapshot.find") else payload.get("max_entries"),
                    path=None,
                    max_log_bytes=payload.get("max_log_bytes"),
                )
            if command in ("snapshot.search", "snapshot.find"):
                entries = self._filter_snapshot_entries(entries, payload.get("query"), payload.get("max_entries"))
            value = {
                "schema_version": 1,
                "status": status,
                "status_code": summary.get("status_code"),
                "target_id": payload.get("target_id"),
                "entries": entries,
                **listing_fields,
            }
            if command == "snapshot.ls" and error and "listing_error_code" not in listing_fields:
                listing_fields["listing_error_code"] = self._snapshot_listing_error_code(
                    summary.get("status_code"), error
                )
                value["listing_error_code"] = listing_fields["listing_error_code"]
            if error:
                value["error"] = error
            if error == self.MISSING_RESTIC_REPOSITORY_ERROR:
                direct_log_lines[:] = ([context_line] if context_line else []) + [error]
            else:
                direct_log_lines.extend(
                    self._bounded_log_lines(
                        payload,
                        summary.get("logs"),
                        summary.get("stderr"),
                    )
                )
                if error and not direct_log_lines:
                    direct_log_lines.append(error)
            return value

        context = self._snapshot_cache_context(command, payload)
        cache_hit = False
        source = "restic-fallback"
        if self.snapshot_cache is not None and context is not None:
            value, cache_hit, source = self.snapshot_cache.get_or_compute(
                context,
                compute,
                cacheable=lambda result: result.get("status") == JobStatus.SUCCEEDED,
                cancel_check=cancel_check,
            )
        else:
            value = compute()

        result_summary = {
            "status_code": value.get("status_code"),
            "target_id": payload.get("target_id"),
            "cache_hit": bool(cache_hit),
            "source": source,
        }
        storage_context = self._storage_context(payload)
        if storage_context:
            result_summary["storage_context"] = storage_context
        if progress_reporter and progress_reporter.latest_progress:
            result_summary["progress"] = dict(progress_reporter.latest_progress)
        if command == "snapshots.list":
            result_summary["snapshots"] = value.get("snapshots", [])
        else:
            result_summary["entries"] = value.get("entries", [])
            if command == "snapshot.ls":
                for key in (
                    "listing_mode",
                    "listing_complete",
                    "listing_entry_count",
                    "listing_output_limit_bytes",
                    "listing_error_code",
                ):
                    if key in value:
                        result_summary[key] = value[key]
        if value.get("error"):
            result_summary["error"] = value["error"]
        log_lines = direct_log_lines if not cache_hit else direct_log_lines + ["Snapshot metadata served from Redis"]
        return WorkerJobExecutionResult(
            status=value.get("status", JobStatus.FAILED),
            result_summary=result_summary,
            log_lines=log_lines,
        )

    @staticmethod
    def _optional_method(instance: Any, name: str):
        method = getattr(type(instance), name, None)
        if callable(method):
            return getattr(instance, name)
        values = getattr(instance, "__dict__", {})
        configured = values.get(name) if isinstance(values, dict) else None
        return configured if callable(configured) else None

    @staticmethod
    def _runtime_status(summary: Dict[str, Any]) -> str:
        if summary.get("canceled") or summary.get("status_code") == 130:
            return JobStatus.CANCELED
        return JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED

    @staticmethod
    def _storage_context(payload: Dict[str, Any]) -> Dict[str, Any]:
        context = payload.get("storage_context") if isinstance(payload, dict) else None
        if not isinstance(context, dict):
            return {}
        allowed = (
            "storage_profile_id",
            "storage_profile_name",
            "backend_type",
            "repository_source",
            "repository_kind",
            "repository_display",
            "rclone_config_source",
        )
        result = {}
        for key in allowed:
            value = context.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                result[key] = " ".join(value.split())[:256]
        return result

    @classmethod
    def _storage_repository_unconfigured(cls, payload: Dict[str, Any]) -> bool:
        context = cls._storage_context(payload)
        return context.get("repository_source") == "unconfigured"

    @classmethod
    def _storage_context_log_line(cls, payload: Dict[str, Any]) -> Optional[str]:
        context = cls._storage_context(payload)
        if not context:
            return None
        if context.get("repository_source") == "unconfigured" or not context.get("repository_display"):
            return "Storage no configurado; Repositorio no configurado."
        profile = context.get("storage_profile_name") or context.get("storage_profile_id") or "sin perfil"
        backend = context.get("backend_type") or context.get("repository_kind") or "unknown"
        return f"Storage profile: {profile}; backend: {backend}; repository: {context.get('repository_display')}"

    @staticmethod
    def _about_metrics(value: Any) -> Dict[str, int]:
        if not isinstance(value, dict):
            return {}
        metrics: Dict[str, int] = {}
        for field in ("total", "used", "free", "trashed"):
            raw = value.get(field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            metrics[field] = int(raw)
        return metrics

    @classmethod
    def _is_unsupported_about_error(cls, text: str) -> bool:
        lowered = text.casefold()
        return any(
            marker in lowered
            for marker in (
                "not supported by this backend",
                "about is not supported",
                "does not support about",
                "doesn't support about",
                "about: not supported",
            )
        )

    def _execute_storage_about(
        self,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None,
    ) -> WorkerJobExecutionResult:
        remote = payload.get("remote")
        try:
            DockerRuntimeAdapter._validated_rclone_remote(remote)
        except (TypeError, ValueError) as exc:
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"state": "transient-failure", "error": self._safe_job_text(payload, exc)},
                log_lines=[self._safe_job_text(payload, exc)],
            )
        argv = ["rclone", "about", remote, "--json"]
        about_payload = {**payload, "command": argv}
        image = payload.get("image") or self.config.backup_runtime_image
        summary = self._invoke_runtime(self.docker_runtime.run_runtime_job, image, about_payload, cancel_check)
        logs = summary.get("logs", "")
        safe_logs = self._safe_job_text(payload, logs)

        if not summary.get("success"):
            error = self._safe_job_text(payload, summary.get("error", "") or summary.get("logs", ""))
            if self._is_unsupported_about_error(error) or self._is_unsupported_about_error(safe_logs):
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED,
                    result_summary={"state": "about-unsupported"},
                    log_lines=["Remote does not support the about operation."],
                )
            state = "transient-failure"
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"state": state, "error": error},
                log_lines=self._bounded_log_lines(payload, summary.get("logs"), summary.get("stderr"), error),
            )

        try:
            parsed = json.loads(logs or "{}")
        except (json.JSONDecodeError, TypeError):
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"state": "transient-failure", "error": "failed to parse rclone about JSON"},
                log_lines=["Failed to parse rclone about JSON."],
            )
        metrics = self._about_metrics(parsed)
        if not metrics:
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"state": "transient-failure", "error": "rclone about JSON is missing capacity fields"},
                log_lines=["Failed to parse rclone about JSON."],
            )
        return WorkerJobExecutionResult(
            status=JobStatus.SUCCEEDED,
            result_summary={"state": "available", "metrics": metrics},
            log_lines=self._bounded_log_lines(payload, summary.get("logs"), summary.get("stderr")),
        )

    @staticmethod
    def _safe_job_text(payload: Dict[str, Any], value: Any) -> str:
        secrets = DockerRuntimeAdapter._collect_secret_values(payload)
        return DockerRuntimeAdapter._redact_text(value, secrets)

    def _bounded_log_lines(self, payload: Dict[str, Any], *values: Any) -> List[str]:
        combined = "\n".join(str(value or "") for value in values if value is not None)
        combined = self._safe_job_text(payload, combined)[: self.MAX_LOG_CHARS]
        return combined.strip().splitlines()[-self.MAX_LOG_LINES :]

    def _cancellation_check(self, job: Dict[str, Any]) -> Callable[[], bool] | None:
        payload = job.get("payload") or {}
        event = payload.get("_cancel_event") if isinstance(payload, dict) else None
        hook = payload.get("_cancel_check") if isinstance(payload, dict) else None
        if not callable(hook):
            hook = job.get("cancel_check")
        client_probe = self._optional_method(self.control_plane_client, "is_job_cancelled")
        if not callable(hook):
            hook = client_probe
        if not callable(hook) and not (event and callable(getattr(event, "is_set", None))):
            return None

        last_probe = [0.0]
        last_value = [False]

        def check() -> bool:
            if (
                JobStatus.normalize(str(job.get("status") or "")) == JobStatus.CANCELED
                or bool(job.get("canceled"))
                or bool(job.get("cancelled"))
            ):
                return True
            if event is not None and callable(getattr(event, "is_set", None)) and event.is_set():
                return True
            now = time.monotonic()
            if now - last_probe[0] < 0.2:
                return last_value[0]
            last_probe[0] = now
            try:
                if callable(hook):
                    if hook is client_probe:
                        try:
                            value = hook(self.config.worker_id, job.get("id"))
                        except TypeError:
                            value = hook(job.get("id"))
                    else:
                        value = hook()
                    last_value[0] = bool(value)
            except Exception as exc:
                logger.debug("Cancellation probe failed for job %s: %s", job.get("id"), self._safe_job_text(payload, exc))
                last_value[0] = False
            return last_value[0]

        return check

    @staticmethod
    def _invoke_runtime(
        method: Callable[..., Dict[str, Any]],
        image: str,
        payload: Dict[str, Any],
        cancel_check,
        output_callback=None,
    ):
        kwargs: Dict[str, Any] = {"image": image, "payload": payload}
        if cancel_check is not None:
            kwargs["cancel_check"] = cancel_check
        if output_callback is not None:
            try:
                parameters = inspect.signature(method).parameters
                accepts_callback = "output_callback" in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                )
            except (TypeError, ValueError):
                accepts_callback = True
            if accepts_callback:
                kwargs["output_callback"] = output_callback
        return method(**kwargs)

    def _start_lease_renewal(self, worker_id: str, job: Dict[str, Any]):
        renew = self._optional_method(self.control_plane_client, "renew_job_lease")
        job_id = job.get("id")
        lease_token = job.get("lease_token")
        if not callable(renew) or not job_id or not isinstance(lease_token, str) or not lease_token:
            return None
        stop_event = threading.Event()

        def renew_until_done():
            while not stop_event.wait(self.JOB_LEASE_RENEWAL_INTERVAL_SECONDS):
                try:
                    renew(worker_id, job_id, lease_token)
                except Exception as exc:
                    logger.warning(
                        "Job lease renewal failed for %s (error_type=%s)",
                        job_id,
                        exc.__class__.__name__,
                    )

        thread = threading.Thread(target=renew_until_done, daemon=True, name=f"job-lease-{job_id}")
        thread.start()
        return stop_event, thread

    @staticmethod
    def _stop_lease_renewal(renewal) -> None:
        if not renewal:
            return
        stop_event, thread = renewal
        stop_event.set()
        thread.join(timeout=1)

    def _fetch_jobs(self, worker_id: str, interactive: bool) -> List[Dict[str, Any]]:
        if interactive:
            fetch_interactive = self._optional_method(self.control_plane_client, "fetch_interactive_jobs")
            if fetch_interactive:
                try:
                    return fetch_interactive(worker_id)
                except (AttributeError, NotImplementedError):
                    logger.debug("Interactive job lane unavailable; using durable polling")
        return self.control_plane_client.fetch_jobs(worker_id)

    def _process_jobs(self, worker_id: str, jobs: List[Dict[str, Any]]):
        results = []
        for job in jobs or []:
            self._persist_recovery_record(worker_id, job)
            renewal = self._start_lease_renewal(worker_id, job)
            progress_reporter = _JobProgressReporter(self.control_plane_client, worker_id, job)
            progress_reporter.start()
            try:
                execution = self.execute_job(job, progress_reporter=progress_reporter)
            finally:
                progress_reporter.finish()
                self._stop_lease_renewal(renewal)
            try:
                updated = self.control_plane_client.update_job_status(
                    worker_id=worker_id,
                    job_id=job["id"],
                    status=execution.status,
                    result_summary=execution.result_summary,
                    log_lines=execution.log_lines,
                    lease_token=job.get("lease_token"),
                )
            except ValueError as exc:
                if execution.status != JobStatus.CANCELED or "canceled" not in str(exc).lower():
                    if self._is_definitive_control_plane_rejection(exc):
                        self._clear_recovery_record(job.get("id"))
                    raise
                self._clear_recovery_record(job.get("id"))
                updated = job
            except Exception as exc:
                if self._is_definitive_control_plane_rejection(exc):
                    self._clear_recovery_record(job.get("id"))
                raise
            else:
                self._clear_recovery_record(job.get("id"))
            results.append(updated)
        return results

    def sync_inventory(self):
        worker_id = self.ensure_registered()
        inventory = self.docker_runtime.collect_inventory()
        return self.control_plane_client.sync_inventory(worker_id, inventory)

    def poll_once(self, interactive: bool = False):
        worker_id = self.ensure_registered()
        jobs = self._fetch_jobs(worker_id, interactive=interactive)
        if interactive:
            jobs = sorted(jobs or [], key=lambda item: item.get("command") not in self.INTERACTIVE_COMMANDS)
        return self._process_jobs(worker_id, jobs)

    def poll_interactive_once(self):
        """Use the optional fast lane and fall back to the durable job fetch once."""
        return self.poll_once(interactive=True)

    def execute_job(
        self,
        job: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        progress_reporter: _JobProgressReporter | None = None,
    ) -> WorkerJobExecutionResult:
        command = job.get("command")
        payload = job.get("payload") or {}
        if isinstance(payload, dict) and job.get("id") is not None:
            payload = {**payload, "_job_id": job["id"]}
        logger.info("Executing worker job %s (%s)", job.get("id"), self._safe_job_text(payload, command))

        cancel_check = cancel_check or self._cancellation_check(job)
        if JobStatus.normalize(str(job.get("status") or "")) == JobStatus.CANCELED:
            return WorkerJobExecutionResult(
                status=JobStatus.CANCELED,
                result_summary={"error": "job canceled before execution"},
                log_lines=["Job canceled before execution"],
            )

        try:
            if command == "inventory.refresh":
                inventory = self.docker_runtime.collect_inventory()
                self.control_plane_client.sync_inventory(self.config.worker_id, inventory)
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED,
                    result_summary={"docker_available": inventory.get("docker_available", False)},
                    log_lines=["Inventory synchronized"],
                )

            if command == "worker.self_check":
                summary = self.docker_runtime.self_check()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED,
                    result_summary=summary,
                    log_lines=["Self check completed"],
                )

            if command == "containers.stop":
                summary = self.docker_runtime.stop_containers(payload.get("container_ids") or [])
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if not summary["errors"] else JobStatus.FAILED,
                    result_summary=summary,
                    log_lines=["Stop containers executed"],
                )

            if command == "containers.start":
                summary = self.docker_runtime.start_containers(payload.get("container_ids") or [])
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if not summary["errors"] else JobStatus.FAILED,
                    result_summary=summary,
                    log_lines=["Start containers executed"],
                )

            if command == "containers.restart":
                summary = self.docker_runtime.restart_containers(payload.get("container_ids") or [])
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if not summary["errors"] else JobStatus.FAILED,
                    result_summary=summary,
                    log_lines=["Restart containers executed"],
                )

            if command == "backup.run":
                if self._storage_repository_unconfigured(payload):
                    error = self.UNCONFIGURED_RESTIC_REPOSITORY_ERROR
                    context_line = self._storage_context_log_line(payload)
                    return WorkerJobExecutionResult(
                        status=JobStatus.FAILED,
                        result_summary={
                            "target_id": payload.get("target_id"),
                            "compose_project": payload.get("compose_project"),
                            "storage_context": self._storage_context(payload),
                            "error": error,
                        },
                        log_lines=([context_line] if context_line else []) + [error],
                    )
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self._invoke_runtime(
                    self.docker_runtime.run_runtime_job,
                    image,
                    payload,
                    cancel_check,
                    progress_reporter.callback if progress_reporter else None,
                )
                result_summary = {
                    "status_code": summary.get("status_code"),
                    "target_id": payload.get("target_id"),
                    "compose_project": payload.get("compose_project"),
                }
                storage_context = self._storage_context(payload)
                if storage_context:
                    result_summary["storage_context"] = storage_context
                if progress_reporter and progress_reporter.latest_progress:
                    result_summary["progress"] = dict(progress_reporter.latest_progress)
                log_lines = self._bounded_log_lines(payload, summary.get("logs"), summary.get("stderr"))
                if context_line := self._storage_context_log_line(payload):
                    log_lines = [context_line] + log_lines
                return WorkerJobExecutionResult(
                    status=self._runtime_status(summary),
                    result_summary=result_summary,
                    log_lines=log_lines,
                )

            if command == "snapshots.list":
                return self._execute_snapshot_metadata(command, payload, cancel_check, progress_reporter)

            if command in ("snapshot.ls", "snapshot.search", "snapshot.find"):
                return self._execute_snapshot_metadata(command, payload, cancel_check, progress_reporter)

            if command == "snapshot.dump":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self._invoke_runtime(self.docker_runtime.run_runtime_job_binary, image, payload, cancel_check)
                stdout_bytes = summary.get("stdout_bytes", b"")
                b64_content = base64.b64encode(stdout_bytes).decode("ascii") if stdout_bytes else ""
                return WorkerJobExecutionResult(
                    status=self._runtime_status(summary),
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "b64_content": b64_content,
                        "stderr": self._safe_job_text(payload, summary.get("stderr", "")),
                    },
                    log_lines=self._bounded_log_lines(payload, summary.get("stderr")),
                )

            if command == "stats.get":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self._invoke_runtime(
                    self.docker_runtime.get_restic_stats,
                    image,
                    payload,
                    cancel_check,
                    progress_reporter.callback if progress_reporter else None,
                )
                return WorkerJobExecutionResult(
                    status=self._runtime_status(summary),
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "stats": summary.get("stats", {}),
                    },
                    log_lines=self._bounded_log_lines(payload, summary.get("logs"), summary.get("stderr")),
                )

            if command == "storage.about":
                return self._execute_storage_about(payload, cancel_check)

            if command == "retention.run":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self._invoke_runtime(
                    self.docker_runtime.run_runtime_job,
                    image,
                    payload,
                    cancel_check,
                    progress_reporter.callback if progress_reporter else None,
                )
                return WorkerJobExecutionResult(
                    status=self._runtime_status(summary),
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "retention_command": payload.get("command"),
                    },
                    log_lines=self._bounded_log_lines(payload, summary.get("logs"), summary.get("stderr")),
                )

            if command in ("restore.dry_run", "restore.run"):
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self._invoke_runtime(
                    self.docker_runtime.run_runtime_job,
                    image,
                    payload,
                    cancel_check,
                    progress_reporter.callback if progress_reporter else None,
                )
                status = self._runtime_status(summary)
                safe_error = self._safe_job_text(payload, summary.get("error", ""))
                safe_stderr = self._safe_job_text(payload, summary.get("stderr", ""))
                restore_summary = {
                    "status_code": summary.get("status_code"),
                    "target_id": payload.get("target_id"),
                    "dry_run": command == "restore.dry_run",
                }
                if safe_error:
                    restore_summary["error"] = safe_error
                if safe_stderr:
                    restore_summary["stderr"] = safe_stderr
                log_lines = self._bounded_log_lines(
                    payload,
                    summary.get("logs"),
                    summary.get("stderr"),
                    summary.get("error"),
                )
                if not log_lines:
                    log_lines = [
                        "Restore runtime failed without logs."
                        if status == JobStatus.FAILED
                        else "Restore runtime completed without logs."
                    ]
                return WorkerJobExecutionResult(
                    status=status,
                    result_summary=restore_summary,
                    log_lines=log_lines,
                )

            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"error": f"unsupported command: {command}"},
                log_lines=[f"Unsupported command: {command}"],
            )
        except Exception as exc:
            logger.exception("Worker job failed")
            error = self._safe_job_text(payload, exc)
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"error": error, "command": command},
                log_lines=[error][: self.MAX_LOG_LINES],
            )
