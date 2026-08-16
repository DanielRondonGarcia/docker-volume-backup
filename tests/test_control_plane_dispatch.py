import io
import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.auth import ROLE_OPERATOR, ROLE_VIEWER
from src.control_plane.domain.models import (
    BackupTargetRecord,
    IndexStatusRecord,
    JobStatus,
    SnapshotRecord,
    WorkerRecord,
    utcnow,
)
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
from src.control_plane.main import ControlPlaneRequestHandler
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient


class ControlPlaneDispatchTests(unittest.TestCase):
    def make_service(self, cache_repository=None, index_repository=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a", status="online", last_seen_at=utcnow()))
        workers.save(WorkerRecord(name="worker-b", host_name="test", id="worker-b", status="online", last_seen_at=utcnow()))
        targets = InMemoryTargetRepository()
        targets.save(
            BackupTargetRecord(
                name="target-a",
                worker_id="worker-a",
                id="target-a",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-a"},
            )
        )
        targets.save(
            BackupTargetRecord(
                name="target-b",
                worker_id="worker-b",
                id="target-b",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-b"},
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
            cache_repository=cache_repository,
            index_repository=index_repository,
        )

    def test_read_dispatch_returns_without_waiting_and_preserves_request_contract(self):
        service = self.make_service()
        service._wait_for_job_completion = Mock(side_effect=AssertionError("interactive dispatch must not wait"))

        response = service.dispatch_snapshot_read(
            target_id="target-a",
            operation="browse",
            snapshot_id="abcdef12",
            path="folder with spaces/file.txt",
            request_id="request-1",
            max_entries=25,
        )

        job = service.get_job(response["job_id"])
        self.assertEqual(response["status"], JobStatus.PENDING)
        self.assertEqual(response["source"], "durable")
        self.assertFalse(response["cache_hit"])
        self.assertEqual(job.trigger, "interactive")
        self.assertEqual(job.target_id, "target-a")
        self.assertEqual(job.payload["schema_version"], 1)
        self.assertEqual(job.payload["request_id"], "request-1")
        self.assertEqual(job.payload["path"], "/folder with spaces/file.txt")
        self.assertEqual(job.payload["operation"], "browse")
        self.assertEqual(job.payload["command"][-1], "/folder with spaces/file.txt")
        self.assertEqual(job.payload["cache_generation"], 0)

    def test_snapshot_payload_uses_current_target_cache_generation(self):
        cache = InMemoryCacheRepository()
        service = self.make_service(cache_repository=cache)
        target = service.target_repository.get("target-a")
        fingerprint = service._repository_fingerprint(target)
        cache.bump_generation(target.id, fingerprint)

        response = service.dispatch_snapshot_read("target-a", "browse", "abcdef12")

        job = service.get_job(response["job_id"])
        self.assertEqual(job.payload["cache_generation"], 1)
        self.assertNotIn("cache_generation", response)

    def test_snapshot_job_contract_preserves_worker_cache_source(self):
        service = self.make_service()
        response = service.dispatch_snapshot_read("target-a", "browse", "abcdef12")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            claimed.id,
            JobStatus.SUCCEEDED,
            result_summary={"entries": [], "cache_hit": True, "source": "redis"},
            lease_token=claimed.lease_token,
        )

        contract = service.snapshot_job_contract(response["job_id"])

        self.assertEqual(contract["source"], "redis")
        self.assertTrue(contract["cache_hit"])

    def test_interactive_fetch_prioritizes_explorer_jobs_and_search_is_bounded(self):
        service = self.make_service()
        service.dispatch_job("worker-a", "backup.run", target_id="target-a")
        search = service.dispatch_snapshot_read(
            "target-a", "search", "abcdef12", query="needle", max_entries=1, request_id="search-1"
        )

        jobs = service.fetch_interactive_jobs_for_worker("worker-a")

        self.assertEqual(jobs[0].id, search["job_id"])
        self.assertEqual(jobs[0].command, "snapshot.search")
        self.assertEqual(jobs[0].owner_worker_id, "worker-a")

        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": True,
            "logs": '\n'.join(
                [
                    '{"type":"file","path":"/needle.txt"}',
                    '{"type":"file","path":"/other.txt"}',
                ]
            ),
            "stderr": "",
        }
        worker = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker-a", "host"),
            Mock(),
            runtime,
        )
        result = worker.execute_job(
            {
                "command": "snapshot.search",
                "payload": {"query": "needle", "max_entries": 1},
            }
        )
        self.assertEqual(result.result_summary["entries"], [{"type": "file", "path": "/needle.txt"}])

    def test_cross_target_snapshot_metadata_is_rejected(self):
        service = self.make_service()
        service.snapshot_repository.replace_for_target(
            "target-b",
            [SnapshotRecord("target-b", "worker-b", "abcdef12", utcnow())],
        )

        with self.assertRaisesRegex(ValueError, "another target"):
            service.dispatch_snapshot_read("target-a", "browse", "abcdef12")

    def test_boundary_rejects_invalid_values_without_stripping_them(self):
        service = self.make_service()
        for snapshot_id in (" abcdef12", "abcdef12 ", "not-a-snapshot", "abcdef1"):
            with self.subTest(snapshot_id=snapshot_id), self.assertRaises(ValueError):
                service.dispatch_snapshot_read("target-a", "browse", snapshot_id)
        for path in ("/../secret", r"/safe\file", "C:/host", "//server/share", "/safe\x00path"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                service.dispatch_snapshot_read("target-a", "browse", "abcdef12", path=path)
        for request_id in ("bad id", "/bad", "", "x" * 129):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                service.dispatch_snapshot_read("target-a", "browse", "abcdef12", request_id=request_id)
        with self.assertRaises(ValueError):
            service.dispatch_snapshot_read("target-a", "browse", "abcdef12", max_entries=10_001)

    def test_cancellation_returns_the_same_sanitized_contract(self):
        service = self.make_service()
        response = service.dispatch_snapshot_read("target-a", "browse", "abcdef12", request_id="cancel-1")

        service.cancel_job(response["job_id"])
        canceled = service.snapshot_job_contract(response["job_id"])

        self.assertEqual(canceled["request_id"], "cancel-1")
        self.assertEqual(canceled["status"], JobStatus.CANCELED)
        self.assertEqual(canceled["error"], "job canceled")
        self.assertNotIn("RESTIC_REPOSITORY", canceled)

    def test_worker_cancellation_probe_is_owned_and_status_only(self):
        service = self.make_service()
        response = service.dispatch_snapshot_read("target-a", "browse", "abcdef12")

        self.assertFalse(service.is_job_cancelled("worker-a", response["job_id"]))
        service.cancel_job(response["job_id"])
        self.assertTrue(service.is_job_cancelled("worker-a", response["job_id"]))
        with self.assertRaisesRegex(ValueError, "does not own"):
            service.is_job_cancelled("worker-b", response["job_id"])

    def test_successful_write_bumps_only_target_generation_and_deletes_its_index(self):
        cache = InMemoryCacheRepository()
        index = InMemoryIndexRepository()
        service = self.make_service(cache, index)
        index.upsert_status(IndexStatusRecord("target-a", "abcdef12", status="indexed"))
        index.upsert_status(IndexStatusRecord("target-b", "abcdef12", status="indexed"))
        cache.bump_generation("target-b", service._repository_fingerprint(service.target_repository.get("target-b")))

        job = service.dispatch_backup_for_target("target-a")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)

        target_a_fp = service._repository_fingerprint(service.target_repository.get("target-a"))
        self.assertEqual(cache.get_generation("target-a", target_a_fp).generation, 1)
        self.assertEqual(index.list_by_target("target-a"), [])
        self.assertEqual(len(index.list_by_target("target-b")), 1)

        service.delete_target("target-a")
        self.assertIsNone(cache.get_generation("target-a", target_a_fp))
        self.assertIsNotNone(
            cache.get_generation("target-b", service._repository_fingerprint(service.target_repository.get("target-b")))
        )
        self.assertEqual(len(index.list_by_target("target-b")), 1)

    def test_successful_backup_schedules_current_generation_sync_once(self):
        cache = InMemoryCacheRepository()
        service = self.make_service(cache_repository=cache)

        first_backup = service.dispatch_backup_for_target("target-a")
        first_claim = next(
            job for job in service.fetch_jobs_for_worker("worker-a") if job.id == first_backup.id
        )
        completed = service.update_job_status(
            "worker-a",
            first_backup.id,
            JobStatus.SUCCEEDED,
            lease_token=first_claim.lease_token,
        )

        target = service.target_repository.get("target-a")
        fingerprint = service._repository_fingerprint(target)
        sync_jobs = [
            job
            for job in service.job_repository.list()
            if job.target_id == "target-a" and job.command == "snapshots.list"
        ]
        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(cache.get_generation("target-a", fingerprint).generation, 1)
        self.assertEqual(len(sync_jobs), 1)
        self.assertEqual(sync_jobs[0].status, JobStatus.PENDING)
        self.assertEqual(sync_jobs[0].payload["cache_generation"], 1)

        second_backup = service.dispatch_backup_for_target("target-a")
        second_claim = next(
            job for job in service.fetch_jobs_for_worker("worker-a") if job.id == second_backup.id
        )
        service.update_job_status(
            "worker-a",
            second_backup.id,
            JobStatus.SUCCEEDED,
            lease_token=second_claim.lease_token,
        )

        sync_jobs = [
            job
            for job in service.job_repository.list()
            if job.target_id == "target-a" and job.command == "snapshots.list"
        ]
        self.assertEqual(len(sync_jobs), 1)
        self.assertEqual(sync_jobs[0].status, JobStatus.IN_PROGRESS)

    def test_concurrent_read_and_write_dispatches_keep_target_and_operation_boundaries(self):
        service = self.make_service()
        results = []

        def dispatch_read():
            results.append(service.dispatch_snapshot_read("target-a", "browse", "abcdef12", request_id="read-1"))

        def dispatch_write():
            results.append(service.dispatch_backup_for_target("target-a"))

        threads = [threading.Thread(target=dispatch_read), threading.Thread(target=dispatch_write)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        jobs = [service.get_job(item["job_id"] if isinstance(item, dict) else item.id) for item in results]
        self.assertEqual({job.target_id for job in jobs}, {"target-a"})
        self.assertEqual({job.command for job in jobs}, {"snapshot.ls", "backup.run"})
        read_job = next(job for job in jobs if job.command == "snapshot.ls")
        self.assertEqual(read_job.payload["command"][1], "ls")
        self.assertNotIn("--no-lock", read_job.payload["command"])

    def test_explicit_worker_id_does_not_merge_same_name(self):
        service = self.make_service()

        registered = service.register_worker("worker-a", "new-host", worker_id="worker-new")

        self.assertEqual(registered.id, "worker-new")
        self.assertEqual(len(service.worker_repository.list()), 3)
        self.assertEqual(service.worker_repository.get("worker-a").host_name, "test")

    def test_stale_and_missing_heartbeats_are_offline_and_rejected(self):
        service = self.make_service()
        stale = service.worker_repository.get("worker-a")
        stale.last_seen_at = utcnow() - timedelta(hours=1)
        service.worker_repository.save(stale)

        self.assertEqual(service.list_workers()[0].status, "offline")
        self.assertEqual(service.worker_repository.get("worker-a").status, "online")
        with self.assertRaisesRegex(ValueError, "not eligible"):
            service.dispatch_backup_for_target("target-a")
        with self.assertRaisesRegex(ValueError, "not eligible"):
            service.dispatch_snapshot_sync_for_target("target-a")

        missing = service.worker_repository.get("worker-b")
        missing.last_seen_at = None
        service.worker_repository.save(missing)
        with self.assertRaisesRegex(ValueError, "not eligible"):
            service.register_target("new-target", "worker-b")

    def test_snapshot_sync_persists_catalog_and_deduplicates_pending_job(self):
        service = self.make_service()

        first = service.dispatch_snapshot_sync_for_target("target-a")
        duplicate = service.dispatch_snapshot_sync_for_target("target-a")
        self.assertEqual(first.id, duplicate.id)

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            claimed.id,
            JobStatus.SUCCEEDED,
            result_summary={"snapshots": [{"short_id": "abcdef12", "time": utcnow().isoformat()}]},
            lease_token=claimed.lease_token,
        )

        self.assertEqual([item.snapshot_id for item in service.list_snapshots("target-a")], ["abcdef12"])


class ControlPlaneRouteTests(unittest.TestCase):
    @staticmethod
    def make_handler(path, body):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        raw_body = body.encode("utf-8")
        handler.headers = {"Content-Length": str(len(raw_body))}
        handler.rfile = io.BytesIO(raw_body)
        handler._require_auth = Mock(return_value={"role": ROLE_VIEWER})
        handler._write_json = Mock(return_value=None)
        return handler

    def test_browse_route_requires_viewer_role(self):
        handler = self.make_handler(
            "/api/v2/targets/target-a/browse",
            '{"snapshot_id":"abcdef12","path":"/"}',
        )
        service = Mock()
        service.dispatch_snapshot_read.return_value = {"schema_version": 1, "status": "pending"}
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, api_mode=True)
        service.dispatch_snapshot_read.assert_called_once()
        self.assertEqual(handler._write_json.call_args.args[0], 202)

    def test_cancel_route_requires_operator_role(self):
        handler = self.make_handler("/api/v2/jobs/job-1/cancel", "{}")
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_OPERATOR}
        service = Mock()
        service.cancel_job.return_value = SimpleNamespace(id="job-1")
        service.snapshot_job_contract.return_value = {"schema_version": 1, "status": "canceled"}
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_OPERATOR, api_mode=True)
        service.cancel_job.assert_called_once_with("job-1")
        self.assertEqual(handler._write_json.call_args.args[0], 200)

    def test_worker_cancel_status_route_returns_only_boolean(self):
        handler = self.make_handler("/api/v1/workers/worker-a/jobs/job-1/cancel-status", "{}")
        handler._require_worker_identity = Mock(return_value=True)
        service = Mock()
        service.is_job_cancelled.return_value = True
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_worker_identity.assert_called_once_with("worker-a")
        service.is_job_cancelled.assert_called_once_with("worker-a", "job-1")
        self.assertEqual(handler._write_json.call_args.args, (200, {"canceled": True}))

    def test_worker_lease_renewal_route_keeps_lease_token_private_to_service(self):
        handler = self.make_handler(
            "/api/v1/workers/worker-a/jobs/job-1/renew-lease",
            '{"lease_token":"lease-token"}',
        )
        handler._require_worker_identity = Mock(return_value=True)
        service = Mock()
        service.renew_job_lease.return_value = SimpleNamespace(id="job-1", status=JobStatus.IN_PROGRESS)
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_worker_identity.assert_called_once_with("worker-a")
        service.renew_job_lease.assert_called_once_with(
            worker_id="worker-a",
            job_id="job-1",
            lease_token="lease-token",
        )
        self.assertEqual(handler._write_json.call_args.args[0], 200)


class ControlPlaneClientFallbackTests(unittest.TestCase):
    def test_worker_cancel_status_uses_signed_post_and_returns_boolean(self):
        client = ControlPlaneClient.__new__(ControlPlaneClient)
        client._post = Mock(return_value={"canceled": True})

        result = client.is_job_cancelled("worker-a", "job-1")

        self.assertTrue(result)
        client._post.assert_called_once_with(
            "/api/v1/workers/worker-a/jobs/job-1/cancel-status", {}
        )

    def test_worker_lease_renewal_uses_signed_post(self):
        client = ControlPlaneClient.__new__(ControlPlaneClient)
        client._post = Mock(return_value={"status": JobStatus.IN_PROGRESS})

        result = client.renew_job_lease("worker-a", "job-1", "lease-token")

        self.assertEqual(result["status"], JobStatus.IN_PROGRESS)
        client._post.assert_called_once_with(
            "/api/v1/workers/worker-a/jobs/job-1/renew-lease",
            {"lease_token": "lease-token"},
        )

    def test_interactive_fetch_falls_back_only_for_missing_optional_endpoint(self):
        client = ControlPlaneClient.__new__(ControlPlaneClient)
        client._post = Mock(
            side_effect=[
                HTTPError("http://control-plane", 404, "missing", {}, io.BytesIO()),
                {"items": [{"id": "durable-1"}]},
            ]
        )

        result = client.fetch_interactive_jobs("worker-a")

        self.assertEqual(result, [{"id": "durable-1"}])
        self.assertEqual(client._post.call_args_list[0].args[0], "/api/v1/workers/worker-a/jobs/fetch-interactive")
        self.assertEqual(client._post.call_args_list[1].args[0], "/api/v1/workers/worker-a/jobs/fetch")


if __name__ == "__main__":
    unittest.main()
