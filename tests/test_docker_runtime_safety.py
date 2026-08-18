import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.security.job_recovery_journal import WorkerJobRecoveryJournal


class DockerRuntimeSafetyTests(unittest.TestCase):
    @staticmethod
    def container(logs=b"ok"):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = logs
        return container

    def runtime(self, container, no_lock=False, cache_dir=None):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        runtime.no_lock = no_lock
        runtime.cache_dir = cache_dir
        return runtime

    def test_shell_metacharacters_and_non_allowlisted_argv_fail_closed(self):
        runtime = self.runtime(self.container())

        shell_result = runtime.run_runtime_job(
            "runtime",
            {"command": ["restic", "snapshots", "--json", "$(touch", "/tmp/pwned)"]},
        )
        executable_result = runtime.run_runtime_job(
            "runtime",
            {"command": ["python", "-c", "dangerous"]},
        )

        self.assertFalse(shell_result["success"])
        self.assertIn("shell metacharacters", shell_result["error"])
        self.assertFalse(executable_result["success"])
        self.assertIn("unsupported", executable_result["error"])
        runtime.client.containers.run.assert_not_called()

    def test_no_lock_is_added_only_to_read_operations(self):
        read_runtime = self.runtime(self.container(logs=b"[]"), no_lock=True)
        result = read_runtime.run_runtime_job(
            "runtime",
            {"command": "restic snapshots --json"},
        )

        self.assertTrue(result["success"])
        read_command = read_runtime.client.containers.run.call_args.kwargs["command"]
        self.assertEqual(read_command, ["restic", "snapshots", "--json", "--no-lock"])

        for command in ("/root/backup.sh", "restic forget --keep-last 1", "restic prune"):
            write_runtime = self.runtime(self.container(), no_lock=True)
            write_result = write_runtime.run_runtime_job("runtime", {"command": command})
            self.assertTrue(write_result["success"])
            write_command = write_runtime.client.containers.run.call_args.kwargs["command"]
            self.assertNotIn("--no-lock", write_command)

    def test_rclone_about_argv_is_admitted_and_variants_fail_closed(self):
        admitted = DockerRuntimeAdapter._runtime_command_argv(["rclone", "about", "rem:", "--json"])
        self.assertEqual(admitted, ["rclone", "about", "rem:", "--json"])

        variants = (
            ["rclone", "lsjson", "rem:", "--json"],
            ["rclone", "about", "rem:", "--json", "--extra"],
            ["rclone", "about", "rem:"],
            ["rclone", "about", "rem: --json"],
            ["rclone", "about", "rem:/path", "--json"],
            ["rclone", "about", "rem", "--json"],
            ["rclone", "about", "rem:$(touch /tmp/pwned)", "--json"],
            ["rclone", "about", "..", "--json"],
            ["rclone", "about", "not a remote:", "--json"],
        )
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    DockerRuntimeAdapter._runtime_command_argv(variant)

        invalid_runtime = self.runtime(self.container())
        invalid_result = invalid_runtime.run_runtime_job(
            "runtime",
            {"command": ["rclone", "about", "rem:/path", "--json"]},
        )
        self.assertFalse(invalid_result["success"])
        invalid_runtime.client.containers.run.assert_not_called()

    def test_rclone_about_runs_admitted_argv_without_shell(self):
        runtime = self.runtime(self.container(logs=b'{"total":1,"used":2,"free":3,"trashed":0}'))

        result = runtime.run_runtime_job(
            "runtime",
            {"command": ["rclone", "about", "rem:", "--json"]},
        )

        self.assertTrue(result["success"])
        command = runtime.client.containers.run.call_args.kwargs["command"]
        self.assertEqual(command, ["rclone", "about", "rem:", "--json"])
        self.assertNotIn("/bin/sh", " ".join(command))

    def test_snapshot_path_id_and_target_scope_are_validated_before_launch(self):
        cases = (
            {"command": "restic ls --json not-a-restic-id /"},
            {"command": "restic ls --json abcdef12 /../secret"},
            {
                "command": "restic ls --json abcdef12 /",
                "target_id": "target-a",
                "snapshot_target_id": "target-b",
            },
            {"command": "restic ls --json abcdef12 /", "path": "/safe\x00path"},
        )

        for payload in cases:
            runtime = self.runtime(self.container())
            result = runtime.run_runtime_job("runtime", payload)
            self.assertFalse(result["success"], payload)
            runtime.client.containers.run.assert_not_called()

        with self.assertRaisesRegex(ValueError, "traversal"):
            DockerRuntimeAdapter.normalize_snapshot_path("/safe/../secret")

    def test_timeout_stops_then_removes_only_the_request_container(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("container wait timed out")
        runtime = self.runtime(container)

        result = runtime.run_runtime_job(
            "runtime",
            {"command": "/root/backup.sh", "timeout_seconds": 1},
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 124)
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_cancellation_stops_container_and_returns_canceled_without_leak(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("wait timed out")
        runtime = self.runtime(container)
        checks = iter((False, False, True))

        result = runtime.run_runtime_job(
            "runtime",
            {"command": "/root/backup.sh", "timeout_seconds": 2},
            cancel_check=lambda: next(checks),
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_repository_and_secret_values_are_redacted_from_logs_and_errors(self):
        repository = "https://user:repo-password@example.invalid/backups"
        secret = "restic-password-value"
        container = self.container(
            f"repository={repository} password={secret}".encode("utf-8")
        )
        runtime = self.runtime(container)
        payload = {
            "command": "restic snapshots --json",
            "environment": {
                "RESTIC_REPOSITORY": repository,
                "RESTIC_PASSWORD": secret,
            },
        }

        result = runtime.run_runtime_job("runtime", payload)

        self.assertTrue(result["success"])
        self.assertNotIn(repository, repr(result))
        self.assertNotIn(secret, repr(result))
        self.assertIn("<redacted", result["logs"])

        failing = self.runtime(self.container())
        failing.client.containers.run.side_effect = RuntimeError(f"cannot open {repository}: {secret}")
        error = failing.run_runtime_job("runtime", payload)
        self.assertNotIn(repository, repr(error))
        self.assertNotIn(secret, repr(error))

    def test_cache_path_isolated_by_target_and_repository_fingerprint(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            runtime = self.runtime(self.container(logs=b"[]"), cache_dir=cache_dir)
            payload = {
                "target_id": "target-a",
                "command": "restic snapshots --json",
                "environment": {"RESTIC_REPOSITORY": "https://example.invalid/repo-a"},
            }
            runtime.run_runtime_job("runtime", payload)
            first = runtime.client.containers.run.call_args.kwargs["volumes"]
            first_source = next(source for source, spec in first.items() if spec["bind"] == runtime.CACHE_MOUNT_PATH)
            first_command = runtime.client.containers.run.call_args.kwargs["command"]

            runtime.client.containers.run.reset_mock()
            runtime.run_runtime_job(
                "runtime",
                {
                    **payload,
                    "target_id": "target-b",
                    "environment": {"RESTIC_REPOSITORY": "https://example.invalid/repo-b"},
                },
            )
            second = runtime.client.containers.run.call_args.kwargs["volumes"]
            second_source = next(source for source, spec in second.items() if spec["bind"] == runtime.CACHE_MOUNT_PATH)

            self.assertNotEqual(first_source, second_source)
            self.assertTrue(first_source.endswith(os.path.join("target-a", DockerRuntimeAdapter.repository_fingerprint("https://example.invalid/repo-a"))))
            self.assertNotIn("https://example.invalid/repo-a", first_source)
            self.assertIn("--cache-dir", first_command)

    def test_orphan_sweep_only_removes_stopped_marked_runtime_containers(self):
        runtime = self.runtime(self.container())

        marked = self.container()
        marked.id = "marked-container"
        marked.status = "exited"
        marked.labels = {
            DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
            DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: "job-1",
        }
        unmarked = self.container()
        unmarked.id = "unmarked-container"
        unmarked.status = "exited"
        unmarked.labels = {"user.container": "true"}
        running = self.container()
        running.id = "running-container"
        running.status = "running"
        running.labels = {
            DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
            DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: "job-2",
        }
        runtime.client.containers.list.return_value = [marked, unmarked, running]

        result = runtime.cleanup_orphaned_runtime_containers()

        runtime.client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": f"{DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL}=true"},
        )
        self.assertEqual(result["inspected"], 3)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 2)
        marked.stop.assert_not_called()
        marked.remove.assert_called_once_with(force=True)
        unmarked.remove.assert_not_called()
        running.remove.assert_not_called()
        self.assertEqual(result["removed_ids"], ["marked-container"])

    def test_dump_zip_output_is_allowlisted_and_bounded(self):
        small = self.runtime(self.container(logs=b"zip-content"))
        result = small.run_runtime_job_binary(
            "runtime",
            {
                "command": "restic dump -a zip abcdef12 /directory",
                "max_output_bytes": 32,
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["stdout_bytes"], b"zip-content")

        large = self.runtime(self.container(logs=b"0123456789"))
        result = large.run_runtime_job_binary(
            "runtime",
            {
                "command": "restic dump -a zip abcdef12 /directory",
                "max_output_bytes": 4,
            },
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 413)
        self.assertEqual(result["stdout_bytes"], b"")


class WorkerReadPathTests(unittest.TestCase):
    def test_snapshot_parser_is_bounded(self):
        entries = "\n".join(
            '{"struct_type":"node","type":"file","path":"/file-%s"}' % index
            for index in range(50)
        )
        parsed = WorkerAgentService._parse_snapshot_ls_entries(entries, max_entries=7)
        self.assertEqual(len(parsed), 7)
        self.assertEqual(parsed[0]["path"], "/file-0")

    def test_storage_about_is_an_interactive_command(self):
        self.assertIn("storage.about", WorkerAgentService.INTERACTIVE_COMMANDS)

    def test_storage_about_success_returns_whitelisted_metrics(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": '{"total":100,"used":25,"free":75,"trashed":2}',
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["state"], "available")
        self.assertEqual(
            result.result_summary["metrics"],
            {"total": 100, "used": 25, "free": 75, "trashed": 2},
        )
        self.assertNotIn("error", result.result_summary)
        runtime.run_runtime_job.assert_called_once()
        launched_payload = runtime.run_runtime_job.call_args.kwargs["payload"]
        self.assertEqual(launched_payload["command"], ["rclone", "about", "rem:", "--json"])

    def test_storage_about_rejects_invalid_remote_without_launch(self):
        runtime = Mock()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        for remote in ("", "rem", "not a remote", "..", "rem:/path", "rem:$(id)"):
            with self.subTest(remote=remote):
                result = service.execute_job(
                    {"command": "storage.about", "payload": {"remote": remote}}
                )
                self.assertEqual(result.status, JobStatus.FAILED)
                self.assertIn("invalid", result.result_summary["error"])
                runtime.run_runtime_job.assert_not_called()

    def test_storage_about_malformed_json_returns_safe_failure_summary(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": "not json at all",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["state"], "transient-failure")
        self.assertIn("failed to parse", result.result_summary["error"])
        self.assertNotIn("metrics", result.result_summary)

    def test_storage_about_unsupported_remote_is_explicit_not_transient(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 1,
            "logs": "rclone about: about is not supported by this backend",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["state"], "about-unsupported")
        self.assertNotIn("metrics", result.result_summary)
        self.assertNotIn("error", result.result_summary)

    def test_storage_about_transient_failure_is_retryable_and_redacts_secrets(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 124,
            "logs": "rclone about timed out after about-secret",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "storage.about",
                "payload": {"remote": "rem:", "environment": {"RCLONE_CONF_CONTENT": "about-secret"}},
            }
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["state"], "transient-failure")
        self.assertIn("timed out", result.result_summary["error"])
        self.assertNotIn("about-secret", repr(result))
        self.assertNotIn("about-secret", "\n".join(result.log_lines))

    def test_interactive_poll_falls_back_to_durable_fetch(self):
        class CredentialStore:
            def load(self):
                return object()

        class Client:
            credential_store = CredentialStore()

            def __init__(self):
                self.fetched = 0
                self.updated = []

            def fetch_jobs(self, worker_id):
                self.fetched += 1
                return [{"id": "job-1", "command": "snapshot.ls", "payload": {}, "lease_token": "lease"}]

            def update_job_status(self, **kwargs):
                self.updated.append(kwargs)
                return kwargs

        client = Client()
        runtime = Mock()
        runtime.run_runtime_job.return_value = {"success": True, "logs": "[]", "stderr": ""}
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            runtime,
        )

        result = service.poll_interactive_once()

        self.assertEqual(len(result), 1)
        self.assertEqual(client.fetched, 1)
        self.assertEqual(client.updated[0]["status"], JobStatus.SUCCEEDED)

    def test_canceled_job_is_not_left_as_a_running_lease(self):
        class CancelRuntime:
            def run_runtime_job(self, image, payload, cancel_check=None):
                self.observed = bool(cancel_check and cancel_check())
                return {"success": False, "canceled": True, "status_code": 130, "logs": "", "stderr": ""}

        class Client:
            credential_store = None

            def update_job_status(self, **kwargs):
                raise ValueError("job is canceled")

        cancel_runtime = CancelRuntime()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Client(),
            cancel_runtime,
        )
        job = {
            "id": "job-1",
            "command": "snapshot.ls",
            "payload": {"_cancel_check": lambda: True},
            "lease_token": "lease",
        }

        result = service.execute_job(job)

        self.assertEqual(result.status, JobStatus.CANCELED)
        self.assertTrue(cancel_runtime.observed)

    def test_restore_failure_preserves_redacted_runtime_error_and_stderr(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 2,
            "error": "restore failed with restore-secret",
            "stderr": "stderr contains restore-secret",
            "logs": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "restore.run",
                "payload": {
                    "environment": {"RESTIC_PASSWORD": "restore-secret"},
                },
            }
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["error"], "restore failed with <redacted>")
        self.assertEqual(result.result_summary["stderr"], "stderr contains <redacted>")
        self.assertTrue(result.log_lines)
        self.assertNotIn("restore-secret", "\n".join(result.log_lines))

    def test_restore_failure_without_runtime_output_has_fallback_log(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {"success": False, "status_code": 1, "logs": "", "stderr": ""}
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job({"command": "restore.run", "payload": {}})

        self.assertEqual(result.log_lines, ["Restore runtime failed without logs."])

    def test_worker_renews_long_running_job_when_client_supports_it(self):
        class Client:
            def __init__(self):
                self.renewals = []

            def renew_job_lease(self, worker_id, job_id, lease_token):
                self.renewals.append((worker_id, job_id, lease_token))
                return {"status": JobStatus.IN_PROGRESS}

            def update_job_status(self, **kwargs):
                return kwargs

        client = Client()
        runtime = Mock()

        def run_runtime_job(**kwargs):
            time.sleep(0.05)
            return {"success": True, "status_code": 0, "logs": "done", "stderr": ""}

        runtime.run_runtime_job.side_effect = run_runtime_job
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            runtime,
        )
        service.JOB_LEASE_RENEWAL_INTERVAL_SECONDS = 0.01

        service._process_jobs(
            "worker",
            [{"id": "job-1", "command": "backup.run", "payload": {}, "lease_token": "lease-token"}],
        )

        self.assertTrue(client.renewals)
        self.assertEqual(client.renewals[0], ("worker", "job-1", "lease-token"))

    def test_control_plane_cancellation_probe_is_bounded_and_exception_safe(self):
        class Client:
            credential_store = None

            def __init__(self):
                self.calls = []

            def is_job_cancelled(self, worker_id, job_id):
                self.calls.append((worker_id, job_id))
                return True

        client = Client()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            Mock(),
        )
        check = service._cancellation_check({"id": "job-1", "payload": {}})

        self.assertTrue(check())
        self.assertTrue(check())
        self.assertEqual(client.calls, [("worker", "job-1")])


class WorkerMainPollingTests(unittest.TestCase):
    def test_worker_labels_json_remains_available_to_service_configuration(self):
        from src.worker_agent import main as worker_main

        with patch.dict(os.environ, {"WORKER_LABELS": '{"lane":"interactive"}'}, clear=False):
            self.assertEqual(worker_main._labels_from_env(), {"lane": "interactive"})

    def test_build_service_derives_recovery_file_beside_worker_credentials(self):
        from src.worker_agent import main as worker_main

        with tempfile.TemporaryDirectory() as directory:
            credential_file = os.path.join(directory, "worker_credentials.json")
            with patch.object(worker_main, "DockerRuntimeAdapter", return_value=Mock()), patch.object(
                worker_main.RedisSnapshotCache, "from_env", return_value=None
            ), patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": credential_file}, clear=True):
                service = worker_main.build_service()

            self.assertEqual(
                str(service.recovery_journal.path),
                os.path.join(directory, "worker_job_recovery.json"),
            )

    def test_main_poll_helper_falls_back_when_fast_lane_is_unavailable(self):
        from src.worker_agent import main as worker_main

        service = Mock()
        service.poll_interactive_once.side_effect = NotImplementedError()
        service.poll_once.return_value = ["durable"]

        result = worker_main._poll_worker(service)

        self.assertEqual(result, ["durable"])
        service.poll_once.assert_called_once_with()

    def test_interactive_interval_is_sub_second_by_default(self):
        from src.worker_agent import main as worker_main

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS", None)
            self.assertLess(worker_main._bounded_interval("WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS", 0.5, 0.1, 5.0), 1.0)

    def test_runtime_orphan_sweep_interval_uses_default_and_safe_bounds(self):
        from src.worker_agent import main as worker_main

        variable = "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 15.0)
        with patch.dict(os.environ, {variable: "5"}, clear=True):
            self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 5.0)
        with patch.dict(os.environ, {variable: "3600"}, clear=True):
            self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 3600.0)
        for value in ("4", "3601", "not-a-number"):
            with self.subTest(value=value), patch.dict(os.environ, {variable: value}, clear=True):
                self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 15.0)

    def test_main_runs_runtime_orphan_sweep_once_after_registration(self):
        from src.worker_agent import main as worker_main

        service = Mock()
        service.config = WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker")
        service.ensure_registered.return_value = "worker"
        service.cleanup_orphaned_runtime_containers.return_value = {
            "inspected": 2,
            "removed": 1,
            "failed": 0,
            "skipped": 1,
        }
        service.poll_interactive_once.return_value = []

        with patch.object(worker_main, "build_service", return_value=service), patch.object(worker_main, "start_health_server"), patch.dict(
            os.environ,
            {
                "WORKER_RUN_ONCE": "true",
                "WORKER_HEALTH_PORT": "0",
                "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS": "15",
            },
            clear=False,
        ):
            worker_main.main()

        service.cleanup_orphaned_runtime_containers.assert_called_once_with()

    def test_main_repeats_runtime_orphan_sweep_only_after_interval(self):
        from src.worker_agent import main as worker_main

        service = Mock()
        service.config = WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker")
        service.ensure_registered.return_value = "worker"
        sweep_times = []

        def sweep():
            sweep_times.append(clock.value)
            return {"inspected": 0, "removed": 0, "failed": 0, "skipped": 0}

        service.cleanup_orphaned_runtime_containers.side_effect = sweep
        service.poll_interactive_once.return_value = []

        class Clock:
            value = None

            def __init__(self):
                self._values = iter((0.0, 0.0, 5.0, 5.0, 16.0, 16.0))

            def monotonic(self):
                self.value = next(self._values)
                return self.value

        clock = Clock()
        sleep_calls = []

        def controlled_sleep(delay):
            sleep_calls.append(delay)
            if len(sleep_calls) == 3:
                raise StopIteration("stop controlled worker loop")

        with patch.object(worker_main, "build_service", return_value=service), patch.object(worker_main, "start_health_server"), patch.object(
            worker_main.time, "monotonic", side_effect=clock.monotonic
        ), patch.object(worker_main.time, "sleep", side_effect=controlled_sleep), patch.dict(
            os.environ,
            {
                "WORKER_RUN_ONCE": "false",
                "WORKER_HEALTH_PORT": "0",
                "WORKER_POLL_INTERVAL_SECONDS": "3600",
                "WORKER_INVENTORY_SYNC_INTERVAL_SECONDS": "3600",
                "WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS": "0.1",
                "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS": "15",
            },
            clear=True,
        ):
            with self.assertRaises(StopIteration):
                worker_main.main()

        self.assertEqual(sweep_times, [0.0, 16.0])


class WorkerRestartRecoveryTests(unittest.TestCase):
    @staticmethod
    def runtime_for(container):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.list.return_value = [container]
        return runtime

    @staticmethod
    def container(job_id, status="exited", status_code=0, logs=b""):
        container = Mock()
        container.id = f"runtime-{job_id}"
        container.status = status
        container.labels = {
            DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
            DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: job_id,
        }
        container.wait.return_value = {"StatusCode": status_code}
        container.logs.return_value = logs
        return container

    @staticmethod
    def job(job_id="job-restore", command="restore.run"):
        return {
            "id": job_id,
            "command": command,
            "payload": {"environment": {"RESTIC_PASSWORD": "runtime-secret"}},
            "lease_token": "lease-token-never-logged",
        }

    class RecordingClient:
        def __init__(self, failure=None):
            self.updates = []
            self.failure = failure

        def update_job_status(self, **kwargs):
            self.updates.append(kwargs)
            if self.failure:
                failure = self.failure
                self.failure = None
                raise failure
            return {"status": kwargs["status"]}

    def service(self, client, recovery_file):
        return WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            self.runtime_for(self.container("unrelated")),
            recovery_file=recovery_file,
        )

    def test_worker_loss_recovers_exited_restore_once_with_redacted_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "worker_job_recovery.json")

            class CrashRuntime:
                def run_runtime_job(self, image, payload, cancel_check=None):
                    raise KeyboardInterrupt()

            first_service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                self.RecordingClient(),
                CrashRuntime(),
                recovery_file=recovery_file,
            )
            with self.assertLogs("src.worker_agent.application.services.worker_agent_service", level="INFO") as captured:
                with self.assertRaises(KeyboardInterrupt):
                    first_service._process_jobs("worker", [self.job()])
            self.assertNotIn("lease-token-never-logged", "\n".join(captured.output))
            with open(recovery_file, encoding="utf-8") as journal_stream:
                self.assertIn("lease-token-never-logged", journal_stream.read())

            container = self.container("job-restore", logs=b"restore completed lease-token-never-logged\n")
            client = self.RecordingClient()
            runtime = self.runtime_for(container)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                runtime,
                recovery_file=recovery_file,
            )

            result = service.cleanup_orphaned_runtime_containers()

            self.assertEqual(result["removed"], 1)
            self.assertEqual(client.updates[0]["status"], JobStatus.SUCCEEDED)
            self.assertEqual(client.updates[0]["result_summary"]["recovery"], "worker_restart_recovered")
            self.assertIn("restore completed", "\n".join(client.updates[0]["log_lines"]))
            self.assertNotIn("lease-token-never-logged", "\n".join(client.updates[0]["log_lines"]))
            self.assertFalse(os.path.exists(recovery_file))
            container.remove.assert_called_once_with(force=True)

            runtime.client.containers.list.return_value = []
            service.cleanup_orphaned_runtime_containers()
            self.assertEqual(len(client.updates), 1)

    def test_failed_runtime_recovery_is_terminal_and_has_useful_fallback_log(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-restore", "worker", "restore.run", "lease-token"
            )
            container = self.container("job-restore", status_code=7)
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            self.assertEqual(client.updates[0]["status"], JobStatus.FAILED)
            self.assertEqual(client.updates[0]["result_summary"]["status_code"], 7)
            self.assertIn("exited with status code 7", "\n".join(client.updates[0]["log_lines"]))
            self.assertFalse(os.path.exists(recovery_file))

    def test_snapshots_list_recovery_rebuilds_bounded_catalog_result(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-snapshots", "worker", "snapshots.list", "lease-token"
            )
            container = self.container(
                "job-snapshots",
                logs=b'[{"short_id":"abcdef12","time":"2026-08-15T00:00:00"}]',
            )
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            self.assertEqual(client.updates[0]["status"], JobStatus.SUCCEEDED)
            self.assertEqual(client.updates[0]["result_summary"]["snapshots"][0]["short_id"], "abcdef12")

    def test_running_orphan_is_retained_for_a_later_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-restore", "worker", "restore.run", "lease-token"
            )
            container = self.container("job-restore", status="running")
            client = self.RecordingClient()
            runtime = self.runtime_for(container)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                runtime,
                recovery_file=recovery_file,
            )

            result = service.cleanup_orphaned_runtime_containers()

            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["retained"], 0)
            client.update_job_status = Mock()
            container.remove.assert_not_called()
            container.logs.assert_not_called()
            self.assertIsNotNone(WorkerJobRecoveryJournal(recovery_file).load())

    def test_transient_control_plane_failure_retains_container_and_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-restore", "worker", "restore.run", "lease-token"
            )
            container = self.container("job-restore", logs=b"restore completed")
            client = self.RecordingClient(RuntimeError("network unavailable"))
            runtime = self.runtime_for(container)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                runtime,
                recovery_file=recovery_file,
            )

            first = service.cleanup_orphaned_runtime_containers()
            self.assertEqual(first["retained"], 1)
            container.remove.assert_not_called()
            self.assertIsNotNone(WorkerJobRecoveryJournal(recovery_file).load())

            second = service.cleanup_orphaned_runtime_containers()
            self.assertEqual(second["removed"], 1)
            self.assertEqual(len(client.updates), 2)
            self.assertFalse(os.path.exists(recovery_file))

    def test_mismatched_or_stale_journal_never_reuses_a_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "different-job", "other-worker", "restore.run", "wrong-lease"
            )
            container = self.container("job-restore", logs=b"restore completed")
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            client_updates = client.updates
            self.assertEqual(client_updates, [])
            container.remove.assert_called_once_with(force=True)

    def test_stale_journal_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            with open(recovery_file, "w", encoding="utf-8") as journal_stream:
                json.dump(
                    {
                        "version": 1,
                        "job_id": "job-restore",
                        "worker_id": "worker",
                        "command": "restore.run",
                        "lease_token": "old-lease",
                        "created_at": "2000-01-01T00:00:00+00:00",
                    },
                    journal_stream,
                )
            container = self.container("job-restore", logs=b"restore completed")
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            self.assertEqual(client.updates, [])
            container.remove.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
