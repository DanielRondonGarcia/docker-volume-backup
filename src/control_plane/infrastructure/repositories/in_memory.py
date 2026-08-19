import hmac
import secrets
from datetime import timedelta, timezone
from threading import Lock
from typing import Dict, List, Optional, Tuple

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
    WorkerEnrollmentRepository,
    WorkerRepository,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    CacheGenerationRecord,
    IndexStatusRecord,
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
    utcnow,
)


class InMemoryWorkerRepository(WorkerRepository):
    def __init__(self):
        self._items: Dict[str, WorkerRecord] = {}
        self._lock = Lock()

    def save(self, worker: WorkerRecord) -> WorkerRecord:
        with self._lock:
            self._items[worker.id] = worker
        return worker

    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        return self._items.get(worker_id)

    def find_by_name(self, name: str) -> Optional[WorkerRecord]:
        for worker in sorted(self._items.values(), key=lambda item: item.created_at):
            if worker.name == name:
                return worker
        return None

    def list(self) -> List[WorkerRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, worker_id: str) -> bool:
        with self._lock:
            return self._items.pop(worker_id, None) is not None


class InMemoryWorkerEnrollmentRepository(WorkerEnrollmentRepository):
    def __init__(self):
        self._items: Dict[str, WorkerEnrollmentRecord] = {}
        self._lock = Lock()

    def save(self, enrollment: WorkerEnrollmentRecord) -> WorkerEnrollmentRecord:
        with self._lock:
            self._items[enrollment.id] = enrollment
        return enrollment

    def get(self, enrollment_id: str) -> Optional[WorkerEnrollmentRecord]:
        return self._items.get(enrollment_id)

    def get_by_token_hash(self, token_hash: str) -> Optional[WorkerEnrollmentRecord]:
        for enrollment in self._items.values():
            if enrollment.token_hash == token_hash:
                return enrollment
        return None

    def list(self) -> List[WorkerEnrollmentRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)


class InMemoryInventoryRepository(InventoryRepository):
    def __init__(self):
        self._items: Dict[str, InventorySnapshot] = {}
        self._lock = Lock()

    def save(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        with self._lock:
            self._items[snapshot.worker_id] = snapshot
        return snapshot

    def get_by_worker(self, worker_id: str) -> Optional[InventorySnapshot]:
        return self._items.get(worker_id)

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock:
            return self._items.pop(worker_id, None) is not None


class InMemoryTargetRepository(TargetRepository):
    def __init__(self):
        self._items: Dict[str, BackupTargetRecord] = {}
        self._lock = Lock()

    def save(self, target: BackupTargetRecord) -> BackupTargetRecord:
        with self._lock:
            self._items[target.id] = target
        return target

    def get(self, target_id: str) -> Optional[BackupTargetRecord]:
        return self._items.get(target_id)

    def list(self) -> List[BackupTargetRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, target_id: str) -> bool:
        with self._lock:
            existed = target_id in self._items
            self._items.pop(target_id, None)
            return existed


class InMemoryJobRepository(JobRepository):
    def __init__(self):
        self._items: Dict[str, JobRecord] = {}
        self._lock = Lock()

    def save(self, job: JobRecord) -> JobRecord:
        with self._lock:
            job.status = JobStatus.normalize(job.status)
            self._items[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._items.get(job_id)

    def list(self) -> List[JobRecord]:
        return sorted(self._items.values(), key=lambda item: item.submitted_at, reverse=True)

    def _reconcile_expired_leases_locked(self, now) -> int:
        interruption_log = "Worker lease expired before terminal status was reported."
        reconciled = 0
        for job in self._items.values():
            lease_expires_at = job.lease_expires_at
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if (
                job.status == JobStatus.IN_PROGRESS
                and lease_expires_at
                and lease_expires_at <= now
            ):
                result_summary = dict(job.result_summary or {})
                result_summary.setdefault("error", "worker lease expired before the job reported a terminal result")
                result_summary["recovery"] = "worker_interrupted"
                job.status = JobStatus.FAILED
                job.owner_worker_id = None
                job.lease_token = None
                job.lease_issued_at = None
                job.lease_expires_at = None
                job.result_summary = result_summary
                job.log_lines = list(job.log_lines or [])
                if interruption_log not in job.log_lines:
                    job.log_lines.append(interruption_log)
                job.finished_at = now
                job.updated_at = now
                reconciled += 1
        return reconciled

    def reconcile_expired_leases(self) -> int:
        with self._lock:
            return self._reconcile_expired_leases_locked(utcnow())

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_duration_seconds: int = 300,
    ) -> Optional[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            now = utcnow()
            job = self._items.get(job_id)
            lease_expires_at = job.lease_expires_at if job else None
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if (
                job is None
                or job.status != JobStatus.IN_PROGRESS
                or job.owner_worker_id != worker_id
                or not hmac.compare_digest(job.lease_token or "", lease_token or "")
                or not lease_expires_at
                or lease_expires_at <= now
            ):
                return None
            job.lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
            job.updated_at = now
            return job

    def claim_pending_for_worker(self, worker_id: str, lease_duration_seconds: int = 300) -> List[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            now = utcnow()
            self._reconcile_expired_leases_locked(now)
            expires_at = now + timedelta(seconds=lease_duration_seconds)
            claimed = []
            for job in sorted(self._items.values(), key=lambda item: item.submitted_at):
                if job.worker_id != worker_id or job.status != JobStatus.PENDING:
                    continue
                job.status = JobStatus.IN_PROGRESS
                job.owner_worker_id = worker_id
                job.lease_token = secrets.token_urlsafe(32)
                job.lease_issued_at = now
                job.lease_expires_at = expires_at
                job.attempt_count = (job.attempt_count or 0) + 1
                job.started_at = now
                job.updated_at = now
                claimed.append(job)
            return claimed


class InMemoryStorageProfileRepository(StorageProfileRepository):
    def __init__(self):
        self._items: Dict[str, StorageProfileRecord] = {}
        self._lock = Lock()

    def save(self, profile: StorageProfileRecord) -> StorageProfileRecord:
        with self._lock:
            self._items[profile.id] = profile
        return profile

    def get(self, profile_id: str) -> Optional[StorageProfileRecord]:
        return self._items.get(profile_id)

    def list(self) -> List[StorageProfileRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            return self._items.pop(profile_id, None) is not None


class InMemorySecretRepository(SecretRepository):
    def __init__(self):
        self._items: Dict[str, SecretRecord] = {}
        self._lock = Lock()

    def save(self, secret: SecretRecord) -> SecretRecord:
        with self._lock:
            self._items[secret.id] = secret
        return secret

    def get(self, secret_id: str) -> Optional[SecretRecord]:
        return self._items.get(secret_id)

    def list(self) -> List[SecretRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, secret_id: str) -> None:
        with self._lock:
            self._items.pop(secret_id, None)


class InMemorySnapshotRepository(SnapshotRepository):
    def __init__(self):
        self._items: Dict[str, List[SnapshotRecord]] = {}
        self._lock = Lock()

    def replace_for_target(self, target_id: str, snapshots: List[SnapshotRecord]) -> None:
        with self._lock:
            self._items[target_id] = list(snapshots)

    def list_by_target(self, target_id: str) -> List[SnapshotRecord]:
        return list(self._items.get(target_id, []))


class InMemoryRetentionPolicyRepository(RetentionPolicyRepository):
    def __init__(self):
        self._items: Dict[str, RetentionPolicyRecord] = {}
        self._lock = Lock()

    def save(self, policy: RetentionPolicyRecord) -> RetentionPolicyRecord:
        with self._lock:
            self._items[policy.id] = policy
        return policy

    def get(self, policy_id: str) -> Optional[RetentionPolicyRecord]:
        return self._items.get(policy_id)

    def list(self) -> List[RetentionPolicyRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)


class InMemoryTargetStatsRepository(TargetStatsRepository):
    def __init__(self):
        self._items: Dict[str, TargetStatsRecord] = {}
        self._lock = Lock()

    def save(self, stats_record: TargetStatsRecord) -> TargetStatsRecord:
        with self._lock:
            self._items[stats_record.target_id] = stats_record
        return stats_record

    def get_by_target(self, target_id: str) -> Optional[TargetStatsRecord]:
        return self._items.get(target_id)

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock:
            stale_targets = [target_id for target_id, item in self._items.items() if item.worker_id == worker_id]
            for target_id in stale_targets:
                self._items.pop(target_id, None)
            return bool(stale_targets)


class InMemorySettingsRepository(SettingsRepository):
    def __init__(self):
        self._item: Optional[SettingsRecord] = None
        self._lock = Lock()

    def get(self) -> Optional[SettingsRecord]:
        with self._lock:
            return self._item

    def save(self, settings: SettingsRecord) -> SettingsRecord:
        with self._lock:
            self._item = settings
        return settings


class InMemoryCacheRepository(CacheRepository):
    def __init__(self):
        self._items: Dict[Tuple[str, str], CacheGenerationRecord] = {}
        self._lock = Lock()

    def get_generation(self, target_id: str, repository_fingerprint: str) -> Optional[CacheGenerationRecord]:
        return self._items.get((target_id, repository_fingerprint))

    def bump_generation(self, target_id: str, repository_fingerprint: str) -> CacheGenerationRecord:
        with self._lock:
            key = (target_id, repository_fingerprint)
            current = self._items.get(key)
            record = CacheGenerationRecord(
                target_id=target_id,
                repository_fingerprint=repository_fingerprint,
                generation=(current.generation if current else 0) + 1,
            )
            self._items[key] = record
            return record

    def cleanup_orphaned(self, active_keys: List[Tuple[str, str]]) -> int:
        with self._lock:
            active = set(active_keys)
            stale = [key for key in self._items if key not in active]
            for key in stale:
                self._items.pop(key, None)
            return len(stale)


class InMemoryIndexRepository(IndexRepository):
    def __init__(self):
        self._items: Dict[Tuple[str, str], IndexStatusRecord] = {}
        self._lock = Lock()

    def upsert_status(self, record: IndexStatusRecord) -> IndexStatusRecord:
        with self._lock:
            self._items[(record.target_id, record.snapshot_id)] = record
        return record

    def get_status(self, target_id: str, snapshot_id: str) -> Optional[IndexStatusRecord]:
        return self._items.get((target_id, snapshot_id))

    def list_by_target(self, target_id: str) -> List[IndexStatusRecord]:
        return [
            record
            for (record_target_id, _), record in sorted(self._items.items(), key=lambda item: item[0][1])
            if record_target_id == target_id
        ]

    def delete_for_target(self, target_id: str) -> None:
        with self._lock:
            stale = [key for key in self._items if key[0] == target_id]
            for key in stale:
                self._items.pop(key, None)

    def cleanup_orphaned(self, active_target_ids: List[str]) -> int:
        with self._lock:
            active = set(active_target_ids)
            stale = [key for key in self._items if key[0] not in active]
            for key in stale:
                self._items.pop(key, None)
            return len(stale)
