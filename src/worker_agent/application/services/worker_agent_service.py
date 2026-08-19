import base64
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError

from src.control_plane.domain.models import JobStatus
from src.worker_agent.domain.models import WorkerAgentConfig, WorkerJobExecutionResult
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.adapters.redis_cache import RedisSnapshotCache
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.job_recovery_journal import WorkerJobRecoveryJournal

logger = logging.getLogger(__name__)


class WorkerAgentService:
    MAX_LOG_LINES = 1000
    MAX_LOG_CHARS = 4 * 1024 * 1024
    MAX_SNAPSHOT_ENTRIES = 10_000
    MISSING_RESTIC_REPOSITORY_ERROR = (
        "Restic repository is not initialized or RESTIC_REPOSITORY points to the wrong path. "
        "Verify the target repository configuration before running restic init."
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
    ):
        self.config = config
        self.control_plane_client = control_plane_client
        self.docker_runtime = docker_runtime
        self.snapshot_cache = snapshot_cache
        self.recovery_journal = recovery_journal or (
            WorkerJobRecoveryJournal(recovery_file) if recovery_file else None
        )

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
        return self.docker_runtime.cleanup_orphaned_runtime_containers(
            recover_callback=self._recover_orphaned_runtime_container
        )

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

        if command in ("snapshot.ls", "snapshot.search", "snapshot.find"):
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
    def _parse_snapshot_ls_entries(logs: str, max_entries: Optional[int] = None) -> List[Dict[str, Any]]:
        if not isinstance(logs, str) or not logs:
            return []

        limit = max_entries if max_entries is not None else WorkerAgentService.MAX_SNAPSHOT_ENTRIES
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("snapshot entry limit must be positive")
        limit = min(limit, WorkerAgentService.MAX_SNAPSHOT_ENTRIES)
        logs = logs[: WorkerAgentService.MAX_LOG_CHARS]

        decoder = json.JSONDecoder()
        parsed_values: List[Any] = []
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
            parsed_values.append(value)
            offset = end

        candidates: List[Dict[str, Any]] = []
        for value in parsed_values:
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                candidates.append(value)
        return [
            entry
            for entry in candidates
            if entry.get("struct_type") == "node" or entry.get("type") in ("file", "dir")
        ][:limit]

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
        }

    def _execute_snapshot_metadata(
        self,
        command: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None,
    ) -> WorkerJobExecutionResult:
        image = payload.get("image") or self.config.backup_runtime_image
        direct_log_lines: List[str] = []

        def compute() -> Dict[str, Any]:
            if command == "snapshots.list":
                summary = self._invoke_runtime(
                    self.docker_runtime.list_restic_snapshots,
                    image,
                    payload,
                    cancel_check,
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
                    direct_log_lines[:] = [error]
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

            summary = self._invoke_runtime(self.docker_runtime.run_runtime_job, image, payload, cancel_check)
            status = self._runtime_status(summary)
            error = (
                self._classify_snapshot_runtime_error(command, payload, summary)
                if status != JobStatus.SUCCEEDED
                else None
            )
            entries = self._parse_snapshot_ls_entries(
                summary.get("logs", ""),
                None if command in ("snapshot.search", "snapshot.find") else payload.get("max_entries"),
            )
            if command in ("snapshot.search", "snapshot.find"):
                entries = self._filter_snapshot_entries(entries, payload.get("query"), payload.get("max_entries"))
            value = {
                "schema_version": 1,
                "status": status,
                "status_code": summary.get("status_code"),
                "target_id": payload.get("target_id"),
                "entries": entries,
            }
            if error:
                value["error"] = error
            if error == self.MISSING_RESTIC_REPOSITORY_ERROR:
                direct_log_lines[:] = [error]
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
        if command == "snapshots.list":
            result_summary["snapshots"] = value.get("snapshots", [])
        else:
            result_summary["entries"] = value.get("entries", [])
        if value.get("error"):
            result_summary["error"] = value["error"]
        log_lines = direct_log_lines if not cache_hit else ["Snapshot metadata served from Redis"]
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
    def _invoke_runtime(method: Callable[..., Dict[str, Any]], image: str, payload: Dict[str, Any], cancel_check):
        if cancel_check is None:
            return method(image=image, payload=payload)
        return method(image=image, payload=payload, cancel_check=cancel_check)

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
            try:
                execution = self.execute_job(job)
            finally:
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
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self._invoke_runtime(self.docker_runtime.run_runtime_job, image, payload, cancel_check)
                return WorkerJobExecutionResult(
                    status=self._runtime_status(summary),
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "compose_project": payload.get("compose_project"),
                    },
                    log_lines=self._bounded_log_lines(payload, summary.get("logs"), summary.get("stderr")),
                )

            if command == "snapshots.list":
                return self._execute_snapshot_metadata(command, payload, cancel_check)

            if command in ("snapshot.ls", "snapshot.search", "snapshot.find"):
                return self._execute_snapshot_metadata(command, payload, cancel_check)

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
                summary = self._invoke_runtime(self.docker_runtime.get_restic_stats, image, payload, cancel_check)
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
                summary = self._invoke_runtime(self.docker_runtime.run_runtime_job, image, payload, cancel_check)
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
                summary = self._invoke_runtime(self.docker_runtime.run_runtime_job, image, payload, cancel_check)
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
