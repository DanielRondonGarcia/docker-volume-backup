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

    def execute_snapshot_ls(self, logs, payload=None):
        service = self.build_worker_service(logs)
        result = service.execute_job({"command": "snapshot.ls", "payload": payload or {}})
        return result

    def test_large_ndjson_listing_keeps_root_entries_before_log_limit(self):
        root = {"struct_type": "node", "type": "dir", "path": "/"}
        top_level = {"struct_type": "node", "type": "dir", "path": "/top-level"}
        files = [
            {"struct_type": "node", "type": "file", "path": f"/file-{index}"}
            for index in range(1001)
        ]
        logs = "\n".join(json.dumps(entry) for entry in [root, top_level, *files])

        result = self.execute_snapshot_ls(logs)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["entries"][:2], [root, top_level])
        self.assertEqual(len(result.result_summary["entries"]), 1003)
        self.assertEqual(len(result.log_lines), 1000)

    def test_browse_filters_requested_path_before_visible_limit(self):
        requested_path = "/backup/baget/packages/packages"
        unrelated = [
            {
                "struct_type": "node",
                "type": "file",
                "path": f"{requested_path}/Old.Package.{index}/1.0.0/Old.Package.{index}.nupkg",
            }
            for index in range(200)
        ]
        nested_package_file = {
            "struct_type": "node",
            "type": "file",
            "path": f"{requested_path}/Nested.Package/1.0.0/Nested.Package.1.0.0.nupkg",
        }
        package = {
            "struct_type": "node",
            "type": "file",
            "path": f"{requested_path}/Acme.Library.1.0.0.nupkg",
        }
        logs = "\n".join(json.dumps(entry) for entry in [*unrelated, nested_package_file, package])

        result = self.execute_snapshot_ls(
            logs,
            {"path": requested_path, "max_entries": 200},
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertNotIn("error", result.result_summary)
        self.assertEqual(result.result_summary["entries"], [package])

    def test_json_array_listing_is_parsed(self):
        root = {"struct_type": "node", "type": "dir", "path": "/"}
        file_entry = {"type": "file", "path": "/document.txt"}
        informational = {"message": "listing completed"}

        result = self.execute_snapshot_ls(json.dumps([root, file_entry, informational]))

        self.assertEqual(result.result_summary["entries"], [root, file_entry])

    def test_malformed_and_informational_lines_do_not_create_entries(self):
        logs = "\n".join(
            [
                "INFO listing started",
                '{"message":"listing completed"}',
                '{"type":"file"',
                "INFO listing ended",
            ]
        )

        result = self.execute_snapshot_ls(logs)

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["entries"], [])

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


if __name__ == "__main__":
    unittest.main()
