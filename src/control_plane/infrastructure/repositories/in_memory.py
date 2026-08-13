from threading import Lock
from typing import Dict, List, Optional

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
            self._items[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._items.get(job_id)

    def list(self) -> List[JobRecord]:
        return sorted(self._items.values(), key=lambda item: item.submitted_at, reverse=True)

    def list_pending_for_worker(self, worker_id: str) -> List[JobRecord]:
        return [
            job for job in self.list()
            if job.worker_id == worker_id and job.status == JobStatus.PENDING
        ]


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
