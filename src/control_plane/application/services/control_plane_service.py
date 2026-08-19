import copy
import hashlib
import hmac
import json
import logging
import math
import os
import posixpath
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.control_plane.application.ports.ports import (
    CacheRepository,
    IndexRepository,
    InventoryRepository,
    JobRepository,
    RetentionPolicyRepository,
    SecretRepository,
    SettingsRepository,
    SnapshotRepository,
    StorageProfileRepository,
    TargetStatsRepository,
    TargetRepository,
    WorkerRepository,
)
from src.control_plane.application.services.job_event_broker import JobEventBroker
from src.control_plane.domain.models import (
    BackupTargetRecord,
    InventorySnapshot,
    JobRecord,
    JobStatus,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotReadRequest,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    WorkerRecord,
    WorkerStatus,
    utcnow,
)

logger = logging.getLogger(__name__)


class WorkerDeletionConflict(ValueError):
    """Raised when deleting a worker would orphan active work."""


class ControlPlaneService:
    SUPPORTED_SECRET_TYPES = {"generic", "env", "file"}
    DEFAULT_WORKER_OFFLINE_AFTER_SECONDS = 90.0
    MIN_WORKER_OFFLINE_AFTER_SECONDS = 1.0
    MAX_WORKER_OFFLINE_AFTER_SECONDS = 3600.0
    JOB_LEASE_DURATION_SECONDS = 300
    MAX_SNAPSHOT_ENTRIES = 10_000
    MAX_PROGRESS_LOG_LINES = 100
    MAX_PROGRESS_LOG_CHARS = 64 * 1024
    MAX_JOB_LOG_LINES = 1000
    MAX_JOB_LOG_CHARS = 512 * 1024
    MAX_REPOSITORY_DISPLAY_LENGTH = 256
    MAX_PUBLIC_SUMMARY_ITEMS = 200
    MAX_REQUEST_ID_LENGTH = 128
    MAX_SEARCH_QUERY_LENGTH = 256
    LIVE_REVISION_SCHEMA_VERSION = 1
    SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8,64}$", re.IGNORECASE)
    REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    SNAPSHOT_READ_COMMANDS = {
        "browse": "snapshot.ls",
        "search": "snapshot.search",
        "find": "snapshot.search",
        "dump": "snapshot.dump",
    }
    CACHE_INVALIDATING_COMMANDS = frozenset(
        {"backup.run", "retention.run", "forget", "prune", "restore.run", "restore.write", "write"}
    )
    CATALOG_MUTATING_COMMANDS = frozenset({"backup.run", "retention.run", "forget", "prune"})
    PROGRESS_FIELDS = frozenset(
        {
            "percent_done",
            "files_done",
            "total_files",
            "bytes_done",
            "total_bytes",
            "current_file",
            "phase",
        }
    )

    def __init__(
        self,
        worker_repository: WorkerRepository,
        inventory_repository: InventoryRepository,
        target_repository: TargetRepository,
        job_repository: JobRepository,
        storage_profile_repository: StorageProfileRepository,
        secret_repository: SecretRepository,
        snapshot_repository: SnapshotRepository,
        retention_policy_repository: RetentionPolicyRepository,
        target_stats_repository: TargetStatsRepository,
        secret_codec,
        settings_repository: Optional[SettingsRepository] = None,
        cache_repository: Optional[CacheRepository] = None,
        index_repository: Optional[IndexRepository] = None,
        job_event_broker: Optional[JobEventBroker] = None,
    ):
        self.worker_repository = worker_repository
        self.inventory_repository = inventory_repository
        self.target_repository = target_repository
        self.job_repository = job_repository
        self.storage_profile_repository = storage_profile_repository
        self.secret_repository = secret_repository
        self.snapshot_repository = snapshot_repository
        self.retention_policy_repository = retention_policy_repository
        self.target_stats_repository = target_stats_repository
        self.secret_codec = secret_codec
        self.settings_repository = settings_repository
        self.cache_repository = cache_repository
        self.index_repository = index_repository
        self.job_event_broker = job_event_broker or JobEventBroker()
        self.worker_auth = None

    def register_worker(
        self,
        name: str,
        host_name: str,
        version: str = "dev",
        labels: Optional[Dict[str, str]] = None,
        worker_id: Optional[str] = None,
    ) -> WorkerRecord:
        existing = self.worker_repository.get(worker_id) if worker_id else self.worker_repository.find_by_name(name)
        if existing:
            existing.host_name = host_name
            existing.version = version
            existing.labels = labels or existing.labels or {}
            if existing.status != WorkerStatus.DISABLED:
                existing.status = WorkerStatus.ONLINE
            existing.last_seen_at = utcnow()
            existing.updated_at = utcnow()
            return self.worker_repository.save(existing)
        worker = WorkerRecord(
            name=name,
            host_name=host_name,
            version=version,
            labels=labels or {},
            id=worker_id or str(uuid4()),
            status=WorkerStatus.ONLINE,
            last_seen_at=utcnow(),
        )
        return self.worker_repository.save(worker)

    def create_worker_enrollment(
        self,
        worker_id: str,
        secret: str,
        ttl_minutes: int = 30,
    ) -> Dict[str, Any]:
        worker = self.worker_repository.get(worker_id)
        if worker is None:
            raise ValueError(f"worker not found: {worker_id}")
        worker_auth = getattr(self, "worker_auth", None)
        if worker_auth is None:
            raise ValueError("worker authentication is not configured")
        enrollment = worker_auth.create_enrollment(
            name=worker.name,
            host_name=worker.host_name,
            labels=dict(worker.labels or {}),
            secret=secret,
            worker_id=worker.id,
            ttl_minutes=ttl_minutes,
            replace_pending=True,
        )
        return {
            **enrollment,
            "worker_id": worker.id,
            "name": worker.name,
            "host_name": worker.host_name,
            "labels": dict(worker.labels or {}),
        }

    def heartbeat(self, worker_id: str, version: Optional[str] = None, labels: Optional[Dict[str, str]] = None) -> WorkerRecord:
        worker = self._require_worker(worker_id)
        if worker.status != WorkerStatus.DISABLED:
            worker.status = WorkerStatus.ONLINE
        worker.last_seen_at = utcnow()
        worker.updated_at = utcnow()
        if version:
            worker.version = version
        if labels:
            worker.labels.update(labels)
        return self.worker_repository.save(worker)

    def update_worker(self, worker_id: str, labels: Optional[Dict[str, str]] = None) -> WorkerRecord:
        worker = self._require_worker(worker_id)
        if labels is not None:
            worker.labels = self._validate_worker_labels(labels)
        worker.updated_at = utcnow()
        return self.worker_repository.save(worker)

    @staticmethod
    def _validate_worker_labels(labels: Dict[str, str]) -> Dict[str, str]:
        if not isinstance(labels, dict):
            raise ValueError("worker labels must be an object")
        normalized: Dict[str, str] = {}
        for key, value in labels.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("worker label keys must not be blank")
            normalized_key = key.strip()
            if normalized_key in normalized:
                raise ValueError(f"duplicate worker label key: {normalized_key}")
            normalized[normalized_key] = "" if value is None else str(value)
        return normalized

    @staticmethod
    def _validate_backup_mode(mode: str) -> str:
        if mode not in ("hot", "cold"):
            raise ValueError("backup_mode must be exactly 'hot' or 'cold'")
        return mode

    def revoke_worker(self, worker_id: str, version: Optional[str] = None) -> Dict[str, Any]:
        worker = self._require_worker(worker_id)
        auth = getattr(self, "worker_auth", None)
        if auth is not None:
            if version is None and callable(getattr(auth, "revoke_all", None)):
                result = auth.revoke_all(worker_id)
            else:
                result = auth.revoke(worker_id, version)
        else:
            result = {"worker_id": worker_id, "credentials_revoked": 0}
        worker.status = WorkerStatus.DISABLED
        worker.updated_at = utcnow()
        self.worker_repository.save(worker)
        self._disable_targets_for_worker(worker_id)
        result = dict(result or {})
        result.update({"worker_id": worker_id, "status": "revoked", "worker_status": WorkerStatus.DISABLED})
        return result

    def delete_worker(self, worker_id: str) -> bool:
        worker = self.worker_repository.get(worker_id)
        if worker is None:
            return False

        self._reconcile_expired_jobs()
        active_jobs = [
            job
            for job in self.job_repository.list()
            if JobStatus.normalize(job.status) in (JobStatus.PENDING, JobStatus.IN_PROGRESS)
            and (job.worker_id == worker_id or job.owner_worker_id == worker_id)
        ]
        if active_jobs:
            job_ids = ", ".join(f"{job.id} ({JobStatus.normalize(job.status)})" for job in active_jobs)
            raise WorkerDeletionConflict(
                f"worker '{worker_id}' still has active or pending jobs: {job_ids}"
            )

        self._disable_targets_for_worker(worker_id)
        auth = getattr(self, "worker_auth", None)
        if auth is not None:
            revoke_all = getattr(auth, "revoke_all", None)
            if callable(revoke_all):
                revoke_all(worker_id)
            delete_auth_worker = getattr(auth, "delete_worker", None)
            if callable(delete_auth_worker):
                delete_auth_worker(worker_id)
        self.inventory_repository.delete_by_worker(worker_id)
        self.target_stats_repository.delete_by_worker(worker_id)
        deleted = self.worker_repository.delete(worker_id)
        if deleted:
            self._cleanup_orphaned_explorer_metadata()
        return deleted

    def _disable_targets_for_worker(self, worker_id: str) -> None:
        for target in self.target_repository.list():
            if target.worker_id != worker_id or not target.enabled:
                continue
            target.enabled = False
            target.updated_at = utcnow()
            self.target_repository.save(target)

    def get_worker(self, worker_id: str) -> WorkerRecord:
        return self._worker_view(self._require_worker(worker_id))

    def sync_inventory(self, worker_id: str, inventory: Dict[str, Any]) -> InventorySnapshot:
        worker = self._require_worker(worker_id)
        worker.updated_at = utcnow()
        self.worker_repository.save(worker)

        existing = self.inventory_repository.get_by_worker(worker_id)
        snapshot = existing or InventorySnapshot(worker_id=worker_id, inventory={})
        snapshot.inventory = inventory
        snapshot.updated_at = utcnow()
        return self.inventory_repository.save(snapshot)

    def register_target(
        self,
        name: str,
        worker_id: str,
        compose_project: Optional[str] = None,
        volume_targets: Optional[List[str]] = None,
        backup_mode: str = "hot",
        backup_strategy: str = "restic",
        runtime_image: Optional[str] = None,
        runtime_command: Optional[str] = None,
        runtime_environment: Optional[Dict[str, str]] = None,
        runtime_volumes: Optional[Dict[str, Dict[str, str]]] = None,
        runtime_network_mode: Optional[str] = None,
        storage_profile_id: Optional[str] = None,
        retention_policy_id: Optional[str] = None,
        restic_password_secret_id: Optional[str] = None,
        restore_defaults: Optional[Dict[str, Any]] = None,
        labels: Optional[Dict[str, str]] = None,
        cron_expression: Optional[str] = None,
        live_access_enabled: bool = False,
    ) -> BackupTargetRecord:
        backup_mode = self._validate_backup_mode(backup_mode)
        if not isinstance(live_access_enabled, bool):
            raise ValueError("live_access_enabled must be a boolean")
        self._require_eligible_worker(worker_id)
        if storage_profile_id:
            self._require_storage_profile(storage_profile_id)
        if retention_policy_id:
            self._require_retention_policy(retention_policy_id)
        if restic_password_secret_id:
            self._require_secret(restic_password_secret_id)

        client_volume_targets: List[str] = list(volume_targets or [])
        client_runtime_volumes: Dict[str, Dict[str, str]] = dict(runtime_volumes or {})
        if compose_project:
            derived = self._derive_volumes_from_worker_inventory(worker_id, compose_project)
            if client_volume_targets:
                client_volume_set = set(client_volume_targets)
                for vol_name, vol_info in derived["runtime_volumes"].items():
                    if vol_info.get("bind") in client_volume_set:
                        client_runtime_volumes.setdefault(vol_name, vol_info)
            else:
                for bind_path in derived["volume_targets"]:
                    if bind_path not in client_volume_targets:
                        client_volume_targets.append(bind_path)
                for vol_name, vol_info in derived["runtime_volumes"].items():
                    client_runtime_volumes.setdefault(vol_name, vol_info)

        target = BackupTargetRecord(
            name=name,
            worker_id=worker_id,
            compose_project=compose_project,
            volume_targets=client_volume_targets,
            backup_mode=backup_mode,
            backup_strategy=backup_strategy,
            runtime_image=runtime_image,
            runtime_command=runtime_command,
            runtime_environment=runtime_environment or {},
            runtime_volumes=client_runtime_volumes,
            runtime_network_mode=runtime_network_mode,
            storage_profile_id=storage_profile_id,
            retention_policy_id=retention_policy_id,
            restic_password_secret_id=restic_password_secret_id,
            restore_defaults=restore_defaults or {},
            labels=labels or {},
            live_access_enabled=live_access_enabled,
            cron_expression=(cron_expression.strip() if cron_expression else None),
        )
        return self.target_repository.save(target)

    def _derive_volumes_from_worker_inventory(self, worker_id: str, compose_project: str) -> Dict[str, Any]:
        snapshot = self.inventory_repository.get_by_worker(worker_id)
        if snapshot is None or not snapshot.inventory:
            return {"volume_targets": [], "runtime_volumes": {}}
        projects = snapshot.inventory.get("compose_project_details") or []
        matching = next((project for project in projects if project.get("name") == compose_project), None)
        if matching is None:
            return {"volume_targets": [], "runtime_volumes": {}}
        volume_targets = list(matching.get("volume_targets") or [])
        runtime_volumes = dict(matching.get("runtime_volumes") or {})
        return {"volume_targets": volume_targets, "runtime_volumes": runtime_volumes}

    def update_target(
        self,
        target_id: str,
        name: Optional[str] = None,
        worker_id: Optional[str] = None,
        compose_project: Optional[str] = None,
        volume_targets: Optional[List[str]] = None,
        backup_mode: Optional[str] = None,
        backup_strategy: Optional[str] = None,
        runtime_image: Optional[str] = None,
        runtime_command: Optional[str] = None,
        runtime_environment: Optional[Dict[str, str]] = None,
        storage_profile_id: Optional[str] = None,
        retention_policy_id: Optional[str] = None,
        restic_password_secret_id: Optional[str] = None,
        restore_defaults: Optional[Dict[str, Any]] = None,
        labels: Optional[Dict[str, str]] = None,
        cron_expression: Optional[str] = None,
        enabled: Optional[bool] = None,
        live_access_enabled: Optional[bool] = None,
    ) -> BackupTargetRecord:
        target = self._require_target(target_id)
        if backup_mode is not None:
            backup_mode = self._validate_backup_mode(backup_mode)
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if live_access_enabled is not None and not isinstance(live_access_enabled, bool):
            raise ValueError("live_access_enabled must be a boolean")
        effective_worker_id = worker_id if worker_id is not None else target.worker_id
        if worker_id is not None:
            self._require_eligible_worker(worker_id)
        if enabled is True:
            self._require_eligible_worker(effective_worker_id)
        if live_access_enabled is True:
            self._require_eligible_worker(effective_worker_id)
        if worker_id is not None:
            target.worker_id = worker_id
        if name is not None:
            target.name = name
        if compose_project is not None:
            target.compose_project = compose_project
        if volume_targets is not None:
            client_set = set(volume_targets)
            target.volume_targets = list(volume_targets)
            if target.runtime_volumes:
                target.runtime_volumes = {
                    k: v for k, v in target.runtime_volumes.items() if v.get("bind") in client_set
                }
        if backup_mode is not None:
            target.backup_mode = backup_mode
        if backup_strategy is not None:
            target.backup_strategy = backup_strategy
        if runtime_command is not None:
            target.runtime_command = runtime_command
        if runtime_image is not None:
            target.runtime_image = runtime_image or None
        if runtime_environment is not None:
            target.runtime_environment = runtime_environment
        if storage_profile_id is not None:
            if storage_profile_id:
                self._require_storage_profile(storage_profile_id)
            target.storage_profile_id = storage_profile_id or None
        if retention_policy_id is not None:
            if retention_policy_id:
                self._require_retention_policy(retention_policy_id)
            target.retention_policy_id = retention_policy_id or None
        if restic_password_secret_id is not None:
            if restic_password_secret_id:
                self._require_secret(restic_password_secret_id)
            target.restic_password_secret_id = restic_password_secret_id or None
        if restore_defaults is not None:
            target.restore_defaults = restore_defaults
        if labels is not None:
            target.labels = labels
        if cron_expression is not None:
            target.cron_expression = cron_expression.strip() or None
        if enabled is not None:
            target.enabled = bool(enabled)
        if live_access_enabled is not None:
            target.live_access_enabled = bool(live_access_enabled)
        target.updated_at = utcnow()
        return self.target_repository.save(target)

    def delete_target(self, target_id: str) -> bool:
        self._require_target(target_id)
        deleted = self.target_repository.delete(target_id)
        if deleted:
            if self.index_repository:
                self.index_repository.delete_for_target(target_id)
            self._cleanup_orphaned_explorer_metadata()
        return deleted

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        self._reconcile_expired_jobs()
        return self.job_repository.get(job_id)

    def dispatch_job(
        self,
        worker_id: str,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        requested_by: str = "system",
        target_id: Optional[str] = None,
        trigger: str = "manual",
    ) -> JobRecord:
        self._require_eligible_worker(worker_id)
        if target_id is not None:
            target = self._require_target(target_id)
            if target.worker_id != worker_id:
                raise ValueError(f"target '{target_id}' is assigned to worker '{target.worker_id}'")
            if command == "backup.run" and not target.enabled:
                raise ValueError(f"target '{target_id}' is disabled and cannot be dispatched")
        job = JobRecord(
            worker_id=worker_id,
            command=command,
            payload=payload or {},
            requested_by=requested_by,
            target_id=target_id,
            trigger=trigger,
        )
        return self.job_repository.save(job)

    def dispatch_backup_for_target(
        self,
        target_id: str,
        requested_by: str = "system",
        trigger: str = "manual",
        backup_mode: Optional[str] = None,
    ) -> JobRecord:
        target = self._require_target(target_id)
        if not target.enabled:
            raise ValueError(f"target '{target_id}' is disabled and cannot be dispatched")
        payload = self._build_backup_payload(target, backup_mode=backup_mode)
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="backup.run",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger=trigger,
        )

    def has_active_backup_for_target(self, target_id: str) -> bool:
        return any(
            job.target_id == target_id
            and job.command == "backup.run"
            and JobStatus.normalize(job.status) in (JobStatus.PENDING, JobStatus.IN_PROGRESS)
            for job in self.job_repository.list()
        )

    def has_active_snapshot_sync_for_target(self, target_id: str, worker_id: Optional[str] = None) -> bool:
        self._reconcile_expired_jobs()
        target = self._require_target(target_id)
        expected_worker_id = worker_id or target.worker_id
        return any(
            job.target_id == target_id
            and job.worker_id == expected_worker_id
            and job.command == "snapshots.list"
            and JobStatus.normalize(job.status) in (JobStatus.PENDING, JobStatus.IN_PROGRESS)
            for job in self.job_repository.list()
        )

    def dispatch_snapshot_sync_for_target(
        self,
        target_id: str,
        requested_by: str = "system",
        trigger: str = "manual",
    ) -> JobRecord:
        target = self._require_target(target_id)
        self._require_eligible_worker(target.worker_id)
        self._reconcile_expired_jobs()
        existing = next(
            (
                job
                for job in self.job_repository.list()
                if job.target_id == target.id
                and job.worker_id == target.worker_id
                and job.command == "snapshots.list"
                and JobStatus.normalize(job.status) in (JobStatus.PENDING, JobStatus.IN_PROGRESS)
            ),
            None,
        )
        if existing is not None:
            return existing
        payload = self._build_snapshot_list_payload(target)
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="snapshots.list",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger=trigger,
        )

    def dispatch_snapshot_read(
        self,
        target_id: str,
        operation: str,
        snapshot_id: str,
        path: str = "",
        request_id: Optional[str] = None,
        max_entries: Optional[int] = None,
        query: Optional[str] = None,
        archive: bool = False,
        max_output_bytes: Optional[int] = None,
        requested_by: str = "api",
    ) -> Dict[str, Any]:
        """Queue a bounded explorer read without waiting for worker polling."""
        target = self._require_target(target_id)
        normalized_operation = operation if isinstance(operation, str) else ""
        if normalized_operation not in self.SNAPSHOT_READ_COMMANDS:
            raise ValueError("unsupported snapshot read operation")
        normalized_snapshot_id = self._validate_snapshot_id(snapshot_id)
        normalized_path = self._normalize_snapshot_path(path)
        normalized_request_id = self._normalize_request_id(request_id)
        normalized_max_entries = self._validate_max_entries(max_entries)
        normalized_query = self._validate_search_query(query)
        if normalized_query is not None and normalized_operation not in {"search", "find"}:
            raise ValueError("query is only supported for snapshot search")
        if not isinstance(archive, bool):
            raise ValueError("archive must be a boolean")
        if archive and normalized_operation != "dump":
            raise ValueError("archive is only supported for snapshot dump")
        normalized_max_output_bytes = self._validate_max_output_bytes(max_output_bytes, archive)
        self._validate_snapshot_metadata_target(target.id, normalized_snapshot_id)
        request = SnapshotReadRequest(
            snapshot_id=normalized_snapshot_id,
            path=normalized_path,
            operation=normalized_operation,
            schema_version=1,
            request_id=normalized_request_id,
            target_id=target.id,
            max_entries=normalized_max_entries,
        )

        payload = self._build_snapshot_read_payload(
            target=target,
            snapshot_id=request.snapshot_id,
            path=request.path,
            operation=request.operation,
            request_id=request.request_id,
            max_entries=request.max_entries,
            query=normalized_query,
            archive=archive,
            max_output_bytes=normalized_max_output_bytes,
        )
        job = self.dispatch_job(
            worker_id=target.worker_id,
            command=self.SNAPSHOT_READ_COMMANDS[normalized_operation],
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger="interactive",
        )
        return self._snapshot_job_contract(job)

    def snapshot_job_contract(self, job_id: str) -> Dict[str, Any]:
        """Return the v2 read contract without exposing job payloads or logs."""
        return self._snapshot_job_contract(self._require_job(job_id))

    def snapshot_catalog(self, target_id: str) -> List[Dict[str, Any]]:
        self._require_target(target_id)
        return [
            {
                "snapshot_id": snapshot.snapshot_id,
                "created_at": snapshot.created_at,
                "hostname": snapshot.hostname,
                "paths": list(snapshot.paths or []),
                "tags": list(snapshot.tags or []),
            }
            for snapshot in self.snapshot_repository.list_by_target(target_id)
        ]

    def fetch_interactive_jobs_for_worker(self, worker_id: str) -> List[JobRecord]:
        """Claim through the durable lease path, returning explorer work first."""
        jobs = self.fetch_jobs_for_worker(worker_id)
        interactive = {"snapshots.list", "snapshot.ls", "snapshot.search", "snapshot.find", "snapshot.dump", "stats.get"}
        return sorted(jobs, key=lambda job: job.command not in interactive)

    def dispatch_snapshot_ls(self, target_id: str, snapshot_id: str, path: str = "") -> Dict[str, Any]:
        target = self._require_target(target_id)
        payload = self._build_snapshot_ls_payload(target, snapshot_id, path)
        job = self.dispatch_job(
            worker_id=target.worker_id,
            command="snapshot.ls",
            payload=payload,
            requested_by="api",
            target_id=target.id,
            trigger="manual",
        )
        result = self._wait_for_job_completion(job.id, timeout_seconds=60)
        job_status = result.get("status")
        logs = result.get("logs", "") or ""
        result_summary = result.get("result_summary") or {}
        if job_status == "failed" or job_status == JobStatus.CANCELED:
            structured_error = result_summary.get("error")
            if isinstance(structured_error, str) and structured_error.strip():
                error_msg = structured_error.strip()
            else:
                error_msg = logs.strip().splitlines()[-1] if logs.strip() else f"job {job_status}"
            return {"entries": [], "job_id": job.id, "error": f"restic ls failed: {error_msg}"}
        if job_status == "timeout":
            return {"entries": [], "job_id": job.id, "error": "restic ls timed out (60s)"}
        if "entries" in result_summary:
            entries = result_summary.get("entries") or []
        else:
            entries = []
            try:
                parsed = json.loads(logs)
                entries = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                for line in logs.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return {"entries": entries, "job_id": job.id}

    def dispatch_snapshot_dump(self, target_id: str, snapshot_id: str, path: str) -> Dict[str, Any]:
        target = self._require_target(target_id)
        payload = self._build_snapshot_dump_payload(target, snapshot_id, path)
        job = self.dispatch_job(
            worker_id=target.worker_id,
            command="snapshot.dump",
            payload=payload,
            requested_by="api",
            target_id=target.id,
            trigger="manual",
        )
        result = self._wait_for_job_completion(job.id, timeout_seconds=120)
        result_summary = result.get("result_summary") or {}
        return {
            "b64_content": result_summary.get("b64_content", ""),
            "job_id": job.id,
            "success": result.get("status") == "succeeded",
            "error": result_summary.get("stderr", "") if result.get("status") != "succeeded" else "",
        }

    def storage_about(self, profile_id: str) -> Dict[str, Any]:
        """Query the selected storage profile's configured rclone remote.

        Resolves the profile-scoped rclone.conf secret, dispatches one durable
        ``storage.about`` job to the first online worker in repository order,
        waits for its completion, and returns the truthful per-card contract
        ``{profile_id, state, metrics, error, job_id}``. Secrets, the remote
        name, and the job payload are never serialized into the response.
        """
        profile = self._require_storage_profile(profile_id)
        remote = self._profile_rclone_remote(profile)
        if remote is None:
            return {
                "profile_id": profile.id,
                "state": "not-configured",
                "metrics": None,
                "error": None,
                "job_id": None,
            }
        worker = self._select_online_worker()
        if worker is None:
            return {
                "profile_id": profile.id,
                "state": "transient-failure",
                "metrics": None,
                "error": "no online worker available for storage.about",
                "job_id": None,
            }
        payload = self._build_storage_about_payload(
            profile=profile,
            remote_name=remote["remote_name"],
            rclone_content=remote["rclone_content"],
        )
        job = self.dispatch_job(
            worker_id=worker.id,
            command="storage.about",
            payload=payload,
            requested_by="api",
            trigger="interactive",
        )
        result = self._wait_for_job_completion(job.id, timeout_seconds=60)
        return self._storage_about_contract(profile.id, job.id, result)

    def _global_rclone_remote(self, settings: Optional[SettingsRecord]) -> Optional[Dict[str, str]]:
        """Resolve the global Settings rclone.conf secret into remote + content."""
        if not settings or not settings.rclone_conf_secret_id:
            return None
        secret = self.secret_repository.get(settings.rclone_conf_secret_id)
        if not secret:
            return None
        rclone_content = self.secret_codec.decrypt(secret.ciphertext)
        remote_name = self._extract_rclone_remote_name(settings.rclone_conf_secret_id)
        if not remote_name:
            return None
        return {"remote_name": remote_name, "rclone_content": rclone_content}

    def _profile_rclone_remote(self, profile: StorageProfileRecord) -> Optional[Dict[str, str]]:
        """Resolve a profile's rclone file secret into remote + content."""
        if (profile.backend_type or "").strip().lower() != "rclone":
            return None
        resolved_files = self._resolve_file_secret_refs(profile.file_secret_refs or {})
        rclone_file = next(
            (
                file_spec
                for file_spec in resolved_files
                if "rclone" in file_spec.get("secret_name", "").lower()
                or file_spec.get("container_path", "").endswith("rclone.conf")
            ),
            None,
        )
        if not rclone_file:
            return None
        remote_name = self._extract_rclone_remote_name(rclone_file["secret_id"])
        if not remote_name:
            return None
        return {"remote_name": remote_name, "rclone_content": rclone_file["content"]}

    def _storage_about_contract(self, profile_id: str, job_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        status = result.get("status")
        result_summary = result.get("result_summary") or {}
        if not isinstance(result_summary, dict):
            result_summary = {}
        state = result_summary.get("state")
        if status == "timeout":
            return {
                "profile_id": profile_id,
                "state": "transient-failure",
                "metrics": None,
                "error": "storage.about timed out (60s)",
                "job_id": job_id,
            }
        if status == JobStatus.SUCCEEDED and state == "about-unsupported":
            return {
                "profile_id": profile_id,
                "state": "about-unsupported",
                "metrics": None,
                "error": None,
                "job_id": job_id,
            }
        if status == JobStatus.SUCCEEDED and state == "available":
            raw_metrics = result_summary.get("metrics")
            metrics = None
            if isinstance(raw_metrics, dict):
                metrics = {
                    field: int(value)
                    for field, value in raw_metrics.items()
                    if field in {"total", "used", "free", "trashed"}
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                }
            return {
                "profile_id": profile_id,
                "state": "available",
                "metrics": metrics or None,
                "error": None,
                "job_id": job_id,
            }
        error = result_summary.get("error")
        if not isinstance(error, str) or not error:
            logs = result.get("logs") or ""
            error = logs.strip().splitlines()[-1] if logs.strip() else f"storage.about job {status}"
        return {
            "profile_id": profile_id,
            "state": "transient-failure",
            "metrics": None,
            "error": error,
            "job_id": job_id,
        }

    def _select_online_worker(self) -> Optional[WorkerRecord]:
        """Return the first online worker in repository (created-at) order."""
        for worker in self.worker_repository.list():
            if self._worker_status(worker) == WorkerStatus.ONLINE:
                return worker
        return None

    def dispatch_restore_for_target(
        self,
        target_id: str,
        requested_by: str = "system",
        restore_source: Optional[str] = None,
        restore_target_path: Optional[str] = None,
        dry_run: bool = True,
        force_overwrite: bool = False,
        stop_containers: Optional[bool] = None,
        chown: Optional[str] = None,
        layout: Optional[str] = None,
        snapshot_id: Optional[str] = None,
    ) -> JobRecord:
        target = self._require_target(target_id)
        if snapshot_id:
            restore_source = snapshot_id
        payload = self._build_restore_payload(
            target=target,
            restore_source=restore_source,
            restore_target_path=restore_target_path,
            dry_run=dry_run,
            force_overwrite=force_overwrite,
            stop_containers=stop_containers,
            chown=chown,
            layout=layout,
        )
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="restore.dry_run" if dry_run else "restore.run",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger="manual",
        )

    def dispatch_stats_for_target(self, target_id: str, requested_by: str = "system") -> JobRecord:
        target = self._require_target(target_id)
        payload = self._build_stats_payload(target)
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="stats.get",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger="manual",
        )

    def dispatch_retention_for_target(self, target_id: str, requested_by: str = "system") -> JobRecord:
        target = self._require_target(target_id)
        payload = self._build_retention_payload(target)
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="retention.run",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger="manual",
        )

    @staticmethod
    def _safe_backup_name(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (name or "").strip("/"))
        return cleaned or "root"

    @classmethod
    def _repository_kind(cls, repository: Any) -> str:
        value = str(repository or "").strip().lower()
        if value.startswith("rclone:"):
            return "rclone"
        if value.startswith("s3:") or value.startswith("s3://"):
            return "s3"
        if (
            value.startswith("local:")
            or value.startswith("file:")
            or value.startswith("/")
            or ("/" in value and "://" not in value and not re.match(r"^[a-z][a-z0-9+.-]*:", value))
        ):
            return "local"
        return "unknown"

    @classmethod
    def _repository_display(cls, repository: Any) -> Optional[str]:
        text = " ".join(str(repository or "").split())
        if not text:
            return None
        text = text.split("?", 1)[0].split("#", 1)[0]
        text = re.sub(r"(?i)(://)[^/\s@]+@", r"\1<redacted>@", text)
        text = re.sub(r"(?i)(^|[:/])[^/\s:@]+:[^/@\s]+@", r"\1<redacted>@", text)
        text = re.sub(r"(?i)(password|passphrase|token|secret|access[_-]?key|secret[_-]?key|credential)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", text)
        if len(text) > cls.MAX_REPOSITORY_DISPLAY_LENGTH:
            return f"{text[: cls.MAX_REPOSITORY_DISPLAY_LENGTH - 3]}..."
        return text

    def _profile_has_rclone_file(
        self,
        profile: Optional[StorageProfileRecord],
        resolved_files: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        if profile is None:
            return False
        if resolved_files is not None:
            for file_spec in resolved_files:
                path = str(file_spec.get("container_path") or "").lower()
                name = str(file_spec.get("secret_name") or "").lower()
                if "rclone" in path or "rclone" in name or path.endswith(".conf"):
                    return True
        if (profile.backend_type or "").strip().lower() == "rclone" and profile.file_secret_refs:
            return True
        for path in (profile.file_secret_refs or {}):
            if "rclone" in str(path).lower() or str(path).lower().endswith(".conf"):
                return True
        return False

    def _settings_repository_for_target(
        self,
        target: BackupTargetRecord,
        settings: Optional[SettingsRecord],
    ) -> Optional[str]:
        if target.backup_strategy != "restic" or not settings or not settings.restic_repository_base:
            return None
        base = settings.restic_repository_base.strip("/")
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "-", target.name or target.id)
        safe_name = safe_name or "root"
        if re.match(r"^[a-z]+:", base, re.IGNORECASE):
            return f"{base}/{safe_name}"
        if settings.rclone_conf_secret_id:
            remote_name = self._extract_rclone_remote_name(settings.rclone_conf_secret_id)
            if remote_name:
                return f"rclone:{remote_name}:{base}/{safe_name}"
        return f"{base}/{safe_name}"

    def _storage_context(
        self,
        target: BackupTargetRecord,
        environment: Optional[Dict[str, str]] = None,
        resolved_files: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        environment = environment if isinstance(environment, dict) else {}
        profile = self.storage_profile_repository.get(target.storage_profile_id) if target.storage_profile_id else None
        settings = self.get_settings()
        target_environment = target.runtime_environment or {}
        profile_environment = profile.environment if profile else {}
        profile_secret_refs = profile.secret_refs if profile else {}
        repository = None
        repository_source = "unconfigured"

        if "RESTIC_REPOSITORY" in target_environment:
            repository = target_environment.get("RESTIC_REPOSITORY")
            repository_source = "target" if repository else "unconfigured"
        elif "RESTIC_REPOSITORY" in profile_environment or "RESTIC_REPOSITORY" in profile_secret_refs:
            repository = environment.get("RESTIC_REPOSITORY")
            repository_source = "profile" if repository else "unconfigured"
        else:
            repository = environment.get("RESTIC_REPOSITORY") or self._settings_repository_for_target(target, settings)
            repository_source = "settings" if repository else "unconfigured"

        repository_kind = self._repository_kind(repository) if repository else "unknown"
        backend_type = None
        if profile is not None:
            backend_type = str(profile.backend_type or "").strip().lower() or None
        elif repository:
            backend_type = repository_kind
        rclone_source = "none"
        if self._profile_has_rclone_file(profile, resolved_files):
            rclone_source = "profile"
        elif settings and settings.rclone_conf_secret_id:
            rclone_source = "settings"

        return {
            "storage_profile_id": profile.id if profile else target.storage_profile_id,
            "storage_profile_name": profile.name if profile else None,
            "backend_type": backend_type,
            "repository_source": repository_source,
            "repository_kind": repository_kind,
            "repository_display": self._repository_display(repository),
            "rclone_config_source": rclone_source,
        }

    @classmethod
    def _safe_storage_context(cls, context: Any) -> Dict[str, Any]:
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
        result = {key: context.get(key) for key in allowed}
        for key in ("storage_profile_id", "storage_profile_name", "backend_type", "repository_display"):
            value = result.get(key)
            if value is not None:
                result[key] = " ".join(str(value).split())[: cls.MAX_REPOSITORY_DISPLAY_LENGTH]
        if result.get("repository_display"):
            result["repository_display"] = cls._repository_display(result["repository_display"])
        for key in ("repository_source", "repository_kind", "rclone_config_source"):
            value = str(result.get(key) or "")
            result[key] = value if value in {"target", "profile", "settings", "unconfigured", "rclone", "s3", "local", "unknown", "none"} else "unknown"
        return result

    @classmethod
    def _validate_progress_payload(
        cls,
        sequence: Any,
        progress: Any,
        log_lines: Any,
    ) -> tuple[int, Dict[str, Any], List[str]]:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0 or sequence > 1_000_000_000:
            raise ValueError("progress sequence is outside the permitted bounds")
        if not isinstance(progress, dict) or len(progress) > len(cls.PROGRESS_FIELDS):
            raise ValueError("progress must be a bounded object")
        normalized: Dict[str, Any] = {}
        numeric_fields = {"percent_done", "files_done", "total_files", "bytes_done", "total_bytes"}
        for key, value in progress.items():
            if key not in cls.PROGRESS_FIELDS:
                raise ValueError(f"unsupported progress field: {key}")
            if key in numeric_fields:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError(f"progress field {key} must be numeric")
                if value < 0 or value > 100_000_000_000_000:
                    raise ValueError(f"progress field {key} is outside the permitted bounds")
                if key == "percent_done" and value > 100:
                    raise ValueError("progress percent_done must be between 0 and 100")
                normalized[key] = round(float(value), 3) if isinstance(value, float) else int(value)
            else:
                if not isinstance(value, str) or len(value) > 512 or any(ord(character) < 32 for character in value):
                    raise ValueError(f"progress field {key} is invalid")
                normalized[key] = value
        if not isinstance(log_lines, list) or len(log_lines) > cls.MAX_PROGRESS_LOG_LINES:
            raise ValueError("progress log_lines are outside the permitted bounds")
        lines: List[str] = []
        total_chars = 0
        for line in log_lines:
            if not isinstance(line, str) or len(line) > 16 * 1024 or any(ord(character) < 9 for character in line):
                raise ValueError("progress log line is invalid")
            total_chars += len(line)
            if total_chars > cls.MAX_PROGRESS_LOG_CHARS:
                raise ValueError("progress log_lines exceed the permitted size")
            lines.append(line)
        return sequence, normalized, lines

    def _normalize_runtime_volumes(self, volumes: Dict[str, Dict[str, str]], target: BackupTargetRecord) -> Dict[str, Dict[str, str]]:
        normalized: Dict[str, Dict[str, str]] = {}
        for source, spec in (volumes or {}).items():
            bind = spec.get("bind") if isinstance(spec, dict) else None
            mode = spec.get("mode", "ro") if isinstance(spec, dict) else "ro"
            if not bind:
                continue
            if bind.startswith("/backup/"):
                normalized[source] = {"bind": bind, "mode": mode}
            else:
                safe = self._safe_backup_name(bind)
                normalized[source] = {"bind": f"/backup/{safe}", "mode": mode}
        return normalized

    def _normalized_backup_sources(self, volumes: Dict[str, Dict[str, str]]) -> List[str]:
        seen = set()
        sources = []
        for spec in (volumes or {}).values():
            if not isinstance(spec, dict):
                continue
            bind = spec.get("bind")
            if bind and bind not in seen:
                seen.add(bind)
                sources.append(bind)
        return sources

    def _build_backup_payload(
        self,
        target: BackupTargetRecord,
        backup_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        configured_mode = self._validate_backup_mode(target.backup_mode)
        effective_mode = configured_mode if backup_mode is None else self._validate_backup_mode(backup_mode)
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        environment.setdefault("BACKUP_STRATEGY", target.backup_strategy)
        volumes = self._normalize_runtime_volumes(volumes, target)
        if target.volume_targets and "BACKUP_SOURCES" not in environment:
            environment["BACKUP_SOURCES"] = " ".join(self._normalized_backup_sources(volumes))
        environment["BACKUP_STOP_CONTAINERS"] = "true" if effective_mode == "cold" else "false"
        if effective_mode == "cold":
            environment.setdefault("BACKUP_CUSTOM_LABEL", f"control-plane.target={target.id}")
        storage_context = self._storage_context(target, environment, resolved_files)

        payload = {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "volume_targets": target.volume_targets,
            "backup_mode": effective_mode,
            "backup_strategy": target.backup_strategy,
            "image": target.runtime_image,
            "command": target.runtime_command,
            "environment": environment,
            "volumes": volumes,
            "network_mode": target.runtime_network_mode,
            "resolved_files": resolved_files,
            "labels": target.labels,
            "storage_context": storage_context,
        }
        return payload

    def _build_snapshot_list_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        storage_context = self._storage_context(target, environment, resolved_files)
        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": "restic snapshots --json",
            "cache_generation": self._snapshot_cache_generation(target),
            "environment": environment,
            "volumes": volumes,
            "network_mode": "bridge",
            "resolved_files": resolved_files,
            "labels": target.labels,
            "storage_context": storage_context,
        }

    def _build_snapshot_ls_payload(self, target: BackupTargetRecord, snapshot_id: str, path: str = "") -> Dict[str, Any]:
        return self._build_snapshot_read_payload(
            target=target,
            snapshot_id=self._validate_snapshot_id(snapshot_id),
            path=self._normalize_snapshot_path(path),
            operation="browse",
        )

    def _build_snapshot_dump_payload(self, target: BackupTargetRecord, snapshot_id: str, path: str) -> Dict[str, Any]:
        return self._build_snapshot_read_payload(
            target=target,
            snapshot_id=self._validate_snapshot_id(snapshot_id),
            path=self._normalize_snapshot_path(path),
            operation="dump",
        )

    def _build_storage_about_payload(
        self,
        profile: StorageProfileRecord,
        remote_name: str,
        rclone_content: str,
    ) -> Dict[str, Any]:
        """Build the durable ``storage.about`` payload for a profile's remote.

        The payload carries only the validated remote name, the rclone.conf
        secret content needed by the worker runtime, and the runtime image.
        ``resolved_files`` mirrors the ``RCLONE_CONF_CONTENT`` convention so
        the runtime writes and mounts the config as a file; secrets are
        redacted by the worker before any result/log crosses the boundary.
        """
        return {
            "profile_id": profile.id,
            "remote": f"{remote_name}:",
            "environment": {
                "RCLONE_CONF_CONTENT": rclone_content,
                "RCLONE_CONFIG": "/run/secrets/rclone.conf",
            },
            "resolved_files": [
                {
                    "container_path": "/run/secrets/rclone.conf",
                    "content": rclone_content,
                    "secret_id": "",
                    "secret_name": "rclone.conf",
                }
            ],
            "image": None,
            "network_mode": "bridge",
            "labels": profile.labels,
        }

    def _build_snapshot_read_payload(
        self,
        target: BackupTargetRecord,
        snapshot_id: str,
        path: str,
        operation: str,
        request_id: Optional[str] = None,
        max_entries: Optional[int] = None,
        query: Optional[str] = None,
        archive: bool = False,
        max_output_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        storage_context = self._storage_context(target, environment, resolved_files)
        if operation in {"browse", "search", "find"}:
            command = ["restic", "ls", "--json", snapshot_id, path]
        elif operation == "dump" and archive:
            command = ["restic", "dump", "-a", "zip", snapshot_id, path]
        elif operation == "dump":
            command = ["restic", "dump", snapshot_id, path]
        else:
            raise ValueError("unsupported snapshot read operation")
        payload = {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": command,
            "environment": environment,
            "volumes": volumes,
            "network_mode": "bridge",
            "resolved_files": resolved_files,
            "labels": target.labels,
            "cache_generation": self._snapshot_cache_generation(target),
            "storage_context": storage_context,
        }
        if request_id is not None:
            payload.update(
                {
                    "request_id": request_id,
                    "schema_version": 1,
                    "operation": operation,
                    "path": path,
                    "snapshot_id": snapshot_id,
                }
            )
        if max_entries is not None:
            payload["max_entries"] = max_entries
        if query is not None:
            payload["query"] = query
        if max_output_bytes is not None:
            payload["max_output_bytes"] = max_output_bytes
        if archive:
            payload["archive"] = True
        return payload

    def _snapshot_cache_generation(self, target: BackupTargetRecord) -> int:
        if self.cache_repository is None:
            return 0
        record = self.cache_repository.get_generation(target.id, self._repository_fingerprint(target))
        try:
            generation = (
                record.get("generation", 0)
                if isinstance(record, dict)
                else getattr(record, "generation", 0)
            )
            return max(0, int(generation)) if record is not None else 0
        except (TypeError, ValueError):
            return 0

    def _build_stats_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        storage_context = self._storage_context(target, environment, resolved_files)
        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": "restic stats --mode raw-data --json",
            "environment": environment,
            "volumes": volumes,
            "network_mode": target.runtime_network_mode,
            "resolved_files": resolved_files,
            "labels": target.labels,
            "storage_context": storage_context,
        }

    def _build_retention_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        storage_context = self._storage_context(target, environment, resolved_files)
        policy = self._require_retention_policy(target.retention_policy_id) if target.retention_policy_id else None
        if policy is None:
            raise ValueError(f"target {target.id} has no retention policy configured")

        command_parts = ["restic", "forget"]
        if policy.keep_last is not None:
            command_parts.extend(["--keep-last", str(policy.keep_last)])
        if policy.keep_hourly is not None:
            command_parts.extend(["--keep-hourly", str(policy.keep_hourly)])
        if policy.keep_daily is not None:
            command_parts.extend(["--keep-daily", str(policy.keep_daily)])
        if policy.keep_weekly is not None:
            command_parts.extend(["--keep-weekly", str(policy.keep_weekly)])
        if policy.keep_monthly is not None:
            command_parts.extend(["--keep-monthly", str(policy.keep_monthly)])
        if policy.keep_yearly is not None:
            command_parts.extend(["--keep-yearly", str(policy.keep_yearly)])
        if policy.prune:
            command_parts.append("--prune")

        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": " ".join(command_parts),
            "environment": environment,
            "volumes": volumes,
            "network_mode": target.runtime_network_mode,
            "resolved_files": resolved_files,
            "labels": target.labels,
            "storage_context": storage_context,
        }

    def _build_restore_payload(
        self,
        target: BackupTargetRecord,
        restore_source: Optional[str],
        restore_target_path: Optional[str],
        dry_run: bool,
        force_overwrite: bool,
        stop_containers: Optional[bool],
        chown: Optional[str],
        layout: Optional[str],
    ) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        defaults = dict(target.restore_defaults)
        environment.update({
            "RESTORE_MODE": "true",
            "RESTORE_BACKUP_STRATEGY": target.backup_strategy,
            "RESTORE_DRY_RUN": "true" if dry_run else "false",
            "RESTORE_FORCE_OVERWRITE": "true" if force_overwrite else "false",
            "RESTORE_TARGET_PATH": restore_target_path or defaults.get("target_path") or defaults.get("RESTORE_TARGET_PATH") or "/backup",
            "RESTORE_LAYOUT": layout or defaults.get("layout") or defaults.get("RESTORE_LAYOUT") or "auto",
            "RESTORE_STOP_CONTAINERS": "true" if (stop_containers if stop_containers is not None else defaults.get("stop_containers", False)) else "false",
            "RESTORE_STOP_LABEL": "docker-volume-backup.stop-during-backup=true",
        })
        if target.labels.get("BACKUP_CUSTOM_LABEL"):
            environment["RESTORE_CUSTOM_LABEL"] = target.labels["BACKUP_CUSTOM_LABEL"]
        final_source = restore_source or defaults.get("source") or defaults.get("RESTORE_SOURCE")
        if final_source:
            environment["RESTORE_SOURCE"] = final_source
        final_chown = chown or defaults.get("chown") or defaults.get("RESTORE_CHOWN")
        if final_chown:
            environment["RESTORE_CHOWN"] = final_chown

        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": target.runtime_command,
            "environment": environment,
            "volumes": volumes,
            "network_mode": target.runtime_network_mode,
            "resolved_files": resolved_files,
            "labels": target.labels,
        }

    def _resolve_runtime_dependencies(self, target: BackupTargetRecord):
        environment = {}
        volumes = {}
        resolved_files = []
        profile = None
        profile_has_rclone_file = False

        if target.storage_profile_id:
            profile = self._require_storage_profile(target.storage_profile_id)
            environment.update(profile.environment)
            environment.update(self._resolve_secret_refs(profile.secret_refs))
            volumes.update(profile.runtime_volumes)
            resolved_files.extend(self._resolve_file_secret_refs(profile.file_secret_refs))
            profile_has_rclone_file = self._profile_has_rclone_file(profile, resolved_files)
            for file_spec in resolved_files:
                if "rclone" in file_spec.get("secret_name", "").lower() or file_spec.get("container_path", "").endswith("rclone.conf"):
                    environment["RCLONE_CONF_CONTENT"] = file_spec["content"]
                    if "RCLONE_CONFIG" not in environment:
                        environment["RCLONE_CONFIG"] = "/run/secrets/rclone.conf"
                    break

        environment.update(target.runtime_environment)
        volumes.update(target.runtime_volumes)

        if target.backup_strategy == "restic":
            settings = self.get_settings()
            if settings and settings.restic_repository_base and "RESTIC_REPOSITORY" not in environment:
                repository = self._settings_repository_for_target(target, settings)
                if repository:
                    environment["RESTIC_REPOSITORY"] = repository
            if "RESTIC_PASSWORD" not in environment:
                password_secret_id = target.restic_password_secret_id
                if not password_secret_id and settings:
                    password_secret_id = settings.restic_password_secret_id
                if password_secret_id:
                    secret = self.secret_repository.get(password_secret_id)
                    if secret:
                        environment["RESTIC_PASSWORD"] = self.secret_codec.decrypt(secret.ciphertext)
            if settings and settings.rclone_conf_secret_id and not profile_has_rclone_file:
                existing_paths = {f["container_path"] for f in resolved_files}
                rclone_path = "/run/secrets/rclone.conf"
                if rclone_path not in existing_paths:
                    secret = self.secret_repository.get(settings.rclone_conf_secret_id)
                    if secret:
                        rclone_content = self.secret_codec.decrypt(secret.ciphertext)
                        resolved_files.append({
                            "container_path": rclone_path,
                            "content": rclone_content,
                            "secret_id": secret.id,
                            "secret_name": secret.name,
                        })
                        environment["RCLONE_CONFIG"] = rclone_path
                        environment["RCLONE_CONF_CONTENT"] = rclone_content

        return environment, volumes, resolved_files

    def get_settings(self) -> Optional[SettingsRecord]:
        if not self.settings_repository:
            return None
        return self.settings_repository.get()

    def update_settings(
        self,
        restic_repository_base: Optional[str] = None,
        restic_password_secret_id: Optional[str] = None,
        rclone_conf_secret_id: Optional[str] = None,
        global_cron_expression: Optional[str] = None,
        control_plane_public_url: Optional[str] = None,
    ) -> SettingsRecord:
        if not self.settings_repository:
            raise ValueError("settings repository not configured")
        existing = self.settings_repository.get() or SettingsRecord()
        if restic_repository_base is not None:
            existing.restic_repository_base = restic_repository_base
        if restic_password_secret_id is not None:
            if restic_password_secret_id:
                self._require_secret(restic_password_secret_id)
            existing.restic_password_secret_id = restic_password_secret_id or None
        if rclone_conf_secret_id is not None:
            if rclone_conf_secret_id:
                secret = self._require_secret(rclone_conf_secret_id)
                if secret.secret_type != "file":
                    raise ValueError("rclone conf secret must be of type 'file'")
                existing.rclone_conf_secret_id = rclone_conf_secret_id or None
        if global_cron_expression is not None:
            existing.global_cron_expression = global_cron_expression.strip() or None
        if control_plane_public_url is not None:
            existing.control_plane_public_url = control_plane_public_url.strip()
        existing.updated_at = utcnow()
        return self.settings_repository.save(existing)

    def create_storage_profile(
        self,
        name: str,
        backend_type: str,
        environment: Optional[Dict[str, str]] = None,
        secret_refs: Optional[Dict[str, str]] = None,
        file_secret_refs: Optional[Dict[str, str]] = None,
        runtime_volumes: Optional[Dict[str, Dict[str, str]]] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> StorageProfileRecord:
        normalized_file_secret_refs = file_secret_refs or {}
        self._validate_storage_profile_file_secret_refs(
            backend_type=backend_type,
            file_secret_refs=normalized_file_secret_refs,
        )
        profile = StorageProfileRecord(
            name=name,
            backend_type=backend_type,
            environment=environment or {},
            secret_refs=secret_refs or {},
            file_secret_refs=normalized_file_secret_refs,
            runtime_volumes=runtime_volumes or {},
            labels=labels or {},
        )
        return self.storage_profile_repository.save(profile)

    def update_storage_profile(
        self,
        profile_id: str,
        name: Optional[str] = None,
        backend_type: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        secret_refs: Optional[Dict[str, str]] = None,
        file_secret_refs: Optional[Dict[str, str]] = None,
        runtime_volumes: Optional[Dict[str, Dict[str, str]]] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> StorageProfileRecord:
        existing = self.storage_profile_repository.get(profile_id)
        if not existing:
            raise ValueError("storage profile not found")
        if name is not None:
            existing.name = name
        if backend_type is not None:
            existing.backend_type = backend_type
        if environment is not None:
            existing.environment = environment
        if secret_refs is not None:
            existing.secret_refs = secret_refs
        if file_secret_refs is not None:
            self._validate_storage_profile_file_secret_refs(
                backend_type=existing.backend_type,
                file_secret_refs=file_secret_refs,
            )
            existing.file_secret_refs = file_secret_refs
        if runtime_volumes is not None:
            existing.runtime_volumes = runtime_volumes
        if labels is not None:
            existing.labels = labels
        existing.updated_at = utcnow()
        return self.storage_profile_repository.save(existing)

    def delete_storage_profile(self, profile_id: str) -> bool:
        return self.storage_profile_repository.delete(profile_id)

    def create_retention_policy(
        self,
        name: str,
        keep_last: Optional[int] = None,
        keep_hourly: Optional[int] = None,
        keep_daily: Optional[int] = None,
        keep_weekly: Optional[int] = None,
        keep_monthly: Optional[int] = None,
        keep_yearly: Optional[int] = None,
        prune: bool = True,
        labels: Optional[Dict[str, str]] = None,
    ) -> RetentionPolicyRecord:
        policy = RetentionPolicyRecord(
            name=name,
            keep_last=keep_last,
            keep_hourly=keep_hourly,
            keep_daily=keep_daily,
            keep_weekly=keep_weekly,
            keep_monthly=keep_monthly,
            keep_yearly=keep_yearly,
            prune=prune,
            labels=labels or {},
        )
        return self.retention_policy_repository.save(policy)

    def create_secret(
        self,
        name: str,
        scope: str,
        secret_type: str,
        plaintext: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SecretRecord:
        normalized_secret_type = (secret_type or "generic").strip().lower()
        if normalized_secret_type not in self.SUPPORTED_SECRET_TYPES:
            raise ValueError(
                f"unsupported secret_type: {normalized_secret_type}. supported values: "
                + ", ".join(sorted(self.SUPPORTED_SECRET_TYPES))
            )
        secret = SecretRecord(
            name=name,
            scope=scope,
            secret_type=normalized_secret_type,
            ciphertext=self.secret_codec.encrypt(plaintext),
            key_version=getattr(self.secret_codec, "key_version", "v1"),
            metadata=metadata or {},
        )
        return self.secret_repository.save(secret)

    def list_storage_profiles(self) -> List[StorageProfileRecord]:
        return self.storage_profile_repository.list()

    def list_retention_policies(self) -> List[RetentionPolicyRecord]:
        return self.retention_policy_repository.list()

    def list_secrets(self) -> List[SecretRecord]:
        return self.secret_repository.list()

    def get_secret(self, secret_id: str) -> SecretRecord:
        return self._require_secret(secret_id)

    def update_secret(self, secret_id: str, plaintext: Optional[str] = None, name: Optional[str] = None) -> SecretRecord:
        secret = self._require_secret(secret_id)
        if name is not None:
            secret.name = name.strip() or secret.name
        if plaintext is not None and plaintext != "":
            secret.ciphertext = self.secret_codec.encrypt(plaintext)
            secret.key_version = getattr(self.secret_codec, "key_version", "v1")
        secret.updated_at = utcnow()
        return self.secret_repository.save(secret)

    def find_secret_usages(self, secret_id: str) -> List[Dict[str, Any]]:
        usages: List[Dict[str, Any]] = []
        for target in self.target_repository.list():
            if target.restic_password_secret_id == secret_id:
                usages.append({"type": "target", "id": target.id, "name": target.name, "field": "restic_password"})
        for profile in self.storage_profile_repository.list():
            for env_name, ref_id in (profile.secret_refs or {}).items():
                if ref_id == secret_id:
                    usages.append({"type": "storage_profile", "id": profile.id, "name": profile.name, "field": f"env:{env_name}"})
            for container_path, ref_id in (profile.file_secret_refs or {}).items():
                if ref_id == secret_id:
                    usages.append({"type": "storage_profile", "id": profile.id, "name": profile.name, "field": f"file:{container_path}"})
        settings = self.get_settings()
        if settings:
            if settings.restic_password_secret_id == secret_id:
                usages.append({"type": "settings", "id": "global", "name": "Settings", "field": "restic_password"})
            if settings.rclone_conf_secret_id == secret_id:
                usages.append({"type": "settings", "id": "global", "name": "Settings", "field": "rclone_conf"})
        return usages

    def delete_secret(self, secret_id: str) -> None:
        self._require_secret(secret_id)
        usages = self.find_secret_usages(secret_id)
        if usages:
            refs = ", ".join(f"{u['type']}:{u['name']}({u['field']})" for u in usages)
            raise ValueError(f"secret is in use by: {refs}")
        self.secret_repository.delete(secret_id)

    def fetch_jobs_for_worker(self, worker_id: str) -> List[JobRecord]:
        self._require_eligible_worker(worker_id)
        self._reconcile_expired_jobs()
        jobs = self.job_repository.claim_pending_for_worker(worker_id)
        for job in jobs:
            self._publish_job_event(job)
        return jobs

    def is_job_cancelled(self, worker_id: str, job_id: str) -> bool:
        """Return only cancellation state for an authenticated owning worker."""
        self._require_worker(worker_id)
        job = self._require_job(job_id)
        if job.worker_id != worker_id or (
            job.owner_worker_id is not None and job.owner_worker_id != worker_id
        ):
            raise ValueError(f"worker '{worker_id}' does not own job '{job_id}'")
        return JobStatus.normalize(job.status) == JobStatus.CANCELED

    def renew_job_lease(self, worker_id: str, job_id: str, lease_token: str) -> JobRecord:
        worker = self._require_worker(worker_id)
        if worker.status == WorkerStatus.DISABLED:
            raise ValueError(f"worker '{worker_id}' is disabled")
        job = self._require_job(job_id, reconcile=False)
        if job.owner_worker_id != worker_id:
            raise ValueError(f"worker '{worker_id}' does not own job '{job_id}'")
        if not isinstance(lease_token, str) or not hmac.compare_digest(job.lease_token or "", lease_token):
            raise ValueError(f"job '{job_id}' lease token is invalid or stale")
        now = utcnow()
        if job.status != JobStatus.IN_PROGRESS:
            raise ValueError(f"job '{job_id}' is not in progress (current status: {job.status})")
        if not job.lease_expires_at or job.lease_expires_at <= now:
            raise ValueError(f"job '{job_id}' lease has expired")
        renewed = self.job_repository.renew_lease(
            job_id=job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_duration_seconds=self.JOB_LEASE_DURATION_SECONDS,
        )
        if renewed is None:
            self._reconcile_expired_jobs()
            raise ValueError(f"job '{job_id}' lease is invalid or stale")
        worker.last_seen_at = now
        worker.updated_at = now
        if worker.status != WorkerStatus.DISABLED:
            worker.status = WorkerStatus.ONLINE
        self.worker_repository.save(worker)
        return renewed

    def _job_storage_context(self, job: JobRecord) -> Dict[str, Any]:
        summary = job.result_summary if isinstance(job.result_summary, dict) else {}
        context = summary.get("storage_context")
        if not isinstance(context, dict):
            payload = job.payload if isinstance(job.payload, dict) else {}
            context = payload.get("storage_context")
        if not isinstance(context, dict) and job.target_id:
            target = self.target_repository.get(job.target_id)
            if target is not None:
                try:
                    environment, _, resolved_files = self._resolve_runtime_dependencies(target)
                    context = self._storage_context(target, environment, resolved_files)
                except Exception:
                    context = {}
        return self._safe_storage_context(context)

    @staticmethod
    def _job_sensitive_values(job: JobRecord) -> set[str]:
        values: set[str] = set()
        payload = job.payload if isinstance(job.payload, dict) else {}
        markers = ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY", "ACCESS_KEY", "CREDENTIAL", "PLAINTEXT")

        def collect(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    collect(nested_value, str(nested_key))
            elif isinstance(value, (list, tuple)):
                for nested_value in value:
                    collect(nested_value, key)
            elif isinstance(value, str) and value and (
                any(marker in key.upper() for marker in markers)
                or "RCLONE" in key.upper()
                or key in {"RESTIC_REPOSITORY", "content"}
            ):
                values.add(value)

        collect(payload)
        if job.lease_token:
            values.add(job.lease_token)
        return values

    @classmethod
    def _redact_job_text(cls, value: Any, secrets: set[str]) -> str:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1<redacted>@", text)
        text = re.sub(r"(?i)\brclone:[^\s\"'<>]+", "<redacted-repository>", text)
        text = re.sub(r"(?i)\b(?:s3|local|file):[^\s\"'<>]+", "<redacted-repository>", text)
        text = re.sub(
            r"(?i)(password|passphrase|token|secret|access[_-]?key|secret[_-]?key|credential)\s*[:=]\s*[^\s,;]+",
            r"\1=<redacted>",
            text,
        )
        return text

    def _bounded_job_log_lines(self, job: JobRecord, log_lines: Any = None) -> List[str]:
        source = job.log_lines if log_lines is None else log_lines
        if not isinstance(source, list):
            return []
        secrets = self._job_sensitive_values(job)
        context = self._job_storage_context(job)
        context_line = None
        if context.get("repository_display"):
            profile = context.get("storage_profile_name") or context.get("storage_profile_id") or "sin perfil"
            backend = context.get("backend_type") or context.get("repository_kind") or "unknown"
            context_line = f"Storage profile: {profile}; backend: {backend}; repository: {context['repository_display']}"
        lines = []
        for line in source:
            if not isinstance(line, str):
                continue
            if context_line and line.startswith("Storage profile:"):
                lines.append(context_line)
            else:
                lines.append(self._redact_job_text(line, secrets)[:16 * 1024])
        lines = lines[-self.MAX_JOB_LOG_LINES :]
        while lines and sum(len(line) for line in lines) > self.MAX_JOB_LOG_CHARS:
            lines.pop(0)
        return lines

    def _safe_public_value(self, value: Any, job: JobRecord, depth: int = 0) -> Any:
        if depth > 4:
            return None
        sensitive_names = {"payload", "lease_token", "ciphertext", "b64_content", "content"}
        markers = ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "PRIVATE_KEY", "ACCESS_KEY")
        if isinstance(value, dict):
            safe: Dict[str, Any] = {}
            for key, item in list(value.items())[: self.MAX_PUBLIC_SUMMARY_ITEMS]:
                name = str(key)
                if name.lower() in sensitive_names or "RCLONE" in name.upper() or any(marker in name.upper() for marker in markers):
                    continue
                safe[name[:128]] = self._safe_public_value(item, job, depth + 1)
            return safe
        if isinstance(value, list):
            return [self._safe_public_value(item, job, depth + 1) for item in value[: self.MAX_PUBLIC_SUMMARY_ITEMS]]
        if isinstance(value, str):
            return self._redact_job_text(value, self._job_sensitive_values(job))[:16 * 1024]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return None

    def _safe_result_summary(self, job: JobRecord) -> Dict[str, Any]:
        raw = job.result_summary if isinstance(job.result_summary, dict) else {}
        safe: Dict[str, Any] = {}
        allowed_scalar = {
            "status_code",
            "target_id",
            "compose_project",
            "state",
            "source",
            "cache_hit",
            "recovery",
            "error",
            "retention_command",
        }
        for key in allowed_scalar:
            if key not in raw:
                continue
            value = raw[key]
            if key == "error":
                safe[key] = self._redact_job_text(value, self._job_sensitive_values(job))[:2048]
            else:
                safe[key] = self._safe_public_value(value, job)
        context = self._safe_storage_context(raw.get("storage_context"))
        if context:
            safe["storage_context"] = context
        progress = raw.get("progress")
        if isinstance(progress, dict):
            try:
                _, normalized, _ = self._validate_progress_payload(
                    max(1, int(raw.get("progress_sequence", 1) or 1)), progress, []
                )
                safe["progress"] = normalized
            except (TypeError, ValueError):
                pass
        for key in ("entries", "snapshots"):
            value = raw.get(key)
            if not isinstance(value, list):
                continue
            safe[key] = [self._safe_public_value(item, job) for item in value[: self.MAX_PUBLIC_SUMMARY_ITEMS] if isinstance(item, dict)]
        for key in ("metrics", "stats"):
            value = raw.get(key)
            if isinstance(value, dict):
                safe[key] = self._safe_public_value(value, job)
        return safe

    def _job_public_view(self, job: JobRecord) -> Dict[str, Any]:
        summary = self._safe_result_summary(job)
        context = summary.get("storage_context") or self._job_storage_context(job)
        progress = summary.get("progress")
        return {
            "id": job.id,
            "worker_id": job.worker_id,
            "command": job.command,
            "requested_by": job.requested_by,
            "target_id": job.target_id,
            "trigger": job.trigger,
            "status": JobStatus.normalize(job.status),
            "attempt_count": job.attempt_count,
            "result_summary": summary,
            "storage_context": context,
            "progress": progress if isinstance(progress, dict) else None,
            "log_lines": self._bounded_job_log_lines(job),
            "submitted_at": job.submitted_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
        }

    def _publish_job_event(self, job: JobRecord) -> None:
        try:
            self.job_event_broker.publish(job.id, self._job_public_view(job))
        except Exception as exc:
            logger.warning("Job event publication failed for %s (%s)", job.id, type(exc).__name__)

    def get_job_view(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.get_job(job_id)
        return self._job_public_view(job) if job is not None else None

    def public_job_view(self, job: JobRecord) -> Dict[str, Any]:
        return self._job_public_view(job)

    def list_job_views(self, limit: Optional[int] = None, offset: int = 0) -> tuple:
        self._reconcile_expired_jobs()
        jobs = self.job_repository.list()
        total = len(jobs)
        views = [self._job_public_view(job) for job in jobs]
        if limit is not None and limit > 0:
            return views[offset:offset + limit], total
        if offset > 0:
            return views[offset:], total
        return views, total

    def update_job_progress(
        self,
        worker_id: str,
        job_id: str,
        sequence: Any,
        progress: Any,
        log_lines: Any = None,
        lease_token: Optional[str] = None,
    ) -> JobRecord:
        self._require_worker(worker_id)
        job = self._require_job(job_id, reconcile=False)
        if job.owner_worker_id != worker_id:
            raise ValueError(f"worker '{worker_id}' does not own job '{job_id}'")
        if not isinstance(lease_token, str) or not hmac.compare_digest(job.lease_token or "", lease_token):
            raise ValueError(f"job '{job_id}' lease token is invalid or stale")
        if not job.lease_expires_at or job.lease_expires_at <= utcnow():
            raise ValueError(f"job '{job_id}' lease has expired")
        normalized_sequence, normalized_progress, normalized_lines = self._validate_progress_payload(
            sequence, progress, [] if log_lines is None else log_lines
        )
        if isinstance(normalized_progress.get("percent_done"), (int, float)) and normalized_progress["percent_done"] >= 100:
            normalized_progress["percent_done"] = 99.9
        if JobStatus.normalize(job.status) != JobStatus.IN_PROGRESS:
            return job

        current_summary = job.result_summary if isinstance(job.result_summary, dict) else {}
        summary_context = self._safe_storage_context(current_summary.get("storage_context"))
        if not summary_context:
            summary_context = self._safe_storage_context(
                (job.payload or {}).get("storage_context") if isinstance(job.payload, dict) else None
            )
        result_summary = {"storage_context": summary_context} if summary_context else None
        updater = getattr(self.job_repository, "update_progress", None)
        if callable(updater):
            updated = updater(
                job_id=job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                sequence=normalized_sequence,
                progress=normalized_progress,
                log_lines=normalized_lines,
                result_summary=result_summary,
            )
            if updated is None:
                raise ValueError(f"job '{job_id}' progress lease is invalid or stale")
            self._publish_job_event(updated)
            return updated

        try:
            current_sequence = int(current_summary.get("progress_sequence", 0) or 0)
        except (TypeError, ValueError):
            current_sequence = 0
        if normalized_sequence <= current_sequence:
            return job
        merged_summary = dict(current_summary)
        if result_summary:
            merged_summary.update(result_summary)
        merged_summary["progress"] = normalized_progress
        merged_summary["progress_sequence"] = normalized_sequence
        job.result_summary = merged_summary
        job.log_lines = self._bounded_job_log_lines(job, list(job.log_lines or []) + normalized_lines)
        job.updated_at = utcnow()
        saved_job = self.job_repository.save(job)
        self._publish_job_event(saved_job)
        return saved_job

    def update_job_status(
        self,
        worker_id: str,
        job_id: str,
        status: str,
        result_summary: Optional[Dict[str, Any]] = None,
        log_lines: Optional[List[str]] = None,
        lease_token: Optional[str] = None,
    ) -> JobRecord:
        self._require_worker(worker_id)
        job = self._require_job(job_id, reconcile=False)
        if job.owner_worker_id != worker_id:
            raise ValueError(f"worker '{worker_id}' does not own job '{job_id}'")
        job.status = JobStatus.normalize(job.status)
        if job.status == JobStatus.CANCELED:
            raise ValueError(f"job '{job_id}' is canceled")
        if job.status != JobStatus.IN_PROGRESS:
            raise ValueError(f"job '{job_id}' is not in progress (current status: {job.status})")
        if not isinstance(lease_token, str) or not hmac.compare_digest(job.lease_token or "", lease_token):
            raise ValueError(f"job '{job_id}' lease token is invalid or stale")
        if not job.lease_expires_at or job.lease_expires_at <= utcnow():
            raise ValueError(f"job '{job_id}' lease has expired")
        status = JobStatus.normalize(status)
        if status not in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            raise ValueError(f"job '{job_id}' cannot be completed with status: {status}")
        job.status = status
        job.updated_at = utcnow()
        if result_summary is not None:
            completed_summary = dict(result_summary) if isinstance(result_summary, dict) else {}
            if "storage_context" not in completed_summary:
                context = self._job_storage_context(job)
                if context:
                    completed_summary["storage_context"] = context
            job.result_summary = completed_summary
        if log_lines:
            job.log_lines = self._bounded_job_log_lines(job, list(job.log_lines or []) + list(log_lines))
        else:
            job.log_lines = self._bounded_job_log_lines(job)
        if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED):
            job.finished_at = utcnow()
            completed_summary = result_summary or job.result_summary or {}
            if status == JobStatus.SUCCEEDED and job.command == "snapshots.list" and job.target_id:
                self._sync_snapshots_from_result(job.target_id, worker_id, completed_summary)
            if status == JobStatus.SUCCEEDED and job.command == "stats.get" and job.target_id:
                self._sync_stats_from_result(job.target_id, worker_id, completed_summary)
            if status == JobStatus.SUCCEEDED:
                self._invalidate_explorer_metadata_after_success(job)
        saved_job = self.job_repository.save(job)
        self._publish_job_event(saved_job)
        if status == JobStatus.SUCCEEDED:
            try:
                self._schedule_snapshot_sync_after_mutation(saved_job)
            except Exception as exc:
                logger.warning(
                    "Snapshot catalog sync scheduling failed after job %s for target %s (%s)",
                    saved_job.id,
                    saved_job.target_id,
                    type(exc).__name__,
                )
        return saved_job

    def list_workers(self) -> List[WorkerRecord]:
        return [self._worker_view(worker) for worker in self.worker_repository.list()]

    def list_jobs(self, limit: Optional[int] = None, offset: int = 0, include_logs: bool = False, include_payload: bool = False) -> tuple:
        self._reconcile_expired_jobs()
        jobs = self.job_repository.list()
        total = len(jobs)
        result = []
        for j in jobs:
            if not include_logs or not include_payload:
                j = copy.copy(j)
                if not include_logs:
                    j.log_lines = []
                if not include_payload:
                    j.payload = {}
            result.append(j)
        if limit is not None and limit > 0:
            return result[offset:offset + limit], total
        if offset > 0:
            return result[offset:], total
        return result, total

    def cancel_job(self, job_id: str) -> JobRecord:
        job = self._require_job(job_id)
        job.status = JobStatus.normalize(job.status)
        if job.status not in (JobStatus.PENDING, JobStatus.IN_PROGRESS):
            raise ValueError(f"job '{job_id}' cannot be cancelled (current status: {job.status})")
        job.status = JobStatus.CANCELED
        job.updated_at = utcnow()
        if not job.finished_at:
            job.finished_at = utcnow()
        if not job.result_summary:
            job.result_summary = {
                "recovery": "operator_canceled",
                "error": "job canceled",
                "message": "Job canceled by operator before terminal worker completion.",
            }
        if not job.log_lines:
            job.log_lines = ["Job canceled by operator before terminal worker completion."]
        saved_job = self.job_repository.save(job)
        self._publish_job_event(saved_job)
        return job

    def list_targets(self) -> List[BackupTargetRecord]:
        return self.target_repository.list()

    def list_snapshots(self, target_id: str) -> List[SnapshotRecord]:
        self._require_target(target_id)
        return self.snapshot_repository.list_by_target(target_id)

    def get_target_stats(self, target_id: str) -> Optional[TargetStatsRecord]:
        self._require_target(target_id)
        return self.target_stats_repository.get_by_target(target_id)

    def validate_target(self, target_id: str) -> Dict[str, Any]:
        target = self._require_target(target_id)
        issues: List[str] = []
        warnings: List[str] = []

        if not target.runtime_image:
            issues.append("runtime_image is required")
        if not target.volume_targets and "BACKUP_SOURCES" not in target.runtime_environment:
            warnings.append("No volume_targets or BACKUP_SOURCES configured")
        if target.backup_strategy == "restic":
            has_repository = "RESTIC_REPOSITORY" in target.runtime_environment
            has_secret_repo = False
            has_secret_password = False
            if target.storage_profile_id:
                profile = self._require_storage_profile(target.storage_profile_id)
                has_repository = has_repository or "RESTIC_REPOSITORY" in profile.environment
                has_secret_repo = "RESTIC_REPOSITORY" in profile.secret_refs
                has_secret_password = "RESTIC_PASSWORD" in profile.secret_refs or "RESTIC_PASSWORD" in profile.environment
            if not (has_repository or has_secret_repo):
                issues.append("RESTIC_REPOSITORY is not configured in target or storage profile")
            if "RESTIC_PASSWORD" not in target.runtime_environment and not has_secret_password:
                issues.append("RESTIC_PASSWORD is not configured in target or storage profile")
        if target.retention_policy_id:
            self._require_retention_policy(target.retention_policy_id)
        if target.restore_defaults:
            restore_target_path = target.restore_defaults.get("target_path") or target.restore_defaults.get("RESTORE_TARGET_PATH")
            if restore_target_path and not any(spec.get("bind") == restore_target_path for spec in target.runtime_volumes.values()):
                warnings.append(f"restore target path {restore_target_path} is not currently mounted in runtime_volumes")

        return {
            "target_id": target.id,
            "valid": not issues,
            "issues": issues,
            "warnings": warnings,
        }

    def get_inventory(self, worker_id: str) -> Optional[InventorySnapshot]:
        self._require_worker(worker_id)
        return self.inventory_repository.get_by_worker(worker_id)

    @classmethod
    def _validate_snapshot_id(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls.SNAPSHOT_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid snapshot ID")
        return value

    @classmethod
    def _normalize_snapshot_path(cls, value: Any) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError("snapshot path must be a POSIX string")
        if value == "":
            return "/"
        if len(value) > 4096 or "\x00" in value:
            raise ValueError("snapshot path is invalid")
        if "\\" in value or any(ord(character) < 32 for character in value):
            raise ValueError("snapshot path must use POSIX separators")
        if value.startswith("//") or re.match(r"^[A-Za-z]:", value):
            raise ValueError("host absolute paths are not allowed")
        if ".." in value.split("/"):
            raise ValueError("snapshot path traversal is not allowed")
        forbidden = frozenset({";", "|", "&", "`", "$", ">", "<", '"', "'", "\n", "\r", "(", ")", "{", "}", "[", "]", "*", "?", "!"})
        if any(character in forbidden for character in value):
            raise ValueError("snapshot path contains unsupported characters")
        candidate = value if value.startswith("/") else f"/{value}"
        normalized = posixpath.normpath(candidate)
        return normalized if normalized.startswith("/") else "/"

    @classmethod
    def _normalize_request_id(cls, value: Optional[str]) -> str:
        if value is None:
            return str(uuid4())
        if (
            not isinstance(value, str)
            or not value
            or len(value) > cls.MAX_REQUEST_ID_LENGTH
            or not cls.REQUEST_ID_PATTERN.fullmatch(value)
        ):
            raise ValueError("malformed request ID")
        return value

    @classmethod
    def _validate_max_entries(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > cls.MAX_SNAPSHOT_ENTRIES:
            raise ValueError(f"max_entries must be between 1 and {cls.MAX_SNAPSHOT_ENTRIES}")
        return value

    @classmethod
    def _validate_search_query(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > cls.MAX_SEARCH_QUERY_LENGTH:
            raise ValueError("search query is invalid")
        if value == "":
            return None
        if "\x00" in value or any(ord(character) < 32 for character in value):
            raise ValueError("search query is invalid")
        return value

    @staticmethod
    def _validate_max_output_bytes(value: Optional[int], archive: bool) -> Optional[int]:
        if value is None:
            return None
        maximum = 64 * 1024 * 1024 if archive else 16 * 1024 * 1024
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
            raise ValueError(f"max_output_bytes must be between 1 and {maximum}")
        return value

    def _validate_snapshot_metadata_target(self, target_id: str, snapshot_id: str) -> None:
        for candidate_target in self.target_repository.list():
            for snapshot in self.snapshot_repository.list_by_target(candidate_target.id):
                if snapshot.snapshot_id != snapshot_id:
                    continue
                metadata_target_id = getattr(snapshot, "target_id", candidate_target.id)
                if metadata_target_id != target_id or candidate_target.id != target_id:
                    raise ValueError("snapshot metadata belongs to another target")

    def _snapshot_job_contract(self, job: JobRecord) -> Dict[str, Any]:
        status = JobStatus.normalize(job.status)
        payload = job.payload if isinstance(job.payload, dict) else {}
        summary = job.result_summary if isinstance(job.result_summary, dict) else {}
        entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
        entries = [entry for entry in entries if isinstance(entry, dict)]
        b64_content = summary.get("b64_content") if isinstance(summary.get("b64_content"), str) else ""
        error = summary.get("error") if isinstance(summary.get("error"), str) else None
        if not error and status == JobStatus.CANCELED:
            error = "job canceled"
        if not error and status == JobStatus.FAILED:
            error = "snapshot read failed"
        source = summary.get("source")
        if not isinstance(source, str) or not source:
            source = "durable" if status != JobStatus.SUCCEEDED else "restic"
        return {
            "schema_version": 1,
            "request_id": payload.get("request_id"),
            "job_id": job.id,
            "status": status,
            "source": source,
            "cache_hit": bool(summary.get("cache_hit", False)),
            "entries": entries,
            "b64_content": b64_content,
            "error": error,
        }

    def _repository_fingerprint(self, target: BackupTargetRecord) -> str:
        repository = (target.runtime_environment or {}).get("RESTIC_REPOSITORY")
        if not repository and target.storage_profile_id:
            profile = self.storage_profile_repository.get(target.storage_profile_id)
            if profile:
                repository = (profile.environment or {}).get("RESTIC_REPOSITORY")
                if not repository:
                    repository_secret_id = (profile.secret_refs or {}).get("RESTIC_REPOSITORY")
                    if repository_secret_id:
                        repository = f"secret-ref:{repository_secret_id}"
        if not repository:
            settings = self.get_settings()
            if settings and settings.restic_repository_base:
                base = settings.restic_repository_base.strip("/")
                safe_name = re.sub(r"[^a-zA-Z0-9._-]", "-", target.name or target.id)
                repository = f"{base}/{safe_name}"
        repository = repository or f"target:{target.id}"
        return hashlib.sha256(str(repository).encode("utf-8")).hexdigest()

    def get_live_target_revision(self, target_id: str) -> str:
        return self._live_target_revision(self._require_target(target_id))

    def get_live_target_context(self, target_id: str) -> Dict[str, Any]:
        target = self._require_target(target_id)
        worker = self.worker_repository.get(target.worker_id)
        profile = (
            self.storage_profile_repository.get(target.storage_profile_id)
            if target.storage_profile_id
            else None
        )
        eligible, reason = self._live_target_eligibility(target, worker, profile)
        return {
            "target_id": target.id,
            "worker_id": target.worker_id,
            "config_revision": self._live_target_revision(target, worker, profile),
            "eligible": eligible,
            "reason": reason,
        }

    def is_live_target_eligible(self, target_id: str) -> bool:
        return bool(self.get_live_target_context(target_id)["eligible"])

    def _live_target_revision(
        self,
        target: BackupTargetRecord,
        worker: Optional[WorkerRecord] = None,
        profile=None,
    ) -> str:
        worker = worker if worker is not None else self.worker_repository.get(target.worker_id)
        if target.storage_profile_id and profile is None:
            profile = self.storage_profile_repository.get(target.storage_profile_id)

        revision_inputs = {
            "schema_version": self.LIVE_REVISION_SCHEMA_VERSION,
            "target": {
                "id": target.id,
                "worker_id": target.worker_id,
                "enabled": bool(target.enabled),
                "live_access_enabled": bool(target.live_access_enabled),
                "compose_project": target.compose_project,
                "volume_targets": list(target.volume_targets or []),
                "runtime_volumes": dict(target.runtime_volumes or {}),
                "runtime_image": target.runtime_image,
                "runtime_network_mode": target.runtime_network_mode,
                "updated_at": target.updated_at.isoformat() if target.updated_at else None,
            },
            "worker": (
                {
                    "id": worker.id,
                    "name": worker.name,
                    "host_name": worker.host_name,
                    "version": worker.version,
                    "labels": dict(worker.labels or {}),
                    "status": worker.status,
                }
                if worker is not None
                else {"id": target.worker_id, "missing": True}
            ),
            "storage_profile": (
                {
                    "id": profile.id,
                    "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
                }
                if profile is not None
                else (
                    {"id": target.storage_profile_id, "missing": True}
                    if target.storage_profile_id
                    else None
                )
            ),
        }
        try:
            canonical = json.dumps(
                revision_inputs,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("live target configuration cannot be canonicalized") from exc
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _live_target_eligibility(self, target, worker, profile):
        if not target.live_access_enabled:
            return False, "live_access_disabled"
        if not target.enabled:
            return False, "target_disabled"
        if worker is None:
            return False, "worker_missing"
        worker_status = self._worker_status(worker)
        if worker_status != WorkerStatus.ONLINE:
            return False, f"worker_{worker_status}"
        if target.storage_profile_id and profile is None:
            return False, "storage_profile_missing"
        return True, None

    def _invalidate_explorer_metadata_after_success(self, job: JobRecord) -> None:
        if not job.target_id:
            return
        command = job.command
        runtime_command = str((job.payload or {}).get("command", ""))
        if command not in self.CACHE_INVALIDATING_COMMANDS and not re.search(r"\b(forget|prune)\b", runtime_command):
            return
        target = self.target_repository.get(job.target_id)
        if target is None:
            return
        if self.cache_repository:
            self.cache_repository.bump_generation(target.id, self._repository_fingerprint(target))
        if self.index_repository:
            self.index_repository.delete_for_target(target.id)

    def _schedule_snapshot_sync_after_mutation(self, job: JobRecord) -> Optional[JobRecord]:
        if JobStatus.normalize(job.status) != JobStatus.SUCCEEDED or not job.target_id:
            return None
        command = str(job.command or "")
        runtime_command = str((job.payload or {}).get("command", ""))
        if command not in self.CATALOG_MUTATING_COMMANDS and not re.search(r"\b(forget|prune)\b", runtime_command):
            return None
        target = self._require_target(job.target_id)
        if target.worker_id != job.worker_id or self.has_active_snapshot_sync_for_target(target.id, target.worker_id):
            return None
        return self.dispatch_snapshot_sync_for_target(
            target.id,
            requested_by="system",
            trigger="automatic",
        )

    def _cleanup_orphaned_explorer_metadata(self) -> None:
        targets = self.target_repository.list()
        if self.index_repository:
            self.index_repository.cleanup_orphaned([target.id for target in targets])
        if self.cache_repository:
            self.cache_repository.cleanup_orphaned(
                [(target.id, self._repository_fingerprint(target)) for target in targets]
            )

    def _require_worker(self, worker_id: str) -> WorkerRecord:
        worker = self.worker_repository.get(worker_id)
        if worker is None:
            raise ValueError(f"worker not found: {worker_id}")
        return worker

    @classmethod
    def _worker_offline_after_seconds(cls) -> float:
        raw = os.environ.get("WORKER_OFFLINE_AFTER_SECONDS", str(cls.DEFAULT_WORKER_OFFLINE_AFTER_SECONDS))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return cls.DEFAULT_WORKER_OFFLINE_AFTER_SECONDS
        if not math.isfinite(value) or not cls.MIN_WORKER_OFFLINE_AFTER_SECONDS <= value <= cls.MAX_WORKER_OFFLINE_AFTER_SECONDS:
            return cls.DEFAULT_WORKER_OFFLINE_AFTER_SECONDS
        return value

    @classmethod
    def _worker_status(cls, worker: WorkerRecord, now: Optional[datetime] = None) -> str:
        if worker.status == WorkerStatus.DISABLED:
            return WorkerStatus.DISABLED
        if worker.last_seen_at is None:
            return WorkerStatus.OFFLINE
        now = now or utcnow()
        last_seen_at = worker.last_seen_at
        if last_seen_at.tzinfo is not None and now.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=None)
        try:
            age_seconds = (now - last_seen_at).total_seconds()
        except TypeError:
            return WorkerStatus.OFFLINE
        return WorkerStatus.ONLINE if age_seconds <= cls._worker_offline_after_seconds() else WorkerStatus.OFFLINE

    @classmethod
    def _worker_view(cls, worker: WorkerRecord) -> WorkerRecord:
        view = copy.copy(worker)
        view.status = cls._worker_status(worker)
        return view

    def is_worker_eligible(self, worker_id: str) -> bool:
        worker = self._require_worker(worker_id)
        return self._worker_status(worker) == WorkerStatus.ONLINE

    def _require_eligible_worker(self, worker_id: str) -> WorkerRecord:
        worker = self._require_worker(worker_id)
        status = self._worker_status(worker)
        if status != WorkerStatus.ONLINE:
            raise ValueError(
                f"worker '{worker_id}' is not eligible (status: {status}; a recent heartbeat is required)"
            )
        return worker

    def _reconcile_expired_jobs(self) -> int:
        reconcile = getattr(self.job_repository, "reconcile_expired_leases", None)
        return reconcile() if callable(reconcile) else 0

    def _require_target(self, target_id: str) -> BackupTargetRecord:
        target = self.target_repository.get(target_id)
        if target is None:
            raise ValueError(f"target not found: {target_id}")
        return target

    def _wait_for_job_completion(self, job_id: str, timeout_seconds: int = 60) -> Dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            job = self.job_repository.get(job_id)
            if job and job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED):
                return {
                    "status": job.status,
                    "logs": "\n".join(job.log_lines or []),
                    "result_summary": job.result_summary or {},
                }
            time.sleep(1)
        return {"status": "timeout", "logs": "", "result_summary": {}}

    def _require_job(self, job_id: str, reconcile: bool = True) -> JobRecord:
        job = self.get_job(job_id) if reconcile else self.job_repository.get(job_id)
        if job is None:
            raise ValueError(f"job not found: {job_id}")
        return job

    def _require_storage_profile(self, profile_id: str) -> StorageProfileRecord:
        profile = self.storage_profile_repository.get(profile_id)
        if profile is None:
            raise ValueError(f"storage profile not found: {profile_id}")
        return profile

    def _require_retention_policy(self, policy_id: Optional[str]) -> RetentionPolicyRecord:
        if not policy_id:
            raise ValueError("retention policy not configured")
        policy = self.retention_policy_repository.get(policy_id)
        if policy is None:
            raise ValueError(f"retention policy not found: {policy_id}")
        return policy

    def _require_secret(self, secret_id: str) -> SecretRecord:
        secret = self.secret_repository.get(secret_id)
        if secret is None:
            raise ValueError(f"secret not found: {secret_id}")
        return secret

    def _resolve_secret_refs(self, secret_refs: Dict[str, str]) -> Dict[str, str]:
        resolved = {}
        for env_name, secret_id in secret_refs.items():
            resolved[env_name] = self.secret_codec.decrypt(self._require_secret(secret_id).ciphertext)
        return resolved

    def _resolve_file_secret_refs(self, file_secret_refs: Dict[str, str]) -> List[Dict[str, str]]:
        resolved = []
        for container_path, secret_id in file_secret_refs.items():
            secret = self._require_secret(secret_id)
            resolved.append(
                {
                    "container_path": container_path,
                    "content": self.secret_codec.decrypt(secret.ciphertext),
                    "secret_id": secret.id,
                    "secret_name": secret.name,
                }
            )
        return resolved

    def _validate_storage_profile_file_secret_refs(self, backend_type: str, file_secret_refs: Dict[str, str]) -> None:
        if len(file_secret_refs) > 1:
            raise ValueError("storage profile supports only one file secret reference")
        if not file_secret_refs:
            return

        container_path, secret_id = next(iter(file_secret_refs.items()))
        if not container_path.strip():
            raise ValueError("file secret reference path is required")

        secret = self._require_secret(secret_id)
        if secret.secret_type != "file":
            raise ValueError("storage profile file secret reference must point to a file secret")

        if "rclone" not in backend_type.lower():
            return

        if not container_path.strip().endswith("rclone.conf"):
            raise ValueError("rclone storage profile file secret must mount to an rclone.conf path")

        content = self.secret_codec.decrypt(secret.ciphertext)
        self._validate_single_rclone_profile_content(content)

    @staticmethod
    def _validate_single_rclone_profile_content(content: str) -> None:
        section_headers = re.findall(r"(?m)^\s*\[([^\]\r\n]+)\]\s*$", content or "")
        if len(section_headers) != 1:
            raise ValueError("rclone profile secret must contain exactly one [profile] entry")
        if not re.search(r"(?m)^\s*type\s*=\s*\S+\s*$", content or ""):
            raise ValueError("rclone profile secret must include a type = ... entry")

    def _extract_rclone_remote_name(self, secret_id: str) -> Optional[str]:
        secret = self.secret_repository.get(secret_id)
        if not secret:
            return None
        content = self.secret_codec.decrypt(secret.ciphertext)
        section_headers = re.findall(r"(?m)^\s*\[([^\]\r\n]+)\]\s*$", content or "")
        return section_headers[0] if section_headers else None

    def _sync_snapshots_from_result(self, target_id: str, worker_id: str, result_summary: Dict[str, Any]) -> None:
        raw_snapshots = result_summary.get("snapshots") or []
        snapshots: List[SnapshotRecord] = []
        for item in raw_snapshots:
            timestamp = item.get("time") or item.get("timestamp") or utcnow().isoformat()
            try:
                created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, AttributeError):
                created_at = utcnow().replace(tzinfo=None)
            snapshots.append(
                SnapshotRecord(
                    target_id=target_id,
                    worker_id=worker_id,
                    snapshot_id=item.get("short_id") or item.get("id") or "unknown",
                    created_at=created_at,
                    hostname=item.get("hostname"),
                    paths=item.get("paths") or [],
                    tags=item.get("tags") or [],
                    summary=item,
                )
            )
        self.snapshot_repository.replace_for_target(target_id, snapshots)

    def _sync_stats_from_result(self, target_id: str, worker_id: str, result_summary: Dict[str, Any]) -> None:
        stats_record = self.target_stats_repository.get_by_target(target_id) or TargetStatsRecord(
            target_id=target_id,
            worker_id=worker_id,
        )
        stats_record.worker_id = worker_id
        stats_record.stats = result_summary.get("stats") or result_summary
        stats_record.updated_at = utcnow()
        self.target_stats_repository.save(stats_record)
