import io
import json
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
    IndexStatusRecord,
    JobStatus,
    SecretRecord,
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
        self.assertEqual(job.payload["command"][-1], "/folder with spaces/file.txt")
        self.assertEqual(job.payload["cache_generation"], 0)

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

    def test_disabled_targets_cannot_be_dispatched_as_backups(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.enabled = False
        service.target_repository.save(target)

        with self.assertRaisesRegex(ValueError, "disabled"):
            service.dispatch_backup_for_target("target-a")
        with self.assertRaisesRegex(ValueError, "disabled"):
            service.dispatch_job("worker-a", "backup.run", target_id="target-a")

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
