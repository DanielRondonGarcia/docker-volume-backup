from abc import ABC, abstractmethod
from typing import List, Optional

from src.control_plane.domain.models import (
    BackupTargetRecord,
    InventorySnapshot,
    JobRecord,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
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
    def list_pending_for_worker(self, worker_id: str) -> List[JobRecord]:
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


class SettingsRepository(ABC):
    @abstractmethod
    def get(self) -> Optional[SettingsRecord]:
        raise NotImplementedError

    @abstractmethod
    def save(self, settings: SettingsRecord) -> SettingsRecord:
        raise NotImplementedError
