import os
import shutil
import subprocess
import logging
from src.app.domain.models import BackupConfig
from src.app.application.ports.ports import StoragePort

logger = logging.getLogger(__name__)

class MultiStorageAdapter(StoragePort):
    def upload(self, file_path: str, config: BackupConfig) -> None:
        if config.aws_s3_bucket:
            self._upload_s3(file_path, config.aws_s3_bucket)
        
        if config.aws_glacier_vault:
            self._upload_glacier(file_path, config.aws_glacier_vault)
            
        if config.scp_host:
            self._upload_scp(file_path, config)
            
        if config.rclone_remote:
            self._upload_rclone(file_path, config.rclone_remote)

        if config.local_archive_path and os.path.exists(config.local_archive_path):
            self._archive_local(file_path, config.local_archive_path)

    def cleanup(self, file_path: str) -> None:
        if os.path.exists(file_path):
            logger.info(f"Cleaning up {file_path}")
            os.remove(file_path)

    def _upload_s3(self, file_path: str, bucket: str):
        try:
            logger.info(f"Uploading to S3 bucket: {bucket}")
            cmd = ["aws", "s3", "cp", "--only-show-errors", file_path, f"s3://{bucket}/"]
            subprocess.run(cmd, check=True)
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")

    def _upload_glacier(self, file_path: str, vault: str):
        try:
            logger.info(f"Uploading to Glacier vault: {vault}")
            cmd = ["aws", "glacier", "upload-archive", "--account-id", "-", "--vault-name", vault, "--body", file_path]
            subprocess.run(cmd, check=True)
        except Exception as e:
            logger.error(f"Glacier upload failed: {e}")

    def _upload_scp(self, file_path: str, config: BackupConfig):
        try:
            logger.info(f"Uploading via SCP to {config.scp_host}")
            ssh_key = "/ssh/id_rsa"
            user = config.scp_user or "root"
            host = config.scp_host
            remote_dir = config.scp_directory or "/tmp"
            
            cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-i", ssh_key, file_path, f"{user}@{host}:{remote_dir}"]
            subprocess.run(cmd, check=True)
        except Exception as e:
            logger.error(f"SCP upload failed: {e}")

    def _upload_rclone(self, file_path: str, remote: str):
        try:
            logger.info(f"Uploading via Rclone to {remote}")
            # Assume remote is configured or passed as "remote:path"
            # If config.rclone_remote is just remote name, we might need path.
            # Assuming config.rclone_remote includes path like "myremote:/backups"
            cmd = ["rclone", "copy", file_path, remote]
            subprocess.run(cmd, check=True)
        except Exception as e:
            logger.error(f"Rclone upload failed: {e}")

    def _archive_local(self, file_path: str, archive_path: str):
        try:
            logger.info(f"Archiving locally to {archive_path}")
            shutil.copy2(file_path, archive_path)
        except Exception as e:
            logger.error(f"Local archive failed: {e}")
