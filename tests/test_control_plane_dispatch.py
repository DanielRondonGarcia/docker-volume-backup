import io
import json
import os
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from cryptography.fernet import Fernet

from src.control_plane.application.services.control_plane_service import ControlPlaneService, WorkerDeletionConflict
from src.control_plane.auth import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER
from src.control_plane.domain.models import (
    BackupTargetRecord,
    JobRecord,
    IndexStatusRecord,
    JobStatus,
    SecretRecord,
    SettingsRecord,
    SnapshotRecord,
    StorageProfileRecord,
    WorkerRecord,
    WorkerStatus,
    utcnow,
)
from src.control_plane.infrastructure.security.secret_codec import SecretCodec
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
from src.control_plane.main import ControlPlaneRequestHandler, _to_jsonable
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
        self.assertEqual(job.payload["command"][:3], ["restic", "ls", "--json"])
        self.assertEqual(job.payload["command"][-2:], ["abcdef12", "/folder with spaces/file.txt"])
        self.assertEqual(job.payload["cache_generation"], 0)

    def test_snapshot_about_dispatch_uses_scoped_restore_size_stats_and_safe_contract(self):
        service = self.make_service()
        created_at = utcnow()
        service.snapshot_repository.replace_for_target(
            "target-a",
            [
                SnapshotRecord(
                    "target-a",
                    "worker-a",
                    "abcdef12",
                    created_at,
                    hostname="backup-host",
                    paths=["/data"],
                    tags=["nightly"],
                )
            ],
        )

        response = service.dispatch_snapshot_about("target-a", "abcdef12", request_id="about-1")
        job = service.get_job(response["job_id"])

        self.assertEqual(response["status"], JobStatus.PENDING)
        self.assertEqual(response["request_id"], "about-1")
        self.assertEqual(job.command, "snapshot.about")
        self.assertEqual(
            job.payload["command"],
            ["restic", "stats", "--mode", "restore-size", "--json", "abcdef12"],
        )
        self.assertEqual(job.payload["snapshot_id"], "abcdef12")
        self.assertEqual(job.payload["cache_generation"], 0)

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            claimed.id,
            JobStatus.SUCCEEDED,
            result_summary={
                "snapshot_id": "abcdef12",
                "stats": {
                    "total_size": 2048,
                    "total_file_count": 3,
                    "snapshots_count": 1,
                    "repository": "local:/must-not-leak",
                },
                "cache_hit": True,
                "source": "redis",
            },
            lease_token=claimed.lease_token,
        )

        contract = service.snapshot_job_contract(job.id)
        self.assertEqual(contract["snapshot_id"], "abcdef12")
        self.assertEqual(contract["created_at"], created_at.isoformat())
        self.assertEqual(contract["hostname"], "backup-host")
        self.assertEqual(contract["paths"], ["/data"])
        self.assertEqual(contract["tags"], ["nightly"])
        self.assertEqual(
            contract["stats"],
            {"total_size": 2048, "total_file_count": 3, "snapshots_count": 1},
        )
        self.assertTrue(contract["cache_hit"])
        self.assertEqual(contract["source"], "redis")
        self.assertNotIn("repository", contract["stats"])

    def test_target_stats_dispatch_persists_both_modes_and_preserves_legacy_raw_stats(self):
        service = self.make_service()

        response = service.dispatch_stats_for_target("target-a", requested_by="operator")
        job = service.get_job(response.id)
        self.assertEqual(job.command, "stats.get")
        self.assertEqual(job.payload["command"], "restic stats --mode raw-data --json")
        self.assertEqual(job.payload["stats_modes"], ["raw-data", "blobs-per-file"])

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        raw_stats = {"total_size": 4096, "unique_size": 3072, "snapshots_count": 2}
        blobs_stats = {"total_size": 2048, "total_file_count": 7, "total_blob_count": 9}
        service.update_job_status(
            "worker-a",
            claimed.id,
            JobStatus.SUCCEEDED,
            result_summary={
                "stats": raw_stats,
                "stats_by_mode": {"raw-data": raw_stats, "blobs-per-file": blobs_stats},
            },
            lease_token=claimed.lease_token,
        )

        record = service.get_target_stats("target-a")
        self.assertIsNotNone(record)
        self.assertEqual(record.stats["total_size"], 4096)
        self.assertEqual(record.stats["modes"], {"raw-data": raw_stats, "blobs-per-file": blobs_stats})

    def test_browse_uses_direct_restic_ls_but_search_and_find_keep_restic_ls(self):
        service = self.make_service()

        root = service.dispatch_snapshot_read("target-a", "browse", "abcdef12", path="/")
        root_job = service.get_job(root["job_id"])
        nested = service.dispatch_snapshot_read("target-a", "browse", "abcdef12", path="/packages")
        nested_job = service.get_job(nested["job_id"])
        search = service.dispatch_snapshot_read("target-a", "search", "abcdef12", path="/packages", query="needle")
        search_job = service.get_job(search["job_id"])
        find = service.dispatch_snapshot_read("target-a", "find", "abcdef12", path="/packages", query="needle")
        find_job = service.get_job(find["job_id"])

        self.assertEqual(root_job.payload["command"], ["restic", "ls", "--json", "abcdef12", "/"])
        self.assertEqual(nested_job.payload["command"], ["restic", "ls", "--json", "abcdef12", "/packages"])
        self.assertEqual(search_job.payload["command"], ["restic", "ls", "--json", "abcdef12", "/packages"])
        self.assertEqual(find_job.payload["command"], ["restic", "ls", "--json", "abcdef12", "/packages"])

    def test_job_trigger_is_preserved_in_api_payload(self):
        service = self.make_service()

        for trigger in ("manual", "schedule", "automatic", "interactive", "future-trigger"):
            job = service.dispatch_job("worker-a", "worker.self_check", trigger=trigger)
            self.assertEqual(_to_jsonable(job)["trigger"], trigger)

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

    def test_snapshot_listing_setting_validates_bounds_and_preserves_partial_updates(self):
        service = self.make_service()

        default = service.update_settings()
        self.assertEqual(
            default.snapshot_explorer_listing_max_output_bytes,
            SettingsRecord.DEFAULT_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES,
        )
        updated = service.update_settings(
            restic_repository_base="backup",
            snapshot_explorer_listing_max_output_bytes=8 * 1024 * 1024,
        )
        self.assertEqual(updated.snapshot_explorer_listing_max_output_bytes, 8 * 1024 * 1024)

        for invalid in (
            SettingsRecord.MIN_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES - 1,
            SettingsRecord.MAX_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES + 1,
            True,
            8.0,
            "8388608",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.update_settings(snapshot_explorer_listing_max_output_bytes=invalid)

        preserved = service.update_settings(global_cron_expression="0 3 * * *")
        self.assertEqual(preserved.restic_repository_base, "backup")
        self.assertEqual(preserved.snapshot_explorer_listing_max_output_bytes, 8 * 1024 * 1024)

    def test_snapshot_metadata_payloads_use_listing_limit_but_dump_keeps_download_limit(self):
        service = self.make_service()
        service.update_settings(snapshot_explorer_listing_max_output_bytes=8 * 1024 * 1024)

        catalog_job = service.dispatch_snapshot_sync_for_target("target-a")
        metadata_jobs = [catalog_job]
        for operation in ("browse", "search", "find"):
            response = service.dispatch_snapshot_read(
                "target-a",
                operation,
                "abcdef12",
                query="needle" if operation in {"search", "find"} else None,
            )
            metadata_jobs.append(service.get_job(response["job_id"]))

        dump_response = service.dispatch_snapshot_read(
            "target-a",
            "dump",
            "abcdef12",
            max_output_bytes=1234,
        )
        dump_job = service.get_job(dump_response["job_id"])

        self.assertTrue(all(job.payload["max_log_bytes"] == 8 * 1024 * 1024 for job in metadata_jobs))
        self.assertNotIn("max_log_bytes", dump_job.payload)
        self.assertEqual(dump_job.payload["max_output_bytes"], 1234)

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

    def test_snapshot_contract_forwards_listing_diagnostics_and_rejects_success_with_error(self):
        service = self.make_service()
        response = service.dispatch_snapshot_read("target-a", "browse", "abcdef12")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            claimed.id,
            JobStatus.SUCCEEDED,
            result_summary={
                "entries": [],
                "status_code": 413,
                "listing_mode": "direct",
                "listing_complete": False,
                "listing_entry_count": 0,
                "listing_output_limit_bytes": 8 * 1024 * 1024,
                "listing_error_code": "output_limit",
                "error": "runtime logs exceeded the permitted limit",
            },
            lease_token=claimed.lease_token,
        )

        contract = service.snapshot_job_contract(response["job_id"])

        self.assertEqual(contract["status"], JobStatus.FAILED)
        self.assertEqual(contract["status_code"], 413)
        self.assertEqual(contract["listing_mode"], "direct")
        self.assertFalse(contract["listing_complete"])
        self.assertEqual(contract["listing_error_code"], "output_limit")
        self.assertTrue(contract["error"])

    def test_snapshot_contract_allows_authentication_listing_error_code(self):
        service = self.make_service()
        response = service.dispatch_snapshot_read("target-a", "browse", "abcdef12")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            claimed.id,
            JobStatus.FAILED,
            result_summary={
                "entries": [],
                "listing_mode": "direct",
                "listing_complete": False,
                "listing_error_code": "authentication",
                "error": "Restic repository could not be unlocked.",
            },
            lease_token=claimed.lease_token,
        )

        contract = service.snapshot_job_contract(response["job_id"])

        self.assertEqual(contract["listing_error_code"], "authentication")
        self.assertEqual(contract["status"], JobStatus.FAILED)

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

    def test_duplicate_snapshot_short_id_is_scoped_to_requested_target(self):
        service = self.make_service()
        service.target_repository.save(
            BackupTargetRecord(
                name="target-c",
                worker_id="worker-a",
                id="target-c",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-c"},
            )
        )
        service.snapshot_repository.replace_for_target(
            "target-a",
            [SnapshotRecord("target-a", "worker-a", "abcdef12", utcnow())],
        )
        service.snapshot_repository.replace_for_target(
            "target-b",
            [SnapshotRecord("target-b", "worker-b", "abcdef12", utcnow())],
        )

        about = service.dispatch_snapshot_about("target-a", "abcdef12")
        read = service.dispatch_snapshot_read("target-a", "browse", "abcdef12")

        self.assertEqual(service.get_job(about["job_id"]).target_id, "target-a")
        self.assertEqual(service.get_job(read["job_id"]).target_id, "target-a")
        with self.assertRaisesRegex(ValueError, "another target"):
            service.dispatch_snapshot_about("target-c", "abcdef12")

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
        self.assertEqual(read_job.payload["command"][1:3], ["ls", "--json"])
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

    def test_kubernetes_target_requires_capable_worker_namespace_and_explicit_pvcs(self):
        service = self.make_service()
        worker = service.worker_repository.get("worker-a")
        worker.labels = {"runtime_type": "kubernetes"}
        service.worker_repository.save(worker)
        service.sync_inventory(
            "worker-a",
            {
                "runtime": "kubernetes",
                "namespaces": [{"name": "backups", "pvc_names": ["discourse-data"]}],
            },
        )

        target = service.register_target(
            "discourse-kubernetes",
            "worker-a",
            runtime_type="kubernetes",
            namespace="backups",
            pvc_names=["discourse-data"],
        )

        self.assertEqual(target.runtime_type, "kubernetes")
        self.assertEqual(target.namespace, "backups")
        self.assertEqual(target.pvc_names, ["discourse-data"])
        self.assertEqual(target.volume_targets, [])
        job = service.dispatch_backup_for_target(target.id)
        self.assertEqual(job.payload["runtime_type"], "kubernetes")
        self.assertEqual(job.payload["namespace"], "backups")
        self.assertEqual(job.payload["pvc_names"], ["discourse-data"])

        for kwargs, message in (
            ({"namespace": "backups", "pvc_names": []}, "one or more explicit PVC"),
            ({"namespace": ["backups"], "pvc_names": ["discourse-data"]}, "exactly one valid namespace"),
            ({"namespace": "backups", "pvc_names": ["missing"]}, "not present in worker inventory"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                service.register_target(
                    "invalid-kubernetes",
                    "worker-a",
                    runtime_type="kubernetes",
                    **kwargs,
                )

        worker.labels = {}
        service.worker_repository.save(worker)
        with self.assertRaisesRegex(ValueError, "does not advertise Kubernetes capability"):
            service.register_target(
                "uncapable-kubernetes",
                "worker-a",
                runtime_type="kubernetes",
                namespace="backups",
                pvc_names=["discourse-data"],
            )

    def test_docker_target_keeps_legacy_runtime_defaults(self):
        service = self.make_service()

        target = service.register_target("legacy-docker", "worker-a")

        self.assertEqual(target.runtime_type, "docker")
        self.assertIsNone(target.namespace)
        self.assertEqual(target.pvc_names, [])
        job = service.dispatch_backup_for_target(target.id)
        self.assertEqual(job.payload["runtime_type"], "docker")
        self.assertIsNone(job.payload["namespace"])
        self.assertEqual(job.payload["pvc_names"], [])

    def test_kubernetes_target_rejects_bearer_tokens_and_kubeconfigs(self):
        service = self.make_service()
        worker = service.worker_repository.get("worker-a")
        worker.labels = {"runtime": "kubernetes"}
        service.worker_repository.save(worker)
        service.sync_inventory(
            "worker-a",
            {
                "runtime": "kubernetes",
                "namespaces": [{"name": "backups", "pvcs": ["discourse-data"]}],
            },
        )

        with self.assertRaisesRegex(ValueError, "bearer tokens and kubeconfigs"):
            service.register_target(
                "credentialed-kubernetes",
                "worker-a",
                runtime_type="kubernetes",
                namespace="backups",
                pvc_names=["discourse-data"],
                runtime_environment={"KUBERNETES_BEARER_TOKEN": "do-not-store"},
            )

        with self.assertRaisesRegex(ValueError, "bearer tokens and kubeconfigs"):
            service.sync_inventory(
                "worker-a",
                {"runtime": "kubernetes", "kubeconfig": "do-not-store"},
            )

    def test_explicit_volume_sources_select_one_shared_bind_and_path_only_keeps_legacy_sources(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_targets": ["/bitnami/discourse"],
                        "runtime_volumes": {
                            "discourse_data": {"bind": "/bitnami/discourse", "mode": "rw"},
                            "discourse_sidekiq_data": {"bind": "/bitnami/discourse", "mode": "rw"},
                        },
                        "volume_candidates": [
                            {
                                "source": "discourse_data",
                                "name": "discourse_data",
                                "bind": "/bitnami/discourse",
                                "mode": "rw",
                                "mount_type": "volume",
                                "anonymous": False,
                                "services": ["web"],
                                "containers": ["discourse-web-1"],
                            },
                            {
                                "source": "discourse_sidekiq_data",
                                "name": "discourse_sidekiq_data",
                                "bind": "/bitnami/discourse",
                                "mode": "rw",
                                "mount_type": "volume",
                                "anonymous": False,
                                "services": ["sidekiq"],
                                "containers": ["discourse-sidekiq-1"],
                            },
                        ],
                    },
                ],
            },
        )

        selected = service.register_target(
            "discourse-data-only",
            "worker-a",
            compose_project="discourse",
            volume_targets=["/some/stale/path"],
            volume_sources=["discourse_data"],
            runtime_volumes={"unexpected": {"bind": "/unexpected", "mode": "rw"}},
        )
        self.assertEqual(selected.volume_targets, ["/bitnami/discourse"])
        self.assertEqual(
            selected.runtime_volumes,
            {
                "discourse_data": {
                    "bind": "/bitnami/discourse",
                    "mode": "rw",
                    "name": "discourse_data",
                    "stable_key": "compose:discourse:discourse_data",
                }
            },
        )

        explicitly_empty = service.register_target(
            "discourse-no-sources",
            "worker-a",
            compose_project="discourse",
            volume_sources=[],
        )
        self.assertEqual(explicitly_empty.volume_targets, [])
        self.assertEqual(explicitly_empty.runtime_volumes, {})

        legacy = service.register_target(
            "discourse-path-only",
            "worker-a",
            compose_project="discourse",
            volume_targets=["/bitnami/discourse"],
        )
        self.assertEqual(
            set(legacy.runtime_volumes),
            {"discourse_data", "discourse_sidekiq_data"},
        )

    def test_inventory_volume_metadata_preserves_stable_identity_and_rejects_unsafe_identities(self):
        service = self.make_service()
        generated = "a" * 32
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_targets": ["/safe", "/host/data", "/anonymous"],
                        "volume_candidates": [
                            {
                                "source": "safe_source",
                                "name": "friendly-data",
                                "compose_volume": "declared_data",
                                "stable_key": "compose:discourse:preserved_data",
                                "bind": "/safe",
                                "mode": "rw",
                                "mount_type": "volume",
                                "anonymous": False,
                            },
                            {
                                "source": "/host/data",
                                "name": "/host/data",
                                "bind": "/host/data",
                                "mode": "ro",
                                "mount_type": "bind",
                            },
                            {
                                "source": generated,
                                "name": generated,
                                "compose_volume": "anonymous_data",
                                "stable_key": "compose:discourse:anonymous_data",
                                "bind": "/anonymous",
                                "mode": "rw",
                                "mount_type": "volume",
                                "anonymous": True,
                            },
                        ],
                    },
                ],
            },
        )

        target = service.register_target(
            "discourse-ownership",
            "worker-a",
            compose_project="discourse",
            volume_sources=["safe_source", "/host/data", generated],
        )

        self.assertEqual(
            target.runtime_volumes,
            {
                "safe_source": {
                    "bind": "/safe",
                    "mode": "rw",
                    "name": "friendly-data",
                    "compose_volume": "declared_data",
                    "stable_key": "compose:discourse:preserved_data",
                },
                "/host/data": {"bind": "/host/data", "mode": "ro", "name": "/host/data"},
                generated: {
                    "bind": "/anonymous",
                    "mode": "rw",
                    "name": generated,
                    "compose_volume": "anonymous_data",
                },
            },
        )

    def test_runtime_volume_normalization_preserves_metadata_without_inventing_bind_identities(self):
        service = self.make_service()
        target = BackupTargetRecord(name="target", worker_id="worker-a", compose_project="discourse")

        normalized = service._normalize_runtime_volumes(
            {
                "named": {
                    "bind": "/var/lib/app",
                    "mode": "rw",
                    "name": "app_data",
                    "compose_volume": "app_data",
                    "mount_type": "volume",
                },
                "provided": {
                    "bind": "/var/lib/provided",
                    "mode": "ro",
                    "stable_key": "compose:discourse:provided_data",
                },
                "host": {
                    "bind": "/host/data",
                    "mode": "ro",
                    "name": "/host/data",
                    "mount_type": "bind",
                },
                "backup-path": {"bind": "/backup/sanitized_name", "mode": "ro"},
            },
            target,
        )

        self.assertEqual(normalized["named"]["bind"], "/backup/var_lib_app")
        self.assertEqual(normalized["named"]["mode"], "rw")
        self.assertEqual(normalized["named"]["stable_key"], "compose:discourse:app_data")
        self.assertEqual(normalized["provided"]["stable_key"], "compose:discourse:provided_data")
        self.assertNotIn("stable_key", normalized["host"])
        self.assertNotIn("stable_key", normalized["backup-path"])
        self.assertEqual(normalized["backup-path"]["bind"], "/backup/sanitized_name")

    def test_restore_payload_emits_normalized_read_only_paths_without_volume_sources(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.runtime_volumes = {
            "secret-mount": {"bind": "/backup/run_secrets/automation_worker_secret", "mode": "ro"},
            "data-mount": {"bind": "/backup/data", "mode": "rw"},
        }

        payload = service._build_restore_payload(target, None, "/backup", True, False, None, None, "auto")

        self.assertEqual(
            payload["volumes"],
            {
                "secret-mount": {"bind": "/backup/run_secrets/automation_worker_secret", "mode": "ro"},
                "data-mount": {"bind": "/backup/data", "mode": "rw"},
            },
        )
        self.assertEqual(
            json.loads(payload["environment"]["RESTORE_READ_ONLY_PATHS"]),
            ["/backup/run_secrets/automation_worker_secret"],
        )
        self.assertNotIn("secret-mount", payload["environment"]["RESTORE_READ_ONLY_PATHS"])

    def test_target_jsonable_enriches_legacy_runtime_volumes_by_source_then_bind_without_persisting(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_candidates": [
                            {
                                "source": "legacy-source",
                                "name": "source-data",
                                "compose_volume": "source_data",
                                "bind": "/inventory/source",
                                "mode": "rw",
                                "mount_type": "volume",
                            },
                            {
                                "source": "inventory-bind-source",
                                "name": "bind-data",
                                "compose_volume": "bind_data",
                                "bind": "/restore/bind",
                                "mode": "rw",
                                "mount_type": "volume",
                            },
                            {
                                "source": "not-selected",
                                "name": "unselected-data",
                                "compose_volume": "unselected_data",
                                "bind": "/not-selected",
                                "mode": "rw",
                                "mount_type": "volume",
                            },
                        ],
                    },
                ],
            },
        )
        legacy_volumes = {
            "legacy-source": {"bind": "/restore/source", "mode": "ro"},
            "legacy-bind-key": {"bind": "/restore/bind", "mode": "ro"},
        }
        target = BackupTargetRecord(
            name="legacy-target",
            worker_id="worker-a",
            compose_project="discourse",
            volume_targets=["/restore/source", "/restore/bind"],
            runtime_volumes=legacy_volumes,
            id="legacy-target",
        )
        service.target_repository.save(target)

        handler = object.__new__(ControlPlaneRequestHandler)
        handler._control_plane_service = Mock(return_value=service)
        handler._scheduler = Mock(return_value=None)

        payload = handler._target_jsonable(target)

        self.assertEqual(
            payload["runtime_volumes"],
            {
                "legacy-source": {
                    "bind": "/restore/source",
                    "mode": "ro",
                    "name": "source-data",
                    "compose_volume": "source_data",
                    "stable_key": "compose:discourse:source_data",
                },
                "legacy-bind-key": {
                    "bind": "/restore/bind",
                    "mode": "ro",
                    "name": "bind-data",
                    "compose_volume": "bind_data",
                    "stable_key": "compose:discourse:bind_data",
                },
            },
        )
        self.assertEqual(target.runtime_volumes, legacy_volumes)
        self.assertNotIn("not-selected", payload["runtime_volumes"])

    def test_target_jsonable_keeps_legacy_fallback_when_inventory_is_unsafe_or_unavailable(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_candidates": [
                            {
                                "source": "host-source",
                                "name": "/host/data",
                                "compose_volume": "host_data",
                                "stable_key": "compose:discourse:host_data",
                                "bind": "/restore/data",
                                "mode": "rw",
                                "mount_type": "bind",
                            },
                            {
                                "source": "anonymous-source",
                                "name": "a" * 32,
                                "compose_volume": "anonymous_data",
                                "stable_key": "compose:discourse:anonymous_data",
                                "bind": "/restore/anonymous",
                                "mode": "rw",
                                "mount_type": "volume",
                                "anonymous": True,
                            },
                        ],
                    },
                ],
            },
        )
        legacy = BackupTargetRecord(
            name="unsafe-legacy-target",
            worker_id="worker-a",
            compose_project="discourse",
            volume_targets=["/restore/data", "/restore/anonymous"],
            runtime_volumes={
                "host-source": {"bind": "/restore/data", "mode": "ro"},
                "anonymous-source": {"bind": "/restore/anonymous", "mode": "rw"},
            },
            id="unsafe-legacy-target",
        )
        service.target_repository.save(legacy)
        handler = object.__new__(ControlPlaneRequestHandler)
        handler._control_plane_service = Mock(return_value=service)
        handler._scheduler = Mock(return_value=None)

        unsafe_payload = handler._target_jsonable(legacy)
        self.assertNotIn("stable_key", unsafe_payload["runtime_volumes"]["host-source"])
        self.assertNotIn("stable_key", unsafe_payload["runtime_volumes"]["anonymous-source"])
        self.assertEqual(unsafe_payload["runtime_volumes"]["host-source"]["bind"], "/restore/data")

        service.inventory_repository.get_by_worker = Mock(side_effect=RuntimeError("inventory unavailable"))
        unavailable_payload = handler._target_jsonable(legacy)
        self.assertEqual(unavailable_payload["runtime_volumes"], legacy.runtime_volumes)

    def test_explicit_volume_sources_reject_unknown_or_stale_sources(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_candidates": [
                            {
                                "source": "discourse_data",
                                "bind": "/bitnami/discourse",
                                "mode": "rw",
                            },
                        ],
                        "runtime_volumes": {
                            "discourse_data": {"bind": "/bitnami/discourse", "mode": "rw"},
                        },
                    },
                ],
            },
        )

        with self.assertRaisesRegex(ValueError, "unknown or stale"):
            service.register_target(
                "stale-source",
                "worker-a",
                compose_project="discourse",
                volume_sources=["discourse_data", "removed_volume"],
            )

    def test_update_target_resolves_selected_volume_sources_and_persists_metadata(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_candidates": [
                            {
                                "source": "source-a",
                                "name": "friendly-a",
                                "bind": "/data/a",
                                "mode": "ro",
                                "mount_type": "volume",
                                "anonymous": False,
                            },
                            {
                                "source": "source-b",
                                "name": "friendly-b",
                                "compose_volume": "declared_b",
                                "bind": "/data/b",
                                "mode": "rw",
                                "mount_type": "volume",
                                "anonymous": False,
                                "services": ["web"],
                                "containers": ["discourse-web-1"],
                            },
                        ],
                    },
                ],
            },
        )

        updated = service.update_target(
            "target-a",
            compose_project="discourse",
            volume_sources=["source-b"],
        )

        self.assertEqual(updated.volume_targets, ["/data/b"])
        self.assertEqual(
            updated.runtime_volumes,
            {
                "source-b": {
                    "bind": "/data/b",
                    "mode": "rw",
                    "name": "friendly-b",
                    "compose_volume": "declared_b",
                    "stable_key": "compose:discourse:declared_b",
                },
            },
        )

        cleared = service.update_target("target-a", volume_sources=[])
        self.assertEqual(cleared.volume_targets, [])
        self.assertEqual(cleared.runtime_volumes, {})

    def test_update_target_rejects_unknown_volume_sources_without_mutating_target(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_candidates": [
                            {"source": "known", "bind": "/data", "mode": "rw", "mount_type": "volume"},
                        ],
                    },
                ],
            },
        )
        target = service.target_repository.get("target-a")
        before = (target.compose_project, list(target.volume_targets), dict(target.runtime_volumes))

        with self.assertRaisesRegex(ValueError, "unknown or stale"):
            service.update_target(
                "target-a",
                compose_project="discourse",
                volume_sources=["removed"],
            )

        target = service.target_repository.get("target-a")
        self.assertEqual((target.compose_project, target.volume_targets, target.runtime_volumes), before)

    def test_update_target_rejects_protected_inventory_mounts_as_volume_sources(self):
        service = self.make_service()
        service.sync_inventory(
            "worker-a",
            {
                "compose_project_details": [
                    {
                        "name": "discourse",
                        "volume_candidates": [
                            {"source": "secret", "bind": "/run/secrets/app", "mode": "ro", "mount_type": "bind"},
                            {"source": "socket", "bind": "/var/run/docker.sock", "mode": "rw", "mount_type": "bind"},
                        ],
                    },
                ],
            },
        )

        for source in ("secret", "socket"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "unknown or stale"):
                service.update_target(
                    "target-a",
                    compose_project="discourse",
                    volume_sources=[source],
                )

    def test_update_target_omitting_volume_sources_preserves_legacy_path_behavior(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.compose_project = "discourse"
        target.volume_targets = ["/keep"]
        target.runtime_volumes = {
            "keep": {"bind": "/keep", "mode": "rw"},
            "drop": {"bind": "/drop", "mode": "ro"},
        }
        service.target_repository.save(target)

        updated = service.update_target("target-a", name="legacy-renamed")

        self.assertEqual(updated.name, "legacy-renamed")
        self.assertEqual(updated.volume_targets, ["/keep"])
        self.assertEqual(updated.runtime_volumes, target.runtime_volumes)

    def test_disabled_targets_allow_manual_backups_but_reject_scheduled_dispatch(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.enabled = False
        service.target_repository.save(target)

        manual_target_job = service.dispatch_backup_for_target("target-a")
        manual_direct_job = service.dispatch_job("worker-a", "backup.run", target_id="target-a")
        self.assertEqual(manual_target_job.trigger, "manual")
        self.assertEqual(manual_direct_job.trigger, "manual")

        with self.assertRaisesRegex(ValueError, "disabled"):
            service.dispatch_backup_for_target("target-a", trigger="schedule")
        with self.assertRaisesRegex(ValueError, "disabled"):
            service.dispatch_job("worker-a", "backup.run", target_id="target-a", trigger="scheduled")

    def test_manual_backup_mode_override_changes_payload_only(self):
        service = self.make_service()

        cold_job = service.dispatch_backup_for_target("target-a", backup_mode="cold")
        self.assertEqual(cold_job.payload["backup_mode"], "cold")
        self.assertEqual(cold_job.payload["environment"]["BACKUP_STOP_CONTAINERS"], "true")
        self.assertEqual(service.target_repository.get("target-a").backup_mode, "hot")

        hot_job = service.dispatch_backup_for_target("target-a", backup_mode="hot")
        self.assertEqual(hot_job.payload["backup_mode"], "hot")
        self.assertEqual(hot_job.payload["environment"]["BACKUP_STOP_CONTAINERS"], "false")
        with self.assertRaisesRegex(ValueError, "exactly 'hot' or 'cold'"):
            service.dispatch_backup_for_target("target-a", backup_mode="warm")
        with self.assertRaisesRegex(ValueError, "exactly 'hot' or 'cold'"):
            service.register_target("invalid-mode", "worker-a", backup_mode="warm")
        with self.assertRaisesRegex(ValueError, "exactly 'hot' or 'cold'"):
            service.update_target("target-a", backup_mode="warm")

    def test_restore_payload_selects_bounded_operation_timeout_without_affecting_backup(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")

        default_payload = service._build_restore_payload(target, None, "/restore", True, False, None, None, None)
        self.assertEqual(
            default_payload["timeout_seconds"],
            service.DEFAULT_RESTORE_RUNTIME_TIMEOUT_SECONDS,
        )
        self.assertNotIn("timeout_seconds", service._build_backup_payload(target))

        target.restore_defaults = {"timeout_seconds": 7200}
        configured_payload = service._build_restore_payload(target, None, "/restore", True, False, None, None, None)
        self.assertEqual(configured_payload["timeout_seconds"], 7200.0)

        for invalid in (True, 0, service.MAX_RUNTIME_TIMEOUT_SECONDS + 1):
            target.restore_defaults = {"timeout_seconds": invalid}
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service._build_restore_payload(target, None, "/restore", True, False, None, None, None)

    def test_path_storage_uses_profile_repository_over_conflicting_global_settings(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.runtime_environment = {}
        profile = StorageProfileRecord(
            name="sftp-profile",
            backend_type="rclone",
            id="sftp-profile",
            environment={"RESTIC_REPOSITORY": "rclone:sftp:/docker-backups/"},
        )
        service.storage_profile_repository.save(profile)
        target.storage_profile_id = profile.id
        target.path_storage = " targets//custom/ "
        target.retention_policy_id = service.create_retention_policy("daily", keep_daily=7).id
        service.target_repository.save(target)
        service.settings_repository.save(SettingsRecord(restic_repository_base="rclone:global:settings"))

        payloads = [
            service._build_backup_payload(target),
            service._build_snapshot_list_payload(target),
            service._build_snapshot_read_payload(target, "abcdef12", "/", "browse"),
            service._build_stats_payload(target),
            service._build_retention_payload(target),
            service._build_restore_payload(target, None, "/restore", True, False, None, None, None),
        ]

        expected = "rclone:sftp:/docker-backups/targets/custom"
        self.assertTrue(all(payload["environment"]["RESTIC_REPOSITORY"] == expected for payload in payloads))
        self.assertTrue(all(payload["storage_context"]["repository_display"] == expected for payload in payloads))
        self.assertEqual(payloads[-1]["environment"]["RESTORE_TARGET_PATH"], "/restore")
        self.assertNotEqual(payloads[-1]["environment"]["RESTORE_TARGET_PATH"], target.path_storage)
        self.assertNotIn("global", json.dumps(payloads))
        self.assertEqual(
            service._append_repository_path("s3:https://example.invalid/bucket", "tenant/custom"),
            "s3:https://example.invalid/bucket/tenant/custom",
        )
        self.assertEqual(service._append_repository_path("local:/repo/", "tenant"), "local:/repo/tenant")
        self.assertEqual(service._append_repository_path("rclone:sftp:/", "tenant"), "rclone:sftp:/tenant")
        with self.assertRaisesRegex(ValueError, "unsupported|format"):
            service._append_repository_path("scp://host/repository", "tenant")

        first_fingerprint = service._repository_fingerprint(target)
        target.path_storage = "targets/other"
        second_fingerprint = service._repository_fingerprint(target)
        self.assertNotEqual(first_fingerprint, second_fingerprint)

    def test_path_storage_replaces_only_the_global_target_suffix(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.runtime_environment = {}
        target.storage_profile_id = None
        target.path_storage = "tenant//custom/"
        service.settings_repository.save(SettingsRecord(restic_repository_base="backup"))

        environment, _, _ = service._resolve_runtime_dependencies(target)

        self.assertEqual(environment["RESTIC_REPOSITORY"], "backup/tenant/custom")
        self.assertNotIn("target-a", environment["RESTIC_REPOSITORY"])

        service.settings_repository.save(SettingsRecord())
        target.path_storage = "tenant/one"
        first_fingerprint = service._repository_fingerprint(target)
        target.path_storage = "tenant/two"
        second_fingerprint = service._repository_fingerprint(target)
        self.assertNotEqual(first_fingerprint, second_fingerprint)

    def test_path_storage_does_not_modify_legacy_tar_or_rclone_remote_environment(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.backup_strategy = "tar"
        target.runtime_environment = {
            "RCLONE_REMOTE": "legacy:archive",
            "RESTIC_REPOSITORY": "local:/legacy-repository",
        }
        target.path_storage = "tenant/custom"
        service.settings_repository.save(SettingsRecord(restic_repository_base="backup"))

        payload = service._build_backup_payload(target)

        self.assertEqual(payload["environment"]["RCLONE_REMOTE"], "legacy:archive")
        self.assertEqual(payload["environment"]["RESTIC_REPOSITORY"], "local:/legacy-repository")

    def test_path_storage_does_not_turn_an_empty_profile_repository_into_global_fallback(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.runtime_environment = {}
        target.storage_profile_id = "profile-empty"
        target.path_storage = "tenant/custom"
        service.storage_profile_repository.save(
            StorageProfileRecord(
                name="empty-profile",
                backend_type="rclone",
                id="profile-empty",
                environment={"RESTIC_REPOSITORY": ""},
            )
        )
        service.settings_repository.save(SettingsRecord(restic_repository_base="backup"))

        environment, _, _ = service._resolve_runtime_dependencies(target)
        context = service._storage_context(target, environment, [])

        self.assertEqual(environment["RESTIC_REPOSITORY"], "")
        self.assertEqual(context["repository_source"], "unconfigured")
        self.assertIsNone(context["repository_display"])

    def test_path_storage_validation_rejects_unsafe_values_and_ambiguous_target_override(self):
        service = self.make_service()
        invalid_values = (
            "",
            "../backup",
            "backup/../target",
            "./backup",
            "/absolute",
            r"backup\\target",
            "https://example.invalid/repository",
            "s3:bucket/path",
            "tenant?query",
            "tenant#fragment",
            "backup\x00target",
            "backup\t target",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                service._normalize_path_storage(value)

        self.assertEqual(service._normalize_path_storage(" backup//tenant/ "), "backup/tenant")
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            service.register_target(
                "ambiguous-target",
                "worker-a",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo"},
                path_storage="tenant/custom",
            )
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            service.update_target("target-a", path_storage="tenant/custom")

        target = service.register_target("custom-target", "worker-a")
        updated = service.update_target(target.id, path_storage="tenant/custom")
        self.assertEqual(updated.path_storage, "tenant/custom")
        cleared = service.update_target(target.id, path_storage=None)
        self.assertIsNone(cleared.path_storage)

        indexed = InMemoryIndexRepository()
        indexed_service = self.make_service(index_repository=indexed)
        indexed_target = indexed_service.target_repository.get("target-a")
        indexed_target.runtime_environment = {}
        indexed_service.target_repository.save(indexed_target)
        indexed.upsert_status(IndexStatusRecord("target-a", "abcdef12", status="indexed"))
        indexed_service.update_target("target-a", path_storage="tenant/custom")
        self.assertEqual(indexed.list_by_target("target-a"), [])

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

    def test_restore_result_round_trip_preserves_lifecycle_evidence(self):
        service = self.make_service()
        job = JobRecord(worker_id="worker-a", command="restore.run", result_summary={"restore_ownership": {"schema_version": 1, "status": "failed", "category": "restart_failed", "partial": True, "destructive_state": "complete", "restart": {"requested": True, "state": "failed"}, "metadata": {"restored_metadata_proven": False}}})
        evidence = service.public_job_view(job)["result_summary"]["restore_ownership"]
        self.assertEqual(evidence["category"], "restart_failed"); self.assertTrue(evidence["partial"]); self.assertEqual(evidence["restart"]["state"], "failed"); self.assertFalse(evidence["metadata"]["restored_metadata_proven"])


class StorageAboutDispatchTests(unittest.TestCase):
    """PR 2 / Control Plane: storage.about dispatch, worker selection, state classification."""

    RCLONE_CONF = (
        "[rem]\n"
        "type = s3\n"
        "provider = AWS\n"
        "access_key_id = AKIAEXAMPLE1234\n"
        "secret_access_key = rclone-conf-secret-key\n"
    )
    REMOTE_CREDENTIALS = "AKIAEXAMPLE1234"
    RCLONE_CONF_SECRET = "rclone-conf-secret"

    def make_service(self, with_remote=True, offline_workers=()):
        workers = InMemoryWorkerRepository()
        for worker_id in ("worker-a", "worker-b"):
            workers.save(WorkerRecord(name=worker_id, host_name="test", id=worker_id, status="online", last_seen_at=utcnow()))
        for worker_id in offline_workers:
            worker = workers.get(worker_id)
            worker.last_seen_at = None
            workers.save(worker)
        profiles = InMemoryStorageProfileRepository()
        secrets = InMemorySecretRepository()
        codec = SecretCodec(Fernet.generate_key())
        settings_repository = InMemorySettingsRepository()
        if with_remote:
            secrets.save(SecretRecord(
                name="rclone-conf",
                scope="storage_profile",
                secret_type="file",
                id="rclone-conf-secret",
                ciphertext=codec.encrypt(self.RCLONE_CONF),
            ))
            profiles.save(StorageProfileRecord(
                name="profile-a",
                backend_type="rclone",
                id="profile-a",
                file_secret_refs={"/run/secrets/rclone.conf": "rclone-conf-secret"},
            ))
        else:
            profiles.save(StorageProfileRecord(name="profile-a", backend_type="rclone", id="profile-a"))
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=profiles,
            secret_repository=secrets,
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=codec,
            settings_repository=settings_repository,
        )

    def completed(self, service, state, metrics=None, error=None, status=None):
        """Mock a completed durable storage.about job (worker-side runtime mocked)."""
        if status is None:
            status = JobStatus.SUCCEEDED if state in ("available", "about-unsupported") else JobStatus.FAILED
        summary = {"state": state}
        if metrics is not None:
            summary["metrics"] = metrics
        if error is not None:
            summary["error"] = error
        return patch.object(service, "_wait_for_job_completion", return_value={
            "status": status,
            "logs": error or "",
            "result_summary": summary,
        })

    def test_storage_about_route_contract_available_and_not_configured(self):
        service = self.make_service(with_remote=True)
        with self.completed(service, "available", {"total": 100, "used": 25, "free": 75, "trashed": 2}) as waiter:
            response = service.storage_about("profile-a")
        self.assertEqual(set(response.keys()), {"profile_id", "state", "metrics", "error", "job_id"})
        self.assertEqual(response["profile_id"], "profile-a")
        self.assertEqual(response["state"], "available")
        self.assertEqual(response["metrics"], {"total": 100, "used": 25, "free": 75, "trashed": 2})
        self.assertIsNone(response["error"])
        self.assertTrue(response["job_id"])
        waiter.assert_called_once()

        unconfigured = self.make_service(with_remote=False)
        self.assertEqual(
            unconfigured.storage_about("profile-a"),
            {"profile_id": "profile-a", "state": "not-configured", "metrics": None, "error": None, "job_id": None},
        )

    def test_storage_about_uses_selected_profile_config_over_conflicting_global_settings(self):
        service = self.make_service(with_remote=False)
        profile_configs = {
            "profile-a": (
                "[profile-a-remote]\n"
                "type = s3\n"
                "access_key_id = profile-a-access\n"
                "secret_access_key = profile-a-secret\n"
            ),
            "profile-b": (
                "[profile-b-remote]\n"
                "type = s3\n"
                "access_key_id = profile-b-access\n"
                "secret_access_key = profile-b-secret\n"
            ),
        }
        profile_a_secret = service.create_secret(
            name="profile-a-rclone.conf",
            scope="storage_profile",
            secret_type="file",
            plaintext=profile_configs["profile-a"],
        )
        profile_a = service.update_storage_profile(
            "profile-a",
            file_secret_refs={"/run/secrets/rclone.conf": profile_a_secret.id},
        )
        profile_b_secret = service.create_secret(
            name="profile-b-rclone.conf",
            scope="storage_profile",
            secret_type="file",
            plaintext=profile_configs["profile-b"],
        )
        profile_b = service.create_storage_profile(
            name="profile-b",
            backend_type="rclone",
            file_secret_refs={"/run/secrets/rclone.conf": profile_b_secret.id},
        )
        global_config = (
            "[global-settings-remote]\n"
            "type = s3\n"
            "access_key_id = global-access\n"
            "secret_access_key = global-secret\n"
        )
        global_secret = service.create_secret(
            name="global-rclone.conf",
            scope="settings",
            secret_type="file",
            plaintext=global_config,
        )
        service.update_settings(rclone_conf_secret_id=global_secret.id)

        for profile in (profile_a, profile_b):
            with self.completed(service, "available", {"total": 1, "used": 0, "free": 1, "trashed": 0}):
                response = service.storage_about(profile.id)
            self.assertEqual(response["state"], "available")

        expected = {
            profile_a.id: ("profile-a-remote:", profile_configs["profile-a"]),
            profile_b.id: ("profile-b-remote:", profile_configs["profile-b"]),
        }
        about_jobs = [job for job in service.job_repository.list() if job.command == "storage.about"]
        self.assertEqual(len(about_jobs), 2)
        for job in about_jobs:
            remote, content = expected[job.payload["profile_id"]]
            self.assertEqual(job.payload["remote"], remote)
            self.assertEqual(job.payload["environment"]["RCLONE_CONF_CONTENT"], content)
            self.assertEqual(job.payload["resolved_files"][0]["content"], content)
            self.assertNotEqual(job.payload["remote"], "global-settings-remote:")
            self.assertNotEqual(job.payload["environment"]["RCLONE_CONF_CONTENT"], global_config)

    def test_storage_about_unconfigured_profiles_do_not_dispatch_jobs(self):
        service = self.make_service(with_remote=False)
        local_profile = service.create_storage_profile(name="local", backend_type="local")

        for profile in (service.storage_profile_repository.get("profile-a"), local_profile):
            with self.subTest(profile=profile.id):
                self.assertEqual(
                    service.storage_about(profile.id),
                    {
                        "profile_id": profile.id,
                        "state": "not-configured",
                        "metrics": None,
                        "error": None,
                        "job_id": None,
                    },
                )

        self.assertFalse(any(job.command == "storage.about" for job in service.job_repository.list()))

    def test_storage_about_route_401_without_viewer_role(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/storage-profiles/profile-a/about"
        handler.headers = {}
        service = Mock()
        handler.server = SimpleNamespace(application=SimpleNamespace(control_plane_service=service))
        handler._require_auth = Mock(return_value=None)
        handler._write_json = Mock(return_value=None)

        handler._handle_get_request(head_only=False)

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, head_only=False, api_mode=True)
        service.storage_about.assert_not_called()

    def test_storage_about_route_returns_contract_for_viewer(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/storage-profiles/profile-a/about"
        handler.headers = {}
        service = Mock()
        service.storage_about.return_value = {
            "profile_id": "profile-a",
            "state": "available",
            "metrics": {"total": 100, "used": 25, "free": 75, "trashed": 2},
            "error": None,
            "job_id": "job-1",
        }
        handler.server = SimpleNamespace(application=SimpleNamespace(control_plane_service=service))
        handler._require_auth = Mock(return_value={"role": ROLE_VIEWER})
        handler._write_json = Mock(return_value=None)

        handler._handle_get_request(head_only=False)

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, head_only=False, api_mode=True)
        service.storage_about.assert_called_once_with("profile-a")
        self.assertEqual(handler._write_json.call_args.args[0], 200)
        payload = handler._write_json.call_args.args[1]
        self.assertEqual(set(payload.keys()), {"profile_id", "state", "metrics", "error", "job_id"})

    def test_storage_about_worker_selection_picks_first_online_worker_in_repository_order(self):
        service = self.make_service(with_remote=True)
        workers = service.worker_repository.list()
        self.assertEqual(workers[0].id, "worker-a")
        self.assertEqual(workers[1].id, "worker-b")

        with self.completed(service, "available", {"total": 1, "used": 0, "free": 1, "trashed": 0}):
            service.storage_about("profile-a")

        job = service.job_repository.list()[0]
        self.assertEqual(job.worker_id, "worker-a")
        self.assertEqual(job.command, "storage.about")
        self.assertEqual(job.trigger, "interactive")
        self.assertEqual(job.payload["remote"], "rem:")
        self.assertEqual(job.payload["profile_id"], "profile-a")
        self.assertEqual(job.payload["environment"]["RCLONE_CONF_CONTENT"], self.RCLONE_CONF)
        self.assertEqual(job.payload["environment"]["RCLONE_CONFIG"], "/run/secrets/rclone.conf")

    def test_storage_about_worker_selection_skips_offline_workers(self):
        service = self.make_service(with_remote=True, offline_workers=("worker-a",))
        with self.completed(service, "about-unsupported"):
            response = service.storage_about("profile-a")
        job = service.job_repository.list()[0]
        self.assertEqual(job.worker_id, "worker-b")
        self.assertEqual(response["state"], "about-unsupported")
        self.assertIsNone(response["metrics"])
        self.assertIsNone(response["error"])

    def test_storage_about_classifies_available_unsupported_and_transient(self):
        with self.completed(service := self.make_service(with_remote=True), "available", {"total": 100, "used": 25, "free": 75, "trashed": 2}):
            response = service.storage_about("profile-a")
        self.assertEqual(response["state"], "available")
        self.assertEqual(response["metrics"], {"total": 100, "used": 25, "free": 75, "trashed": 2})
        self.assertIsNone(response["error"])

        with self.completed(unsupported := self.make_service(with_remote=True), "about-unsupported"):
            response = unsupported.storage_about("profile-a")
        self.assertEqual(response["state"], "about-unsupported")
        self.assertIsNone(response["metrics"])
        self.assertIsNone(response["error"])

        with self.completed(transient := self.make_service(with_remote=True), "transient-failure", error="timed out after 60s"):
            response = transient.storage_about("profile-a")
        self.assertEqual(response["state"], "transient-failure")
        self.assertIsNone(response["metrics"])
        self.assertEqual(response["error"], "timed out after 60s")

        with self.completed(timeout := self.make_service(with_remote=True), "transient-failure", error="", status="timeout"):
            response = timeout.storage_about("profile-a")
        self.assertEqual(response["state"], "transient-failure")
        self.assertIsNone(response["metrics"])
        self.assertIn("timed out", response["error"])

    def test_storage_about_durable_job_completion_round_trip(self):
        """Runtime harness: real durable dispatch + real _wait_for_job_completion
        polling, with a background worker claim completing the job through the
        repository lease path (mocked worker runtime)."""
        service = self.make_service(with_remote=True)
        completed = {}

        def worker_completes_pending_job():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                claimed = service.fetch_jobs_for_worker("worker-a")
                about_jobs = [item for item in claimed if item.command == "storage.about"]
                if not about_jobs:
                    time.sleep(0.01)
                    continue
                job = about_jobs[0]
                completed["job"] = service.update_job_status(
                    "worker-a",
                    job.id,
                    JobStatus.SUCCEEDED,
                    result_summary={"state": "available", "metrics": {"total": 100, "used": 25, "free": 75, "trashed": 2}},
                    lease_token=job.lease_token,
                )
                return
            raise AssertionError("worker did not observe the pending storage.about job")

        thread = threading.Thread(target=worker_completes_pending_job)
        thread.start()
        response = service.storage_about("profile-a")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

        self.assertEqual(response["state"], "available")
        self.assertEqual(response["metrics"], {"total": 100, "used": 25, "free": 75, "trashed": 2})
        self.assertIsNone(response["error"])
        self.assertEqual(completed["job"].command, "storage.about")
        self.assertEqual(completed["job"].result_summary["state"], "available")

    def test_storage_about_response_never_leaks_secrets_remote_or_payload(self):
        service = self.make_service(with_remote=True)
        with self.completed(service, "available", {"total": 100, "used": 25, "free": 75, "trashed": 2}):
            response = service.storage_about("profile-a")
        raw = json.dumps(response)
        self.assertNotIn(self.REMOTE_CREDENTIALS, raw)
        self.assertNotIn(self.RCLONE_CONF_SECRET, raw)
        self.assertNotIn(self.RCLONE_CONF, raw)
        self.assertNotIn("AKIAEXAMPLE", raw)
        self.assertNotIn("RCLONE_CONF_CONTENT", raw)
        self.assertNotIn("environment", raw)

    def test_storage_about_missing_profile_raises_error(self):
        service = self.make_service(with_remote=True)
        with self.assertRaisesRegex(ValueError, "storage profile not found"):
            service.storage_about("missing")


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

    @staticmethod
    def make_html_handler(path):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        handler.headers = {}
        handler._require_auth = Mock(return_value={"role": ROLE_VIEWER})
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        return handler

    def test_html_routes_version_static_assets_with_app_version(self):
        expected_common_assets = (
            "/styles/tokens.css",
            "/styles/components.css",
            "/favicon.ico",
        )
        for path in ("/login", "/", "/change-password"):
            with self.subTest(path=path), patch.dict(os.environ, {"APP_VERSION": "3.5.3"}, clear=False):
                handler = self.make_html_handler(path)
                if path == "/change-password":
                    handler._current_session = Mock(return_value={"must_change_password": True})

                handler._handle_get_request(head_only=False)

                self.assertEqual(handler.send_response.call_args.args, (200,))
                body = handler.wfile.getvalue().decode("utf-8")
                expected_assets = expected_common_assets + (("/styles/app.css",) if path == "/" else ())
                for asset_path in expected_assets:
                    self.assertIn(f'{asset_path}?v=3.5.3"', body)
                    self.assertNotIn(f'{asset_path}"', body)

    def test_html_routes_fallback_to_dev_for_missing_or_unsafe_app_version(self):
        for app_version in (None, "", "3.5.3&unsafe"):
            with self.subTest(app_version=app_version):
                environment = {} if app_version is None else {"APP_VERSION": app_version}
                with patch.dict(os.environ, environment, clear=True):
                    handler = self.make_html_handler("/login")

                    handler._handle_get_request(head_only=False)

                    body = handler.wfile.getvalue().decode("utf-8")
                    self.assertIn('/styles/tokens.css?v=dev"', body)
                    self.assertIn('/styles/components.css?v=dev"', body)
                    self.assertIn('/favicon.ico?v=dev"', body)

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

    def test_about_route_requires_viewer_role_and_uses_exact_snapshot_payload(self):
        handler = self.make_handler(
            "/api/v2/targets/target-a/about",
            '{"snapshot_id":"abcdef12"}',
        )
        service = Mock()
        service.dispatch_snapshot_about.return_value = {"schema_version": 1, "status": "pending"}
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, api_mode=True)
        service.dispatch_snapshot_about.assert_called_once_with(
            target_id="target-a",
            snapshot_id="abcdef12",
            request_id=None,
            requested_by="api",
        )
        self.assertEqual(handler._write_json.call_args.args[0], 202)

    def test_target_stats_sync_route_requires_operator_and_returns_public_job(self):
        handler = self.make_handler("/api/v1/targets/target-a/stats-sync", "{}")
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_OPERATOR}
        service = Mock()
        service.dispatch_stats_for_target.return_value = SimpleNamespace(id="job-1")
        service.public_job_view.return_value = {"id": "job-1", "status": "pending"}
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_OPERATOR, api_mode=True)
        service.dispatch_stats_for_target.assert_called_once_with("target-a", requested_by="api")
        service.public_job_view.assert_called_once_with(service.dispatch_stats_for_target.return_value)
        self.assertEqual(handler._write_json.call_args.args, (202, service.public_job_view.return_value))

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

    def test_admin_revoke_route_delegates_worker_state_change_to_service(self):
        handler = self.make_handler("/api/v1/admin/workers/worker-a/revoke", "{}")
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.revoke_worker.return_value = {"worker_id": "worker-a", "status": "revoked"}
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.revoke_worker.assert_called_once_with("worker-a", None)
        self.assertEqual(handler._write_json.call_args.args[0], 200)

    def test_admin_enrollment_renewal_route_requires_admin_and_delegates_worker_id(self):
        handler = self.make_handler(
            "/api/v1/admin/workers/worker-a/enrollment",
            '{"secret":"' + ("n" * 32) + '","ttl_minutes":45}',
        )
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.create_worker_enrollment.return_value = {
            "enrollment_id": "worker-a",
            "worker_id": "worker-a",
            "name": "worker-a",
            "host_name": "host-a",
            "expires_at": "2030-01-01T00:00:00",
        }
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.create_worker_enrollment.assert_called_once_with(
            worker_id="worker-a",
            secret="n" * 32,
            ttl_minutes=45,
        )
        self.assertEqual(handler._write_json.call_args.args, (201, service.create_worker_enrollment.return_value))

    def test_admin_enrollment_renewal_route_returns_service_error(self):
        handler = self.make_handler(
            "/api/v1/admin/workers/missing-worker/enrollment",
            '{"secret":"' + ("s" * 32) + '"}',
        )
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.create_worker_enrollment.side_effect = ValueError("worker not found: missing-worker")
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        self.assertEqual(
            handler._write_json.call_args.args,
            (400, {"error": "worker not found: missing-worker"}),
        )

    def test_target_backup_route_forwards_manual_backup_mode(self):
        handler = self.make_handler("/api/v1/targets/target-a/backup", '{"backup_mode":"cold"}')
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_OPERATOR}
        service = Mock()
        service.dispatch_backup_for_target.return_value = SimpleNamespace(id="job-1")
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_OPERATOR, api_mode=True)
        service.dispatch_backup_for_target.assert_called_once_with(
            "target-a",
            requested_by="api",
            backup_mode="cold",
        )
        self.assertEqual(handler._write_json.call_args.args[0], 202)

    def test_target_backup_route_returns_bad_request_for_invalid_or_blocked_backup(self):
        for error in (
            "backup_mode must be exactly 'hot' or 'cold'",
            "target 'target-a' is disabled and cannot be dispatched",
        ):
            with self.subTest(error=error):
                handler = self.make_handler("/api/v1/targets/target-a/backup", '{"backup_mode":"warm"}')
                handler._require_auth.reset_mock()
                handler._require_auth.return_value = {"role": ROLE_ADMIN}
                service = Mock()
                service.dispatch_backup_for_target.side_effect = ValueError(error)
                handler._control_plane_service = Mock(return_value=service)

                handler.do_POST()

                self.assertEqual(handler._write_json.call_args.args, (400, {"error": error}))

    def test_target_create_route_forwards_path_storage(self):
        handler = self.make_handler(
            "/api/v1/targets",
            '{"name":"target-c","worker_id":"worker-a","path_storage":"tenant/custom","volume_sources":["discourse_data"]}',
        )
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.register_target.return_value = SimpleNamespace(id="target-c")
        handler._control_plane_service = Mock(return_value=service)
        handler._target_jsonable = Mock(return_value={"id": "target-c", "path_storage": "tenant/custom"})

        handler.do_POST()

        self.assertEqual(service.register_target.call_args.kwargs["path_storage"], "tenant/custom")
        self.assertEqual(service.register_target.call_args.kwargs["volume_sources"], ["discourse_data"])
        self.assertEqual(handler._write_json.call_args.args, (201, handler._target_jsonable.return_value))

    def test_target_create_route_forwards_kubernetes_fields(self):
        handler = self.make_handler(
            "/api/v1/targets",
            json.dumps(
                {
                    "name": "target-k8s",
                    "worker_id": "worker-k8s",
                    "runtime_type": "kubernetes",
                    "namespace": "backups",
                    "pvc_names": ["discourse-data"],
                }
            ),
        )
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.register_target.return_value = SimpleNamespace(id="target-k8s")
        handler._control_plane_service = Mock(return_value=service)
        handler._target_jsonable = Mock(return_value={"id": "target-k8s", "runtime_type": "kubernetes"})

        handler.do_POST()

        self.assertEqual(service.register_target.call_args.kwargs["runtime_type"], "kubernetes")
        self.assertEqual(service.register_target.call_args.kwargs["namespace"], "backups")
        self.assertEqual(service.register_target.call_args.kwargs["pvc_names"], ["discourse-data"])
        self.assertEqual(handler._write_json.call_args.args, (201, handler._target_jsonable.return_value))

    def test_target_create_route_rejects_kubernetes_credentials_before_service_call(self):
        handler = self.make_handler(
            "/api/v1/targets",
            '{"name":"target-k8s","worker_id":"worker-k8s","runtime_type":"kubernetes",'
            '"namespace":"backups","pvc_names":["discourse-data"],"kubeconfig":"do-not-store"}',
        )
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        service.register_target.assert_not_called()
        self.assertEqual(
            handler._write_json.call_args.args,
            (400, {"error": "Kubernetes bearer tokens and kubeconfigs must not be provided"}),
        )

    def test_target_create_route_returns_clear_path_storage_validation_error(self):
        handler = self.make_handler(
            "/api/v1/targets",
            '{"name":"target-c","worker_id":"worker-a","path_storage":"../tenant"}',
        )
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.register_target.side_effect = ValueError("path_storage traversal is not allowed")
        handler._control_plane_service = Mock(return_value=service)

        handler.do_POST()

        self.assertEqual(handler._write_json.call_args.args, (400, {"error": "path_storage traversal is not allowed"}))

    def test_target_patch_route_forwards_null_path_storage_to_clear_it(self):
        handler = self.make_handler("/api/v1/targets/target-a", '{"path_storage":null}')
        service = Mock()
        service.update_target.return_value = SimpleNamespace(id="target-a")
        handler._control_plane_service = Mock(return_value=service)
        handler._target_jsonable = Mock(return_value={"id": "target-a", "path_storage": None})
        handler._live_service = Mock(return_value=None)
        handler._live_lane = Mock(return_value=None)

        handler.do_PATCH()

        self.assertIn("path_storage", service.update_target.call_args.kwargs)
        self.assertIsNone(service.update_target.call_args.kwargs["path_storage"])
        self.assertEqual(handler._write_json.call_args.args, (200, handler._target_jsonable.return_value))

    def test_target_patch_route_forwards_volume_sources(self):
        handler = self.make_handler(
            "/api/v1/targets/target-a",
            '{"compose_project":"discourse","volume_sources":["source-b"]}',
        )
        service = Mock()
        service.update_target.return_value = SimpleNamespace(id="target-a")
        handler._control_plane_service = Mock(return_value=service)
        handler._target_jsonable = Mock(return_value={"id": "target-a"})
        handler._live_service = Mock(return_value=None)
        handler._live_lane = Mock(return_value=None)

        handler.do_PATCH()

        self.assertEqual(service.update_target.call_args.kwargs["compose_project"], "discourse")
        self.assertEqual(service.update_target.call_args.kwargs["volume_sources"], ["source-b"])
        self.assertEqual(handler._write_json.call_args.args, (200, handler._target_jsonable.return_value))

    def test_target_patch_route_forwards_kubernetes_fields(self):
        handler = self.make_handler(
            "/api/v1/targets/target-k8s",
            '{"runtime_type":"kubernetes","namespace":"backups","pvc_names":["discourse-data"]}',
        )
        service = Mock()
        service.update_target.return_value = SimpleNamespace(id="target-k8s")
        handler._control_plane_service = Mock(return_value=service)
        handler._target_jsonable = Mock(return_value={"id": "target-k8s", "runtime_type": "kubernetes"})
        handler._live_service = Mock(return_value=None)
        handler._live_lane = Mock(return_value=None)

        handler.do_PATCH()

        self.assertEqual(service.update_target.call_args.kwargs["runtime_type"], "kubernetes")
        self.assertEqual(service.update_target.call_args.kwargs["namespace"], "backups")
        self.assertEqual(service.update_target.call_args.kwargs["pvc_names"], ["discourse-data"])
        self.assertEqual(handler._write_json.call_args.args, (200, handler._target_jsonable.return_value))

    def test_target_jsonable_distinguishes_revoked_and_deleted_workers(self):
        service = ControlPlaneDispatchTests().make_service()
        revoked_worker = service.worker_repository.get("worker-a")
        revoked_worker.status = WorkerStatus.DISABLED
        service.worker_repository.save(revoked_worker)
        revoked_target = service.target_repository.get("target-a")
        revoked_target.enabled = False
        service.target_repository.save(revoked_target)

        handler = object.__new__(ControlPlaneRequestHandler)
        handler._control_plane_service = Mock(return_value=service)
        handler._scheduler = Mock(return_value=None)

        revoked_payload = handler._target_jsonable(revoked_target)
        self.assertEqual(revoked_payload["worker_name"], "worker-a")
        self.assertEqual(revoked_payload["worker_status"], "disabled")
        self.assertTrue(revoked_payload["execution_blocked"])
        self.assertEqual(revoked_payload["blocked_reason"], "worker_revoked")

        deleted_target = service.target_repository.get("target-b")
        service.worker_repository.delete("worker-b")
        deleted_payload = handler._target_jsonable(deleted_target)
        self.assertEqual(deleted_payload["worker_status"], "missing")
        self.assertTrue(deleted_payload["execution_blocked"])
        self.assertEqual(deleted_payload["blocked_reason"], "worker_missing")
        self.assertEqual(deleted_payload["worker_id"], "worker-b")
        deleted_target.path_storage = "tenant/custom"
        self.assertEqual(handler._target_jsonable(deleted_target)["path_storage"], "tenant/custom")

    def test_target_jsonable_separates_inactive_scheduling_from_worker_execution_blocking(self):
        service = ControlPlaneDispatchTests().make_service()
        target = service.target_repository.get("target-a")
        target.enabled = False
        service.target_repository.save(target)

        handler = object.__new__(ControlPlaneRequestHandler)
        handler._control_plane_service = Mock(return_value=service)
        handler._scheduler = Mock(return_value=None)

        payload = handler._target_jsonable(target)

        self.assertFalse(payload["execution_blocked"])
        self.assertTrue(payload["scheduling_disabled"])
        self.assertEqual(payload["blocked_reason"], "target_disabled")

    def test_admin_delete_worker_route_is_admin_only_and_returns_no_content(self):
        handler = self.make_handler("/api/v1/workers/worker-a", "")
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.delete_worker.return_value = True
        handler._control_plane_service = Mock(return_value=service)

        handler.do_DELETE()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.delete_worker.assert_called_once_with("worker-a")
        self.assertEqual(handler._write_json.call_args.args[0], 204)

    def test_admin_delete_worker_route_returns_conflict_for_dependency_error(self):
        handler = self.make_handler("/api/v1/workers/worker-a", "")
        handler._require_auth.reset_mock()
        handler._require_auth.return_value = {"role": ROLE_ADMIN}
        service = Mock()
        service.delete_worker.side_effect = WorkerDeletionConflict("worker 'worker-a' still has active or pending jobs: job-a (pending)")
        handler._control_plane_service = Mock(return_value=service)

        handler.do_DELETE()

        self.assertEqual(
            handler._write_json.call_args.args,
            (409, {"error": "worker 'worker-a' still has active or pending jobs: job-a (pending)", "code": "worker_deletion_conflict"}),
        )

    def test_public_config_exposes_scheduler_timezone(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/config/public"
        handler.headers = {}
        handler.server = SimpleNamespace(
            application=SimpleNamespace(
                control_plane_service=SimpleNamespace(get_settings=lambda: None),
                scheduler=SimpleNamespace(timezone_name="America/Bogota"),
            )
        )
        handler._write_json = Mock(return_value=None)

        handler._handle_get_request(head_only=False)

        payload = handler._write_json.call_args.args[1]
        self.assertEqual(payload["scheduler_timezone"], "America/Bogota")

    def test_scheduler_preview_route_preserves_target_context_for_empty_cron(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/scheduler/preview?cron_expression=&target_context=true"
        handler.headers = {}
        handler.server = SimpleNamespace(
            application=SimpleNamespace(
                control_plane_service=SimpleNamespace(target_repository=SimpleNamespace(get=lambda target_id: None)),
                scheduler=Mock(),
            )
        )
        handler._require_auth = Mock(return_value=True)
        handler._write_json = Mock(return_value=None)
        handler.server.application.scheduler.preview.return_value = {"cron_source": "global"}

        handler._handle_get_request(head_only=False)

        handler.server.application.scheduler.preview.assert_called_once_with(
            target=None,
            cron_expression="",
            target_context=True,
        )


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
