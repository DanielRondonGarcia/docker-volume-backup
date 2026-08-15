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

    def execute_snapshot_ls(self, logs):
        service = self.build_worker_service(logs)
        result = service.execute_job({"command": "snapshot.ls", "payload": {}})
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


if __name__ == "__main__":
    unittest.main()
