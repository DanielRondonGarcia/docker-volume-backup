import json
import os
import sys
import sqlite3
import threading
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from cryptography.fernet import Fernet

from src.app.domain.models import BackupConfig, RestoreResult
from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy
from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import BackupTargetRecord, JobRecord, JobStatus, SecretRecord, SettingsRecord, StorageProfileRecord, WorkerRecord, utcnow
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
from src.control_plane.infrastructure.security.secret_codec import SecretCodec
from src.security.hmac_protocol import digest_secret, verify_request
from src.worker_agent.application.services.worker_agent_service import _JobProgressReporter, WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient


class BackupObservabilityServiceTests(unittest.TestCase):
    def make_service(self, job_repository=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="host", id="worker-a", status="online", last_seen_at=utcnow()))
        workers.save(WorkerRecord(name="worker-b", host_name="host", id="worker-b", status="online", last_seen_at=utcnow()))
        targets = InMemoryTargetRepository()
        targets.save(BackupTargetRecord(name="target-a", worker_id="worker-a", id="target-a", backup_strategy="restic"))
        codec = SecretCodec(Fernet.generate_key())
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=targets,
            job_repository=job_repository or InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=codec,
            settings_repository=InMemorySettingsRepository(),
        )

    def test_storage_context_reports_effective_source_kind_and_redacted_display(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.runtime_environment = {
            "RESTIC_REPOSITORY": "s3://user:password@example.invalid/bucket/path?token=secret"
        }
        context = service._storage_context(target, target.runtime_environment, [])

        self.assertEqual(context["repository_source"], "target")
        self.assertEqual(context["repository_kind"], "s3")
        self.assertNotIn("password", context["repository_display"])
        self.assertNotIn("token=secret", context["repository_display"])

        target.runtime_environment = {}
        service.settings_repository.save(SettingsRecord(restic_repository_base="s3:https://user:password@example.invalid/bucket"))
        payload = service._build_backup_payload(target)
        self.assertEqual(payload["storage_context"]["repository_source"], "settings")
        self.assertEqual(payload["storage_context"]["repository_kind"], "s3")
        self.assertNotIn("password", json.dumps(payload["storage_context"]))

        service.settings_repository.save(SettingsRecord())
        unconfigured = service._build_snapshot_list_payload(target)["storage_context"]
        self.assertEqual(unconfigured["repository_source"], "unconfigured")
        self.assertEqual(unconfigured["repository_kind"], "unknown")
        self.assertIsNone(unconfigured["repository_display"])

    def test_profile_rclone_file_wins_global_settings_at_arbitrary_profile_path(self):
        service = self.make_service()
        profile_content = "[profile-remote]\ntype = s3\nsecret_access_key = profile-secret\n"
        global_content = "[global-remote]\ntype = s3\nsecret_access_key = global-secret\n"
        profile_secret = service.create_secret("profile-rclone", "storage_profile", "file", profile_content)
        global_secret = service.create_secret("global-rclone", "settings", "file", global_content)
        profile = service.create_storage_profile(
            "profile-a",
            "rclone",
            environment={"RESTIC_REPOSITORY": "rclone:profile-remote:backup/target-a"},
            file_secret_refs={"/root/.config/rclone/rclone.conf": profile_secret.id},
        )
        target = service.target_repository.get("target-a")
        target.storage_profile_id = profile.id
        service.settings_repository.save(SettingsRecord(restic_repository_base="backup", rclone_conf_secret_id=global_secret.id))

        environment, _, resolved_files = service._resolve_runtime_dependencies(target)

        self.assertEqual(environment["RCLONE_CONF_CONTENT"], profile_content)
        self.assertEqual([item["content"] for item in resolved_files], [profile_content])
        context = service._storage_context(target, environment, resolved_files)
        self.assertEqual(context["repository_source"], "profile")
        self.assertEqual(context["repository_kind"], "rclone")
        self.assertEqual(context["rclone_config_source"], "profile")

    def test_profile_only_rclone_file_supplies_backup_and_snapshot_repository(self):
        service = self.make_service()
        profile_content = "[profile-remote]\ntype = s3\nsecret_access_key = profile-secret\n"
        global_content = "[global-remote]\ntype = s3\nsecret_access_key = global-secret\n"
        profile_secret = service.create_secret("profile-rclone", "storage_profile", "file", profile_content)
        global_secret = service.create_secret("global-rclone", "settings", "file", global_content)
        profile = service.create_storage_profile(
            "profile-only",
            "rclone",
            file_secret_refs={"/run/rclone-config/rclone.conf": profile_secret.id},
        )
        target = service.target_repository.get("target-a")
        target.storage_profile_id = profile.id
        service.target_repository.save(target)
        service.settings_repository.save(
            SettingsRecord(restic_repository_base="backup", rclone_conf_secret_id=global_secret.id)
        )

        payloads = [service._build_backup_payload(target), service._build_snapshot_list_payload(target)]
        expected_repository = "rclone:profile-remote:backup/target-a"
        for payload in payloads:
            self.assertEqual(payload["environment"]["RESTIC_REPOSITORY"], expected_repository)
            self.assertEqual(payload["environment"]["RCLONE_CONF_CONTENT"], profile_content)
            self.assertEqual(payload["storage_context"]["repository_source"], "profile")
            self.assertEqual(payload["storage_context"]["rclone_config_source"], "profile")
            self.assertEqual(payload["storage_context"]["repository_display"], expected_repository)
            self.assertNotIn("global-remote", json.dumps(payload["storage_context"]))
            self.assertNotIn(global_content, json.dumps(payload["resolved_files"]))

    def test_profile_rclone_remote_preserves_non_rclone_settings_repository(self):
        service = self.make_service()
        profile_content = "[profile-remote]\ntype = s3\nsecret_access_key = profile-secret\n"
        global_content = "[global-remote]\ntype = s3\nsecret_access_key = global-secret\n"
        profile_secret = service.create_secret("profile-rclone", "storage_profile", "file", profile_content)
        global_secret = service.create_secret("global-rclone", "settings", "file", global_content)
        profile = service.create_storage_profile(
            "profile-s3-base",
            "rclone",
            file_secret_refs={"/run/rclone-config/rclone.conf": profile_secret.id},
        )
        target = service.target_repository.get("target-a")
        target.storage_profile_id = profile.id
        service.target_repository.save(target)
        service.settings_repository.save(
            SettingsRecord(
                restic_repository_base="s3:https://example.invalid/global",
                rclone_conf_secret_id=global_secret.id,
            )
        )

        payloads = [service._build_backup_payload(target), service._build_snapshot_list_payload(target)]
        expected_repository = "s3:https://example.invalid/global/target-a"
        for payload in payloads:
            self.assertEqual(payload["environment"]["RESTIC_REPOSITORY"], expected_repository)
            self.assertEqual(payload["storage_context"]["repository_source"], "settings")
            self.assertEqual(payload["storage_context"]["repository_kind"], "s3")
            self.assertEqual(payload["storage_context"]["rclone_config_source"], "profile")
            self.assertEqual(payload["storage_context"]["repository_display"], expected_repository)

    def test_public_job_view_omits_internal_payload_and_redacts_persisted_output(self):
        service = self.make_service()
        job = JobRecord(
            worker_id="worker-a",
            command="backup.run",
            payload={
                "environment": {
                    "RESTIC_PASSWORD": "restic-password",
                    "RCLONE_CONF_CONTENT": "[remote]\nsecret_access_key = rclone-password",
                    "RESTIC_REPOSITORY": "https://user:repo-password@example.invalid/repository",
                },
                "storage_context": {
                    "repository_source": "target",
                    "repository_kind": "s3",
                    "repository_display": "s3://example.invalid/bucket/path",
                },
            },
            lease_token="lease-token",
            result_summary={"error": "password=restic-password", "b64_content": "private-file-content"},
            log_lines=["restic-password [remote] secret_access_key = rclone-password"],
        )
        service.job_repository.save(job)

        view = service.public_job_view(job)
        serialized = json.dumps(view, default=str)

        self.assertNotIn("payload", view)
        self.assertNotIn("lease_token", view)
        self.assertNotIn("restic-password", serialized)
        self.assertNotIn("rclone-password", serialized)
        self.assertNotIn("repo-password", serialized)
        self.assertNotIn("private-file-content", serialized)
        self.assertIn("storage_context", view)
        self.assertIn("log_lines", view)

    def test_restore_result_writer_is_atomic_bounded_and_sanitized(self):
        fake_docker = SimpleNamespace(from_env=lambda: None, errors=SimpleNamespace(NotFound=RuntimeError))
        with patch.dict(sys.modules, {"docker": fake_docker}):
            from src.app import main as app_main
        result = RestoreResult(datetime.now(), 0, False, error="token=private-value")
        result.category = "ownership_normalization_failed"
        result.partial = True
        result.destructive_state = "partial"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "restore-result.json")
            with patch.dict(os.environ, {"RESTORE_RESULT_FILE": path}, clear=False):
                app_main._write_restore_result(result)
            raw = open(path, encoding="utf-8").read()
            self.assertLessEqual(len(raw.encode("utf-8")), 64 * 1024)
            self.assertNotIn("private-value", raw)
            self.assertEqual(json.loads(raw)["destructive_state"], "partial")
            self.assertEqual(os.listdir(directory), ["restore-result.json"])

    def test_lightweight_job_listing_does_not_load_persisted_payload_or_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            service = self.make_service(job_repository=repository)
            job = JobRecord(
                worker_id="worker-a",
                command="backup.run",
                payload={"large": "payload"},
                result_summary={"entries": [{"path": str(index)} for index in range(1000)]},
                log_lines=["persisted log line"] * 1000,
            )
            repository.save(job)
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "UPDATE jobs SET payload_json = ?, result_summary_json = ?, log_lines_json = ? WHERE id = ?",
                    ("not-json", "not-json", "not-json", job.id),
                )
                connection.commit()
            finally:
                connection.close()

            with patch.object(service, "_job_storage_context", side_effect=AssertionError("listing touched storage context")), patch.object(
                service, "_bounded_job_log_lines", side_effect=AssertionError("listing touched logs")
            ), patch.object(service, "_safe_result_summary", side_effect=AssertionError("listing touched summary")):
                views, total = service.list_job_views(limit=1)

        self.assertEqual(total, 1)
        self.assertEqual(views[0]["log_lines"], [])
        self.assertEqual(views[0]["result_summary"], {})
        self.assertEqual(views[0]["storage_context"], {})
        self.assertNotIn("payload", views[0])

    def test_lightweight_listing_order_total_and_pagination_match_repositories(self):
        submitted_at = datetime(2026, 1, 1, 12, 0, 0)
        jobs = [
            JobRecord(id="job-old", worker_id="worker-a", command="backup.run", submitted_at=submitted_at),
            JobRecord(id="job-tie-low", worker_id="worker-a", command="backup.run", submitted_at=submitted_at),
            JobRecord(id="job-tie-high", worker_id="worker-a", command="backup.run", submitted_at=submitted_at),
            JobRecord(id="job-new", worker_id="worker-a", command="backup.run", submitted_at=submitted_at + timedelta(minutes=1)),
        ]
        expected_order = ["job-new", "job-tie-low", "job-tie-high", "job-old"]
        repositories = [InMemoryJobRepository()]
        with tempfile.TemporaryDirectory() as directory:
            repositories.append(SQLiteJobRepository(f"{directory}/control-plane.db"))
            for repository in repositories:
                for job in jobs:
                    repository.save(
                        JobRecord(
                            id=job.id,
                            worker_id=job.worker_id,
                            command=job.command,
                            submitted_at=job.submitted_at,
                        )
                    )
                page, total = repository.list_for_listing(limit=2, offset=1)
                all_items, all_total = repository.list_for_listing()
                self.assertEqual([item.id for item in all_items], expected_order)
                self.assertEqual([item.id for item in page], expected_order[1:3])
                self.assertEqual(total, len(expected_order))
                self.assertEqual(all_total, len(expected_order))
                self.assertEqual(all_items[0].payload, {})
                self.assertIsNone(all_items[0].result_summary)
                self.assertEqual(all_items[0].log_lines, [])

    def test_sqlite_job_indexes_are_idempotent_without_schema_version_change(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            SQLiteJobRepository(database_path)
            connection = sqlite3.connect(database_path)
            try:
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'jobs'"
                    )
                }
            finally:
                connection.close()
            self.assertIn("idx_jobs_submitted_at_id_desc", indexes)
            self.assertIn("idx_jobs_status_lease_expires_at", indexes)
            self.assertEqual(repository.get_schema_version(), 1)

    def test_progress_is_owned_monotonic_bounded_and_terminal_immutable_for_memory_and_sqlite(self):
        repositories = [InMemoryJobRepository()]
        with tempfile.TemporaryDirectory() as directory:
            repositories.append(SQLiteJobRepository(f"{directory}/control-plane.db"))

            for repository in repositories:
                with self.subTest(repository=type(repository).__name__):
                    service = self.make_service(job_repository=repository)
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    updated = service.update_job_progress(
                        "worker-a",
                        job.id,
                        sequence=1,
                        progress={"percent_done": 10, "files_done": 1, "phase": "backup"},
                        log_lines=["first"],
                        lease_token=claimed.lease_token,
                    )
                    self.assertEqual(updated.result_summary["progress"]["percent_done"], 10)
                    self.assertEqual(updated.log_lines[-1], "first")

                    duplicate = service.update_job_progress(
                        "worker-a", job.id, 1, {"percent_done": 90}, ["duplicate"], claimed.lease_token
                    )
                    self.assertEqual(duplicate.result_summary["progress"]["percent_done"], 10)
                    with self.assertRaisesRegex(ValueError, "between 0 and 100|bounds"):
                        service.update_job_progress("worker-a", job.id, 2, {"percent_done": 101}, ["x"], claimed.lease_token)
                    with self.assertRaisesRegex(ValueError, "invalid or stale"):
                        service.update_job_progress("worker-a", job.id, 2, {"percent_done": 20}, [], "wrong")
                    with self.assertRaisesRegex(ValueError, "does not own"):
                        service.update_job_progress("worker-b", job.id, 2, {"percent_done": 20}, [], claimed.lease_token)

                    claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
                    repository.save(claimed)
                    with self.assertRaisesRegex(ValueError, "expired"):
                        service.update_job_progress("worker-a", job.id, 2, {"percent_done": 20}, [], claimed.lease_token)

                    claimed.lease_expires_at = utcnow() + timedelta(minutes=5)
                    repository.save(claimed)
                    service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)
                    terminal = repository.get(job.id)
                    result = service.update_job_progress("worker-a", job.id, 2, {"percent_done": 99}, ["late"], claimed.lease_token)
                    self.assertEqual(result.result_summary, terminal.result_summary)
                    self.assertEqual(result.log_lines, terminal.log_lines)

    def test_phase_only_progress_is_persisted_and_publicly_projected(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "backup.run")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        service.update_job_progress(
            "worker-a",
            job.id,
            sequence=1,
            progress={"phase": "preparing"},
            log_lines=["Backup starting"],
            lease_token=claimed.lease_token,
        )

        view = service.get_job_view(job.id)
        serialized = json.dumps(view, default=str)
        self.assertEqual(view["progress"], {"phase": "preparing"})
        self.assertIn("Backup starting", view["log_lines"])
        self.assertNotIn("payload", view)
        self.assertNotIn("lease_token", view)
        self.assertNotIn("lease", serialized)


class ProgressClientAndWorkerTests(unittest.TestCase):
    def test_client_progress_request_is_hmac_signed(self):
        store = Mock()
        store.load.return_value = SimpleNamespace(secret="s" * 32, version="7")
        client = ControlPlaneClient("http://control-plane", credential_store=store, worker_id="worker-a")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with patch("src.worker_agent.infrastructure.api_client.control_plane_client.request.urlopen", return_value=response) as opener:
            client.update_job_progress("worker-a", "job-a", 3, {"phase": "backup"}, ["line"], "lease")

        request = opener.call_args.args[0]
        body = request.data
        path = "/api/v1/workers/worker-a/jobs/job-a/progress"
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertTrue(
            verify_request(
                digest_secret("s" * 32),
                headers["x-worker-signature"],
                "POST",
                path,
                body,
                headers["x-worker-timestamp"],
                headers["x-worker-nonce"],
                "worker-a",
                "7",
            )
        )
        self.assertIn('"lease_token": "lease"', body.decode("utf-8"))

    def test_worker_progress_reporter_coalesces_and_ignores_transport_failure(self):
        class Client:
            def __init__(self, failure=None):
                self.calls = []
                self.failure = failure

            def update_job_progress(self, **kwargs):
                self.calls.append(kwargs)
                if self.failure:
                    raise self.failure

        client = Client()
        reporter = _JobProgressReporter(client, "worker-a", {"id": "job-a", "lease_token": "lease"})
        reporter.start()
        reporter.emit('{"message_type":"status","percent_done":42,"files_done":2}\n')
        reporter.finish()
        self.assertLessEqual(len(client.calls), 3)
        self.assertTrue(any(call["progress"].get("percent_done") == 42.0 for call in client.calls))
        self.assertEqual([call["sequence"] for call in client.calls], sorted(call["sequence"] for call in client.calls))

        failing = _JobProgressReporter(Client(RuntimeError("network unavailable")), "worker-a", {"id": "job-a", "lease_token": "lease"})
        failing.start()
        failing.emit("runtime output\n", force=True)
        failing.finish()

    def test_worker_progress_reporter_flushes_phase_changes_before_interval(self):
        class Client:
            def __init__(self):
                self.calls = []

            def update_job_progress(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        reporter = _JobProgressReporter(client, "worker-a", {"id": "job-a", "lease_token": "lease"})
        with patch(
            "src.worker_agent.application.services.worker_agent_service.time.monotonic",
            side_effect=[100.0, 100.1, 100.2, 100.3],
        ):
            reporter.start()
            reporter.emit("Backup starting\n")
            reporter.emit("runtime output\n")
            reporter.emit("Running restic backup\n")

        self.assertEqual([call["progress"].get("phase") for call in client.calls], ["starting", "preparing", "backup"])
        self.assertEqual(client.calls[1]["log_lines"], ["Backup starting"])
        self.assertEqual(client.calls[2]["log_lines"], ["runtime output", "Running restic backup"])

    def test_worker_progress_reporter_infers_phases_and_keeps_json_metrics(self):
        class Client:
            def __init__(self):
                self.calls = []

            def update_job_progress(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        reporter = _JobProgressReporter(client, "worker-a", {"id": "job-a", "lease_token": "lease"})
        for line in (
            "Backup starting\n",
            "Performing backup strategy\n",
            "Restic repository not initialized or not accessible. Attempting to initialize...\n",
            "Restic repository initialized successfully.\n",
            "Running restic backup for ['/backup']\n",
            "Pruning old snapshots...\n",
            "Pruning finished successfully.\n",
        ):
            reporter.emit(line, force=True)
        reporter.emit(
            '{"message_type":"status","percent_done":42.5,"files_done":2,"bytes_done":128,"total_bytes":256}\n',
            force=True,
        )

        phases = [call["progress"].get("phase") for call in client.calls]
        for phase in ("preparing", "initializing", "backup", "pruning", "finalizing"):
            self.assertIn(phase, phases)
        status = client.calls[-1]["progress"]
        self.assertEqual(status["percent_done"], 42.5)
        self.assertEqual(status["files_done"], 2)
        self.assertEqual(status["bytes_done"], 128)
        self.assertEqual(status["total_bytes"], 256)


class RuntimeAndResticStreamingTests(unittest.TestCase):
    def test_docker_streaming_emits_short_initial_chunks_before_long_secret_tail(self):
        secret = "s" * 4096
        first_emitted = threading.Event()
        release_stream = threading.Event()
        chunks = []

        class BlockingStream:
            def __iter__(self):
                yield b"Backup starting\n"
                release_stream.wait(2)
                yield secret[:2048].encode("utf-8")
                yield (secret[2048:] + "\nstatus\n").encode("utf-8")

        container = Mock()
        container.logs.return_value = BlockingStream()
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)

        def on_output(value):
            chunks.append(value)
            first_emitted.set()

        state, thread = runtime._start_log_stream(container, {secret}, 32 * 1024, on_output)
        try:
            self.assertTrue(first_emitted.wait(1))
            self.assertEqual(chunks, ["Backup starting\n"])
        finally:
            release_stream.set()
            runtime._finish_log_stream(state, thread)

        combined = "".join(chunks)
        self.assertNotIn(secret, combined)
        self.assertIn("<redacted>", combined)
        self.assertIn("status", combined)

    def test_docker_streaming_callback_redacts_secret_split_across_chunks(self):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = iter([b"restic-password", b"-value\nstatus\n"])
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        runtime.no_lock = False
        runtime.cache_dir = None
        chunks = []

        result = runtime.run_runtime_job(
            "runtime",
            {
                "command": "restic snapshots --json",
                "environment": {"RESTIC_PASSWORD": "restic-password-value"},
            },
            output_callback=chunks.append,
        )

        self.assertTrue(result["success"])
        self.assertNotIn("restic-password-value", repr(result))
        self.assertNotIn("restic-password-value", "".join(chunks))
        self.assertIn("status", "".join(chunks))

    def test_restic_backup_streams_json_status_and_preserves_summary_and_prune(self):
        status_line = '{"message_type":"status","percent_done":0.5,"files_done":1}\n'
        summary_line = '{"message_type":"summary","data_added":123,"total_duration":4.5}\n'

        class Stream:
            def __init__(self, lines):
                self.lines = list(lines)

            def readline(self):
                return self.lines.pop(0) if self.lines else ""

        class Process:
            stdout = Stream([status_line, summary_line])
            stderr = Stream([])
            returncode = 0

            def wait(self):
                return 0

        callback_lines = []
        strategy = ResticBackupStrategy()
        with patch("src.app.infrastructure.adapters.backup_strategy.subprocess.Popen", return_value=Process()) as popen, patch(
            "src.app.infrastructure.adapters.backup_strategy.subprocess.run"
        ) as run:
            result = strategy.perform_backup(
                BackupConfig(source_paths=["/backup"], restic_repository="local:/repo", restic_password="password"),
                output_callback=callback_lines.append,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.size, 123)
        self.assertEqual(result.duration, 4.5)
        self.assertTrue(any('"message_type":"status"' in line for line in callback_lines))
        popen.assert_called_once()
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[-1].args[0][1], "forget")


if __name__ == "__main__":
    unittest.main()
