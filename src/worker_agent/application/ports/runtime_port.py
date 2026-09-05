from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List


class RuntimePort(ABC):
    """Runtime-neutral operations used by the worker application service."""

    runtime_kind: str = "unknown"

    @abstractmethod
    def collect_inventory(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def self_check(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def run_runtime_job(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def run_runtime_job_binary(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def cleanup_orphaned_runtime_jobs(
        self,
        recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def cleanup_orphaned_runtime_containers(
        self,
        recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None,
    ) -> Dict[str, Any]:
        """Compatibility alias for callers using the legacy Docker wording."""
        return self.cleanup_orphaned_runtime_jobs(recover_callback=recover_callback)

    def stop_containers(self, container_ids: List[str]) -> Dict[str, Any]:
        raise NotImplementedError("container stop is not supported by this runtime")

    def start_containers(self, container_ids: List[str]) -> Dict[str, Any]:
        raise NotImplementedError("container start is not supported by this runtime")

    def restart_containers(self, container_ids: List[str]) -> Dict[str, Any]:
        raise NotImplementedError("container restart is not supported by this runtime")

    def list_restic_snapshots(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError("snapshot listing is not supported by this runtime")

    def get_restic_snapshot_stats(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError("snapshot statistics are not supported by this runtime")

    def get_restic_stats(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError("restic statistics are not supported by this runtime")
