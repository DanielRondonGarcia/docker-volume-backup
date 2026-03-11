import logging
import time
from typing import List, Optional
from datetime import datetime
from src.app.domain.models import BackupConfig, BackupResult, ContainerConfig
from src.app.application.ports.ports import StoragePort, ContainerPort, NotifierPort, BackupStrategy

logger = logging.getLogger(__name__)

class BackupService:
    def __init__(self, 
                 storage_port: StoragePort,
                 container_port: ContainerPort,
                 notifier_port: NotifierPort,
                 backup_strategy: BackupStrategy,
                 backup_config: BackupConfig,
                 container_config: ContainerConfig):
        self.storage_port = storage_port
        self.container_port = container_port
        self.notifier_port = notifier_port
        self.backup_strategy = backup_strategy
        self.backup_config = backup_config
        self.container_config = container_config

    def _get_filter_labels(self, base_label: str) -> List[str]:
        labels = [base_label]
        if self.container_config.custom_label:
            labels.append(self.container_config.custom_label)
        return labels

    def execute_backup(self) -> BackupResult:
        logger.info("Backup starting")
        
        # 1. Stop containers
        stop_labels = self._get_filter_labels(self.container_config.stop_label)
        containers_to_stop = self.container_port.get_containers_by_labels(stop_labels)
        stopped_containers = []
        if containers_to_stop:
            logger.info(f"Stopping containers: {containers_to_stop}")
            stopped_containers = self.container_port.stop_containers(containers_to_stop)
        
        # 2. Pre-exec commands
        pre_exec_labels = self._get_filter_labels(self.container_config.pre_exec_label)
        pre_exec_containers = self.container_port.get_containers_by_labels(pre_exec_labels)
        for container_id in pre_exec_containers:
            cmd = self.container_port.get_label_value(container_id, self.container_config.pre_exec_label)
            if cmd:
                logger.info(f"Pre-exec command for {container_id}: {cmd}")
                self.container_port.exec_command(container_id, cmd)

        # 2.5 Global Pre-backup command
        if self.backup_config.pre_backup_command:
            logger.info(f"Running global pre-backup command: {self.backup_config.pre_backup_command}")
            # This should probably be executed via os.system or subprocess, or a port if we want to be strict.
            # But since it's a shell command, maybe a ShellPort? Or just subprocess here as it's not "domain logic" per se but orchestration.
            import subprocess
            subprocess.run(self.backup_config.pre_backup_command, shell=True, check=True)

        # 3. Perform Backup Strategy
        logger.info("Performing backup strategy")
        result = self.backup_strategy.perform_backup(self.backup_config)

        # 3.5 Global Post-backup command
        if self.backup_config.post_backup_command:
            logger.info(f"Running global post-backup command: {self.backup_config.post_backup_command}")
            import subprocess
            subprocess.run(self.backup_config.post_backup_command, shell=True, check=True)

        # 4. Post-exec commands
        post_exec_labels = self._get_filter_labels(self.container_config.post_exec_label)
        post_exec_containers = self.container_port.get_containers_by_labels(post_exec_labels)
        for container_id in post_exec_containers:
            cmd = self.container_port.get_label_value(container_id, self.container_config.post_exec_label)
            if cmd:
                logger.info(f"Post-exec command for {container_id}: {cmd}")
                self.container_port.exec_command(container_id, cmd)

        # 5. Start containers
        if stopped_containers:
            logger.info(f"Starting containers: {stopped_containers}")
            self.container_port.start_containers(stopped_containers)

        # 6. Upload
        if result.success and result.artifact_path:
            logger.info(f"Uploading artifact: {result.artifact_path}")
            try:
                self.storage_port.upload(result.artifact_path, self.backup_config)
                self.storage_port.cleanup(result.artifact_path)
            except Exception as e:
                logger.error(f"Upload failed: {e}")
                result.success = False
                result.error = str(e)

        # 7. Metrics
        logger.info("Sending metrics")
        self.notifier_port.send_metrics(result)
        
        logger.info("Backup finished")
        return result
