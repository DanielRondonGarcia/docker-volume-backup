from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from src.control_plane.domain.models import (
    BackupTargetRecord,
    CacheGenerationRecord,
    IndexStatusRecord,
    InventorySnapshot,
    JobRecord,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotReadRequest,
    SnapshotReadResponse,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    WorkerEnrollmentRecord,
    WorkerRecord,
)


class WorkerRepository(ABC):
    @abstractmethod
    def save(self, worker: WorkerRecord) -> WorkerRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    def find_by_name(self, name: str) -> Optional[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, worker_id: str) -> bool:
        raise NotImplementedError


class WorkerEnrollmentRepository(ABC):
    @abstractmethod
    def save(self, enrollment: WorkerEnrollmentRecord) -> WorkerEnrollmentRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, enrollment_id: str) -> Optional[WorkerEnrollmentRecord]:
        raise NotImplementedError

    @abstractmethod
    def get_by_token_hash(self, token_hash: str) -> Optional[WorkerEnrollmentRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[WorkerEnrollmentRecord]:
        raise NotImplementedError


class InventoryRepository(ABC):
    @abstractmethod
    def save(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_by_worker(self, worker_id: str) -> Optional[InventorySnapshot]:
        raise NotImplementedError

    @abstractmethod
    def delete_by_worker(self, worker_id: str) -> bool:
        raise NotImplementedError


class TargetRepository(ABC):
    @abstractmethod
    def save(self, target: BackupTargetRecord) -> BackupTargetRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, target_id: str) -> Optional[BackupTargetRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[BackupTargetRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, target_id: str) -> bool:
        raise NotImplementedError


class JobRepository(ABC):
    @abstractmethod
    def save(self, job: JobRecord) -> JobRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, job_id: str) -> Optional[JobRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[JobRecord]:
        raise NotImplementedError

    @abstractmethod
    def claim_pending_for_worker(self, worker_id: str, lease_duration_seconds: int = 300) -> List[JobRecord]:
        raise NotImplementedError

    @abstractmethod
    def reconcile_expired_leases(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_duration_seconds: int = 300,
    ) -> Optional[JobRecord]:
        raise NotImplementedError


class StorageProfileRepository(ABC):
    @abstractmethod
    def save(self, profile: StorageProfileRecord) -> StorageProfileRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, profile_id: str) -> Optional[StorageProfileRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[StorageProfileRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, profile_id: str) -> bool:
        raise NotImplementedError


class SecretRepository(ABC):
    @abstractmethod
    def save(self, secret: SecretRecord) -> SecretRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, secret_id: str) -> Optional[SecretRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[SecretRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, secret_id: str) -> None:
        raise NotImplementedError


class SnapshotRepository(ABC):
    @abstractmethod
    def replace_for_target(self, target_id: str, snapshots: List[SnapshotRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_by_target(self, target_id: str) -> List[SnapshotRecord]:
        raise NotImplementedError


class RetentionPolicyRepository(ABC):
    @abstractmethod
    def save(self, policy: RetentionPolicyRecord) -> RetentionPolicyRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, policy_id: str) -> Optional[RetentionPolicyRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[RetentionPolicyRecord]:
        raise NotImplementedError


class TargetStatsRepository(ABC):
    @abstractmethod
    def save(self, stats_record: TargetStatsRecord) -> TargetStatsRecord:
        raise NotImplementedError

    @abstractmethod
    def get_by_target(self, target_id: str) -> Optional[TargetStatsRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete_by_worker(self, worker_id: str) -> bool:
        raise NotImplementedError


class SettingsRepository(ABC):
    @abstractmethod
    def get(self) -> Optional[SettingsRecord]:
        raise NotImplementedError

    @abstractmethod
    def save(self, settings: SettingsRecord) -> SettingsRecord:
        raise NotImplementedError


class CacheRepository(ABC):
    """Persistent per-target restic cache metadata (generation/invalidation)."""

    @abstractmethod
    def get_generation(self, target_id: str, repository_fingerprint: str) -> Optional[CacheGenerationRecord]:
        raise NotImplementedError

    @abstractmethod
    def bump_generation(self, target_id: str, repository_fingerprint: str) -> CacheGenerationRecord:
        raise NotImplementedError

    @abstractmethod
    def cleanup_orphaned(self, active_keys: List[Tuple[str, str]]) -> int:
        raise NotImplementedError


class IndexRepository(ABC):
    """Bounded eager-index status metadata per snapshot."""

    @abstractmethod
    def upsert_status(self, record: IndexStatusRecord) -> IndexStatusRecord:
        raise NotImplementedError

    @abstractmethod
    def get_status(self, target_id: str, snapshot_id: str) -> Optional[IndexStatusRecord]:
        raise NotImplementedError

    @abstractmethod
    def list_by_target(self, target_id: str) -> List[IndexStatusRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete_for_target(self, target_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def cleanup_orphaned(self, active_target_ids: List[str]) -> int:
        raise NotImplementedError


class InteractiveJobPort(ABC):
    """Bounded interactive lane beside durable jobs for explorer reads."""

    @abstractmethod
    def submit(self, request: SnapshotReadRequest) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_result(self, job_id: str) -> Optional[SnapshotReadResponse]:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, job_id: str) -> bool:
        raise NotImplementedError
