import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.app.application.services.backup_service import BackupService
from src.app.application.services.restore_service import RestoreService
from src.app.domain.models import BackupConfig, BackupResult, ContainerConfig, RestoreConfig, RestoreResult
from src.app.infrastructure.adapters.backup_strategy import _restore_backup_dir_layout
from src.app.infrastructure.adapters import backup_strategy as backup_module
from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy, TarballBackupStrategy, _apply_chown
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

    def test_backup_dir_restore_skips_read_only_mount_and_restores_writable_entries(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            source_root = Path(source) / "backup"
            target_root = Path(target)
            (source_root / "run_secrets").mkdir(parents=True)
            (source_root / "data").mkdir(parents=True)
            (source_root / "run_secrets" / "new_secret").write_text("must not be copied")
            (source_root / "data" / "restored.txt").write_text("restored")
            (target_root / "run_secrets").mkdir()
            (target_root / "data").mkdir()
            (target_root / "run_secrets" / "existing_secret").write_text("preserve")
            (target_root / "data" / "stale.txt").write_text("stale")
            actions = []

            _restore_backup_dir_layout(
                source,
                RestoreConfig(target, layout="backup-dir", read_only_paths=(str(target_root / "run_secrets"),)),
                actions,
            )

            self.assertEqual((target_root / "run_secrets" / "existing_secret").read_text(), "preserve")
            self.assertFalse((target_root / "run_secrets" / "new_secret").exists())
            self.assertTrue((target_root / "data" / "restored.txt").exists())
            self.assertFalse((target_root / "data" / "stale.txt").exists())
            self.assertTrue(any("Read-only mount skipped intentionally" in action for action in actions))

    def test_backup_dir_read_only_mount_matching_is_path_boundary_safe(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            source_root = Path(source) / "backup"
            target_root = Path(target)
            for name in ("data", "data2"):
                (source_root / name).mkdir(parents=True)
                (target_root / name).mkdir()
                (target_root / name / "old.txt").write_text("old")
                (source_root / name / "new.txt").write_text(name)
            actions = []

            _restore_backup_dir_layout(
                source,
                RestoreConfig(target, layout="backup-dir", read_only_paths=(str(target_root / "data"),)),
                actions,
            )

            self.assertTrue((target_root / "data" / "old.txt").exists())
            self.assertFalse((target_root / "data" / "new.txt").exists())
            self.assertTrue((target_root / "data2" / "new.txt").exists())
            self.assertFalse((target_root / "data2" / "old.txt").exists())
            self.assertFalse(any("data2" in action and "read-only" in action.lower() for action in actions))

    def test_direct_restore_read_only_target_fails_before_mutation_for_tar_and_restic(self):
        for strategy_type in (TarballBackupStrategy, ResticBackupStrategy):
            with self.subTest(strategy=strategy_type.__name__), tempfile.TemporaryDirectory() as target:
                marker = Path(target) / "must-remain.txt"
                marker.write_text("preserve")
                config = RestoreConfig(
                    target,
                    source="snapshot",
                    dry_run=False,
                    force_overwrite=True,
                    layout="direct",
                    read_only_paths=(target,),
                )
                with patch.object(backup_module, "_clear_target_contents") as clear, patch.object(
                    backup_module.subprocess, "run"
                ) as run:
                    result = strategy_type().restore("snapshot", config)

                self.assertFalse(result.success)
                self.assertEqual(result.category, "readonly_target")
                self.assertEqual(result.destructive_state, "none")
                self.assertEqual(marker.read_text(), "preserve")
                clear.assert_not_called()
                run.assert_not_called()

    def test_restore_gate_blocks_before_stop_clear_and_dry_run_probes_nothing(self):
        with tempfile.TemporaryDirectory() as target:
            container, strategy, storage = Mock(), Mock(), Mock()
            container.find_containers_using_runtime_volumes.return_value = []
            container.find_containers_using_volume.return_value = []
            config = RestoreConfig(target, source="snapshot", dry_run=False, force_overwrite=True, stop_containers=True, chown="1000:1000")
            probe = Mock(return_value={"state": "readonly", "category": "readonly_target", "mount_mode": "readonly", "writable": False})
            result = RestoreService(storage, container, strategy, config, capability_probe=probe).execute_restore()
            self.assertFalse(result.success)
            self.assertEqual(result.category, "readonly_target")
            self.assertEqual(result.destructive_state, "none")
            container.stop_containers.assert_not_called(); storage.download_restore_candidate.assert_not_called(); strategy.restore.assert_not_called()

            config.dry_run = True; probe.reset_mock()
            result = RestoreService(storage, container, strategy, config, capability_probe=probe).execute_restore()
            self.assertTrue(result.success); probe.assert_not_called(); strategy.restore.assert_not_called()
            config.dry_run = False
            evidence = {"state": "unknown", "category": "chown_capability_unknown", "userns_mode": "unknown"}
            result = RestoreService(storage, container, strategy, config, capability_probe=lambda *_: evidence).execute_restore()
            self.assertFalse(result.success); self.assertEqual(result.category, "chown_capability_unknown")
            self.assertEqual(result.capability["userns_mode"], "unknown"); strategy.restore.assert_not_called()

    def test_tar_and_restic_share_no_follow_normalization_and_partial_state(self):
        for strategy_type, target_flag in ((TarballBackupStrategy, "-C"), (ResticBackupStrategy, "--target")):
            with self.subTest(strategy=strategy_type.__name__), tempfile.TemporaryDirectory() as target:
                target_path = Path(target); (target_path / "stale").write_text("old")
                def run(command, **_):
                    destination = command[command.index(target_flag) + 1]; Path(destination, "restored").write_text("new")
                config = RestoreConfig(target, source="snapshot", dry_run=False, force_overwrite=True, chown="1000:1000", layout="direct")
                with patch.object(backup_module.os, "lchown", create=True), patch.object(backup_module.subprocess, "run", side_effect=run):
                    result = strategy_type().restore("archive", config)
                self.assertTrue(result.success); self.assertTrue(result.normalization["changed"]); self.assertEqual(result.destructive_state, "complete")

    def test_symlink_boundary_and_partial_normalization_are_reported(self):
        with tempfile.TemporaryDirectory() as root:
            target, outside = Path(root) / "target", Path(root) / "outside"; target.mkdir(); outside.mkdir(); (outside / "secret").write_text("x")
            link = target / "link"
            try: link.symlink_to(outside, target_is_directory=True)
            except OSError: self.skipTest("symlink creation is unavailable")
            calls = []
            with patch.object(backup_module.os, "lchown", create=True, side_effect=lambda path, *_: calls.append(path)):
                report = _apply_chown(str(target), "1000:1000")
            self.assertIn(str(link), calls); self.assertNotIn(str(outside), calls); self.assertEqual(report["state"], "complete")
            with patch.object(backup_module.os, "lchown", create=True, side_effect=[None, OSError("denied")]):
                with self.assertRaises(backup_module.OwnershipNormalizationError) as error: _apply_chown(str(target), "1000:1000")
            self.assertEqual(error.exception.report["state"], "partial")

    def test_cold_restore_restart_failure_is_failed_and_disclosed(self):
        storage, strategy, container = Mock(), Mock(), Mock()
        storage.download_restore_candidate.return_value = "/tmp/restore.tar.gz"
        strategy.restore.return_value = RestoreResult(datetime.now(), 0, True)
        container.find_containers_using_runtime_volumes.return_value = ["app"]
        container.stop_containers.return_value = ["app"]; container.start_containers.side_effect = RuntimeError("restart denied")
        config = RestoreConfig("/restore", source="snapshot", dry_run=False, force_overwrite=True, stop_containers=True)
        result = RestoreService(storage, container, strategy, config).execute_restore()
        self.assertFalse(result.success); self.assertEqual(result.category, "restart_failed"); self.assertEqual(result.restart["state"], "failed")


if __name__ == "__main__":
    unittest.main()
