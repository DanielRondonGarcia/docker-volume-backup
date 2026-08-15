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
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a"))
        workers.save(WorkerRecord(name="worker-b", host_name="test", id="worker-b"))
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
