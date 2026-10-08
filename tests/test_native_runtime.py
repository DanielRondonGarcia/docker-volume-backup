import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


class NativeRuntimeAdapterTests(unittest.TestCase):
    def test_backup_payload_uses_filesystem_paths_json_without_mount_rewrite(self):
        adapter = NativeRuntimeAdapter(python_executable="python-test")
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/data with spaces", "C:\\Users\\Alice\\My Documents"],
            "environment": {"BACKUP_STRATEGY": "tar"},
            "command": "/root/backup.sh",
            "backup_mode": "hot",
        }
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            recorded["env"] = kwargs["env"]
            process = Mock()
            process.stdout.readline.side_effect = ["Backup starting\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/bin/python"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused-image", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["python-test", "-m", "src.app.main"])
        self.assertEqual(recorded["env"]["BACKUP_RUNTIME_TYPE"], "native")
        self.assertEqual(recorded["env"]["BACKUP_SOURCES_JSON"], '["/srv/data with spaces","C:\\\\Users\\\\Alice\\\\My Documents"]')
        self.assertNotIn("/backup", recorded["env"].get("BACKUP_SOURCES", ""))

    def test_frozen_backup_payload_uses_current_executable_backup_engine_subcommand(self):
        adapter = NativeRuntimeAdapter(python_executable="python-test")
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "environment": {"BACKUP_STRATEGY": "tar"},
            "command": "/root/backup.sh",
            "backup_mode": "hot",
        }
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            recorded["env"] = kwargs["env"]
            process = Mock()
            process.stdout.readline.side_effect = ["Backup starting\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.sys.executable", "vaultline-worker.exe"), patch.object(
            sys, "frozen", True, create=True
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="vaultline-worker.exe"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused-image", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["vaultline-worker.exe", "backup-engine"])
        self.assertEqual(recorded["env"]["BACKUP_RUNTIME_TYPE"], "native")
        self.assertEqual(recorded["env"]["BACKUP_SOURCES_JSON"], '["/srv/app"]')

    def test_native_backup_rejects_cold_mode_before_process_launch(self):
        adapter = NativeRuntimeAdapter()
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen") as popen:
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "backup_mode": "cold", "filesystem_paths": ["/srv/app"]},
            )

        self.assertFalse(result["success"])
        self.assertIn("hot-only", result["error"])
        popen.assert_not_called()

    def test_native_runtime_reports_missing_executable_clearly(self):
        adapter = NativeRuntimeAdapter(python_executable="missing-python")
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=None), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen"
        ) as popen:
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"},
            )

        self.assertFalse(result["success"])
        self.assertIn("required executable 'missing-python' is not available", result["error"])
        popen.assert_not_called()

    def test_native_runtime_supports_restic_metadata_commands(self):
        adapter = NativeRuntimeAdapter()
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "environment": {"RESTIC_REPOSITORY": "local:/repo", "RESTIC_PASSWORD": "secret"},
            "command": ["restic", "snapshots", "--json"],
        }
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            process = Mock()
            process.stdout.readline.side_effect = ['[{"id":"abc"}]\n', ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.list_restic_snapshots("unused-image", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["restic", "snapshots", "--json"])
        self.assertEqual(result["snapshots"], [{"id": "abc"}])

    def test_native_runtime_timeout_terminates_process_without_waiting_long(self):
        adapter = NativeRuntimeAdapter(timeout_seconds=0.2, python_executable=sys.executable)
        process = Mock()
        process.stdout.readline.side_effect = ["working\n", ""]
        process.stderr.readline.side_effect = [""]
        process.wait.return_value = 0
        process.returncode = None

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", return_value=process
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.time.monotonic", side_effect=[100.0, 100.3]):
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"},
            )

        self.assertFalse(result["success"])
        self.assertFalse(result["canceled"])
        self.assertEqual(result["status_code"], 124)
        self.assertIn("timed out after 0.2 seconds", result["error"])
        process.terminate.assert_called()
        process.kill.assert_not_called()

    def test_native_runtime_timeout_reports_cleanup_failure_when_kill_wait_does_not_finish(self):
        adapter = NativeRuntimeAdapter(timeout_seconds=0.2, python_executable=sys.executable)
        process = Mock()
        process.stdout.readline.side_effect = ["working\n", ""]
        process.stderr.readline.side_effect = [""]
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["python"], 2),
            subprocess.TimeoutExpired(["python"], 2),
        ]
        process.returncode = None

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", return_value=process
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.time.monotonic", side_effect=[100.0, 100.3]):
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"},
            )

        self.assertFalse(result["success"])
        self.assertFalse(result["canceled"])
        self.assertEqual(result["status_code"], 124)
        self.assertIn("timed out after 0.2 seconds", result["error"])
        self.assertIn("could not confirm process termination", result["error"])
        self.assertFalse(result["process_terminated"])
        process.terminate.assert_called()
        process.kill.assert_called()

    def test_native_runtime_cancellation_stops_process_and_reports_canceled(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        process = Mock()
        process.stdout.readline.side_effect = ["working\n", ""]
        process.stderr.readline.side_effect = [""]
        process.wait.return_value = 0
        process.returncode = 0

        cancel_calls = {"count": 0}

        def cancel_after_launch():
            cancel_calls["count"] += 1
            return cancel_calls["count"] > 1

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", return_value=process
        ):
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"},
                cancel_check=cancel_after_launch,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        process.terminate.assert_called()


if __name__ == "__main__":
    unittest.main()
