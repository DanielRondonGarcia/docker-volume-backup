import subprocess
import os
import json
import logging
from datetime import datetime
from src.app.domain.models import BackupConfig, BackupResult
from src.app.application.ports.ports import BackupStrategy

logger = logging.getLogger(__name__)

class TarballBackupStrategy(BackupStrategy):
    def perform_backup(self, config: BackupConfig) -> BackupResult:
        timestamp = datetime.now()
        filename = timestamp.strftime(config.backup_filename)
        sources = config.source_paths
        
        logger.info(f"Creating tarball: {filename} from {sources}")
        cmd = ["tar", "-czvf", filename] + sources
        
        try:
            subprocess.run(cmd, check=True)
            
            if config.gpg_passphrase:
                logger.info("Encrypting backup")
                gpg_filename = f"{filename}.gpg"
                gpg_cmd = [
                    "gpg", "--symmetric", "--cipher-algo", "aes256", "--batch", 
                    "--passphrase", config.gpg_passphrase, "-o", gpg_filename, filename
                ]
                subprocess.run(gpg_cmd, check=True)
                os.remove(filename)
                filename = gpg_filename
            
            size = os.path.getsize(filename)
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=size,
                success=True,
                artifact_path=os.path.abspath(filename)
            )
        except Exception as e:
            logger.error(f"Tarball backup failed: {e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=str(e)
            )

class ResticBackupStrategy(BackupStrategy):
    def perform_backup(self, config: BackupConfig) -> BackupResult:
        timestamp = datetime.now()
        env = os.environ.copy()
        if config.restic_repository:
            env["RESTIC_REPOSITORY"] = config.restic_repository
        if config.restic_password:
            env["RESTIC_PASSWORD"] = config.restic_password
            
        # Rclone config support via env vars if needed, usually handled by user volume mount or envs
            
        sources = config.source_paths
        logger.info(f"Running restic backup for {sources}")
        
        try:
            # Check if repo is initialized
            init_check_cmd = ["restic", "snapshots", "--json", "--latest", "1"]
            try:
                subprocess.run(init_check_cmd, env=env, capture_output=True, check=True)
            except subprocess.CalledProcessError:
                logger.info("Restic repository not initialized or not accessible. Attempting to initialize...")
                # Try to init
                try:
                    init_cmd = ["restic", "init"]
                    subprocess.run(init_cmd, env=env, check=True)
                    logger.info("Restic repository initialized successfully.")
                except subprocess.CalledProcessError as e:
                    # If init fails, maybe it was a connection error or something else, but we can't proceed
                    logger.error(f"Failed to initialize restic repository: {e}")
                    raise e

            cmd = ["restic", "backup", "--json"] + sources
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
            
            # Parse last line of JSON output for summary
            # Restic outputs multiple JSON objects, one per line.
            # The last one usually has summary.
            lines = result.stdout.strip().split('\n')
            summary = {}
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("message_type") == "summary":
                        summary = data
                except:
                    pass
            
            size = summary.get("data_added", 0)
            
            # Prune old snapshots
            logger.info("Pruning old snapshots...")
            prune_cmd = [
                "restic", "forget", "--prune",
                "--keep-daily", str(config.restic_keep_daily),
                "--keep-weekly", str(config.restic_keep_weekly),
                "--keep-monthly", str(config.restic_keep_monthly),
                "--keep-yearly", str(config.restic_keep_yearly)
            ]
            try:
                subprocess.run(prune_cmd, env=env, check=True)
                logger.info("Pruning finished successfully.")
            except subprocess.CalledProcessError as e:
                logger.error(f"Pruning failed: {e}")
                # We don't fail the whole backup if prune fails, but we log it.
            
            return BackupResult(
                timestamp=timestamp,
                duration=summary.get("total_duration", 0),
                size=size,
                success=True,
                artifact_path=None
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Restic backup failed: {e.stderr}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=f"Restic failed: {e.stderr}"
            )
        except Exception as e:
            logger.error(f"Restic backup error: {e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=str(e)
            )
