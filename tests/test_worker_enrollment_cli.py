import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from src.security.hmac_protocol import digest_secret
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


class WorkerEnrollmentCLITests(unittest.TestCase):
    def run_enroll(self, directory, token="t" * 32, client_factory=None, argv=None):
        from src.worker_agent import cli

        output = io.StringIO()
        errors = io.StringIO()
        credential_path = os.path.join(directory, "credentials.json")
        env = {
            "CONTROL_PLANE_URL": "http://control-plane.invalid",
            "WORKER_CREDENTIAL_FILE": credential_path,
        }
        patches = [
            patch.dict(os.environ, env, clear=True),
            patch("getpass.getpass", return_value=token),
        ]
        if client_factory is not None:
            patches.append(patch("src.worker_agent.cli.ControlPlaneClient", side_effect=client_factory))
        with patches[0], patches[1]:
            client_patch = patches[2] if len(patches) > 2 else None
            if client_patch:
                with client_patch:
                    with redirect_stdout(output), redirect_stderr(errors):
                        code = cli.main(argv or ["enroll"])
            else:
                with redirect_stdout(output), redirect_stderr(errors):
                    code = cli.main(argv or ["enroll"])
        return code, output.getvalue(), errors.getvalue(), credential_path

    def test_enroll_prompts_hidden_and_pending_exists_before_http_call(self):
        calls = []

        with tempfile.TemporaryDirectory() as directory:
            credential_path = os.path.join(directory, "credentials.json")

            class FakeClient:
                def __init__(self, base_url, **kwargs):
                    self.store = kwargs["credential_store"]

                def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                    pending = self.store.load_pending_enrollment()
                    self.store.save("worker-cli", credential_secret, "3")
                    calls.append((bootstrap_secret, attempt_id, credential_secret, pending))
                    return {"worker_id": "worker-cli", "credential_version": "3"}

            with patch.dict(os.environ, {"CONTROL_PLANE_URL": "http://cp", "WORKER_CREDENTIAL_FILE": credential_path}, clear=True), \
                patch("getpass.getpass", return_value="bootstrap-token-value-1234567890"), \
                patch("src.worker_agent.cli.ControlPlaneClient", side_effect=FakeClient) as client_class, \
                redirect_stdout(io.StringIO()) as output:
                from src.worker_agent import cli
                code = cli.main(["enroll"])

            self.assertEqual(code, 0)
            self.assertIn("Enrollment completed", output.getvalue())
            client_class.assert_called_once()
            self.assertEqual(calls[0][0], "bootstrap-token-value-1234567890")
            self.assertEqual(calls[0][3].attempt_id, calls[0][1])
            self.assertEqual(calls[0][3].durable_credential, calls[0][2])
            self.assertEqual(calls[0][3].token_digest, digest_secret("bootstrap-token-value-1234567890"))
            self.assertIsNone(WorkerCredentialStore(credential_path).load_pending_enrollment())

    def test_retry_same_token_reuses_exact_attempt_and_credential_after_network_failure(self):
        sent = []

        class FakeClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                sent.append((bootstrap_secret, attempt_id, credential_secret, labels))
                if len(sent) == 1:
                    raise OSError("network unavailable")
                self.store.save("worker-retry", credential_secret, "1")
                return {"worker_id": "worker-retry", "credential_version": "1"}

        with tempfile.TemporaryDirectory() as directory:
            first = self.run_enroll(directory, client_factory=FakeClient)
            self.assertEqual(first[0], 1)
            self.assertIsNotNone(WorkerCredentialStore(first[3]).load_pending_enrollment())
            second = self.run_enroll(directory, client_factory=FakeClient)

        self.assertEqual(second[0], 0)
        self.assertEqual(sent[0], sent[1])

    def test_different_token_with_pending_is_rejected_without_sending_or_leaking_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            store.save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))

            class FakeClient:
                def __init__(self, *args, **kwargs):
                    raise AssertionError("HTTP client must not be created for mismatched pending token")

            code, out, err, _ = self.run_enroll(directory, token="b" * 32, client_factory=FakeClient)
            still_pending = store.load_pending_enrollment()

        rendered = out + err
        self.assertEqual(code, 2)
        self.assertIn("pending enrollment", rendered)
        self.assertIn("reset", rendered)
        self.assertNotIn("b" * 32, rendered)
        self.assertIsNotNone(still_pending)

    def test_pending_file_never_contains_plaintext_token_and_is_removed_on_success(self):
        token = "plaintext-token-value-1234567890!!"
        snapshots = []

        class FakeClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                snapshots.append(self.store.pending_path.read_text(encoding="utf-8"))
                self.store.save("worker-clean", credential_secret, "1")
                return {"worker_id": "worker-clean", "credential_version": "1"}

        with tempfile.TemporaryDirectory() as directory:
            code, out, err, path = self.run_enroll(directory, token=token, client_factory=FakeClient)
            store = WorkerCredentialStore(path)
            self.assertEqual(code, 0)
            self.assertFalse(store.pending_path.exists())
            self.assertNotIn(token, snapshots[0])
            self.assertNotIn(token, out + err)
            self.assertEqual(store.load().worker_id, "worker-clean")

    def test_reset_pending_is_explicit_and_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            WorkerCredentialStore(path).save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))
            env = {"WORKER_CREDENTIAL_FILE": path}
            from src.worker_agent import cli
            with patch.dict(os.environ, env, clear=True), patch("getpass.getpass") as getpass_prompt, redirect_stdout(io.StringIO()) as output:
                code = cli.main(["enroll", "--reset-pending"])
            self.assertEqual(code, 0)
            self.assertIn("Pending enrollment reset", output.getvalue())
            getpass_prompt.assert_not_called()
            self.assertIsNone(WorkerCredentialStore(path).load_pending_enrollment())

    def test_pending_file_uses_restrictive_posix_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX mode assertion is not meaningful on Windows")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            store.save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))
            self.assertEqual(stat.S_IMODE(os.stat(store.pending_path).st_mode), 0o600)

    def test_windows_acl_restriction_uses_icacls_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            with patch("src.worker_agent.infrastructure.security.credential_store.os.name", "nt"), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run") as run:
                store.save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))
            self.assertTrue(run.called)
            args = run.call_args.args[0]
            self.assertEqual(args[0].lower(), "icacls")
            self.assertIn(str(store.pending_path), args)

            with patch("src.worker_agent.infrastructure.security.credential_store.os.name", "nt"), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=OSError("icacls missing")):
                with self.assertRaises(PermissionError):
                    store.save_pending_enrollment("attempt-abcdef123456", "d" * 32, digest_secret("a" * 32))

    def test_windows_acl_failure_removes_empty_temp_before_secret_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            secret = "d" * 32
            writes = []
            real_dump = json.dump

            def record_dump(payload, stream, *args, **kwargs):
                writes.append(stream.name)
                return real_dump(payload, stream, *args, **kwargs)

            with patch("src.worker_agent.infrastructure.security.credential_store.os.name", "nt"), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=OSError("icacls missing")) as run, \
                patch("src.worker_agent.infrastructure.security.credential_store.json.dump", side_effect=record_dump):
                with self.assertRaises(PermissionError):
                    store.save_pending_enrollment("attempt-abcdef123456", secret, digest_secret("a" * 32))

            temp_path = os.path.join(directory, f".{os.path.basename(store.pending_path)}.tmp")
            run.assert_called_once()
            self.assertEqual(writes, [])
            self.assertFalse(os.path.exists(temp_path))
            for name in os.listdir(directory):
                candidate = os.path.join(directory, name)
                if os.path.isfile(candidate):
                    with open(candidate, encoding="utf-8") as stream:
                        self.assertNotIn(secret, stream.read())


if __name__ == "__main__":
    unittest.main()
