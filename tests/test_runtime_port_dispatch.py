import unittest
from unittest.mock import Mock

from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.ports.runtime_port import RuntimePort
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter


class RuntimePortDispatchTests(unittest.TestCase):
    @staticmethod
    def docker_runtime():
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.timeout_seconds = 30.0
        runtime.no_lock = False
        runtime.cache_dir = None
        return runtime

    def test_rejected_argv_creates_no_runtime_job(self):
        runtime = self.docker_runtime()
        self.assertIsInstance(runtime, RuntimePort)
        self.assertEqual(runtime.runtime_kind, "docker")

        cases = (
            (["python", "-c", "dangerous"], "unsupported"),
            ("/root/backup.sh; touch /tmp/runtime-pwned", "shell metacharacters"),
        )
        for command, expected_error in cases:
            with self.subTest(command=command):
                result = runtime.run_runtime_job("runtime", {"command": command})

                self.assertFalse(result["success"])
                self.assertIn(expected_error, result["error"])

        runtime.client.containers.run.assert_not_called()

    def test_service_dispatches_backup_through_runtime_port(self):
        runtime = Mock(spec=RuntimePort)
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": "backup complete",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "id": "job-1",
                "command": "backup.run",
                "payload": {
                    "image": "runtime",
                    "command": "/root/backup.sh",
                    "environment": {"RESTIC_REPOSITORY": "s3:https://example.invalid/backups"},
                },
            }
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        runtime.run_runtime_job.assert_called_once()
        self.assertEqual(runtime.run_runtime_job.call_args.kwargs["image"], "runtime")
        self.assertEqual(runtime.run_runtime_job.call_args.kwargs["payload"]["_job_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
