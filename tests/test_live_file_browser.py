import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import BackupTargetRecord, StorageProfileRecord, WorkerRecord, WorkerStatus, utcnow
from src.control_plane.application.services.live_file_service import LiveFileService, LiveSessionError, LiveSessionKey
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
from src.control_plane.infrastructure.repositories.sqlite import SQLiteTargetRepository
from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
from src.control_plane.main import ControlPlaneApplication, ControlPlaneHTTPServer, ControlPlaneRequestHandler, LiveWorkerError, LiveWorkerLane
from src.worker_agent.application.services.live_target_session_manager import LiveTargetSessionKey, LiveTargetSessionManager
from src.worker_agent.infrastructure.adapters.live_file_runtime import LiveAccessDeniedError, LiveFileRuntime, LiveFileSource
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.live_file_helper import PROTECTED_VOLUME_EXIT_CODE, list_entries, read_file, watch_snapshot


class LiveTargetFoundationTests(unittest.TestCase):
    @staticmethod
    def make_service():
        workers = InMemoryWorkerRepository()
        workers.save(
            WorkerRecord(
                name="worker-a",
                host_name="host-a",
                id="worker-a",
                status=WorkerStatus.ONLINE,
                last_seen_at=utcnow(),
            )
        )
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
        )

    def test_live_access_defaults_false_and_update_does_not_create_a_job(self):
        service = self.make_service()
        target = service.register_target("target-a", "worker-a")

        self.assertFalse(target.live_access_enabled)
        updated = service.update_target(target.id, live_access_enabled=True)

        self.assertTrue(updated.live_access_enabled)
        self.assertFalse(service.job_repository.list())
        self.assertTrue(service.is_live_target_eligible(target.id))

    def test_live_access_rejects_non_boolean_updates(self):
        service = self.make_service()
        target = service.register_target("target-a", "worker-a")

        with self.assertRaisesRegex(ValueError, "live_access_enabled must be a boolean"):
            service.update_target(target.id, live_access_enabled="true")

    def test_sqlite_flag_migrates_existing_rows_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "control-plane.db")
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE targets (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, worker_id TEXT NOT NULL,
                        compose_project TEXT, volume_targets_json TEXT NOT NULL,
                        backup_mode TEXT NOT NULL, backup_strategy TEXT NOT NULL,
                        runtime_image TEXT, runtime_command TEXT,
                        runtime_environment_json TEXT NOT NULL, runtime_volumes_json TEXT NOT NULL,
                        runtime_network_mode TEXT, storage_profile_id TEXT,
                        retention_policy_id TEXT, execution_policy_id TEXT,
                        restic_password_secret_id TEXT, restore_defaults_json TEXT NOT NULL,
                        labels_json TEXT NOT NULL, enabled INTEGER NOT NULL, cron_expression TEXT,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO targets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "target-a", "target-a", "worker-a", None, "[]", "hot", "restic", None, None,
                        "{}", "{}", None, None, None, None, None, "{}", "{}", 1, None,
                        datetime(2026, 1, 1).isoformat(), datetime(2026, 1, 1).isoformat(),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            repository = SQLiteTargetRepository(database_path)
            loaded = repository.get("target-a")
            self.assertFalse(loaded.live_access_enabled)

            loaded.live_access_enabled = True
            repository.save(loaded)
            self.assertTrue(repository.get("target-a").live_access_enabled)

    def test_revision_is_stable_and_excludes_secret_environment_values(self):
        service = self.make_service()
        timestamp = datetime(2026, 1, 1)
        target = BackupTargetRecord(
            name="target-a",
            worker_id="worker-a",
            id="target-a",
            live_access_enabled=True,
            runtime_environment={"RESTIC_PASSWORD": "first-secret", "RCLONE_CONF_CONTENT": "file-content"},
            volume_targets=["/data"],
            updated_at=timestamp,
        )
        service.target_repository.save(target)

        first = service.get_live_target_revision("target-a")
        second = service.get_live_target_revision("target-a")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in first))

        target.runtime_environment = {"RESTIC_PASSWORD": "second-secret", "RCLONE_CONF_CONTENT": "other-content"}
        target.updated_at = timestamp
        service.target_repository.save(target)
        self.assertEqual(first, service.get_live_target_revision("target-a"))
        self.assertNotIn("first-secret", json.dumps(service.get_live_target_context("target-a")))
        self.assertNotIn("file-content", json.dumps(service.get_live_target_context("target-a")))

    def test_revision_changes_for_mount_profile_and_worker_configuration(self):
        service = self.make_service()
        timestamp = datetime(2026, 1, 1)
        profile = StorageProfileRecord(name="profile-a", backend_type="local", id="profile-a", updated_at=timestamp)
        service.storage_profile_repository.save(profile)
        target = BackupTargetRecord(
            name="target-a",
            worker_id="worker-a",
            id="target-a",
            live_access_enabled=True,
            storage_profile_id="profile-a",
            volume_targets=["/data"],
            updated_at=timestamp,
        )
        service.target_repository.save(target)

        initial = service.get_live_target_revision("target-a")
        target.volume_targets = ["/data", "/config"]
        self.assertNotEqual(initial, service.get_live_target_revision("target-a"))

        target.updated_at = timestamp
        service.worker_repository.get("worker-a").version = "next"
        self.assertNotEqual(initial, service.get_live_target_revision("target-a"))

        profile.updated_at = datetime(2026, 1, 2)
        self.assertNotEqual(initial, service.get_live_target_revision("target-a"))

    def test_context_fails_closed_for_target_and_worker_eligibility_changes(self):
        service = self.make_service()
        target = service.register_target("target-a", "worker-a", live_access_enabled=True)
        self.assertTrue(service.get_live_target_context(target.id)["eligible"])

        target.enabled = False
        self.assertFalse(service.get_live_target_context(target.id)["eligible"])
        self.assertEqual(service.get_live_target_context(target.id)["reason"], "target_disabled")

        target.enabled = True
        worker = service.worker_repository.get("worker-a")
        worker.last_seen_at = None
        self.assertFalse(service.is_live_target_eligible(target.id))
        self.assertEqual(service.get_live_target_context(target.id)["reason"], "worker_offline")

        worker.status = WorkerStatus.DISABLED
        worker.last_seen_at = utcnow()
        self.assertFalse(service.is_live_target_eligible(target.id))
        self.assertEqual(service.get_live_target_context(target.id)["reason"], "worker_disabled")

    def test_control_plane_maps_only_known_worker_reasons_to_safe_statuses(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        for reason, code, status in (
            ("source_unavailable", "live_worker_unavailable", 503),
            ("helper_request_failed", "live_worker_unavailable", 503),
            ("invalid_source", "live_request_rejected", 400),
            ("protected_volume", "live_access_denied", 403),
        ):
            with self.assertRaises(LiveWorkerError) as raised:
                handler._raise_live_worker_error("/safe", {"status": status, "code": code, "reason": reason})
            failure = raised.exception
            self.assertEqual((failure.status, failure.code, failure.reason), (status, code, reason))
            payload = handler._live_worker_error_payload(failure)
            self.assertEqual((payload["code"], payload["reason"]), (code, reason))
            if reason != "protected_volume":
                self.assertNotIn("path", payload)

        with self.assertRaises(LiveSessionError):
            handler._raise_live_worker_error(
                "/safe",
                {"status": 500, "code": "worker_error", "reason": "raw_exception"},
            )


class LiveMountDescriptorTests(unittest.TestCase):
    @staticmethod
    def make_handler(service):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.server = SimpleNamespace(application=SimpleNamespace(control_plane_service=service))
        return handler

    def test_mount_descriptors_preserve_selection_order_and_use_destination_folders(self):
        service = LiveTargetFoundationTests.make_service()
        target = BackupTargetRecord(
            name="target-a",
            worker_id="worker-a",
            id="target-a",
            live_access_enabled=True,
            volume_targets=["/var/lib/postgresql/data", "/demo-files"],
            runtime_volumes={
                "demo-volume": {"bind": "/demo-files", "mode": "rw"},
                "postgres-volume": {"bind": "/var/lib/postgresql/data", "mode": "rw"},
            },
        )
        service.target_repository.save(target)
        handler = self.make_handler(service)

        self.assertEqual(
            handler._live_mount_source(target.id),
            [
                {"source": "postgres-volume", "folder": "var-lib-postgresql-data"},
                {"source": "demo-volume", "folder": "demo-files"},
            ],
        )

        target.volume_targets = []
        self.assertEqual(
            [item["source"] for item in handler._live_mount_source(target.id)],
            ["demo-volume", "postgres-volume"],
        )

    def test_mount_descriptor_collisions_are_disambiguated_and_unsafe_selection_is_rejected(self):
        service = LiveTargetFoundationTests.make_service()
        target = BackupTargetRecord(
            name="target-a",
            worker_id="worker-a",
            id="target-a",
            live_access_enabled=True,
            runtime_volumes={
                "volume-a": {"bind": "/var/lib/foo.bar"},
                "volume-b": {"bind": "/var/lib/foo/bar"},
            },
        )
        service.target_repository.save(target)
        handler = self.make_handler(service)
        self.assertEqual(
            [item["folder"] for item in handler._live_mount_source(target.id)],
            ["var-lib-foo-bar", "var-lib-foo-bar-2"],
        )

        target.volume_targets = ["/var/lib/foo.bar", "/var/lib/foo.bar"]
        with self.assertRaises(LiveSessionError):
            handler._live_mount_source(target.id)

        target.volume_targets = []
        target.runtime_volumes = {
            "volume-a": {"bind": "/same-label"},
            "volume-b": {"bind": "/same-label"},
        }
        with self.assertRaises(LiveSessionError):
            handler._live_mount_source(target.id)

        target.runtime_volumes = {"volume-a": {"bind": "/var/lib/../etc"}}
        with self.assertRaises(LiveSessionError):
            handler._live_mount_source(target.id)


class _HarnessAuth:
    def parse_session_token(self, token):
        if token in {"browser-a", "browser-b"}:
            return {"username": token, "role": "viewer"}
        return None

    @staticmethod
    def can_access(role, required_role):
        return role == "viewer" and required_role == "viewer"


class _MemoryCredentialStore:
    def __init__(self, worker_id, secret):
        self.credential = SimpleNamespace(worker_id=worker_id, version="1", secret=secret)

    def load(self):
        return self.credential


class _BrowserFakeLiveHelper:
    def __init__(self, root, denied_paths=()):
        self.root = os.path.realpath(root)
        self.denied_paths = set(denied_paths)
        self.id = "browser-live-helper"
        self.stopped = False
        self.removed = False

    @staticmethod
    def _argument(arguments, name, default=None):
        try:
            return arguments[arguments.index(name) + 1]
        except (ValueError, IndexError):
            return default

    def exec_run(self, cmd, **_kwargs):
        command = cmd
        operation_index = command.index("--max-chunk-bytes") + 2
        operation, arguments = command[operation_index], command[operation_index + 1 :]
        try:
            if operation == "list":
                path = self._argument(arguments, "--path", "/")
                if path in self.denied_paths:
                    return PROTECTED_VOLUME_EXIT_CODE, (b"", b"permission denied: /host/secret")
                output = json.dumps(
                    list_entries(
                        self.root,
                        path,
                        int(self._argument(arguments, "--limit", 100)),
                        self._argument(arguments, "--cursor", ""),
                    ),
                    separators=(",", ":"),
                ).encode()
            elif operation == "snapshot":
                entries, complete = watch_snapshot(
                    self.root,
                    int(self._argument(arguments, "--max-entries", 4096)),
                )
                output = json.dumps({"entries": entries, "complete": complete}, separators=(",", ":")).encode()
            elif operation == "read":
                output = b"".join(
                    read_file(
                        self.root,
                        self._argument(arguments, "--path"),
                        int(self._argument(arguments, "--offset", 0)),
                        int(self._argument(arguments, "--max-bytes", 64 * 1024 * 1024)),
                        int(command[command.index("--max-chunk-bytes") + 1]),
                    )
                )
            else:
                raise ValueError("unsupported helper operation")
            return 0, (output, b"")
        except Exception:
            return 1, (b"", b"helper failure")

    def stop(self, timeout=1):
        self.stopped = True

    def remove(self, force=False):
        self.removed = force


class _BrowserFakeDockerClient:
    def __init__(self, root, denied_paths=()):
        self.helper = _BrowserFakeLiveHelper(root, denied_paths)
        self.volume = SimpleNamespace(attrs={"Mountpoint": "/var/lib/docker/volumes/browser-only/_data"})
        self.volumes = Mock()
        self.volumes.get.side_effect = lambda name: self.volume if name == "volume-a" else None
        self.containers = Mock()
        self.containers.run.side_effect = lambda **kwargs: self._run(**kwargs)

    def _run(self, **kwargs):
        self.run_kwargs = kwargs
        return self.helper


class _FakeLiveWorker:
    def __init__(self, lane, root, control_plane_client=None, source=None):
        self.lane = lane
        self.root = root
        self.source = source or root
        self.control_plane_client = control_plane_client
        self.factory_calls = []
        self.change_calls = []
        self.manager = LiveTargetSessionManager(
            root_resolver=lambda _target_id: self.source,
            runtime_factory=self._runtime_factory,
            change_publisher=self._publish_change if control_plane_client is not None else None,
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _runtime_factory(self, path):
        self.factory_calls.append(path)
        return LiveFileRuntime(path, "s" * 32, max_chunk_bytes=4, watch_interval=0.01)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
        self.manager.close()

    def _publish_change(self, key, kind, path, entry_type, size, mtime_ns):
        self.change_calls.append((key.target_id, kind, path, entry_type, size, mtime_ns))
        return self.control_plane_client.send_live_change(
            worker_id=key.worker_id,
            target_id=key.target_id,
            config_revision=key.config_revision,
            kind=kind,
            path=path,
            entry_type=entry_type,
            size=size,
            mtime_ns=mtime_ns,
        )

    def _run(self):
        while not self.stop_event.is_set():
            for command in self.lane.poll("worker-a"):
                self._handle(command)
            self.stop_event.wait(0.001)

    def _handle(self, command):
        key = LiveTargetSessionKey(command["target_id"], command["config_revision"], "worker-a")
        handle = None
        try:
            operation_id = command["operation_id"]
            if command["operation"] == "watch":
                self.manager.begin_watch(key, target_root=self.source)
                self.lane.respond("worker-a", operation_id, {"status": 200})
                return
            elif command["operation"] == "unwatch":
                self.manager.end_watch(key)
                self.lane.respond("worker-a", operation_id, {"status": 200})
                return
            else:
                handle = self.manager.attach(key, target_root=self.source)
            if command["operation"] == "entries":
                result = handle.list_entries(command["path"], command["limit"], command.get("cursor"))
                self.lane.respond("worker-a", operation_id, {"status": 200, **result})
            elif command["operation"] == "file":
                self.lane.respond("worker-a", operation_id, {"status": 200, "content_type": "application/octet-stream"})
                for chunk in handle.read_file(command["path"], max_bytes=command["max_bytes"]):
                    self.lane.chunk("worker-a", operation_id, chunk)
                self.lane.chunk("worker-a", operation_id, b"", final=True)
            else:
                raise ValueError("unsupported fake operation")
        except LiveAccessDeniedError:
            try:
                self.lane.respond(
                    "worker-a",
                    command.get("operation_id"),
                    {"status": 403, "code": "live_access_denied", "reason": "protected_volume"},
                )
            except Exception:
                pass
        except Exception:
            try:
                self.lane.respond("worker-a", command.get("operation_id"), {"status": 404})
            except Exception:
                pass
        finally:
            if handle is not None:
                handle.release()


class _LiveHttpHarness:
    def __init__(self, named_volume=False, denied_paths=()):
        self.root_directory = tempfile.TemporaryDirectory()
        root = Path(self.root_directory.name)
        (root / "a.txt").write_bytes(b"alpha")
        (root / "b.txt").write_bytes(b"bravo")
        (root / "binary.bin").write_bytes(b"\x00\xffraw\x01")
        if denied_paths:
            (root / "safe-folder").mkdir()
            (root / "safe-folder" / "status.txt").write_bytes(b"safe")
            (root / "protected-volume").mkdir()
        self.root = str(root)
        self.docker_client = _BrowserFakeDockerClient(self.root, denied_paths) if named_volume else None
        self.live_source = LiveFileSource("volume", "volume-a", self.docker_client, "worker-image") if named_volume else None
        mount_source = "volume-a" if named_volume else self.root
        self.service = LiveTargetFoundationTests.make_service()
        self.target = self.service.register_target(
            "target-a",
            "worker-a",
            volume_targets=["/data"],
            runtime_volumes={mount_source: {"bind": "/data"}},
            live_access_enabled=True,
        )
        self.live_service = LiveFileService(cursor_secret="c" * 32)
        self.lane = LiveWorkerLane()
        self.worker_secret = "w" * 32
        self.service.worker_auth = WorkerAuthState()
        self.service.worker_auth.create_enrollment("worker-a", "host-a", {}, self.worker_secret, worker_id="worker-a")
        self.service.worker_auth.complete(self.worker_secret)
        application = ControlPlaneApplication(
            auth_service=_HarnessAuth(),
            control_plane_service=self.service,
            live_file_service=self.live_service,
            live_lane=self.lane,
        )
        self.server = ControlPlaneHTTPServer(("127.0.0.1", 0), ControlPlaneRequestHandler, application=application)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.worker = _FakeLiveWorker(
            self.lane,
            self.root,
            ControlPlaneClient(
                self.base_url,
                timeout_seconds=1,
                credential_store=_MemoryCredentialStore("worker-a", self.worker_secret),
                worker_id="worker-a",
            ),
            source=self.live_source,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.worker.start()

    def close(self):
        self.worker.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=1)
        self.root_directory.cleanup()

    def request(self, path, token="browser-a"):
        headers = {"Cookie": f"cp_session={token}"} if token else {}
        request = Request(self.base_url + path, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    def json_request(self, path, token="browser-a"):
        status, headers, body = self.request(path, token)
        return status, headers, json.loads(body.decode("utf-8"))

    def live_url(self, operation, query=""):
        return f"/api/v1/targets/{quote(self.target.id, safe='')}/live/{operation}{query}"

    def open_sse(self, last_event_id=None, token="browser-a"):
        headers = {"Cookie": f"cp_session={token}", "Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        return urlopen(Request(self.base_url + self.live_url("events"), headers=headers), timeout=3)

    @staticmethod
    def read_sse_event(response):
        while True:
            event_name, event_id, data = None, None, []
            while True:
                line = response.readline()
                if not line:
                    return None
                line = line.decode("utf-8").rstrip("\r\n")
                if not line:
                    break
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("id: "):
                    event_id = line[4:]
                elif line.startswith("data: "):
                    data.append(line[6:])
            if event_name:
                return {"event": event_name, "id": event_id, "data": json.loads("\n".join(data))}


class LiveTargetAcceptanceTests(unittest.TestCase):
    def test_service_enforces_shared_cursors_quotas_cancellation_invalidation_and_resync(self):
        service = LiveFileService(
            cursor_secret="c" * 32,
            max_events=2,
            max_subscribers=2,
            queue_size=1,
            max_chunk_bytes=4,
            max_stream_bytes=8,
        )
        key = LiveSessionKey("target", "revision", "worker")
        first, second = service.attach(key), service.attach(key)
        service.publish_change(key, "modified", "/safe.txt", "file")
        self.assertEqual(first.get(timeout=1)["event"]["path"], "/safe.txt")
        first.acknowledge(1)
        self.assertEqual((first.cursor, second.cursor), (1, 0))
        service.publish_change(key, "modified", "/second.txt", "file")
        service.publish_change(key, "modified", "/third.txt", "file")
        self.assertEqual(second.get(timeout=1)["type"], "resync_required")
        self.assertTrue(service.replay(key, service.issue_cursor(key, 0))["resync_required"])
        stream = service.open_raw_stream(key)
        stream.push(b"1234")
        with self.assertRaisesRegex(Exception, "permitted bound"):
            stream.push(b"12345")
        self.assertTrue(stream.closed)
        stale = service.open_raw_stream(key)
        stale.push(b"old")
        service.invalidate(key, "revoked")
        self.assertEqual(list(stale), [])
        self.assertTrue(first.closed and second.closed)
        with self.assertRaises(Exception):
            service.attach(key)

    def test_routes_are_opaque_confined_bounded_raw_and_job_free_for_two_browsers(self):
        harness = _LiveHttpHarness()
        try:
            status, _, body = harness.request(harness.live_url("entries"), token=None)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body), {"error": "authentication required"})

            harness.target.live_access_enabled = False
            harness.service.target_repository.save(harness.target)
            status, _, body = harness.request(harness.live_url("entries"))
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"error": "live access unavailable"})
            harness.target.live_access_enabled = True
            harness.service.target_repository.save(harness.target)

            traversal = harness.live_url("entries", "?path=" + quote("/../outside", safe=""))
            status, _, body = harness.request(traversal)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"error": "live access unavailable"})
            link = Path(harness.root, "outside-link")
            try:
                link.symlink_to(Path(harness.root).parent / "not-under-target")
            except (OSError, NotImplementedError):
                pass
            else:
                status, _, body = harness.request(harness.live_url("file", "?path=%2Foutside-link"))
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {"error": "live access unavailable"})

            first_status, _, first_page = harness.json_request(harness.live_url("entries", "?path=%2F&limit=1"), "browser-a")
            second_status, _, second_page = harness.json_request(harness.live_url("entries", "?path=%2F&limit=1"), "browser-b")
            self.assertEqual((first_status, second_status), (200, 200))
            self.assertLessEqual(len(first_page["entries"]), 1)
            self.assertLessEqual(len(second_page["entries"]), 1)
            self.assertTrue(first_page["next_cursor"] and second_page["next_cursor"])
            for token, cursor in (("browser-a", first_page["next_cursor"]), ("browser-b", second_page["next_cursor"])):
                status, _, page = harness.json_request(
                    harness.live_url("entries", "?path=%2F&limit=1&cursor=" + quote(cursor, safe="")),
                    token,
                )
                self.assertEqual(status, 200)
                self.assertLessEqual(len(page["entries"]), 1)

            status, headers, body = harness.request(harness.live_url("file", "?path=%2Fbinary.bin"))
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "application/octet-stream")
            self.assertEqual(body, b"\x00\xffraw\x01")
            self.assertNotIn(b"base64", body)
            self.assertEqual(harness.service.job_repository.list(), [])
            self.assertEqual(harness.worker.factory_calls, [harness.root])
            self.assertEqual(harness.worker.manager.session_count, 1)
        finally:
            harness.close()

    def test_signed_production_watcher_delivers_and_reconnects_without_replay_prefix(self):
        harness = _LiveHttpHarness()
        first_response = second_response = None
        try:
            first_response = harness.open_sse()
            Path(harness.root, "a.txt").write_bytes(b"alpha-updated")
            first = harness.read_sse_event(first_response)
            self.assertEqual((first["event"], first["data"]["path"]), ("changed", "/a.txt"))
            first_id = int(first["id"])
            first_response.close()

            Path(harness.root, "b.txt").write_bytes(b"bravo-updated")
            second_response = harness.open_sse(last_event_id=first_id)
            second = harness.read_sse_event(second_response)
            self.assertEqual((second["event"], second["data"]["path"]), ("changed", "/b.txt"))
            self.assertGreater(int(second["id"]), first_id)
            self.assertEqual([call[2] for call in harness.worker.change_calls[-2:]], ["/a.txt", "/b.txt"])
            self.assertNotEqual(second["data"]["path"], first["data"]["path"])
        finally:
            if first_response is not None:
                first_response.close()
            if second_response is not None:
                second_response.close()
            harness.close()

    def test_routes_and_events_work_through_a_named_volume_helper(self):
        harness = _LiveHttpHarness(named_volume=True)
        event_response = None
        try:
            status, _, page = harness.json_request(harness.live_url("entries"))
            self.assertEqual(status, 200)
            self.assertEqual([entry["name"] for entry in page["entries"]], ["a.txt", "b.txt", "binary.bin"])

            status, _, body = harness.request(harness.live_url("file", "?path=%2Fbinary.bin"))
            self.assertEqual((status, body), (200, b"\x00\xffraw\x01"))

            event_response = harness.open_sse()
            Path(harness.root, "a.txt").write_bytes(b"alpha-volume-updated")
            event = harness.read_sse_event(event_response)
            self.assertEqual((event["event"], event["data"]["path"]), ("changed", "/a.txt"))
            self.assertEqual(harness.docker_client.containers.run.call_count, 1)
            self.assertEqual(harness.docker_client.run_kwargs["volumes"], {"volume-a": {"bind": "/target", "mode": "ro"}})
        finally:
            if event_response is not None:
                event_response.close()
            harness.close()

    def test_protected_volume_error_is_structured_and_other_folders_remain_navigable(self):
        harness = _LiveHttpHarness(named_volume=True, denied_paths={"/protected-volume"})
        try:
            status, _, page = harness.json_request(harness.live_url("entries"))
            self.assertEqual(status, 200)
            self.assertEqual(
                {entry["name"] for entry in page["entries"]},
                {"a.txt", "b.txt", "binary.bin", "protected-volume", "safe-folder"},
            )

            status, _, error = harness.json_request(
                harness.live_url("entries", "?path=%2Fprotected-volume")
            )
            self.assertEqual(
                (status, error),
                (
                    403,
                    {
                        "error": "live access denied",
                        "code": "live_access_denied",
                        "reason": "protected_volume",
                        "path": "/protected-volume",
                    },
                ),
            )

            status, _, safe_page = harness.json_request(
                harness.live_url("entries", "?path=%2Fsafe-folder")
            )
            self.assertEqual((status, [entry["name"] for entry in safe_page["entries"]]), (200, ["status.txt"]))
        finally:
            harness.close()

    def test_sse_worker_failure_is_logged_and_returned_before_stream_headers(self):
        harness = _LiveHttpHarness()
        try:
            def reject(command):
                harness.lane.respond(
                    "worker-a",
                    command["operation_id"],
                    {"status": 503, "code": "live_worker_unavailable", "reason": "helper_start_failed"},
                )

            harness.worker._handle = reject
            with self.assertLogs("src.control_plane.main", level="WARNING") as captured:
                status, _, body = harness.request(harness.live_url("events"))
            self.assertEqual(
                (status, json.loads(body)),
                (
                    503,
                    {
                        "error": "live helper unavailable",
                        "code": "live_worker_unavailable",
                        "reason": "helper_start_failed",
                    },
                ),
            )
            self.assertTrue(any("operation=watch" in record.getMessage() for record in captured.records))
            self.assertTrue(all("/host/" not in record.getMessage() for record in captured.records))
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
