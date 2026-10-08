import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.app.application.services.restore_service import RestoreService
from src.app.domain.models import RestoreConfig, RestoreResult
from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import BackupTargetRecord, WorkerRecord, utcnow
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryCacheRepository,
    InMemoryIndexRepository,
    InMemoryInventoryRepository,
    InMemoryJobRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetRepository,
    InMemoryTargetStatsRepository,
    InMemoryWorkerRepository,
)
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


class NativeRestoreTests(unittest.TestCase):
    def make_service(self):
        workers = InMemoryWorkerRepository()
        workers.save(
            WorkerRecord(
                name="native-worker",
                host_name="host",
                id="native-worker",
                status="online",
                last_seen_at=utcnow(),
                labels={"runtime_type": "native"},
            )
        )
        targets = InMemoryTargetRepository()
        targets.save(
            BackupTargetRecord(
                name="native-target",
                worker_id="native-worker",
                id="native-target",
                runtime_type="native",
                backup_strategy="restic",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo", "RESTIC_PASSWORD": "pw"},
                filesystem_paths=["/srv/app", r"C:\\Data\\App"],
            )
        )
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=targets,
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
        )

    def test_native_restore_payload_requires_explicit_destination_and_preserves_host_paths(self):
        service = self.make_service()

        job = service.dispatch_restore_for_target(
            "native-target",
            restore_source="abcdef12",
            restore_target_path="/restore/native-target",
            dry_run=True,
        )

        self.assertEqual(job.command, "restore.dry_run")
        self.assertEqual(job.payload["runtime_type"], "native")
        self.assertEqual(job.payload["filesystem_paths"], ["/srv/app", r"C:\\Data\\App"])
        self.assertEqual(job.payload["environment"]["RESTORE_TARGET_PATH"], "/restore/native-target")
        self.assertEqual(job.payload["environment"]["RESTORE_DRY_RUN"], "true")
        self.assertEqual(job.payload["environment"]["RESTORE_FORCE_OVERWRITE"], "false")
        self.assertEqual(job.payload["environment"]["RESTORE_SOURCE"], "abcdef12")
        self.assertEqual(job.payload["environment"].get("RESTORE_READ_ONLY_PATHS"), "[]")
        self.assertEqual(job.payload["volumes"], {})
        self.assertNotEqual(job.payload["environment"]["RESTORE_TARGET_PATH"], "/backup")

        with self.assertRaisesRegex(ValueError, "explicit host restore destination"):
            service.dispatch_restore_for_target("native-target", restore_source="abcdef12")

    def test_native_restore_accepts_windows_destination_and_rejects_source_overwrite_or_traversal(self):
        service = self.make_service()

        job = service.dispatch_restore_for_target(
            "native-target",
            restore_source="abcdef12",
            restore_target_path=r"C:\\Restore\\native-target",
            dry_run=False,
            force_overwrite=True,
        )

        self.assertEqual(job.command, "restore.run")
        self.assertEqual(job.payload["environment"]["RESTORE_TARGET_PATH"], r"C:\\Restore\\native-target")
        self.assertEqual(job.payload["environment"]["RESTORE_FORCE_OVERWRITE"], "true")

        for destination in ("relative/path", "../escape", "/srv/app", r"C:\\Data\\App\\child"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                service.dispatch_restore_for_target(
                    "native-target",
                    restore_source="abcdef12",
                    restore_target_path=destination,
                    dry_run=False,
                    force_overwrite=True,
                )

    def test_native_restore_rejects_windows_source_overlap_case_insensitively(self):
        service = self.make_service()

        for destination in (r"c:\\data\\app\\child", r"\\\\SERVER\\Share\\App\\child"):
            with self.subTest(destination=destination):
                target = service.target_repository.get("native-target")
                target.filesystem_paths = [r"C:\\Data\\App", r"\\\\server\\share\\app"]
                service.target_repository.save(target)
                with self.assertRaisesRegex(ValueError, "must not overwrite"):
                    service.dispatch_restore_for_target(
                        "native-target",
                        restore_source="abcdef12",
                        restore_target_path=destination,
                    )

    def test_native_restore_keeps_linux_source_overlap_case_sensitive(self):
        service = self.make_service()
        target = service.target_repository.get("native-target")
        target.filesystem_paths = ["/srv/App"]
        service.target_repository.save(target)

        job = service.dispatch_restore_for_target(
            "native-target",
            restore_source="abcdef12",
            restore_target_path="/srv/app/child",
        )

        self.assertEqual(job.payload["environment"]["RESTORE_TARGET_PATH"], "/srv/app/child")

    def test_native_runtime_rejects_windows_source_overlap_case_insensitively(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        for source, destination in (
            (r"C:\\Data\\App", r"c:\\data\\app\\child"),
            (r"\\\\server\\share\\app", r"\\\\SERVER\\Share\\App\\child"),
        ):
            with self.subTest(destination=destination), patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen") as popen:
                result = adapter.run_runtime_job(
                    "unused",
                    {
                        "runtime_type": "native",
                        "filesystem_paths": [source],
                        "command": "/root/backup.sh",
                        "environment": {"RESTORE_MODE": "true", "RESTORE_TARGET_PATH": destination},
                    },
                )
                self.assertFalse(result["success"])
                self.assertIn("must not overwrite", result["error"])
                popen.assert_not_called()

    def test_native_runtime_keeps_linux_source_overlap_case_sensitive(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["env"] = kwargs["env"]
            process = Mock()
            process.stdout.readline.side_effect = ["Restore dry-run complete\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job(
                "unused",
                {
                    "runtime_type": "native",
                    "filesystem_paths": ["/srv/App"],
                    "command": "/root/backup.sh",
                    "environment": {"RESTORE_MODE": "true", "RESTORE_TARGET_PATH": "/srv/app/child"},
                },
            )

        self.assertTrue(result["success"])
        self.assertEqual(recorded["env"]["RESTORE_TARGET_PATH"], "/srv/app/child")

    def test_native_restore_rejects_stop_containers_before_dispatch(self):
        service = self.make_service()

        with self.assertRaisesRegex(ValueError, "must not stop containers"):
            service.dispatch_restore_for_target(
                "native-target",
                restore_source="abcdef12",
                restore_target_path="/restore/native-target",
                stop_containers=True,
            )

    def test_native_runtime_restore_invokes_local_app_with_result_transport(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            recorded["env"] = kwargs["env"]
            result_path = kwargs["env"].get("RESTORE_RESULT_FILE")
            Path(result_path).write_text(json.dumps({"schema_version": 1, "status": "succeeded", "destructive_state": "none"}))
            process = Mock()
            process.stdout.readline.side_effect = ["Restore dry-run complete\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": "/root/backup.sh",
            "environment": {
                "RESTORE_MODE": "true",
                "RESTORE_DRY_RUN": "true",
                "RESTORE_TARGET_PATH": "/restore/native-target",
                "RESTORE_STOP_CONTAINERS": "false",
            },
            "_restore_result_transport": True,
        }
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], [sys.executable, "-m", "src.app.main"])
        self.assertEqual(recorded["env"]["BACKUP_RUNTIME_TYPE"], "native")
        self.assertEqual(recorded["env"]["BACKUP_SOURCES_JSON"], '["/srv/app"]')
        self.assertIn("restore_ownership", result)

    def test_frozen_native_runtime_restore_invokes_backup_engine_with_result_transport(self):
        adapter = NativeRuntimeAdapter(python_executable="python-test")
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            recorded["env"] = kwargs["env"]
            result_path = kwargs["env"].get("RESTORE_RESULT_FILE")
            Path(result_path).write_text(json.dumps({"schema_version": 1, "status": "succeeded", "destructive_state": "none"}))
            process = Mock()
            process.stdout.readline.side_effect = ["Restore dry-run complete\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": "/root/backup.sh",
            "environment": {
                "RESTORE_MODE": "true",
                "RESTORE_DRY_RUN": "true",
                "RESTORE_TARGET_PATH": "/restore/native-target",
                "RESTORE_STOP_CONTAINERS": "false",
            },
            "_restore_result_transport": True,
        }
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.sys.executable", "vaultline-worker.exe"), patch.object(
            sys, "frozen", True, create=True
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="vaultline-worker.exe"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["vaultline-worker.exe", "backup-engine"])
        self.assertEqual(recorded["env"]["BACKUP_RUNTIME_TYPE"], "native")
        self.assertIn("restore_ownership", result)

    def test_native_runtime_rejects_restore_stop_containers(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": "/root/backup.sh",
            "environment": {"RESTORE_MODE": "true", "RESTORE_STOP_CONTAINERS": "true", "RESTORE_TARGET_PATH": "/restore"},
        }
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen") as popen:
            result = adapter.run_runtime_job("unused", payload)
        self.assertFalse(result["success"])
        self.assertIn("must not stop containers", result["error"])
        popen.assert_not_called()

    def test_native_restore_service_does_not_query_or_stop_containers(self):
        storage, strategy, container = Mock(), Mock(), Mock()
        storage.download_restore_candidate.return_value = "snapshot"
        strategy.restore.return_value = RestoreResult(datetime.now(), 0, True)
        config = RestoreConfig(
            target_path="/restore/native-target",
            source="abcdef12",
            dry_run=False,
            force_overwrite=True,
            runtime_type="native",
            filesystem_paths=("/srv/app",),
        )

        result = RestoreService(storage, container, strategy, config).execute_restore()

        self.assertTrue(result.success)
        container.find_containers_using_runtime_volumes.assert_not_called()
        container.find_containers_using_volume.assert_not_called()
        container.stop_containers.assert_not_called()

    def test_windows_ownership_mapping_is_reported_unsupported_before_restore(self):
        storage, strategy, container = Mock(), Mock(), Mock()
        config = RestoreConfig(
            target_path=r"C:\\Restore\\native-target",
            source="abcdef12",
            dry_run=False,
            force_overwrite=True,
            chown="1000:1000",
            runtime_type="native",
            filesystem_paths=(r"C:\\Data\\App",),
        )
        with patch("src.app.application.services.restore_service.os.name", "nt"):
            result = RestoreService(storage, container, strategy, config).execute_restore()

        self.assertFalse(result.success)
        self.assertEqual(result.category, "ownership_unsupported")
        self.assertIn("Windows", result.error)
        strategy.restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
