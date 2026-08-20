from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.utcnow()


class WorkerStatus:
    PENDING = "pending"
    ONLINE = "online"
    OFFLINE = "offline"
    DISABLED = "disabled"


class JobStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @classmethod
    def normalize(cls, status: str) -> str:
        return cls.CANCELED if status == "cancelled" else status


@dataclass
class WorkerRecord:
    name: str
    host_name: str
    version: str = "dev"
    labels: Dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = WorkerStatus.PENDING
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    last_seen_at: Optional[datetime] = None


@dataclass
class WorkerEnrollmentRecord:
    worker_id: str
    token_hash: str
    name: str
    host_name: str
    labels: Dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime = field(default_factory=utcnow)
    used_at: Optional[datetime] = None


@dataclass
class InventorySnapshot:
    worker_id: str
    inventory: Dict[str, Any]
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class BackupTargetRecord:
    name: str
    worker_id: str
    compose_project: Optional[str] = None
    volume_targets: List[str] = field(default_factory=list)
    backup_mode: str = "hot"
    backup_strategy: str = "restic"
    runtime_image: Optional[str] = None
    runtime_command: Optional[str] = None
    runtime_environment: Dict[str, str] = field(default_factory=dict)
    runtime_volumes: Dict[str, Dict[str, str]] = field(default_factory=dict)
    runtime_network_mode: Optional[str] = None
    storage_profile_id: Optional[str] = None
    retention_policy_id: Optional[str] = None
    execution_policy_id: Optional[str] = None
    restic_password_secret_id: Optional[str] = None
    restore_defaults: Dict[str, Any] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    live_access_enabled: bool = False
    cron_expression: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class StorageProfileRecord:
    name: str
    backend_type: str
    environment: Dict[str, str] = field(default_factory=dict)
    secret_refs: Dict[str, str] = field(default_factory=dict)
    file_secret_refs: Dict[str, str] = field(default_factory=dict)
    runtime_volumes: Dict[str, Dict[str, str]] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class RetentionPolicyRecord:
    name: str
    keep_last: Optional[int] = None
    keep_hourly: Optional[int] = None
    keep_daily: Optional[int] = None
    keep_weekly: Optional[int] = None
    keep_monthly: Optional[int] = None
    keep_yearly: Optional[int] = None
    prune: bool = True
    labels: Dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SecretRecord:
    name: str
    scope: str
    secret_type: str
    ciphertext: str
    key_version: str = "v1"
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SnapshotRecord:
    target_id: str
    worker_id: str
    snapshot_id: str
    created_at: datetime
    hostname: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class TargetStatsRecord:
    target_id: str
    worker_id: str
    stats: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class JobRecord:
    worker_id: str
    command: str
    payload: Dict[str, Any] = field(default_factory=dict)
    requested_by: str = "system"
    target_id: Optional[str] = None
    trigger: str = "manual"
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = JobStatus.PENDING
    owner_worker_id: Optional[str] = None
    lease_token: Optional[str] = None
    lease_issued_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    attempt_count: int = 0
    result_summary: Optional[Dict[str, Any]] = None
    log_lines: List[str] = field(default_factory=list)
    submitted_at: datetime = field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SettingsRecord:
    DEFAULT_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES: ClassVar[int] = 4 * 1024 * 1024
    MIN_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES: ClassVar[int] = 1 * 1024 * 1024
    MAX_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES: ClassVar[int] = 16 * 1024 * 1024
    restic_repository_base: str = ""
    restic_password_secret_id: Optional[str] = None
    rclone_conf_secret_id: Optional[str] = None
    global_cron_expression: Optional[str] = None
    control_plane_public_url: str = ""
    snapshot_explorer_listing_max_output_bytes: int = DEFAULT_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
    id: str = "default"
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SnapshotReadRequest:
    """Typed interactive snapshot read request (schema version 1)."""

    snapshot_id: str
    path: str
    operation: str
    schema_version: int = 1
    request_id: str = field(default_factory=lambda: str(uuid4()))
    target_id: Optional[str] = None
    max_entries: Optional[int] = None


@dataclass
class SnapshotReadResponse:
    """Typed interactive snapshot read response (schema version 1)."""

    request_id: str
    job_id: str
    status: str
    source: str = "restic"
    cache_hit: bool = False
    schema_version: int = 1
    entries: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CacheGenerationRecord:
    """Per-target repository cache generation used for invalidation."""

    target_id: str
    repository_fingerprint: str
    generation: int = 0
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class IndexStatusRecord:
    """Per-snapshot eager-index status with bounded entry count."""

    target_id: str
    snapshot_id: str
    status: str = "pending"
    entry_count: int = 0
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SnapshotExplorerConfig:
    """Feature-flag configuration for the Snapshot Explorer v2 read path."""

    no_lock: bool = False
    cache_dir: Optional[str] = None
    redis_url: Optional[str] = None
    eager_index: bool = False

    @classmethod
    def from_env(cls) -> "SnapshotExplorerConfig":
        import os

        def _flag(name: str, default: bool = False) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            no_lock=_flag("SNAPSHOT_EXPLORER_NO_LOCK"),
            cache_dir=os.environ.get("SNAPSHOT_EXPLORER_CACHE_DIR") or None,
            redis_url=os.environ.get("SNAPSHOT_EXPLORER_REDIS_URL") or None,
            eager_index=_flag("SNAPSHOT_EXPLORER_EAGER_INDEX"),
        )
