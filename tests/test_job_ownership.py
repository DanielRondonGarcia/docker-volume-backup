import tempfile
import threading
import unittest
from datetime import timedelta

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import JobRecord, JobStatus, WorkerRecord, utcnow
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


class JobOwnershipTests(unittest.TestCase):
    def make_service(self, job_repository=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a", last_seen_at=utcnow()))
        workers.save(WorkerRecord(name="worker-b", host_name="test", id="worker-b", last_seen_at=utcnow()))
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

    def test_owner_claim_and_completion(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        self.assertEqual(claimed.owner_worker_id, "worker-a")
        self.assertEqual(claimed.attempt_count, 1)
        self.assertTrue(claimed.lease_token)
        completed = service.update_job_status(
            worker_id="worker-a",
            job_id=job.id,
            status=JobStatus.SUCCEEDED,
            lease_token=claimed.lease_token,
        )

        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(service.job_repository.get(job.id).status, JobStatus.SUCCEEDED)

    def test_foreign_worker_cannot_complete(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        with self.assertRaisesRegex(ValueError, "does not own"):
            service.update_job_status(
                worker_id="worker-b",
                job_id=job.id,
                status=JobStatus.SUCCEEDED,
                lease_token=claimed.lease_token,
            )
        self.assertEqual(service.job_repository.get(job.id).status, JobStatus.IN_PROGRESS)

    def test_wrong_and_expired_lease_cannot_complete(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token="wrong")
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        service.job_repository.save(claimed)
        with self.assertRaisesRegex(ValueError, "expired"):
            service.update_job_status(
                "worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token
            )
        self.assertEqual(service.job_repository.get(job.id).status, JobStatus.IN_PROGRESS)

    def test_lease_renewal_extends_only_the_current_owner_lease(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        original_expiry = claimed.lease_expires_at

        renewed = service.renew_job_lease("worker-a", job.id, claimed.lease_token)

        self.assertGreater(renewed.lease_expires_at, original_expiry)
        self.assertIsNotNone(service.worker_repository.get("worker-a").last_seen_at)
        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            service.renew_job_lease("worker-a", job.id, "wrong-token")

        renewed.lease_expires_at = utcnow() - timedelta(seconds=1)
        service.job_repository.save(renewed)
        with self.assertRaisesRegex(ValueError, "expired"):
            service.renew_job_lease("worker-a", job.id, claimed.lease_token)

        with tempfile.TemporaryDirectory() as directory:
            sqlite_service = self.make_service(SQLiteJobRepository(f"{directory}/control-plane.db"))
            sqlite_job = sqlite_service.dispatch_job("worker-a", "worker.self_check")
            sqlite_claimed = sqlite_service.fetch_jobs_for_worker("worker-a")[0]
            sqlite_renewed = sqlite_service.renew_job_lease("worker-a", sqlite_job.id, sqlite_claimed.lease_token)
            self.assertGreater(sqlite_renewed.lease_expires_at, sqlite_claimed.lease_expires_at)

    def test_read_reconciles_expired_lease_without_worker_fetch(self):
        service = self.make_service()
        interrupted = JobRecord(
            worker_id="worker-a",
            command="restore.run",
            status=JobStatus.IN_PROGRESS,
            owner_worker_id="worker-a",
            lease_token="lease-token",
            lease_issued_at=utcnow() - timedelta(minutes=6),
            lease_expires_at=utcnow() - timedelta(minutes=1),
        )
        service.job_repository.save(interrupted)

        observed = service.get_job(interrupted.id)

        self.assertEqual(observed.status, JobStatus.FAILED)
        self.assertEqual(observed.result_summary["recovery"], "worker_interrupted")
        self.assertIsNone(observed.owner_worker_id)
        self.assertIsNone(observed.lease_token)
        self.assertEqual(observed.log_lines.count("Worker lease expired before terminal status was reported."), 1)
        self.assertEqual(service.get_job(interrupted.id).status, JobStatus.FAILED)

    def test_duplicate_and_canceled_completion_are_rejected(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)
        with self.assertRaisesRegex(ValueError, "not in progress"):
            service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)

        canceled = service.dispatch_job("worker-a", "worker.self_check")
        canceled_claim = service.fetch_jobs_for_worker("worker-a")[0]
        service.cancel_job(canceled.id)
        with self.assertRaisesRegex(ValueError, "canceled"):
            service.update_job_status(
                "worker-a", canceled.id, JobStatus.SUCCEEDED, lease_token=canceled_claim.lease_token
            )
        self.assertEqual(service.job_repository.get(canceled.id).status, JobStatus.CANCELED)

    def test_cancel_fills_empty_terminal_summary_and_logs(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "restore.run")
        service.fetch_jobs_for_worker("worker-a")

        canceled = service.cancel_job(job.id)

        self.assertEqual(canceled.status, JobStatus.CANCELED)
        self.assertEqual(canceled.result_summary["recovery"], "operator_canceled")
        self.assertEqual(
            canceled.log_lines,
            ["Job canceled by operator before terminal worker completion."],
        )

    def test_in_memory_claim_does_not_double_assign(self):
        repository = InMemoryJobRepository()
        repository.save(JobRecord(worker_id="worker-a", command="worker.self_check"))
        results = []

        def claim():
            results.append(repository.claim_pending_for_worker("worker-a"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(len(items) for items in results), 1)

    def test_sqlite_claim_does_not_double_assign(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/control-plane.db"
            first = SQLiteJobRepository(path)
            second = SQLiteJobRepository(path)
            first.save(JobRecord(worker_id="worker-a", command="worker.self_check"))
            results = []

            def claim(repository):
                results.append(repository.claim_pending_for_worker("worker-a"))

            threads = [threading.Thread(target=claim, args=(repository,)) for repository in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(len(items) for items in results), 1)


if __name__ == "__main__":
    unittest.main()
