import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import BackupTargetRecord, JobStatus
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig


class SnapshotBrowserTests(unittest.TestCase):
    def build_worker_service(self, logs):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {"success": True, "logs": logs, "stderr": ""}
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )
        return service

    def execute_snapshot(self, command, logs, payload=None, runtime_result=None):
        service = self.build_worker_service(logs)
        if runtime_result is not None:
            service.docker_runtime.run_runtime_job.return_value = runtime_result
        result = service.execute_job({"command": command, "payload": payload or {}})
        return result

    def test_direct_listing_returns_direct_children_without_scanning_descendant_like_data(self):
        entries = [
            {"struct_type": "node", "type": "dir", "path": f"/folder-{index}"}
            for index in range(50)
        ]

        result = self.execute_snapshot(
            "snapshot.ls",
            "\n".join(json.dumps(entry) for entry in entries),
            {"path": "/", "max_entries": 200},
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertTrue(result.result_summary["listing_complete"])
        self.assertEqual(result.result_summary["listing_mode"], "direct")
        self.assertEqual(result.result_summary["listing_entry_count"], 50)
        self.assertEqual(len(result.result_summary["entries"]), 50)
        self.assertEqual(result.result_summary["entries"][0]["path"], "/folder-0")
        self.assertEqual(result.result_summary["entries"][0]["type"], "dir")

    def test_direct_listing_filters_requested_path_and_preserves_safe_metadata(self):
        requested_path = "/backup/baget/packages/packages"
        result = self.execute_snapshot(
            "snapshot.ls",
            "\n".join(
                json.dumps(entry)
                for entry in (
                    {
                        "struct_type": "node",
                        "type": "file",
                        "path": f"{requested_path}/Acme.Library.1.0.0.nupkg",
                        "size": 1234,
                    },
                    {
                        "struct_type": "node",
                        "type": "dir",
                        "path": f"{requested_path}/Nested.Package",
                    },
                )
            ),
            {"path": requested_path, "max_entries": 200},
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(
            result.result_summary["entries"],
            [
                {
                    "struct_type": "node",
                    "type": "file",
                    "path": f"{requested_path}/Acme.Library.1.0.0.nupkg",
                    "size": 1234,
                },
                {
                    "struct_type": "node",
                    "type": "dir",
                    "path": f"{requested_path}/Nested.Package",
                },
            ],
        )

    def test_json_array_listing_is_parsed(self):
        root = {"struct_type": "node", "type": "dir", "path": "/"}
        file_entry = {"type": "file", "path": "/document.txt"}
        informational = {"message": "listing completed"}

        result = self.execute_snapshot("snapshot.search", json.dumps([root, file_entry, informational]))

        self.assertEqual(result.result_summary["entries"], [root, file_entry])

    def test_configured_metadata_log_limit_preserves_entries_after_former_boundary(self):
        entry = {"struct_type": "node", "type": "file", "path": "/after-limit"}
        logs = "x" * (WorkerAgentService.MAX_LOG_CHARS + 16) + "\n" + json.dumps(entry)

        result = self.execute_snapshot(
            "snapshot.search",
            logs,
            {"max_log_bytes": 8 * 1024 * 1024},
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["entries"], [entry])

    def test_empty_direct_listing_is_a_confirmed_success(self):
        result = self.execute_snapshot("snapshot.ls", "", {"path": "/"})

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["entries"], [])
        self.assertTrue(result.result_summary["listing_complete"])
        self.assertEqual(result.result_summary["listing_entry_count"], 0)

    def test_runtime_listing_failure_is_failed_and_not_an_empty_success(self):
        result = self.execute_snapshot(
            "snapshot.ls",
            "runtime failure",
            {"path": "/"},
            runtime_result={
                "success": False,
                "status_code": 1,
                "error": "runtime failure",
                "logs": "runtime failure",
                "stderr": "",
            },
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["entries"], [])
        self.assertFalse(result.result_summary["listing_complete"])
        self.assertEqual(result.result_summary["listing_error_code"], "runtime_failure")

    def test_snapshot_about_projects_safe_restore_size_stats_without_raw_runtime_output(self):
        runtime = Mock()
        runtime.get_restic_snapshot_stats.return_value = {
            "success": True,
            "status_code": 0,
            "stats": {
                "total_size": 4096,
                "total_file_count": 7,
                "snapshots_count": 1,
                "repository": "local:/private",
            },
            "logs": '{"repository":"local:/private"}',
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "snapshot.about",
                "payload": {
                    "target_id": "target-a",
                    "snapshot_id": "abcdef12",
                    "environment": {"RESTIC_REPOSITORY": "local:/private"},
                },
            }
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(
            result.result_summary["stats"],
            {"total_size": 4096, "total_file_count": 7, "snapshots_count": 1},
        )
        self.assertNotIn("repository", result.result_summary["stats"])
        self.assertNotIn("local:/private", repr(result))

    def test_snapshot_about_rejects_malformed_or_incomplete_stats(self):
        runtime = Mock()
        runtime.get_restic_snapshot_stats.return_value = {
            "success": True,
            "status_code": 0,
            "stats": {"total_size": 1, "total_file_count": "7"},
            "logs": "raw output must not cross the boundary",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "snapshot.about",
                "payload": {
                    "target_id": "target-a",
                    "snapshot_id": "abcdef12",
                    "environment": {"RESTIC_REPOSITORY": "local:/private"},
                },
            }
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["error"], "snapshot stats JSON is invalid")
        self.assertNotIn("raw output must not cross the boundary", repr(result))

    def test_timeout_and_output_limit_are_failed_with_bounded_diagnostics(self):
        for status_code, error_code, message in (
            (124, "timeout", "runtime timed out after 30 seconds"),
            (413, "output_limit", "runtime logs exceeded the permitted limit"),
        ):
            with self.subTest(status_code=status_code):
                result = self.execute_snapshot(
                    "snapshot.ls",
                    "",
                    {"path": "/", "max_log_bytes": 8 * 1024 * 1024},
                    runtime_result={
                        "success": False,
                        "status_code": status_code,
                        "error": message,
                        "logs": message,
                        "stderr": "",
                    },
                )

                self.assertEqual(result.status, JobStatus.FAILED)
                self.assertEqual(result.result_summary["status_code"], status_code)
                self.assertEqual(result.result_summary["listing_error_code"], error_code)
                self.assertFalse(result.result_summary["listing_complete"])

    def test_missing_repository_config_is_actionable_and_secret_safe_for_metadata(self):
        repository = "local:/sensitive/repository"
        password = "restic-password-value"
        rclone_config = "[remote]\npassword = rclone-secret-value"
        raw_error = (
            f"Fatal: unable to open config file: {repository}/config does not exist. "
            f"Is there a repository at the following location? {repository} "
            f"{password} {rclone_config}"
        )
        runtime = Mock()
        failure = {
            "success": False,
            "status_code": 1,
            "error": raw_error,
            "logs": raw_error,
            "stderr": "",
            "snapshots": [],
        }
        runtime.run_runtime_job.return_value = failure
        runtime.list_restic_snapshots.return_value = failure
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )
        payload = {
            "environment": {
                "RESTIC_REPOSITORY": repository,
                "RESTIC_PASSWORD": password,
                "RCLONE_CONF_CONTENT": rclone_config,
            }
        }

        for command in ("snapshots.list", "snapshot.ls", "snapshot.search", "snapshot.find"):
            with self.subTest(command=command):
                result = service.execute_job({"command": command, "payload": payload})

                self.assertEqual(result.status, JobStatus.FAILED)
                self.assertEqual(
                    result.result_summary["error"],
                    WorkerAgentService.MISSING_RESTIC_REPOSITORY_ERROR,
                )
                self.assertEqual(result.log_lines, [WorkerAgentService.MISSING_RESTIC_REPOSITORY_ERROR])
                safe_output = "\n".join(result.log_lines) + repr(result.result_summary)
                self.assertNotIn(repository, safe_output)
                self.assertNotIn(password, safe_output)
                self.assertNotIn(rclone_config, safe_output)

    def test_restic_authentication_failure_is_classified_and_secret_safe_for_catalog_and_listing(self):
        repository = "local:/sensitive/repository"
        password = "restic-password-value"
        key = "restic-encryption-key-value"
        raw_error = f"Fatal: wrong password or no key found ({repository} {password} {key})"
        redacted_error = "Fatal: wrong <redacted> or no key found"
        runtime = Mock()
        failure = {
            "success": False,
            "status_code": 1,
            "error": raw_error,
            "logs": redacted_error,
            "stderr": "",
            "snapshots": [],
        }
        runtime.run_runtime_job.return_value = failure
        runtime.list_restic_snapshots.return_value = failure
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )
        payload = {
            "environment": {
                "RESTIC_REPOSITORY": repository,
                "RESTIC_PASSWORD": password,
                "RESTIC_KEY": key,
            }
        }

        self.assertEqual(
            service._classify_snapshot_runtime_error("snapshot.ls", payload, {"error": redacted_error}),
            WorkerAgentService.RESTIC_AUTHENTICATION_ERROR,
        )
        for command in ("snapshots.list", "snapshot.ls"):
            with self.subTest(command=command):
                result = service.execute_job({"command": command, "payload": payload})

                self.assertEqual(result.status, JobStatus.FAILED)
                self.assertEqual(
                    result.result_summary["error"],
                    WorkerAgentService.RESTIC_AUTHENTICATION_ERROR,
                )
                self.assertEqual(
                    result.result_summary["listing_error_code"],
                    WorkerAgentService.RESTIC_AUTHENTICATION_ERROR_CODE,
                )
                self.assertEqual(result.log_lines, [WorkerAgentService.RESTIC_AUTHENTICATION_ERROR])
                safe_output = "\n".join(result.log_lines) + repr(result.result_summary)
                self.assertNotIn(repository, safe_output)
                self.assertNotIn(password, safe_output)
                self.assertNotIn(key, safe_output)
                self.assertNotIn(raw_error, safe_output)

    def test_generic_snapshot_runtime_error_remains_sanitized_and_available(self):
        repository = "local:/sensitive/repository"
        password = "restic-password-value"
        generic_error = f"Fatal: unable to access {repository}: {password}"
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 2,
            "error": generic_error,
            "logs": generic_error,
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "snapshot.ls",
                "payload": {"environment": {"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password}},
            }
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["error"], "Fatal: unable to access <redacted>: <redacted>")
        self.assertIn("Fatal: unable to access", "\n".join(result.log_lines))
        self.assertNotIn(repository, repr(result))
        self.assertNotIn(password, repr(result))

    def test_control_plane_prefers_structured_entries(self):
        structured_entries = [{"struct_type": "node", "type": "dir", "path": "/root"}]
        service = object.__new__(ControlPlaneService)
        service._require_target = Mock(return_value=BackupTargetRecord(name="target", worker_id="worker"))
        service._build_snapshot_ls_payload = Mock(return_value={})
        service.dispatch_job = Mock(return_value=SimpleNamespace(id="job-1"))
        service._wait_for_job_completion = Mock(
            return_value={
                "status": JobStatus.SUCCEEDED,
                "result_summary": {"entries": structured_entries},
                "logs": json.dumps([{"type": "file", "path": "/stale-log-entry"}]),
            }
        )

        result = service.dispatch_snapshot_ls("target", "snapshot")

        self.assertEqual(result["entries"], structured_entries)
        self.assertEqual(result["job_id"], "job-1")

    def test_control_plane_prefers_structured_error_over_last_log_line(self):
        service = object.__new__(ControlPlaneService)
        service._require_target = Mock(return_value=BackupTargetRecord(name="target", worker_id="worker"))
        service._build_snapshot_ls_payload = Mock(return_value={})
        service.dispatch_job = Mock(return_value=SimpleNamespace(id="job-1"))
        service._wait_for_job_completion = Mock(
            return_value={
                "status": JobStatus.FAILED,
                "result_summary": {"error": "Restic repository is not initialized."},
                "logs": "Fatal: unable to open config file: raw terminal detail",
            }
        )

        result = service.dispatch_snapshot_ls("target", "snapshot")

        self.assertEqual(result["error"], "restic ls failed: Restic repository is not initialized.")
        self.assertNotIn("raw terminal detail", result["error"])

    def test_legacy_snapshot_listing_marks_incomplete_result_as_failed(self):
        service = object.__new__(ControlPlaneService)
        service._require_target = Mock(return_value=BackupTargetRecord(name="target", worker_id="worker"))
        service._build_snapshot_ls_payload = Mock(return_value={})
        service.dispatch_job = Mock(return_value=SimpleNamespace(id="job-1"))
        service._wait_for_job_completion = Mock(
            return_value={
                "status": JobStatus.SUCCEEDED,
                "result_summary": {
                    "entries": [],
                    "listing_mode": "direct",
                    "listing_complete": False,
                },
                "logs": "",
            }
        )

        result = service.dispatch_snapshot_ls("target", "snapshot")

        self.assertEqual(result["status"], JobStatus.FAILED)
        self.assertEqual(result["error"], "snapshot listing was incomplete")
        self.assertFalse(result["listing_complete"])


if __name__ == "__main__":
    unittest.main()
