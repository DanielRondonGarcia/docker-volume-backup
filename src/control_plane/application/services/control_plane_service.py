import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from datetime import timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.control_plane.application.ports.ports import (
    InventoryRepository,
    JobRepository,
    RetentionPolicyRepository,
    SecretRepository,
    SettingsRepository,
    SnapshotRepository,
    StorageProfileRepository,
    TargetStatsRepository,
    TargetRepository,
    WorkerEnrollmentRepository,
    WorkerRepository,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    InventorySnapshot,
    JobRecord,
    JobStatus,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    WorkerEnrollmentRecord,
    WorkerRecord,
    WorkerStatus,
    utcnow,
)


class ControlPlaneService:
    SUPPORTED_SECRET_TYPES = {"generic", "env", "file"}

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
        worker_enrollment_repository: WorkerEnrollmentRepository,
        secret_codec,
        tls_manager=None,
        settings_repository: Optional[SettingsRepository] = None,
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
        self.worker_enrollment_repository = worker_enrollment_repository
        self.secret_codec = secret_codec
        self.tls_manager = tls_manager
        self.settings_repository = settings_repository

    def register_worker(
        self,
        name: str,
        host_name: str,
        version: str = "dev",
        labels: Optional[Dict[str, str]] = None,
        worker_id: Optional[str] = None,
        certificate_fingerprint: Optional[str] = None,
    ) -> WorkerRecord:
        existing = None
        if worker_id:
            existing = self.worker_repository.get(worker_id)
        if not existing:
            existing = self.worker_repository.find_by_name(name)
        if existing:
            existing.host_name = host_name
            existing.version = version
            existing.labels = labels or existing.labels or {}
            existing.status = WorkerStatus.ONLINE
            existing.certificate_fingerprint = certificate_fingerprint
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
            certificate_fingerprint=certificate_fingerprint,
            last_seen_at=utcnow(),
        )
        return self.worker_repository.save(worker)

    def heartbeat(self, worker_id: str, version: Optional[str] = None, labels: Optional[Dict[str, str]] = None) -> WorkerRecord:
        worker = self._require_worker(worker_id)
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
            worker.labels = labels
        worker.updated_at = utcnow()
        return self.worker_repository.save(worker)

    def get_worker(self, worker_id: str) -> WorkerRecord:
        return self._require_worker(worker_id)

    def create_worker_enrollment(
        self,
        name: str,
        host_name: str,
        labels: Optional[Dict[str, str]] = None,
        ttl_minutes: int = 30,
    ) -> Dict[str, Any]:
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes must be greater than zero")
        if self.tls_manager is None:
            raise RuntimeError("TLS manager is not configured")

        worker_id = str(uuid4())
        raw_token = secrets.token_urlsafe(32)
        enrollment = WorkerEnrollmentRecord(
            worker_id=worker_id,
            token_hash=self._hash_token(raw_token),
            name=name,
            host_name=host_name,
            labels=labels or {},
            expires_at=utcnow() + timedelta(minutes=ttl_minutes),
        )
        self.worker_enrollment_repository.save(enrollment)
        return {
            "enrollment_id": enrollment.id,
            "worker_id": worker_id,
            "token": raw_token,
            "expires_at": enrollment.expires_at,
            "ca_certificate_pem": self.tls_manager.get_ca_certificate_pem(),
            "server_certificate_fingerprint": self.tls_manager.get_server_certificate_fingerprint(),
        }

    def list_worker_enrollments(self) -> List[WorkerEnrollmentRecord]:
        return self.worker_enrollment_repository.list()

    def enroll_worker_certificate(
        self,
        token: str,
        csr_pem: str,
        version: str = "dev",
        labels: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if self.tls_manager is None:
            raise RuntimeError("TLS manager is not configured")
        enrollment = self._require_valid_enrollment(token)
        signed = self.tls_manager.sign_worker_csr(
            csr_pem=csr_pem,
            worker_id=enrollment.worker_id,
            name=enrollment.name,
            host_name=enrollment.host_name,
        )
        worker = WorkerRecord(
            id=enrollment.worker_id,
            name=enrollment.name,
            host_name=enrollment.host_name,
            version=version,
            labels=dict(enrollment.labels),
            status=WorkerStatus.PENDING,
            certificate_fingerprint=signed["certificate_fingerprint"],
        )
        if labels:
            worker.labels.update(labels)
        existing = self.worker_repository.get(enrollment.worker_id)
        if existing is not None:
            worker.created_at = existing.created_at
            worker.last_seen_at = existing.last_seen_at
        self.worker_repository.save(worker)
        enrollment.used_at = utcnow()
        self.worker_enrollment_repository.save(enrollment)
        return {
            "worker": worker,
            "worker_id": worker.id,
            "certificate_pem": signed["certificate_pem"],
            "ca_certificate_pem": signed["ca_certificate_pem"],
            "certificate_fingerprint": signed["certificate_fingerprint"],
        }

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
    ) -> BackupTargetRecord:
        self._require_worker(worker_id)
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
                for bind_path in derived["volume_targets"]:
                    if bind_path not in client_volume_set and bind_path not in client_volume_targets:
                        client_volume_targets.append(bind_path)
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
    ) -> BackupTargetRecord:
        target = self._require_target(target_id)
        if worker_id is not None:
            self._require_worker(worker_id)
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
        target.updated_at = utcnow()
        return self.target_repository.save(target)

    def delete_target(self, target_id: str) -> bool:
        self._require_target(target_id)
        return self.target_repository.delete(target_id)

    def get_job(self, job_id: str) -> Optional[JobRecord]:
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
        self._require_worker(worker_id)
        job = JobRecord(
            worker_id=worker_id,
            command=command,
            payload=payload or {},
            requested_by=requested_by,
            target_id=target_id,
            trigger=trigger,
        )
        return self.job_repository.save(job)

    def dispatch_backup_for_target(self, target_id: str, requested_by: str = "system", trigger: str = "manual") -> JobRecord:
        target = self._require_target(target_id)
        payload = self._build_backup_payload(target)
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="backup.run",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger=trigger,
        )

    def dispatch_snapshot_sync_for_target(self, target_id: str, requested_by: str = "system") -> JobRecord:
        target = self._require_target(target_id)
        payload = self._build_snapshot_list_payload(target)
        return self.dispatch_job(
            worker_id=target.worker_id,
            command="snapshots.list",
            payload=payload,
            requested_by=requested_by,
            target_id=target.id,
            trigger="manual",
        )

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
        entries = []
        logs = result.get("logs", "") or ""
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

    def _build_backup_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        environment.setdefault("BACKUP_STRATEGY", target.backup_strategy)
        volumes = self._normalize_runtime_volumes(volumes, target)
        if target.volume_targets and "BACKUP_SOURCES" not in environment:
            environment["BACKUP_SOURCES"] = " ".join(self._normalized_backup_sources(volumes))
        if target.backup_mode == "cold":
            environment.setdefault("BACKUP_CUSTOM_LABEL", f"control-plane.target={target.id}")

        payload = {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "volume_targets": target.volume_targets,
            "backup_mode": target.backup_mode,
            "backup_strategy": target.backup_strategy,
            "image": target.runtime_image,
            "command": target.runtime_command,
            "environment": environment,
            "volumes": volumes,
            "network_mode": target.runtime_network_mode,
            "resolved_files": resolved_files,
            "labels": target.labels,
        }
        return payload

    def _build_snapshot_list_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": "restic snapshots --json",
            "environment": environment,
            "volumes": volumes,
            "network_mode": "bridge",
            "resolved_files": resolved_files,
            "labels": target.labels,
        }

    def _build_snapshot_ls_payload(self, target: BackupTargetRecord, snapshot_id: str, path: str = "") -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        safe_snapshot = snapshot_id.replace(" ", "").strip()
        safe_path = path.strip().lstrip("/")
        cmd = f"restic ls --json {safe_snapshot}"
        if safe_path:
            cmd += f" /{safe_path}"
        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": cmd,
            "environment": environment,
            "volumes": volumes,
            "network_mode": "bridge",
            "resolved_files": resolved_files,
            "labels": target.labels,
        }

    def _build_snapshot_dump_payload(self, target: BackupTargetRecord, snapshot_id: str, path: str) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
        safe_snapshot = snapshot_id.replace(" ", "").strip()
        safe_path = path.strip().lstrip("/")
        cmd = f"restic dump {safe_snapshot} /{safe_path}"
        return {
            "target_id": target.id,
            "compose_project": target.compose_project,
            "image": target.runtime_image,
            "command": cmd,
            "environment": environment,
            "volumes": volumes,
            "network_mode": "bridge",
            "resolved_files": resolved_files,
            "labels": target.labels,
        }

    def _build_stats_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
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
        }

    def _build_retention_payload(self, target: BackupTargetRecord) -> Dict[str, Any]:
        environment, volumes, resolved_files = self._resolve_runtime_dependencies(target)
        volumes = self._normalize_runtime_volumes(volumes, target)
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

        if target.storage_profile_id:
            profile = self._require_storage_profile(target.storage_profile_id)
            environment.update(profile.environment)
            environment.update(self._resolve_secret_refs(profile.secret_refs))
            volumes.update(profile.runtime_volumes)
            resolved_files.extend(self._resolve_file_secret_refs(profile.file_secret_refs))
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
                base = settings.restic_repository_base.strip("/")
                safe_name = re.sub(r"[^a-zA-Z0-9._-]", "-", target.name or target.id)
                if re.match(r"^[a-z]+:", base, re.IGNORECASE):
                    environment["RESTIC_REPOSITORY"] = f"{base}/{safe_name}"
                elif settings.rclone_conf_secret_id:
                    remote_name = self._extract_rclone_remote_name(settings.rclone_conf_secret_id)
                    if remote_name:
                        environment["RESTIC_REPOSITORY"] = f"rclone:{remote_name}:{base}/{safe_name}"
                else:
                    environment["RESTIC_REPOSITORY"] = f"{base}/{safe_name}"
            if "RESTIC_PASSWORD" not in environment:
                password_secret_id = target.restic_password_secret_id
                if not password_secret_id and settings:
                    password_secret_id = settings.restic_password_secret_id
                if password_secret_id:
                    secret = self.secret_repository.get(password_secret_id)
                    if secret:
                        environment["RESTIC_PASSWORD"] = self.secret_codec.decrypt(secret.ciphertext)
            if settings and settings.rclone_conf_secret_id:
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
        self._require_worker(worker_id)
        jobs = self.job_repository.list_pending_for_worker(worker_id)
        for job in jobs:
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.IN_PROGRESS
                job.started_at = utcnow()
                job.updated_at = utcnow()
                self.job_repository.save(job)
        return jobs

    def update_job_status(
        self,
        worker_id: str,
        job_id: str,
        status: str,
        result_summary: Optional[Dict[str, Any]] = None,
        log_lines: Optional[List[str]] = None,
    ) -> JobRecord:
        self._require_worker(worker_id)
        job = self._require_job(job_id)
        job.status = status
        job.updated_at = utcnow()
        if result_summary is not None:
            job.result_summary = result_summary
        if log_lines:
            job.log_lines.extend(log_lines)
        if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED):
            job.finished_at = utcnow()
            if status == JobStatus.SUCCEEDED and job.command == "snapshots.list" and job.target_id:
                self._sync_snapshots_from_result(job.target_id, worker_id, result_summary or {})
            if status == JobStatus.SUCCEEDED and job.command == "stats.get" and job.target_id:
                self._sync_stats_from_result(job.target_id, worker_id, result_summary or {})
        return self.job_repository.save(job)

    def list_workers(self) -> List[WorkerRecord]:
        return self.worker_repository.list()

    def list_jobs(self, limit: Optional[int] = None, offset: int = 0, include_logs: bool = False, include_payload: bool = False) -> List[JobRecord]:
        jobs = self.job_repository.list()
        total = len(jobs)
        for j in jobs:
            if not include_logs:
                j.log_lines = []
            if not include_payload:
                j.payload = {}
        if limit is not None and limit > 0:
            return jobs[offset:offset + limit], total
        if offset > 0:
            return jobs[offset:], total
        return jobs, total

    def cancel_job(self, job_id: str) -> JobRecord:
        job = self.job_repository.get(job_id)
        if not job:
            raise ValueError(f"job '{job_id}' not found")
        if job.status not in ("pending", "in_progress"):
            raise ValueError(f"job '{job_id}' cannot be cancelled (current status: {job.status})")
        job.status = "cancelled"
        if not job.finished_at:
            job.finished_at = datetime.utcnow()
        self.job_repository.save(job)
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

    def _require_worker(self, worker_id: str) -> WorkerRecord:
        worker = self.worker_repository.get(worker_id)
        if worker is None:
            raise ValueError(f"worker not found: {worker_id}")
        return worker

    def _require_valid_enrollment(self, token: str) -> WorkerEnrollmentRecord:
        enrollment = self.worker_enrollment_repository.get_by_token_hash(self._hash_token(token))
        if enrollment is None:
            raise ValueError("invalid enrollment token")
        if enrollment.used_at is not None:
            raise ValueError("enrollment token already used")
        if enrollment.expires_at < utcnow():
            raise ValueError("enrollment token expired")
        return enrollment

    def _require_target(self, target_id: str) -> BackupTargetRecord:
        target = self.target_repository.get(target_id)
        if target is None:
            raise ValueError(f"target not found: {target_id}")
        return target

    def _wait_for_job_completion(self, job_id: str, timeout_seconds: int = 60) -> Dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            job = self.job_repository.get(job_id)
            if job and job.status in ("succeeded", "failed", "cancelled"):
                return {
                    "status": job.status,
                    "logs": "\n".join(job.log_lines or []),
                    "result_summary": job.result_summary or {},
                }
            time.sleep(1)
        return {"status": "timeout", "logs": "", "result_summary": {}}

    def _require_job(self, job_id: str) -> JobRecord:
        job = self.job_repository.get(job_id)
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

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _sync_snapshots_from_result(self, target_id: str, worker_id: str, result_summary: Dict[str, Any]) -> None:
        raw_snapshots = result_summary.get("snapshots") or []
        snapshots: List[SnapshotRecord] = []
        for item in raw_snapshots:
            timestamp = item.get("time") or item.get("timestamp") or utcnow().isoformat()
            created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
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
