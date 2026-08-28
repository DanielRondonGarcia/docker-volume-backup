import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.application.services.scheduler_service import (
    DEFAULT_SCHEDULER_TIMEZONE,
    SchedulerService,
    cron_matches,
    cron_next_run,
    resolve_scheduler_timezone,
)
from src.control_plane.domain.models import BackupTargetRecord, JobRecord, JobStatus, SettingsRecord, WorkerRecord, WorkerStatus, utcnow
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

    def test_scheduler_uses_configured_local_timezone_for_eligibility(self):
        service = self._service()
        target = BackupTargetRecord(name="target", worker_id="worker-a", cron_expression="0 3 * * *")
        service.target_repository.save(target)
        scheduler = SchedulerService(
            service,
            timezone_name="America/Bogota",
            now_fn=lambda: datetime(2024, 6, 3, 8, 0, tzinfo=timezone.utc),
        )

        scheduler._tick()

        jobs = service.job_repository.list()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].trigger, "schedule")
        self.assertEqual(scheduler.timezone_name, "America/Bogota")

    def test_scheduler_preview_uses_target_then_global_cron_and_returns_timezone(self):
        service = self._service()
        service.settings_repository.save(SettingsRecord(global_cron_expression="0 3 * * *"))
        target = BackupTargetRecord(name="target", worker_id="worker-a")
        scheduler = SchedulerService(service, timezone_name=DEFAULT_SCHEDULER_TIMEZONE)
        fixed_now = datetime(2024, 6, 3, 7, 0, tzinfo=timezone.utc)

        global_preview = scheduler.preview(target=target, now=fixed_now)
        self.assertEqual(global_preview["effective_cron_expression"], "0 3 * * *")
        self.assertEqual(global_preview["cron_source"], "global")
        self.assertEqual(global_preview["scheduler_timezone"], "America/Bogota")
        self.assertEqual(global_preview["next_scheduled_at"], "2024-06-03T08:00:00Z")

        cleared_global_preview = scheduler.preview(
            cron_expression="",
            settings=SettingsRecord(global_cron_expression="0 3 * * *"),
        )
        self.assertIsNone(cleared_global_preview["effective_cron_expression"])
        self.assertEqual(cleared_global_preview["cron_source"], "manual")

        new_target_preview = scheduler.preview(
            cron_expression="0 4 * * *",
            settings=SettingsRecord(global_cron_expression="0 3 * * *"),
            target_context=True,
            now=fixed_now,
        )
        self.assertEqual(new_target_preview["effective_cron_expression"], "0 4 * * *")
        self.assertEqual(new_target_preview["cron_source"], "target")

        empty_target_preview = scheduler.preview(
            cron_expression="",
            settings=SettingsRecord(global_cron_expression="0 3 * * *"),
            target_context=True,
            now=fixed_now,
        )
        self.assertEqual(empty_target_preview["effective_cron_expression"], "0 3 * * *")
        self.assertEqual(empty_target_preview["cron_source"], "global")

        target.cron_expression = "0 4 * * *"
        target_preview = scheduler.preview(target=target, now=fixed_now)
        self.assertEqual(target_preview["effective_cron_expression"], "0 4 * * *")
        self.assertEqual(target_preview["cron_source"], "target")
        self.assertEqual(target_preview["next_scheduled_at"], "2024-06-03T09:00:00Z")

    def test_cron_next_run_handles_leap_day_beyond_one_year(self):
        next_run = cron_next_run(
            "0 0 29 2 *",
            datetime(2024, 3, 1, 0, 0, tzinfo=timezone.utc),
            ZoneInfo("America/Bogota"),
        )

        self.assertEqual(next_run, datetime(2028, 2, 29, 5, 0, tzinfo=timezone.utc))

    def test_cron_next_run_skips_nonexistent_dst_wall_time(self):
        next_run = cron_next_run(
            "30 2 * * *",
            datetime(2024, 3, 10, 6, 0, tzinfo=timezone.utc),
            ZoneInfo("America/New_York"),
        )

        self.assertEqual(next_run, datetime(2024, 3, 11, 6, 30, tzinfo=timezone.utc))

    def test_invalid_scheduler_timezone_fails_with_configuration_name(self):
        with self.assertRaisesRegex(ValueError, "CONTROL_PLANE_TIMEZONE"):
            resolve_scheduler_timezone("Not/ARealTimezone")

        with patch.dict(os.environ, {"CONTROL_PLANE_TIMEZONE": "Not/ARealTimezone"}):
            with patch("src.control_plane.main._build_service", return_value=object()):
                from src.control_plane.main import build_application

                with self.assertRaisesRegex(ValueError, "CONTROL_PLANE_TIMEZONE"):
                    build_application()

    def test_job_trigger_survives_sqlite_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(str(Path(directory) / "control-plane.db"))
            for trigger in ("manual", "schedule", "automatic", "interactive"):
                job = JobRecord(worker_id="worker-a", command="worker.self_check", trigger=trigger)
                repository.save(job)
                self.assertEqual(repository.get(job.id).trigger, trigger)

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

    def test_revoked_worker_is_not_eligible_for_new_scheduled_jobs(self):
        service = self._service()
        worker = service.worker_repository.get("worker-a")
        worker.status = WorkerStatus.DISABLED
        service.worker_repository.save(worker)
        target = BackupTargetRecord(name="target", worker_id="worker-a", cron_expression="* * * * *")
        service.target_repository.save(target)

        scheduler = SchedulerService(service)
        scheduler._tick()

        self.assertFalse(service.is_worker_eligible("worker-a"))
        self.assertEqual(service.job_repository.list(), [])
        with self.assertRaisesRegex(ValueError, "not eligible"):
            service.dispatch_job("worker-a", "backup.run", target_id=target.id)

    def test_disabled_target_is_skipped_by_scheduler_but_manual_backup_dispatch_remains_available(self):
        service = self._service()
        target = BackupTargetRecord(name="target", worker_id="worker-a", enabled=False, cron_expression="* * * * *")
        service.target_repository.save(target)

        scheduler = SchedulerService(service)
        scheduler._tick()

        self.assertEqual(service.job_repository.list(), [])
        manual_job = service.dispatch_backup_for_target(target.id)
        self.assertEqual(manual_job.trigger, "manual")
        with self.assertRaisesRegex(ValueError, "disabled"):
            service.dispatch_backup_for_target(target.id, trigger="schedule")

    def test_compose_and_ci_defaults(self):
        worker_compose = (ROOT / "deploy/worker/docker-compose.yml").read_text()
        ghcr_compose = (ROOT / "deploy/worker/docker-compose.ghcr.yml").read_text()
        control_plane_compose = (ROOT / "deploy/control-plane/docker-compose.yml").read_text()
        control_plane_ghcr_compose = (ROOT / "deploy/control-plane/docker-compose.ghcr.yml").read_text()
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
        self.assertIn("CONTROL_PLANE_TIMEZONE: ${CONTROL_PLANE_TIMEZONE:-America/Bogota}", control_plane_compose)
        self.assertIn("CONTROL_PLANE_TIMEZONE: ${CONTROL_PLANE_TIMEZONE:-America/Bogota}", control_plane_ghcr_compose)
        self.assertIn("tzdata", (ROOT / "Dockerfile").read_text())
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
