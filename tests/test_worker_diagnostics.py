import io
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from src.worker_agent.infrastructure.api_client.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneHTTPError,
)
from src.worker_agent.main import _classify_worker_loop_error, _format_worker_loop_error


class ControlPlaneHTTPDiagnosticsTests(unittest.TestCase):
    def test_json_http_error_becomes_bounded_typed_rejection(self):
        client = ControlPlaneClient("https://control-plane.example", timeout_seconds=2)
        response_error = HTTPError(
            "https://control-plane.example/api/v1/workers/worker-1/jobs/fetch?token=private",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"worker is revoked","code":"worker_revoked"}'),
        )

        with patch(
            "src.worker_agent.infrastructure.api_client.control_plane_client.request.urlopen",
            side_effect=response_error,
        ):
            with self.assertRaises(ControlPlaneHTTPError) as raised:
                client._post("/api/v1/workers/worker-1/jobs/fetch", {}, authenticate=False)

        error = raised.exception
        self.assertEqual(error.code, 400)
        self.assertEqual(error.status, 400)
        self.assertEqual(error.method, "POST")
        self.assertEqual(error.path, "/api/v1/workers/worker-1/jobs/fetch")
        self.assertEqual(error.server_code, "worker_revoked")
        self.assertIn("HTTP 400", str(error))
        self.assertIn("/api/v1/workers/worker-1/jobs/fetch", str(error))
        self.assertIn("worker is revoked", str(error))
        self.assertIn("code=worker_revoked", str(error))
        self.assertNotIn("control-plane.example", str(error))
        self.assertNotIn("private", str(error))

    def test_empty_and_non_json_http_error_details_are_safe_and_bounded(self):
        for body in (b"", b"not-json " + (b"x" * 2000)):
            with self.subTest(body_is_empty=not body):
                client = ControlPlaneClient("https://control-plane.example")
                response_error = HTTPError(
                    "https://control-plane.example/api/v1/workers/worker-1/jobs/fetch",
                    502,
                    "Bad Gateway",
                    {},
                    io.BytesIO(body),
                )

                with patch(
                    "src.worker_agent.infrastructure.api_client.control_plane_client.request.urlopen",
                    side_effect=response_error,
                ):
                    with self.assertRaises(ControlPlaneHTTPError) as raised:
                        client._post("/api/v1/workers/worker-1/jobs/fetch", {}, authenticate=False)

                error = raised.exception
                self.assertLessEqual(len(error.detail), 512)
                self.assertIn("HTTP 502", str(error))
                if not body:
                    self.assertIn("Bad Gateway", str(error))
                self.assertNotIn("control-plane.example", str(error))

    def test_interactive_fetch_still_falls_back_for_wrapped_404(self):
        client = ControlPlaneClient.__new__(ControlPlaneClient)
        client._post = Mock(
            side_effect=[
                ControlPlaneHTTPError(
                    "POST",
                    "/api/v1/workers/worker-1/jobs/fetch-interactive",
                    404,
                    "endpoint not found",
                    "interactive_jobs_unsupported",
                ),
                {"items": [{"id": "durable-1"}]},
            ]
        )

        self.assertEqual(client.fetch_interactive_jobs("worker-1"), [{"id": "durable-1"}])
        self.assertEqual(client._post.call_count, 2)


class WorkerLoopDiagnosticsTests(unittest.TestCase):
    def test_classifier_distinguishes_control_plane_rejection(self):
        error = ControlPlaneHTTPError("POST", "/api/v1/workers/worker-1/jobs/fetch", 400, "invalid", "bad_request")

        self.assertEqual(_classify_worker_loop_error(error), "control_plane_rejection")
        self.assertIn("Control Plane rejected POST", _format_worker_loop_error(error))

    def test_classifier_distinguishes_dns_url_failure(self):
        error = URLError(socket.gaierror(-2, "Name or service not known"))

        self.assertEqual(_classify_worker_loop_error(error), "control_plane_connectivity")
        self.assertIn("could not connect to the Control Plane", _format_worker_loop_error(error))
        self.assertIn("Name or service not known", _format_worker_loop_error(error))

    def test_classifier_distinguishes_docker_runtime_failure(self):
        docker_error_type = type("APIError", (RuntimeError,), {"__module__": "docker.errors"})
        error = docker_error_type(
            "404 Client Error for http+docker://localhost/v1.41/images/create: No such image: sha256:private"
        )

        self.assertEqual(_classify_worker_loop_error(error), "docker_runtime")
        formatted = _format_worker_loop_error(error)
        self.assertIn("reached the Control Plane but the Docker runtime failed", formatted)
        self.assertIn("No such image", formatted)
        self.assertIn("http+docker://<redacted>", formatted)

    def test_classifier_distinguishes_generic_worker_failure(self):
        error = ValueError("unexpected worker state")

        self.assertEqual(_classify_worker_loop_error(error), "worker_loop")
        self.assertEqual(
            _format_worker_loop_error(error),
            "Worker loop failed (ValueError): unexpected worker state",
        )


if __name__ == "__main__":
    unittest.main()
