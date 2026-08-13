from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

@dataclass
class BackupConfig:
    source_paths: List[str]
    backup_filename: str = "backup.tar.gz"
    gpg_passphrase: Optional[str] = None
    aws_s3_bucket: Optional[str] = None
    aws_glacier_vault: Optional[str] = None
    scp_host: Optional[str] = None
    scp_user: Optional[str] = None
    scp_directory: Optional[str] = None
    restic_repository: Optional[str] = None
    restic_password: Optional[str] = None
    rclone_remote: Optional[str] = None
    local_archive_path: Optional[str] = None
    backup_strategy: str = "tar"  # "tar" or "restic"
    restic_keep_daily: Optional[int] = 7
    restic_keep_weekly: Optional[int] = 4
    restic_keep_monthly: Optional[int] = 12
    restic_keep_yearly: Optional[int] = 1
    pre_backup_command: Optional[str] = None
    post_backup_command: Optional[str] = None

@dataclass
class ContainerConfig:
    stop_label: str = "docker-volume-backup.stop-during-backup=true"
    pre_exec_label: str = "docker-volume-backup.exec-pre-backup"
    post_exec_label: str = "docker-volume-backup.exec-post-backup"
    custom_label: Optional[str] = None

@dataclass
class BackupResult:
    timestamp: datetime
    duration: float
    size: int
    success: bool
    artifact_path: Optional[str] = None
    error: Optional[str] = None

@dataclass
class RestoreConfig:
    target_path: str
    source: Optional[str] = None
    dry_run: bool = True
    force_overwrite: bool = False
    stop_containers: bool = False
    chown: Optional[str] = None
    backup_strategy: str = "tar"
    layout: str = "auto"
    gpg_passphrase: Optional[str] = None
    restic_repository: Optional[str] = None
    restic_password: Optional[str] = None
    aws_s3_bucket: Optional[str] = None
    aws_glacier_vault: Optional[str] = None
    scp_host: Optional[str] = None
    scp_user: Optional[str] = None
    scp_directory: Optional[str] = None
    rclone_remote: Optional[str] = None
    local_archive_path: Optional[str] = None
    stop_label: Optional[str] = None
    custom_label: Optional[str] = None

@dataclass
class RestoreCandidate:
    source: str
    strategy: str = "tar"
    created_at: Optional[datetime] = None
    size: Optional[int] = None
    available: bool = True
    unavailable_reason: Optional[str] = None

@dataclass
class RestoreResult:
    timestamp: datetime
    duration: float
    success: bool
    source: Optional[str] = None
    target_path: Optional[str] = None
    dry_run: bool = True
    force_overwrite: bool = False
    affected_containers: Optional[List[str]] = None
    planned_actions: Optional[List[str]] = None
    permission_warnings: Optional[List[str]] = None
    error: Optional[str] = None
