import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.application.services.scheduler_service import SchedulerService, cron_matches
from src.control_plane.domain.models import BackupTargetRecord, JobRecord, JobStatus, WorkerRecord, utcnow
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
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository


ROOT = Path(__file__).resolve().parents[1]


class SchedulerDeploymentTests(unittest.TestCase):
    def test_sunday_zero_and_seven_map_to_sunday_in_utc(self):
        sunday = datetime(2024, 1, 7, 12, 30, tzinfo=timezone.utc)
        self.assertTrue(cron_matches("30 12 * * 0", sunday))
        self.assertTrue(cron_matches("30 12 * * 7", sunday))
        self.assertFalse(cron_matches("30 12 * * 1", sunday))

    def test_restricted_dom_and_dow_use_or_semantics(self):
        expression = "0 0 15 * 1"
        self.assertTrue(cron_matches(expression, datetime(2024, 6, 15, 0, 0)))
        self.assertTrue(cron_matches(expression, datetime(2024, 6, 3, 0, 0)))
        self.assertFalse(cron_matches(expression, datetime(2024, 6, 4, 0, 0)))

    def test_invalid_cron_is_safe_and_does_not_match(self):
        now = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        for expression in ("", "* * *", "* * * * * *", "*/0 * * * *", "60 * * * *", "* * * * 8", "* * 32 * *"):
            self.assertFalse(cron_matches(expression, now), expression)

    def test_expired_in_progress_lease_fails_before_pending_claim(self):
        self._assert_expired_lease_failed(InMemoryJobRepository())
        with tempfile.TemporaryDirectory() as directory:
            self._assert_expired_lease_failed(SQLiteJobRepository(str(Path(directory) / "control-plane.db")))

    def test_scheduler_suppresses_existing_backup_job(self):
        service = self._service()
        target = BackupTargetRecord(name="target", worker_id="worker-a", cron_expression="* * * * *")
        service.target_repository.save(target)
        service.dispatch_backup_for_target(target.id, requested_by="test")
        scheduler = SchedulerService(service)
        fixed_now = datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc)
        with patch("src.control_plane.application.services.scheduler_service.datetime") as clock:
            clock.now.return_value = fixed_now
            scheduler._tick()
        self.assertEqual(len(service.job_repository.list()), 1)

    def test_compose_and_ci_defaults(self):
        worker_compose = (ROOT / "deploy/worker/docker-compose.yml").read_text()
        ghcr_compose = (ROOT / "deploy/worker/docker-compose.ghcr.yml").read_text()
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("worker_state:/data", worker_compose)
        self.assertIn("worker_state:/data", ghcr_compose)
        self.assertIn(
            "name: ${CONTROL_PLANE_NETWORK:-docker-volume-backup-control-plane-ghcr_default}",
            ghcr_compose,
        )
        self.assertIn("HTTP is trusted-network default", worker_compose)
        self.assertIn("HTTP is trusted-network default", ghcr_compose)
        self.assertIn("SNAPSHOT_EXPLORER_REDIS_TTL_SECONDS: ${SNAPSHOT_EXPLORER_REDIS_TTL_SECONDS:-86400}", worker_compose)
        self.assertIn("SNAPSHOT_EXPLORER_REDIS_TTL_SECONDS: ${SNAPSHOT_EXPLORER_REDIS_TTL_SECONDS:-86400}", ghcr_compose)
        self.assertIn("BACKUP_RUNTIME_IMAGE: ${BACKUP_RUNTIME_IMAGE:-ghcr.io/danielrondongarcia/docker-volume-backup:latest}", ghcr_compose)
        self.assertNotIn("BACKUP_RUNTIME_IMAGE: ${BACKUP_RUNTIME_IMAGE:-ghcr.io/danielrondongarcia/docker-volume-backup-worker:latest}", ghcr_compose)
        self.assertIn("pull_request:", workflow)
        self.assertIn("python-version: \"3.11\"", workflow)
        self.assertIn("python -m pip install -r requirements.txt", workflow)
        self.assertIn("python -m unittest discover tests", workflow)

    def _assert_expired_lease_failed(self, repository):
        stale_token = "stale-token"
        interrupted = JobRecord(
            worker_id="worker-a",
            command="backup.run",
            status=JobStatus.IN_PROGRESS,
            owner_worker_id="worker-a",
            lease_token=stale_token,
            lease_issued_at=utcnow() - timedelta(minutes=6),
            lease_expires_at=utcnow() - timedelta(minutes=1),
            attempt_count=1,
        )
        pending = JobRecord(worker_id="worker-a", command="worker.self_check")
        repository.save(interrupted)
        repository.save(pending)
        claimed = repository.claim_pending_for_worker("worker-a")
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].id, pending.id)
        self.assertEqual(claimed[0].status, JobStatus.IN_PROGRESS)
        self.assertEqual(claimed[0].attempt_count, 1)

        failed = repository.get(interrupted.id)
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, JobStatus.FAILED)
        self.assertEqual(failed.attempt_count, 1)
        self.assertIsNotNone(failed.finished_at)
        self.assertIsNotNone(failed.updated_at)
        self.assertIsNone(failed.owner_worker_id)
        self.assertIsNone(failed.lease_token)
        self.assertIsNone(failed.lease_issued_at)
        self.assertIsNone(failed.lease_expires_at)
        self.assertEqual(
            failed.result_summary,
            {
                "error": "worker lease expired before the job reported a terminal result",
                "recovery": "worker_interrupted",
            },
        )
        self.assertEqual(failed.log_lines, ["Worker lease expired before terminal status was reported."])

        service = self._service(repository)
        with self.assertRaisesRegex(ValueError, "does not own"):
            service.update_job_status(
                worker_id="worker-a",
                job_id=interrupted.id,
                status=JobStatus.SUCCEEDED,
                lease_token=stale_token,
            )
        self.assertEqual(repository.get(interrupted.id).status, JobStatus.FAILED)

    @staticmethod
    def _service(job_repository=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a", last_seen_at=utcnow()))
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=job_repository or InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
        )


if __name__ == "__main__":
    unittest.main()
