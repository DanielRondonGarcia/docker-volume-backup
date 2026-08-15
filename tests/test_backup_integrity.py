import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.app.application.services.backup_service import BackupService
from src.app.application.services.restore_service import RestoreService
from src.app.domain.models import BackupConfig, BackupResult, ContainerConfig, RestoreConfig
from src.app.infrastructure.adapters.backup_strategy import _restore_backup_dir_layout
from src.app.infrastructure.adapters.storage.multi_storage_adapter import (
    MultiStorageAdapter,
    StorageUploadError,
)


class BackupIntegrityTests(unittest.TestCase):
    def service(self, storage, strategy, container=None, container_config=None):
        if container is None:
            container = Mock()
            container.get_containers_by_labels.return_value = []
        return BackupService(
            storage,
            container,
            Mock(),
            strategy,
            BackupConfig(source_paths=["/source"]),
            container_config or ContainerConfig(),
        )

    def test_hot_backup_does_not_stop_containers(self):
        container = Mock()
        container.get_containers_by_labels.side_effect = [[], []]
        strategy = Mock()
        strategy.perform_backup.return_value = BackupResult(datetime.now(), 0, 1, True)

        result = self.service(
            Mock(),
            strategy,
            container,
            ContainerConfig(stop_containers=False),
        ).execute_backup()

        self.assertTrue(result.success)
        container.stop_containers.assert_not_called()
        container.start_containers.assert_not_called()

    def test_cold_backup_stops_and_restarts_containers(self):
        container = Mock()
        container.get_containers_by_labels.side_effect = [["container"], [], []]
        container.stop_containers.return_value = ["container"]
        strategy = Mock()
        strategy.perform_backup.return_value = BackupResult(datetime.now(), 0, 1, True)

        result = self.service(
            Mock(),
            strategy,
            container,
            ContainerConfig(stop_containers=True),
        ).execute_backup()

        self.assertTrue(result.success)
        container.stop_containers.assert_called_once_with(["container"])
        container.start_containers.assert_called_once_with(["container"])

    def test_configured_upload_failure_is_not_success(self):
        with patch(
            "src.app.infrastructure.adapters.storage.multi_storage_adapter.subprocess.run",
            side_effect=RuntimeError("access denied"),
        ):
            with self.assertRaises(StorageUploadError) as error:
                MultiStorageAdapter().upload("/artifact", BackupConfig(["/source"], aws_s3_bucket="bucket"))
        self.assertIn("s3://bucket", str(error.exception))

    def test_local_artifact_is_retained_when_upload_fails(self):
        storage = Mock()
        storage.upload.side_effect = RuntimeError("s3://bucket: denied")
        with tempfile.NamedTemporaryFile(delete=False) as artifact:
            path = artifact.name
        try:
            strategy = Mock()
            strategy.perform_backup.return_value = BackupResult(datetime.now(), 0, 1, True, path)
            result = self.service(storage, strategy).execute_backup()
            self.assertFalse(result.success)
            self.assertTrue(os.path.exists(path))
            self.assertIn(path, result.error)
            storage.cleanup.assert_not_called()
        finally:
            os.unlink(path)

    def test_multiple_destination_failure_is_explicit(self):
        adapter = MultiStorageAdapter()
        adapter._upload_s3 = Mock(side_effect=RuntimeError("denied"))
        adapter._upload_scp = Mock(side_effect=RuntimeError("offline"))
        config = BackupConfig(["/source"], aws_s3_bucket="bucket", scp_host="host")
        with self.assertRaises(StorageUploadError) as error:
            adapter.upload("/artifact", config)
        self.assertIn("s3://bucket", str(error.exception))
        self.assertIn("scp://host", str(error.exception))

    def test_backup_restart_failure_is_reported(self):
        container = Mock()
        container.get_containers_by_labels.return_value = ["container"]
        container.stop_containers.return_value = ["container"]
        container.start_containers.side_effect = RuntimeError("restart denied")
        strategy = Mock()
        strategy.perform_backup.return_value = BackupResult(datetime.now(), 0, 1, True)
        result = self.service(Mock(), strategy, container).execute_backup()
        self.assertFalse(result.success)
        self.assertIn("restart denied", result.error)

    def test_restore_download_cleanup_runs_on_failure(self):
        storage = Mock()
        storage.download_restore_candidate.return_value = "/tmp/private-restore.tar.gz"
        strategy = Mock()
        strategy.restore.side_effect = RuntimeError("extract failed")
        config = RestoreConfig("/restore", source="s3://bucket/selected.tar.gz", dry_run=False, force_overwrite=True)
        container = Mock()
        container.find_containers_using_runtime_volumes.return_value = []
        container.find_containers_using_volume.return_value = []
        result = RestoreService(storage, container, strategy, config).execute_restore()
        self.assertFalse(result.success)
        self.assertIn("extract failed", result.error)
        storage.cleanup.assert_called_once_with("/tmp/private-restore.tar.gz")

    def test_selected_snapshot_reconciliation_removes_absent_files(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            source_volume = Path(source) / "backup" / "volume"
            target_volume = Path(target) / "volume"
            absent = Path(target) / "absent"
            source_volume.mkdir(parents=True)
            target_volume.mkdir(parents=True)
            absent.mkdir()
            (source_volume / "restored.txt").write_text("selected snapshot")
            (target_volume / "stale.txt").write_text("old")
            (absent / "stale.txt").write_text("old")
            actions = []
            _restore_backup_dir_layout(source, RestoreConfig(target, layout="backup-dir"), actions)
            self.assertTrue((target_volume / "restored.txt").exists())
            self.assertFalse((target_volume / "stale.txt").exists())
            self.assertFalse((absent / "stale.txt").exists())
            self.assertTrue(any("absent" in action for action in actions))


if __name__ == "__main__":
    unittest.main()
