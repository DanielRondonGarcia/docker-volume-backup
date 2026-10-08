import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


class WorkerNativeCLITests(unittest.TestCase):
    def test_daemon_defaults_to_native_continuous_worker_main(self):
        from src.worker_agent import cli

        calls = []

        with patch.dict(os.environ, {}, clear=True), patch(
            "src.worker_agent.main.main", side_effect=lambda: calls.append("main")
        ):
            exit_code = cli.main(["daemon"])
            self.assertEqual(os.environ["WORKER_RUNTIME"], "native")
            self.assertEqual(os.environ["WORKER_RUN_ONCE"], "false")

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["main"])

    def test_daemon_once_flag_is_explicit_debug_mode(self):
        from src.worker_agent import cli

        with patch.dict(os.environ, {"WORKER_RUNTIME": "docker", "WORKER_RUN_ONCE": "false"}, clear=True), patch(
            "src.worker_agent.main.main"
        ) as worker_main:
            exit_code = cli.main(["daemon", "--once"])
            self.assertEqual(os.environ["WORKER_RUNTIME"], "native")
            self.assertEqual(os.environ["WORKER_RUN_ONCE"], "true")

        self.assertEqual(exit_code, 0)
        worker_main.assert_called_once_with()

    def test_self_check_reports_presence_without_secret_values(self):
        from src.worker_agent import cli

        output = io.StringIO()
        secret_value = "super-secret-token-value"
        env = {
            "CONTROL_PLANE_URL": "https://cp.example.invalid/private-path",
            "WORKER_ENROLLMENT_TOKEN": secret_value,
            "WORKER_CREDENTIAL_FILE": "missing-worker-credentials.json",
            "WORKER_ID": "worker-private-id",
            "RESTIC_PASSWORD": "restic-secret",
        }

        with patch.dict(os.environ, env, clear=True), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.NativeRuntimeAdapter.self_check",
            return_value={
                "native_available": True,
                "runtime_type": "native",
                "executables": {"restic": False, "rclone": True},
            },
        ):
            with redirect_stdout(output):
                exit_code = cli.main(["self-check"])

        payload = json.loads(output.getvalue())
        rendered = json.dumps(payload, sort_keys=True)
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["configuration"]["control_plane_url_configured"])
        self.assertTrue(payload["configuration"]["enrollment_token_configured"])
        self.assertTrue(payload["configuration"]["worker_id_configured"])
        self.assertFalse(payload["configuration"]["credential_file_exists"])
        self.assertEqual(payload["runtime"]["executables"], {"restic": False, "rclone": True})
        self.assertNotIn(secret_value, rendered)
        self.assertNotIn("cp.example", rendered)
        self.assertNotIn("worker-private-id", rendered)
        self.assertNotIn("restic-secret", rendered)


if __name__ == "__main__":
    unittest.main()
