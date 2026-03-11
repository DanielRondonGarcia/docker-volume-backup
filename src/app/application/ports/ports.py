from abc import ABC, abstractmethod
from typing import List, Optional
from src.app.domain.models import BackupConfig, BackupResult, ContainerConfig

class StoragePort(ABC):
    @abstractmethod
    def upload(self, file_path: str, config: BackupConfig) -> None:
        pass

    @abstractmethod
    def cleanup(self, file_path: str) -> None:
        pass

class ContainerPort(ABC):
    @abstractmethod
    def stop_containers(self, container_ids: List[str]) -> List[str]:
        pass

    @abstractmethod
    def start_containers(self, container_ids: List[str]) -> None:
        pass

    @abstractmethod
    def exec_command(self, container_id: str, command: str) -> None:
        pass

    @abstractmethod
    def get_containers_by_labels(self, labels: List[str]) -> List[str]:
        pass

    @abstractmethod
    def get_label_value(self, container_id: str, label: str) -> Optional[str]:
        pass

    @abstractmethod
    def get_container_name(self, container_id: str) -> str:
        pass

class NotifierPort(ABC):
    @abstractmethod
    def send_metrics(self, result: BackupResult) -> None:
        pass

class BackupStrategy(ABC):
    @abstractmethod
    def perform_backup(self, config: BackupConfig) -> BackupResult:
        pass
