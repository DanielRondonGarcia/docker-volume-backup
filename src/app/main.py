import os
import sys
import logging
from src.app.domain.models import BackupConfig, ContainerConfig, RestoreConfig
from src.app.application.services.backup_service import BackupService
from src.app.application.services.restore_service import RestoreService
from src.app.infrastructure.adapters.storage.multi_storage_adapter import MultiStorageAdapter
from src.app.infrastructure.adapters.container.docker_adapter import DockerAdapter
from src.app.infrastructure.adapters.notifier.influx_notifier import InfluxNotifier
from src.app.infrastructure.adapters.backup_strategy import TarballBackupStrategy, ResticBackupStrategy

class FlushingStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[FlushingStreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")

def _strategy_for(strategy_name: str):
    if strategy_name == "restic":
        return ResticBackupStrategy()
    return TarballBackupStrategy()

def main():
    # Load config from env
    backup_sources = os.environ.get("BACKUP_SOURCES", "/backup").split()
    backup_filename = os.environ.get("BACKUP_FILENAME", "backup-%Y-%m-%dT%H-%M-%S.tar.gz")
    gpg_passphrase = os.environ.get("GPG_PASSPHRASE")

    aws_s3_bucket = os.environ.get("AWS_S3_BUCKET_NAME")
    aws_glacier_vault = os.environ.get("AWS_GLACIER_VAULT_NAME")

    scp_host = os.environ.get("SCP_HOST")
    scp_user = os.environ.get("SCP_USER")
    scp_dir = os.environ.get("SCP_DIRECTORY")

    restic_repo = os.environ.get("RESTIC_REPOSITORY")
    restic_pass = os.environ.get("RESTIC_PASSWORD")

    rclone_remote = os.environ.get("RCLONE_REMOTE")
    local_archive_path = os.environ.get("BACKUP_ARCHIVE")

    restic_keep_daily = int(os.environ.get("RESTIC_KEEP_DAILY", 7))
    restic_keep_weekly = int(os.environ.get("RESTIC_KEEP_WEEKLY", 4))
    restic_keep_monthly = int(os.environ.get("RESTIC_KEEP_MONTHLY", 12))
    restic_keep_yearly = int(os.environ.get("RESTIC_KEEP_YEARLY", 1))

    pre_backup_cmd = os.environ.get("PRE_BACKUP_COMMAND")
    post_backup_cmd = os.environ.get("POST_BACKUP_COMMAND")

    backup_strategy_name = os.environ.get("BACKUP_STRATEGY", "tar").lower()
    restore_strategy_name = os.environ.get("RESTORE_BACKUP_STRATEGY", backup_strategy_name).lower()

    storage_port = MultiStorageAdapter()
    container_port = DockerAdapter()

    if _env_bool("RESTORE_MODE"):
        restore_config = RestoreConfig(
            target_path=os.environ.get("RESTORE_TARGET_PATH", ""),
            source=os.environ.get("RESTORE_SOURCE") or None,
            dry_run=_env_bool("RESTORE_DRY_RUN", True),
            force_overwrite=_env_bool("RESTORE_FORCE_OVERWRITE"),
            stop_containers=_env_bool("RESTORE_STOP_CONTAINERS"),
            chown=os.environ.get("RESTORE_CHOWN") or None,
            backup_strategy=restore_strategy_name,
            layout=os.environ.get("RESTORE_LAYOUT", "auto").lower(),
            gpg_passphrase=gpg_passphrase,
            restic_repository=restic_repo,
            restic_password=restic_pass,
            aws_s3_bucket=aws_s3_bucket,
            aws_glacier_vault=aws_glacier_vault,
            scp_host=scp_host,
            scp_user=scp_user,
            scp_directory=scp_dir,
            rclone_remote=rclone_remote,
            local_archive_path=local_archive_path,
            stop_label=os.environ.get("RESTORE_STOP_LABEL") or None,
            custom_label=os.environ.get("RESTORE_CUSTOM_LABEL") or None,
        )

        service = RestoreService(
            storage_port=storage_port,
            container_port=container_port,
            restore_strategy=_strategy_for(restore_strategy_name),
            restore_config=restore_config,
        )
        result = service.execute_restore()

        if result.permission_warnings:
            for warning in result.permission_warnings:
                logger.warning(warning)
        for action in result.planned_actions or []:
            logger.info(action)
        if result.success:
            logger.info(f"Restore completed successfully in {result.duration:.1f}s")
        else:
            logger.error(f"Restore failed: {result.error}")
            exit(1)
        sys.stderr.flush()
        return

    config = BackupConfig(
        source_paths=backup_sources,
        backup_filename=backup_filename,
        gpg_passphrase=gpg_passphrase,
        aws_s3_bucket=aws_s3_bucket,
        aws_glacier_vault=aws_glacier_vault,
        scp_host=scp_host,
        scp_user=scp_user,
        scp_directory=scp_dir,
        restic_repository=restic_repo,
        restic_password=restic_pass,
        rclone_remote=rclone_remote,
        local_archive_path=local_archive_path,
        backup_strategy=backup_strategy_name,
        restic_keep_daily=restic_keep_daily,
        restic_keep_weekly=restic_keep_weekly,
        restic_keep_monthly=restic_keep_monthly,
        restic_keep_yearly=restic_keep_yearly,
        pre_backup_command=pre_backup_cmd,
        post_backup_command=post_backup_cmd
    )

    container_config = ContainerConfig(
        custom_label=os.environ.get("BACKUP_CUSTOM_LABEL")
    )

    # Adapters
    notifier_port = InfluxNotifier()
    strategy = _strategy_for(backup_strategy_name)

    service = BackupService(
        storage_port=storage_port,
        container_port=container_port,
        notifier_port=notifier_port,
        backup_strategy=strategy,
        backup_config=config,
        container_config=container_config
    )

    # Execute
    result = service.execute_backup()

    if not result.success:
        exit(1)

if __name__ == "__main__":
    main()
