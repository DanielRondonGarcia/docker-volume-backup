import os
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter

class RuntimeProcessBoundaryTests(unittest.TestCase):
    def runtime(self, container):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        return runtime
    @staticmethod
    def container():
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = b"ok"
        return container
    @staticmethod
    def secret_payload(rclone_secret="rclone-secret-value", general_secret="general-secret-value"):
        return {
            "command": "restic snapshots --json",
            "environment": {
                "RESTIC_REPOSITORY": "rclone:remote:/backups",
                "RCLONE_CONF_CONTENT": rclone_secret,
            },
            "resolved_files": [
                {"secret_name": "backup-key", "container_path": "/run/secrets/backup-key", "content": general_secret},
                {"secret_name": "rclone.conf", "container_path": "/run/secrets/rclone.conf", "content": rclone_secret},
            ],
        }
    def test_unsupported_runtime_command_is_rejected_before_launch(self):
        container = self.container()
        runtime = self.runtime(container)
        result = runtime.run_runtime_job("runtime", {"command": ["python", "-c", "dangerous"], "timeout_seconds": 10})
        self.assertFalse(result["success"])
        self.assertIn("unsupported", result["error"])
        runtime.client.containers.run.assert_not_called()
    def test_shell_metacharacters_are_rejected_without_shell_execution(self):
        container = self.container()
        runtime = self.runtime(container)
        result = runtime.run_runtime_job("runtime", {"command": "/root/backup.sh; touch /tmp/runtime-pwned", "timeout_seconds": 10})
        self.assertFalse(result["success"])
        self.assertIn("shell metacharacters", result["error"])
        self.assertNotIn("/bin/sh -c", repr(result))
        runtime.client.containers.run.assert_not_called()
    def test_timeout_returns_failure_and_forces_container_cleanup(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("container wait timed out")
        runtime = self.runtime(container)
        observed_sources = []
        def launch(**kwargs):
            observed_sources.extend(source for source, spec in kwargs["volumes"].items() if spec["bind"] in {"/run/secrets", "/run/rclone-config"})
            return container
        runtime.client.containers.run.side_effect = launch
        payload = self.secret_payload()
        payload["command"] = "/root/backup.sh"
        payload["timeout_seconds"] = 1
        with patch("src.worker_agent.infrastructure.adapters.docker_runtime.os.path.exists", return_value=False):
            result = runtime.run_runtime_job("runtime", payload)
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 124)
        self.assertIn("timed out", result["error"])
        container.wait.assert_called_once_with(timeout=1.0)
        container.remove.assert_called_once_with(force=True)
        self.assertEqual(len(observed_sources), 2)
        self.assertTrue(all(not os.path.exists(source) for source in observed_sources))

    def test_execution_failure_cleans_secret_temp_dirs(self):
        secret = "execution-failure-secret"
        container = self.container()
        container.wait.side_effect = RuntimeError("container failed")
        runtime = self.runtime(container)
        observed_sources = []
        def launch(**kwargs):
            observed_sources.extend(source for source, spec in kwargs["volumes"].items() if spec["bind"] in {"/run/secrets", "/run/rclone-config"})
            return container
        runtime.client.containers.run.side_effect = launch
        with patch("src.worker_agent.infrastructure.adapters.docker_runtime.os.path.exists", return_value=False):
            result = runtime.run_runtime_job("runtime", self.secret_payload(secret, "general-" + secret))
        self.assertFalse(result["success"])
        self.assertNotIn(secret, repr(result))
        self.assertEqual(len(observed_sources), 2)
        self.assertTrue(all(not os.path.exists(source) for source in observed_sources))

    def test_secret_files_are_private_read_only_and_redacted(self):
        rclone_secret = "rclone-secret-value"
        general_secret = "general-secret-value"
        container = self.container()
        container.logs.return_value = (rclone_secret + general_secret).encode()
        runtime = self.runtime(container)
        observed = {}
        def launch(**kwargs):
            observed.update(environment=kwargs["environment"].copy(), command=kwargs["command"])
            mounts = {spec["bind"]: (source, spec["mode"]) for source, spec in kwargs["volumes"].items()}
            secrets_source, secrets_mode = mounts["/run/secrets"]
            rclone_source, rclone_mode = mounts["/run/rclone-config"]
            with open(os.path.join(rclone_source, "rclone.conf"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), rclone_secret)
            with open(os.path.join(secrets_source, "secret_1"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), general_secret)
            observed.update(
                mounts=mounts,
                rclone_permissions=stat.S_IMODE(os.stat(os.path.join(rclone_source, "rclone.conf")).st_mode),
                rclone_dir_permissions=stat.S_IMODE(os.stat(rclone_source).st_mode),
                secrets_dir_permissions=stat.S_IMODE(os.stat(secrets_source).st_mode),
            )
            self.assertEqual(secrets_mode, "ro")
            self.assertEqual(rclone_mode, "rw")
            return container
        runtime.client.containers.run.side_effect = launch
        with patch("src.worker_agent.infrastructure.adapters.docker_runtime.os.path.exists", return_value=False), patch("src.worker_agent.infrastructure.adapters.docker_runtime.os.chmod", wraps=os.chmod) as chmod:
            result = runtime.run_runtime_job("runtime", self.secret_payload(rclone_secret, general_secret))
        mounts = observed["mounts"]
        rclone_source = mounts["/run/rclone-config"][0]
        secrets_source = mounts["/run/secrets"][0]
        self.assertEqual(observed["command"], ["restic", "snapshots", "--json"])
        self.assertEqual(observed["environment"]["RCLONE_CONFIG"], "/run/rclone-config/rclone.conf")
        self.assertNotIn("RCLONE_CONF_CONTENT", observed["environment"])
        self.assertEqual(mounts["/run/secrets"][1], "ro")
        self.assertEqual(mounts["/run/rclone-config"][1], "rw")
        self.assertNotEqual(rclone_source, secrets_source)
        self.assertEqual(os.path.dirname(rclone_source), tempfile.gettempdir())
        self.assertEqual(os.path.dirname(secrets_source), tempfile.gettempdir())
        if os.name != "nt":
            self.assertEqual(observed["rclone_permissions"], 0o600)
            self.assertEqual(observed["rclone_dir_permissions"], 0o700)
            self.assertEqual(observed["secrets_dir_permissions"], 0o700)
        else:
            self.assertTrue(any(call.args[1] == 0o600 for call in chmod.call_args_list))
            self.assertTrue(any(call.args[1] == 0o700 for call in chmod.call_args_list))
        self.assertNotIn(rclone_secret, repr(observed["environment"]))
        self.assertNotIn(general_secret, repr(observed["environment"]))
        self.assertNotIn(rclone_secret, repr(result))
        self.assertNotIn(general_secret, repr(result))
        self.assertFalse(os.path.exists(rclone_source))
        self.assertFalse(os.path.exists(secrets_source))

    def test_secret_temp_dirs_are_cleaned_when_preparation_fails(self):
        temp_dirs = []
        real_mkdtemp = tempfile.mkdtemp
        def record_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            temp_dirs.append(path)
            return path
        runtime = self.runtime(self.container())
        with patch("src.worker_agent.infrastructure.adapters.docker_runtime.tempfile.mkdtemp", side_effect=record_mkdtemp), patch.object(DockerRuntimeAdapter, "_write_secret", side_effect=OSError("secret write failed")), patch("src.worker_agent.infrastructure.adapters.docker_runtime.os.path.exists", return_value=False):
            result = runtime.run_runtime_job("runtime", self.secret_payload())
        self.assertFalse(result["success"])
        self.assertEqual(len(temp_dirs), 2)
        self.assertTrue(all(not os.path.exists(path) for path in temp_dirs))
        runtime.client.containers.run.assert_not_called()
if __name__ == "__main__":
    unittest.main()
