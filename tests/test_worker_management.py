import os
import tempfile
import unittest

from src.control_plane.application.services.control_plane_service import (
    ControlPlaneService,
    WorkerDeletionConflict,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    InventorySnapshot,
    JobRecord,
    JobStatus,
    SnapshotRecord,
    TargetStatsRecord,
    WorkerRecord,
    WorkerStatus,
    utcnow,
)
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
from src.control_plane.infrastructure.repositories.sqlite import (
    SQLiteInventoryRepository,
    SQLiteJobRepository,
    SQLiteRetentionPolicyRepository,
    SQLiteSecretRepository,
    SQLiteSettingsRepository,
    SQLiteSnapshotRepository,
    SQLiteStorageProfileRepository,
    SQLiteTargetRepository,
    SQLiteTargetStatsRepository,
    SQLiteWorkerRepository,
)
from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
from src.security.hmac_protocol import digest_secret


class WorkerManagementTests(unittest.TestCase):
    @staticmethod
    def make_service():
        workers = InMemoryWorkerRepository()
        workers.save(
            WorkerRecord(
                name="worker-a",
                host_name="host-a",
                id="worker-a",
                status=WorkerStatus.ONLINE,
                last_seen_at=utcnow(),
            )
        )
        inventory = InMemoryInventoryRepository()
        targets = InMemoryTargetRepository()
        jobs = InMemoryJobRepository()
        stats = InMemoryTargetStatsRepository()
        service = ControlPlaneService(
            worker_repository=workers,
            inventory_repository=inventory,
            target_repository=targets,
            job_repository=jobs,
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=stats,
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
        )
        auth = WorkerAuthState()
        service.worker_auth = auth
        return service, auth, inventory, targets, jobs, stats

    @staticmethod
    def enroll(auth, secret="s" * 32, worker_id="worker-a"):
        auth.create_enrollment("worker-a", "host-a", {"tier": "prod"}, secret, worker_id=worker_id)
        auth.complete(secret)

    def test_revoke_disables_worker_and_blocks_new_jobs(self):
        service, auth, _, targets, _, _ = self.make_service()
        self.enroll(auth)
        target = BackupTargetRecord(name="target-a", worker_id="worker-a", id="target-a")
        targets.save(target)

        result = service.revoke_worker("worker-a")

        self.assertEqual(result["status"], "revoked")
        self.assertEqual(result["worker_status"], WorkerStatus.DISABLED)
        self.assertEqual(auth._get("worker-a", "1").status, "revoked")
        self.assertFalse(service.is_worker_eligible("worker-a"))
        self.assertFalse(targets.get("target-a").enabled)
        with self.assertRaisesRegex(ValueError, "not eligible"):
            service.dispatch_job("worker-a", "worker.self_check")

    def test_revoke_works_for_offline_worker(self):
        service, auth, _, _, _, _ = self.make_service()
        worker = service.worker_repository.get("worker-a")
        worker.last_seen_at = None
        service.worker_repository.save(worker)
        self.enroll(auth)

        service.revoke_worker("worker-a")

        self.assertEqual(service.worker_repository.get("worker-a").status, WorkerStatus.DISABLED)
        self.assertEqual(auth._get("worker-a", "1").status, "revoked")

    def test_worker_labels_reject_invalid_keys_and_allow_clear(self):
        service, _, _, _, _, _ = self.make_service()

        updated = service.update_worker("worker-a", {" zone ": "prod"})
        self.assertEqual(updated.labels, {"zone": "prod"})
        service.update_worker("worker-a", {})
        self.assertEqual(service.worker_repository.get("worker-a").labels, {})
        with self.assertRaisesRegex(ValueError, "must not be blank"):
            service.update_worker("worker-a", {" ": "value"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            service.update_worker("worker-a", {"zone": "prod", " zone ": "staging"})
        with self.assertRaisesRegex(ValueError, "must be an object"):
            service.update_worker("worker-a", [])

    def test_delete_preserves_assigned_targets_and_rejects_active_jobs(self):
        service, auth, inventory, targets, jobs, _ = self.make_service()
        self.enroll(auth)
        inventory.save(InventorySnapshot(worker_id="worker-a", inventory={"compose": []}))
        targets.save(BackupTargetRecord(name="target-a", worker_id="worker-a", id="target-a"))

        self.assertTrue(service.delete_worker("worker-a"))
        preserved_target = targets.get("target-a")
        self.assertIsNotNone(preserved_target)
        self.assertEqual(preserved_target.worker_id, "worker-a")
        self.assertFalse(preserved_target.enabled)
        self.assertIsNone(service.worker_repository.get("worker-a"))
        self.assertIsNone(auth._get("worker-a", "1"))

        active_service, active_auth, _, active_targets, active_jobs, _ = self.make_service()
        self.enroll(active_auth)
        active_targets.save(BackupTargetRecord(name="target-a", worker_id="worker-a", id="target-a"))
        active_jobs.save(JobRecord(worker_id="worker-a", command="backup.run", id="job-a"))
        with self.assertRaisesRegex(WorkerDeletionConflict, "active or pending jobs"):
            active_service.delete_worker("worker-a")
        self.assertIsNotNone(active_service.worker_repository.get("worker-a"))
        self.assertTrue(active_targets.get("target-a").enabled)

    def test_target_enablement_requires_an_online_effective_worker(self):
        service, auth, _, targets, _, _ = self.make_service()
        target = BackupTargetRecord(name="target-a", worker_id="worker-a", id="target-a", enabled=False)
        targets.save(target)

        self.enroll(auth)
        service.revoke_worker("worker-a")
        with self.assertRaisesRegex(ValueError, "not eligible"):
            service.update_target("target-a", enabled=True)

        missing_target = BackupTargetRecord(name="missing", worker_id="missing-worker", id="missing", enabled=False)
        targets.save(missing_target)
        with self.assertRaisesRegex(ValueError, "worker not found"):
            service.update_target("missing", enabled=True)

        offline_service, _, _, offline_targets, _, _ = self.make_service()
        offline_worker = offline_service.worker_repository.get("worker-a")
        offline_worker.last_seen_at = None
        offline_service.worker_repository.save(offline_worker)
        offline_targets.save(BackupTargetRecord(name="offline", worker_id="worker-a", id="offline", enabled=False))
        with self.assertRaisesRegex(ValueError, "offline"):
            offline_service.update_target("offline", enabled=True)

        reassigned_service, _, _, reassigned_targets, _, _ = self.make_service()
        reassigned_service.worker_repository.save(
            WorkerRecord(name="worker-b", host_name="host-b", id="worker-b", status=WorkerStatus.ONLINE, last_seen_at=utcnow())
        )
        reassigned_targets.save(BackupTargetRecord(name="reassign", worker_id="worker-a", id="reassign", enabled=False))
        reassigned = reassigned_service.update_target("reassign", worker_id="worker-b")
        self.assertFalse(reassigned.enabled)
        updated = reassigned_service.update_target("reassign", enabled=True)
        self.assertEqual(updated.worker_id, "worker-b")
        self.assertTrue(updated.enabled)

    def test_deleted_worker_cleanup_preserves_assigned_target_in_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "control-plane.db")
            auth = WorkerAuthState(database_path)
            workers = SQLiteWorkerRepository(database_path)
            inventory = SQLiteInventoryRepository(database_path)
            targets = SQLiteTargetRepository(database_path)
            jobs = SQLiteJobRepository(database_path)
            snapshots = SQLiteSnapshotRepository(database_path)
            stats = SQLiteTargetStatsRepository(database_path)
            service = ControlPlaneService(
                worker_repository=workers,
                inventory_repository=inventory,
                target_repository=targets,
                job_repository=jobs,
                storage_profile_repository=SQLiteStorageProfileRepository(database_path),
                secret_repository=SQLiteSecretRepository(database_path),
                snapshot_repository=snapshots,
                retention_policy_repository=SQLiteRetentionPolicyRepository(database_path),
                target_stats_repository=stats,
                secret_codec=object(),
                settings_repository=SQLiteSettingsRepository(database_path),
            )
            service.worker_auth = auth
            workers.save(WorkerRecord(name="worker-a", host_name="host-a", id="worker-a", last_seen_at=utcnow()))
            self.enroll(auth)
            targets.save(BackupTargetRecord(name="target-a", worker_id="worker-a", id="target-a"))

            self.assertTrue(service.delete_worker("worker-a"))

            preserved_target = targets.get("target-a")
            self.assertIsNotNone(preserved_target)
            self.assertEqual(preserved_target.worker_id, "worker-a")
            self.assertFalse(preserved_target.enabled)

    def test_delete_removes_worker_metadata_and_auth_but_preserves_history(self):
        service, auth, inventory, _, jobs, stats = self.make_service()
        self.enroll(auth)
        auth.rotate("worker-a", "r" * 32)
        pending_secret = "p" * 32
        auth.create_enrollment("worker-a", "host-a", {}, pending_secret, worker_id="worker-a")
        auth._consume_nonce("worker-a", "2", "nonce-before-delete")
        inventory.save(InventorySnapshot(worker_id="worker-a", inventory={"compose": []}))
        historical_target_id = "historical-target"
        service.snapshot_repository.replace_for_target(
            historical_target_id,
            [SnapshotRecord(historical_target_id, "worker-a", "abcdef12", utcnow())],
        )
        stats.save(TargetStatsRecord(target_id=historical_target_id, worker_id="worker-a"))
        historical_job = JobRecord(
            worker_id="worker-a",
            command="backup.run",
            status=JobStatus.SUCCEEDED,
        )
        jobs.save(historical_job)

        self.assertTrue(service.delete_worker("worker-a"))

        self.assertIsNone(service.worker_repository.get("worker-a"))
        self.assertIsNone(inventory.get_by_worker("worker-a"))
        self.assertIsNone(stats.get_by_target(historical_target_id))
        self.assertIsNotNone(jobs.get(historical_job.id))
        self.assertEqual(len(service.snapshot_repository.list_by_target(historical_target_id)), 1)
        self.assertIsNone(auth._get("worker-a", "1"))
        self.assertIsNone(auth._get("worker-a", "2"))
        self.assertIsNone(auth._enrollment(digest_secret(pending_secret)))
        self.assertTrue(auth._consume_nonce("worker-a", "2", "nonce-before-delete"))
        self.assertFalse(service.delete_worker("worker-a"))

    def test_sqlite_delete_has_the_same_cleanup_and_history_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "control-plane.db")
            auth = WorkerAuthState(database_path)
            workers = SQLiteWorkerRepository(database_path)
            inventory = SQLiteInventoryRepository(database_path)
            targets = SQLiteTargetRepository(database_path)
            jobs = SQLiteJobRepository(database_path)
            snapshots = SQLiteSnapshotRepository(database_path)
            stats = SQLiteTargetStatsRepository(database_path)
            service = ControlPlaneService(
                worker_repository=workers,
                inventory_repository=inventory,
                target_repository=targets,
                job_repository=jobs,
                storage_profile_repository=SQLiteStorageProfileRepository(database_path),
                secret_repository=SQLiteSecretRepository(database_path),
                snapshot_repository=snapshots,
                retention_policy_repository=SQLiteRetentionPolicyRepository(database_path),
                target_stats_repository=stats,
                secret_codec=object(),
                settings_repository=SQLiteSettingsRepository(database_path),
            )
            service.worker_auth = auth
            workers.save(WorkerRecord(name="worker-a", host_name="host-a", id="worker-a", last_seen_at=utcnow()))
            self.enroll(auth)
            auth.rotate("worker-a", "r" * 32)
            inventory.save(InventorySnapshot(worker_id="worker-a", inventory={"docker": True}))
            snapshots.replace_for_target(
                "historical-target",
                [SnapshotRecord("historical-target", "worker-a", "abcdef12", utcnow())],
            )
            stats.save(TargetStatsRecord(target_id="historical-target", worker_id="worker-a"))
            historical_job = JobRecord(worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED)
            jobs.save(historical_job)

            self.assertTrue(service.delete_worker("worker-a"))

            self.assertIsNone(workers.get("worker-a"))
            self.assertIsNone(inventory.get_by_worker("worker-a"))
            self.assertIsNone(stats.get_by_target("historical-target"))
            self.assertIsNotNone(jobs.get(historical_job.id))
            self.assertEqual(len(snapshots.list_by_target("historical-target")), 1)
            persisted_auth = WorkerAuthState(database_path)
            self.assertIsNone(persisted_auth._get("worker-a", "1"))
            self.assertIsNone(persisted_auth._get("worker-a", "2"))


if __name__ == "__main__":
    unittest.main()
