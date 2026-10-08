import os
import tempfile
import unittest

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import BackupTargetRecord, WorkerRecord, utcnow
from src.control_plane.infrastructure.repositories.in_memory import (
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
from src.control_plane.infrastructure.repositories.sqlite import SQLiteTargetRepository, SQLiteWorkerRepository


class NativeFilesystemTargetTests(unittest.TestCase):
    def make_service(self):
        workers = InMemoryWorkerRepository()
        workers.save(
            WorkerRecord(
                name="native-worker",
                host_name="native-host",
                id="worker-native",
                status="online",
                last_seen_at=utcnow(),
                labels={"runtime_type": "native"},
            )
        )
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
        )

    def test_create_read_update_native_target_paths_and_runtime_metadata(self):
        service = self.make_service()

        target = service.register_target(
            "native-documents",
            "worker-native",
            runtime_type="native",
            filesystem_paths=["/home/alice/Documents", "C:\\Users\\Alice\\My Documents"],
            runtime_environment={"RESTIC_REPOSITORY": "local:/repo", "RESTIC_PASSWORD": "pw"},
        )

        self.assertEqual(target.runtime_type, "native")
        self.assertEqual(target.filesystem_paths, ["/home/alice/Documents", "C:\\Users\\Alice\\My Documents"])
        self.assertEqual(target.volume_targets, [])
        self.assertIsNone(target.compose_project)
        listed = service.list_targets()[0]
        self.assertEqual(listed.filesystem_paths, target.filesystem_paths)

        updated = service.update_target(target.id, filesystem_paths=["/srv/data with spaces"])
        self.assertEqual(updated.runtime_type, "native")
        self.assertEqual(updated.filesystem_paths, ["/srv/data with spaces"])

    def test_native_target_is_hot_only_for_create_update_and_dispatch(self):
        service = self.make_service()

        with self.assertRaisesRegex(ValueError, "native filesystem targets support only hot backup mode"):
            service.register_target(
                "native-cold",
                "worker-native",
                runtime_type="native",
                backup_mode="cold",
                filesystem_paths=["/srv/app"],
            )

        docker_cold = service.register_target("docker-cold", "worker-native", backup_mode="cold")
        converted = service.update_target(
            docker_cold.id,
            runtime_type="native",
            filesystem_paths=["/srv/app"],
        )
        self.assertEqual(converted.runtime_type, "native")
        self.assertEqual(converted.backup_mode, "hot")

        with self.assertRaisesRegex(ValueError, "native filesystem targets support only hot backup mode"):
            service.update_target(converted.id, backup_mode="cold")
        with self.assertRaisesRegex(ValueError, "native filesystem targets support only hot backup mode"):
            service.dispatch_backup_for_target(converted.id, backup_mode="cold")

    def test_native_dispatch_preserves_windows_and_space_paths_without_backup_rewrite(self):
        service = self.make_service()
        target = service.register_target(
            "native-documents",
            "worker-native",
            runtime_type="native",
            filesystem_paths=["C:\\Users\\Alice\\My Documents", "/srv/data with spaces"],
            runtime_environment={"RESTIC_REPOSITORY": "local:/repo", "RESTIC_PASSWORD": "pw"},
        )

        job = service.dispatch_backup_for_target(target.id)

        self.assertEqual(job.payload["runtime_type"], "native")
        self.assertEqual(job.payload["filesystem_paths"], ["C:\\Users\\Alice\\My Documents", "/srv/data with spaces"])
        self.assertEqual(job.payload["volume_targets"], [])
        self.assertNotIn("/backup", "\n".join(job.payload["filesystem_paths"]))
        self.assertEqual(job.payload["volumes"], {})

    def test_native_target_rejects_implicit_or_mixed_source_configuration(self):
        service = self.make_service()

        with self.assertRaisesRegex(ValueError, "one or more explicit filesystem paths"):
            service.register_target("native-empty", "worker-native", runtime_type="native")
        with self.assertRaisesRegex(ValueError, "must not specify compose_project"):
            service.register_target(
                "native-compose",
                "worker-native",
                runtime_type="native",
                compose_project="app",
                filesystem_paths=["/srv/app"],
            )
        with self.assertRaisesRegex(ValueError, "must use filesystem_paths"):
            service.register_target(
                "native-volume",
                "worker-native",
                runtime_type="native",
                volume_targets=["/backup/data"],
                filesystem_paths=["/srv/app"],
            )

    def test_sqlite_migration_persists_native_filesystem_paths_and_keeps_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "control-plane.db")
            worker_repo = SQLiteWorkerRepository(db_path)
            target_repo = SQLiteTargetRepository(db_path)
            worker_repo.save(WorkerRecord(name="native", host_name="host", id="worker-native"))
            target_repo.save(
                BackupTargetRecord(
                    name="native-documents",
                    worker_id="worker-native",
                    id="target-native",
                    runtime_type="native",
                    filesystem_paths=["C:\\Users\\Alice\\My Documents", "/srv/data with spaces"],
                )
            )

            persisted = SQLiteTargetRepository(db_path).get("target-native")
            self.assertEqual(persisted.runtime_type, "native")
            self.assertEqual(persisted.filesystem_paths, ["C:\\Users\\Alice\\My Documents", "/srv/data with spaces"])

            target_repo.save(BackupTargetRecord(name="legacy", worker_id="worker-native", id="target-docker"))
            legacy = SQLiteTargetRepository(db_path).get("target-docker")
            self.assertEqual(legacy.runtime_type, "docker")
            self.assertEqual(legacy.filesystem_paths, [])

    def test_ui_exposes_native_path_configuration_without_recursive_browser(self):
        with open("src/control_plane/ui/index.html", "r", encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn('id="targetRuntimeType"', html)
        self.assertIn('id="targetFilesystemPaths"', html)
        self.assertIn('data-runtime-section="docker"', html)
        self.assertIn('targetBackupMode.value = "hot"', html)
        self.assertNotIn("recursive filesystem browser", html.lower())

    def test_ui_edit_preserves_existing_kubernetes_runtime_without_silent_docker_conversion(self):
        with open("src/control_plane/ui/index.html", "r", encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn('<option value="kubernetes" ${target.runtime_type === "kubernetes" ? "selected" : ""}>Kubernetes</option>', html)
        self.assertIn('namespace: target.runtime_type === "kubernetes" ? target.namespace : undefined', html)
        self.assertIn('pvc_names: target.runtime_type === "kubernetes" ? (target.pvc_names || []) : undefined', html)


if __name__ == "__main__":
    unittest.main()
