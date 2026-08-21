import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.application.services.job_event_broker import JobEventBroker
from src.control_plane.auth import ROLE_VIEWER
from src.control_plane.domain.models import JobStatus, WorkerRecord, utcnow
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryInventoryRepository,
    InMemoryJobRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetRepository,
    InMemoryTargetStatsRepository,
    InMemoryWorkerRepository,
)
from src.control_plane.main import ControlPlaneRequestHandler


class JobEventBrokerTests(unittest.TestCase):
    def test_publish_is_bounded_and_keeps_the_newest_event(self):
        broker = JobEventBroker(max_queue_size=2)
        subscription = broker.subscribe("job-1")

        publisher = threading.Thread(
            target=lambda: [broker.publish("job-1", {"sequence": sequence}) for sequence in range(1000)]
        )
        publisher.start()
        publisher.join(timeout=1)

        self.assertFalse(publisher.is_alive())
        self.assertEqual(broker.subscriber_count("job-1"), 1)
        queued = [subscription.get(timeout=1), subscription.get(timeout=1)]
        self.assertEqual(queued[-1]["sequence"], 999)

        subscription.close()
        self.assertEqual(broker.subscriber_count("job-1"), 0)

    def test_publish_does_not_cross_job_subscriptions(self):
        broker = JobEventBroker(max_queue_size=1)
        subscription = broker.subscribe("job-1")

        broker.publish("job-2", {"id": "job-2"})
        self.assertEqual(broker.subscriber_count("job-2"), 0)
        broker.publish("job-1", {"id": "job-1"})
        self.assertEqual(subscription.get(timeout=1)["id"], "job-1")
        subscription.close()


class JobEventServiceTests(unittest.TestCase):
    @staticmethod
    def make_service(broker):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="host", id="worker-a", status="online", last_seen_at=utcnow()))
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
            job_event_broker=broker,
        )

    def assert_safe_view(self, view):
        serialized = json.dumps(view, default=str)
        self.assertNotIn("payload", view)
        self.assertNotIn("lease_token", view)
        for secret in ("private-password", "private-token", "rclone-content"):
            self.assertNotIn(secret, serialized)

    def test_claim_progress_and_terminal_status_publish_after_persistence(self):
        broker = JobEventBroker()
        service = self.make_service(broker)
        job = service.dispatch_job(
            "worker-a",
            "backup.run",
            payload={
                "RESTIC_PASSWORD": "private-password",
                "ACCESS_TOKEN": "private-token",
                "RCLONE_CONF_CONTENT": "rclone-content",
            },
        )
        subscription = broker.subscribe(job.id)
        submitted_updated_at = job.updated_at

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        claim_event = subscription.get(timeout=1)
        self.assertEqual(claim_event["status"], JobStatus.IN_PROGRESS)
        self.assertGreaterEqual(claim_event["updated_at"], submitted_updated_at)
        self.assert_safe_view(claim_event)

        updated = service.update_job_progress(
            "worker-a",
            job.id,
            sequence=1,
            progress={"phase": "backup", "percent_done": 25},
            log_lines=["safe progress"],
            lease_token=claimed.lease_token,
        )
        progress_event = subscription.get(timeout=1)
        self.assertEqual(progress_event["progress"]["percent_done"], 25)
        self.assertEqual(progress_event["log_lines"][-1], "safe progress")
        self.assertGreaterEqual(progress_event["updated_at"], claimed.updated_at)
        self.assertEqual(progress_event["updated_at"], updated.updated_at)
        self.assert_safe_view(progress_event)

        service.update_job_status(
            "worker-a",
            job.id,
            JobStatus.FAILED,
            result_summary={"error": "password=private-password", "token": "private-token"},
            log_lines=["private-password", "terminal failure"],
            lease_token=claimed.lease_token,
        )
        terminal_event = subscription.get(timeout=1)
        self.assertEqual(terminal_event["status"], JobStatus.FAILED)
        self.assertIsNotNone(terminal_event["finished_at"])
        self.assert_safe_view(terminal_event)

        subscription.close()


class _FakeWFile:
    def __init__(self, disconnect_after=None):
        self.writes = []
        self.flush_count = 0
        self.disconnect_after = disconnect_after

    @property
    def body(self):
        return b"".join(self.writes)

    def write(self, value):
        self.writes.append(value)
        if self.disconnect_after is not None and len(self.writes) >= self.disconnect_after:
            raise OSError("client disconnected")
        return len(value)

    def flush(self):
        self.flush_count += 1


class _FakeJobService:
    def __init__(self, broker, view):
        self.job_event_broker = broker
        self.view = view

    def get_job_view(self, job_id):
        return self.view if self.view.get("id") == job_id else None


class JobEventRouteTests(unittest.TestCase):
    @staticmethod
    def view(status=JobStatus.IN_PROGRESS):
        return {
            "id": "job-1",
            "worker_id": "worker-a",
            "command": "backup.run",
            "status": status,
            "updated_at": str(time.time()),
            "finished_at": None,
            "result_summary": {"progress": {"phase": "backup"}},
            "progress": {"phase": "backup"},
            "log_lines": ["safe line"],
        }

    @staticmethod
    def handler(service, wfile=None, path="/api/v1/jobs/job-1/events"):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        handler.headers = {}
        handler.server = SimpleNamespace(application=SimpleNamespace(control_plane_service=service))
        handler._require_auth = Mock(return_value={"role": ROLE_VIEWER})
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = wfile or _FakeWFile()
        handler._write_json = Mock()
        return handler

    @staticmethod
    def wait_for(predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_route_requires_viewer_auth_before_streaming(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler._require_auth = Mock(return_value=None)
        handler._write_json = Mock()

        handler._stream_job_events("job-1")

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, api_mode=True)
        handler._write_json.assert_not_called()

    def test_logs_route_requires_viewer_auth(self):
        handler = self.handler(_FakeJobService(JobEventBroker(), self.view()), path="/api/v1/jobs/job-1/logs")
        handler._require_auth = Mock(return_value=None)

        handler._handle_get_request(head_only=False)

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, head_only=False, api_mode=True)
        handler._write_json.assert_not_called()

    def test_logs_route_returns_404_for_missing_job(self):
        handler = self.handler(
            _FakeJobService(JobEventBroker(), {"id": "other", "status": JobStatus.PENDING}),
            path="/api/v1/jobs/job-1/logs",
        )

        handler._handle_get_request(head_only=False)

        handler._write_json.assert_called_once_with(404, {"error": "job not found"}, head_only=False)

    def test_logs_route_returns_safe_full_detail_view(self):
        broker = JobEventBroker()
        service = JobEventServiceTests.make_service(broker)
        job = service.dispatch_job(
            "worker-a",
            "backup.run",
            payload={
                "RESTIC_PASSWORD": "private-password",
                "ACCESS_TOKEN": "private-token",
                "RCLONE_CONF_CONTENT": "rclone-content",
            },
        )
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            job.id,
            JobStatus.FAILED,
            result_summary={"error": "password=private-password", "token": "private-token"},
            log_lines=["private-password", "terminal failure"],
            lease_token=claimed.lease_token,
        )
        handler = self.handler(service, path=f"/api/v1/jobs/{job.id}/logs")

        handler._handle_get_request(head_only=False)

        status, view = handler._write_json.call_args.args[:2]
        self.assertEqual(status, 200)
        self.assertEqual(view["id"], job.id)
        self.assertIn("log_lines", view)
        serialized = json.dumps(view, default=str)
        self.assertNotIn("payload", view)
        self.assertNotIn("lease_token", view)
        for secret in ("private-password", "private-token", "rclone-content"):
            self.assertNotIn(secret, serialized)

    def test_logs_route_is_selected_separately_from_events_and_single_job_route(self):
        handler = self.handler(
            _FakeJobService(JobEventBroker(), self.view()),
            path="/api/v1/jobs/job-1/logs",
        )
        handler._stream_job_events = Mock()

        handler._handle_get_request(head_only=False)

        handler._stream_job_events.assert_not_called()
        self.assertEqual(handler._write_json.call_args.args[0], 200)

    def test_not_found_is_normal_json_before_subscribing(self):
        broker = JobEventBroker()
        service = _FakeJobService(broker, {"id": "other", "status": JobStatus.PENDING})
        handler = self.handler(service)

        handler._stream_job_events("job-1")

        handler._write_json.assert_called_once_with(404, {"error": "job not found"})
        self.assertEqual(broker.subscriber_count(), 0)
        handler.send_response.assert_not_called()

    def test_stream_sends_initial_snapshot_and_terminal_event_then_cleans_up(self):
        broker = JobEventBroker(max_queue_size=2)
        service = _FakeJobService(broker, self.view())
        handler = self.handler(service)
        stream_thread = threading.Thread(target=handler._stream_job_events, args=("job-1",))
        stream_thread.start()

        self.assertTrue(self.wait_for(lambda: b"data: " in handler.wfile.body))
        self.assertEqual(broker.subscriber_count("job-1"), 1)
        terminal = self.view(JobStatus.SUCCEEDED)
        terminal["finished_at"] = "finished"
        broker.publish("job-1", terminal)
        stream_thread.join(timeout=1)

        self.assertFalse(stream_thread.is_alive())
        self.assertEqual(broker.subscriber_count("job-1"), 0)
        headers = {call.args[0]: call.args[1] for call in handler.send_header.call_args_list}
        self.assertEqual(handler.send_response.call_args.args, (200,))
        self.assertEqual(headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Connection"], "keep-alive")
        frames = [
            json.loads(line[len("data: "):])
            for line in handler.wfile.body.decode("utf-8").splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual([frame["status"] for frame in frames], [JobStatus.IN_PROGRESS, JobStatus.SUCCEEDED])
        self.assertNotIn("lease_token", handler.wfile.body.decode("utf-8"))
        self.assertNotIn("payload", handler.wfile.body.decode("utf-8"))

    def test_stream_body_uses_only_the_service_safe_projection(self):
        broker = JobEventBroker(max_queue_size=4)
        service = JobEventServiceTests.make_service(broker)
        job = service.dispatch_job(
            "worker-a",
            "backup.run",
            payload={
                "RESTIC_PASSWORD": "private-password",
                "ACCESS_TOKEN": "private-token",
                "RCLONE_CONF_CONTENT": "rclone-content",
            },
        )
        handler = self.handler(service)
        stream_thread = threading.Thread(target=handler._stream_job_events, args=(job.id,))
        stream_thread.start()
        self.assertTrue(self.wait_for(lambda: broker.subscriber_count(job.id) == 1))

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a",
            job.id,
            JobStatus.FAILED,
            result_summary={"error": "password=private-password", "token": "private-token"},
            log_lines=["private-password", "terminal failure"],
            lease_token=claimed.lease_token,
        )
        stream_thread.join(timeout=1)

        self.assertFalse(stream_thread.is_alive())
        body = handler.wfile.body.decode("utf-8")
        for secret in ("private-password", "private-token", "rclone-content"):
            self.assertNotIn(secret, body)
        self.assertNotIn("\"payload\"", body)
        self.assertNotIn("\"lease_token\"", body)
        self.assertEqual(broker.subscriber_count(job.id), 0)

    def test_heartbeat_and_disconnect_cleanup(self):
        broker = JobEventBroker()
        service = _FakeJobService(broker, self.view())
        handler = self.handler(service, wfile=_FakeWFile(disconnect_after=2))
        with patch("src.control_plane.main.JOB_EVENT_HEARTBEAT_SECONDS", 0.01):
            stream_thread = threading.Thread(target=handler._stream_job_events, args=("job-1",))
            stream_thread.start()
            stream_thread.join(timeout=1)

        self.assertFalse(stream_thread.is_alive())
        self.assertIn(b": heartbeat\n\n", handler.wfile.body)
        self.assertEqual(broker.subscriber_count("job-1"), 0)

    def test_event_route_is_selected_before_single_job_route(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/jobs/job-1/events"
        handler.headers = {}
        handler._stream_job_events = Mock()

        handler._handle_get_request(head_only=False)

        handler._stream_job_events.assert_called_once_with("job-1")


if __name__ == "__main__":
    unittest.main()
