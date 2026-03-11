import os
import logging
from src.app.domain.models import BackupConfig, ContainerConfig
from src.app.application.services.backup_service import BackupService
from src.app.infrastructure.adapters.storage.multi_storage_adapter import MultiStorageAdapter
from src.app.infrastructure.adapters.container.docker_adapter import DockerAdapter
from src.app.infrastructure.adapters.notifier.influx_notifier import InfluxNotifier
from src.app.infrastructure.adapters.backup_strategy import TarballBackupStrategy, ResticBackupStrategy

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

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
    storage_port = MultiStorageAdapter()
    container_port = DockerAdapter()
    notifier_port = InfluxNotifier()
    
    if backup_strategy_name == "restic":
        strategy = ResticBackupStrategy()
    else:
        strategy = TarballBackupStrategy()
        
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
