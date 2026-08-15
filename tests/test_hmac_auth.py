import os, stat, tempfile, time, unittest; from unittest.mock import patch
from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
from src.security.hmac_protocol import digest_secret, sign_request, verify_request
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore
class HmacAuthTests(unittest.TestCase):
    def setUp(self):
        self.secret, self.worker, self.body = "s" * 32, "worker-1", b'{"job":1}'; self.state = WorkerAuthState(); self.state.create_enrollment("worker", "host", {}, self.secret, self.worker); self.state.complete(self.secret)
    def signed(self, secret=None, worker=None, version="1", nonce="n", timestamp=None, body=None):
        t = str(timestamp or int(time.time())); w = worker or self.worker; b = body or self.body; return t, nonce, sign_request(digest_secret(secret or self.secret), "POST", "/api/v1/workers/worker-1/jobs?b=2&a=1", b, t, nonce, w, version)
    def test_protocol_and_replay(self):
        ts, nonce, signature = self.signed(); self.assertTrue(verify_request(digest_secret(self.secret), signature, "POST", "/api/v1/workers/worker-1/jobs?a=1&b=2", self.body, ts, nonce, self.worker, "1")); self.state.authenticate(self.worker, "POST", "/api/v1/workers/worker-1/jobs?b=2&a=1", self.body, ts, nonce, self.worker, "1", signature)
        for sig, wid, ver in ((self.signed(body=self.body + b"x")[2], self.worker, "1"), (self.signed(worker="other")[2], "other", "1"), (self.signed(version="2")[2], self.worker, "2")): self.assertRaises(ValueError, self.state.authenticate, self.worker, "POST", "/api/v1/workers/worker-1/jobs?b=2&a=1", self.body, ts, "bad", wid, ver, sig)
        self.assertRaises(ValueError, self.state.authenticate, self.worker, "POST", "/api/v1/workers/worker-1/jobs", self.body, "0", "new", self.worker, "1", signature)
    def test_enrollment_persistence_rotation_store(self):
        self.assertRaises(ValueError, self.state.complete, self.secret); self.state.rotate(self.worker, "r" * 32); self.state.revoke(self.worker); self.assertRaises(ValueError, self.state.authenticate, self.worker, "POST", "/", b"", str(int(time.time())), "revoked", self.worker, "2", "")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "credential.json"); store = WorkerCredentialStore(path); client = ControlPlaneClient("http://control-plane", credential_store=store, worker_id=None, enrollment_secret=self.secret); client.enroll_worker = lambda *args: {"worker_id": self.worker, "credential_version": "1"}; result = client.register_worker("worker", "host", "1", {}); self.assertNotIn("secret", result); self.assertEqual(client.worker_id, self.worker)
            with patch("src.worker_agent.infrastructure.api_client.control_plane_client.request.urlopen") as opener:
                opener.return_value.__enter__.return_value.read.return_value = b"{}"; client._post("/api/v1/workers/worker-1/heartbeat", {}); self.assertTrue(next(v for k, v in opener.call_args.args[0].header_items() if k.lower() == "x-worker-signature"))
            c = store.load(); ts, nonce, signature = self.signed(c.secret, nonce="persisted"); self.assertTrue(verify_request(digest_secret(c.secret), signature, "POST", "/api/v1/workers/worker-1/jobs?b=2&a=1", self.body, ts, nonce, self.worker, c.version)); self.assertEqual(c.worker_id, self.worker); self.assertTrue(self.state._consume_nonce(self.worker, "1", "n")); self.assertFalse(self.state._consume_nonce(self.worker, "1", "n"));
            if os.name != "nt": self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
    def test_startup_retry_cap(self):
        import src.worker_agent.main as worker_main; failed = type("Failed", (), {"config": type("Config", (), {"control_plane_url": "", "name": ""})(), "ensure_registered": lambda self: (_ for _ in ()).throw(OSError("down"))})()
        with patch.object(worker_main, "build_service", return_value=failed), patch.dict(os.environ, {"WORKER_RUN_ONCE": "true", "WORKER_HEALTH_PORT": "0", "WORKER_STARTUP_RETRY_DELAY_SECONDS": "0"}): self.assertRaisesRegex(RuntimeError, "5 attempts", worker_main.main)
    def test_documentation_like_input_is_not_executed(self):
        from unittest.mock import Mock
        from src.control_plane.domain.models import JobStatus
        from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
        from src.worker_agent.domain.models import WorkerAgentConfig
        runtime = Mock(); service = WorkerAgentService(WorkerAgentConfig("http://control-plane", "worker", "host"), Mock(), runtime)
        result = service.execute_job({"id": "docs", "command": "docker compose up -d", "payload": {"command": "rm -rf /"}})
        self.assertEqual(result.status, JobStatus.FAILED); self.assertIn("unsupported command", result.result_summary["error"])
        runtime.run_runtime_job.assert_not_called(); runtime.run_runtime_job_binary.assert_not_called()
if __name__ == "__main__": unittest.main()
