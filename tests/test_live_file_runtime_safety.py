import json, os, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src.control_plane.application.services.live_file_service import LiveFileService, LiveLimitError, LiveSessionKey
from src.worker_agent.application.services.live_target_session_manager import LiveTargetSessionKey, LiveTargetSessionManager
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.live_file_runtime import LiveAccessDeniedError, LiveFileRuntime, LiveFileSource
from src.worker_agent.live_file_helper import PROTECTED_VOLUME_EXIT_CODE, ProtectedVolumeError, list_entries, read_file, virtual_parts, watch_snapshot


class _FakeLiveHelperContainer:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.id = "live-helper-1"
        self.status = "running"
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
                payload = list_entries(
                    self.root,
                    self._argument(arguments, "--path", "/"),
                    int(self._argument(arguments, "--limit", 100)),
                    self._argument(arguments, "--cursor", ""),
                )
                output = json.dumps(payload, separators=(",", ":")).encode()
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


class _FakeLiveDockerClient:
    def __init__(self, root):
        self.helper = _FakeLiveHelperContainer(root)
        self.volume = SimpleNamespace(attrs={"Mountpoint": "/var/lib/docker/volumes/worker-only/_data"})
        self.volumes = Mock()
        self.volumes.get.side_effect = lambda name: self.volume if name == "volume-a" else None
        self.containers = Mock()
        self.containers.run.side_effect = self._run

    def _run(self, **kwargs):
        self.run_kwargs = kwargs
        return self.helper


class _MultiSourceFakeLiveHelper:
    def __init__(self, mounts):
        self.mounts = mounts
        self.id = "live-helper-multi"
        self.stopped = False
        self.removed = False

    @staticmethod
    def _argument(arguments, name, default=None):
        try:
            return arguments[arguments.index(name) + 1]
        except (ValueError, IndexError):
            return default

    def _mount_path(self, path):
        parts = virtual_parts(path)
        if not parts or parts[0] not in self.mounts:
            raise ValueError("live mount path is unavailable")
        folder, root = self.mounts[parts[0]]
        relative = "/" + "/".join(parts[1:]) if len(parts) > 1 else "/"
        return folder, root, relative

    def _list(self, path, limit, cursor):
        parts = virtual_parts(path)
        if not parts:
            entries = [
                {"name": folder, "path": f"/{folder}", "type": "dir", "size": None, "mtime_ns": None}
                for folder, _root in self.mounts.values()
            ]
            return {"entries": entries[:limit], "next_cursor": None}
        folder, root, relative = self._mount_path(path)
        result = list_entries(root, relative, limit, cursor)
        for entry in result["entries"]:
            entry["path"] = f"/{folder}{entry['path']}"
        return result

    def _snapshot(self, max_entries):
        entries = {}
        for folder, root in self.mounts.values():
            entries[f"/{folder}"] = ("dir", None, None)
            nested, complete = watch_snapshot(root, max_entries=max_entries)
            entries.update({f"/{folder}{path}": value for path, value in nested.items()})
            if len(entries) >= max_entries or not complete:
                return entries, False
        return entries, True

    def exec_run(self, cmd, **_kwargs):
        operation_index = cmd.index("--max-chunk-bytes") + 2
        operation, arguments = cmd[operation_index], cmd[operation_index + 1 :]
        try:
            if operation == "list":
                payload = self._list(
                    self._argument(arguments, "--path", "/"),
                    int(self._argument(arguments, "--limit", 100)),
                    self._argument(arguments, "--cursor", ""),
                )
                output = json.dumps(payload, separators=(",", ":")).encode()
            elif operation == "snapshot":
                entries, complete = self._snapshot(int(self._argument(arguments, "--max-entries", 4096)))
                output = json.dumps({"entries": entries, "complete": complete}, separators=(",", ":")).encode()
            elif operation == "read":
                _folder, root, relative = self._mount_path(self._argument(arguments, "--path"))
                output = b"".join(
                    read_file(
                        root,
                        relative,
                        int(self._argument(arguments, "--offset", 0)),
                        int(self._argument(arguments, "--max-bytes", 64 * 1024 * 1024)),
                        int(cmd[cmd.index("--max-chunk-bytes") + 1]),
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


class _MultiSourceFakeDockerClient:
    def __init__(self, source_roots):
        self.source_roots = source_roots
        self.volume = SimpleNamespace(attrs={"Mountpoint": "/var/lib/docker/volumes/not-mounted/_data"})
        self.volumes = Mock()
        self.volumes.get.side_effect = lambda name: self.volume if name in source_roots and not os.path.isabs(name) else None
        self.containers = Mock()
        self.containers.run.side_effect = self._run
        self.helper = None

    def _run(self, **kwargs):
        mounts = {}
        for source, spec in kwargs["volumes"].items():
            folder = spec["bind"].removeprefix("/target/")
            mounts[folder] = (folder, os.path.realpath(self.source_roots[source]))
        self.run_kwargs = kwargs
        self.helper = _MultiSourceFakeLiveHelper(mounts)
        return self.helper


class _DeniedLiveRuntime:
    def list_entries(self, *_args, **_kwargs):
        raise LiveAccessDeniedError("live access denied")

    def read_file(self, *_args, **_kwargs):
        raise LiveAccessDeniedError("live access denied")

    def expired(self, *_args, **_kwargs):
        return False

    def cancel(self):
        return None


class _DeniedLiveWatcherRuntime(_DeniedLiveRuntime):
    def watch_changes(self, _stop_event, _on_change, _ready_event=None):
        raise LiveAccessDeniedError("live access denied")


class LiveFileRuntimeSafetyTests(unittest.TestCase):
    def test_hmac_fixed_argv_read_only_and_network_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = LiveFileRuntime(root, "s" * 32); argv = runtime.helper_argv(); self.assertIn("--read-only", argv); self.assertNotIn("/bin/sh", " ".join(argv)); signature = runtime.sign("list", "/", "n", 1); self.assertTrue(runtime.verify("list", "/", "n", 1, signature)); self.assertFalse(runtime.verify("list", "/x", "n", 1, signature)); kwargs = runtime.docker_run_kwargs(); self.assertTrue(kwargs["read_only"] and kwargs["network_disabled"]); self.assertEqual(next(iter(kwargs["volumes"].values()))["mode"], "ro")
    def test_confined_reads_are_bounded_no_follow_and_cancelable(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "safe.txt").write_bytes(b"0123456789"); runtime = LiveFileRuntime(root, "s" * 32, max_chunk_bytes=3, max_file_bytes=32); self.assertEqual(list(runtime.read_file("/safe.txt")), [b"012", b"345", b"678", b"9"])
            with self.assertRaises(ValueError): list(runtime.read_file("/../safe.txt"))
            try: os.symlink(Path(root, "safe.txt"), Path(root, "link.txt"))
            except (OSError, NotImplementedError): pass
            else:
                with self.assertRaises(ValueError): list(runtime.read_file("/link.txt"))
            with self.assertRaisesRegex(Exception, "canceled"): list(runtime.read_file("/safe.txt", cancel_check=lambda: True))

    def test_permission_denials_are_classified_without_exposing_helper_details(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("src.worker_agent.live_file_helper.os.scandir", side_effect=PermissionError("permission denied: /host/secret")):
                with self.assertRaises(ProtectedVolumeError):
                    list_entries(root, "/")

            runtime = LiveFileRuntime(root, "s" * 32)
            runtime._helper_container = SimpleNamespace(
                closed=False,
                container=SimpleNamespace(exec_run=Mock(return_value=(PROTECTED_VOLUME_EXIT_CODE, (b"", b"permission denied: /host/secret")))),
            )
            with self.assertRaises(LiveAccessDeniedError) as error:
                runtime._helper_exec("list", output_limit=1024)
            self.assertEqual(str(error.exception), "live access denied")

    def test_worker_propagates_protected_volume_as_safe_structured_error(self):
        with tempfile.TemporaryDirectory() as root:
            docker_client = _FakeLiveDockerClient(root)
            control_plane_client = SimpleNamespace(
                credential_store=SimpleNamespace(load=lambda: SimpleNamespace(secret="s" * 32)),
                send_live_response=Mock(),
            )
            service = WorkerAgentService(
                WorkerAgentConfig(
                    "http://control-plane",
                    "worker",
                    "host",
                    worker_id="worker",
                    live_helper_image="worker-image",
                ),
                control_plane_client,
                SimpleNamespace(client=docker_client),
            )
            service.live_sessions.runtime_factory = lambda _value: _DeniedLiveRuntime()
            service._process_live_request(
                "worker",
                {
                    "operation_id": "protected-entries",
                    "worker_id": "worker",
                    "target_id": "target",
                    "config_revision": "revision",
                    "operation": "entries",
                    "mount_source": "volume-a",
                    "path": "/protected-volume",
                    "limit": 10,
                },
            )
            result = control_plane_client.send_live_response.call_args.args[2]
            self.assertEqual(
                result,
                {"status": 403, "code": "live_access_denied", "reason": "protected_volume"},
            )

    def test_worker_propagates_watcher_start_failure_before_live_events_begin(self):
        with tempfile.TemporaryDirectory() as root:
            docker_client = _FakeLiveDockerClient(root)
            control_plane_client = SimpleNamespace(
                credential_store=SimpleNamespace(load=lambda: SimpleNamespace(secret="s" * 32)),
                send_live_response=Mock(),
            )
            service = WorkerAgentService(
                WorkerAgentConfig(
                    "http://control-plane",
                    "worker",
                    "host",
                    worker_id="worker",
                    live_helper_image="worker-image",
                ),
                control_plane_client,
                SimpleNamespace(client=docker_client),
            )
            service.live_sessions.runtime_factory = lambda _value: _DeniedLiveWatcherRuntime()
            service._process_live_request(
                "worker",
                {
                    "operation_id": "protected-watch",
                    "worker_id": "worker",
                    "target_id": "target",
                    "config_revision": "revision",
                    "operation": "watch",
                    "mount_source": "volume-a",
                },
            )
            self.assertEqual(
                control_plane_client.send_live_response.call_args.args[2],
                {"status": 403, "code": "live_access_denied", "reason": "protected_volume"},
            )

    def test_worker_classifies_operational_failures_and_logs_only_safe_fields(self):
        def make_service(docker_client):
            control_plane_client = SimpleNamespace(
                credential_store=SimpleNamespace(load=lambda: SimpleNamespace(secret="s" * 32)),
                send_live_response=Mock(),
            )
            return (
                WorkerAgentService(
                    WorkerAgentConfig(
                        "http://control-plane",
                        "worker",
                        "host",
                        worker_id="worker",
                        live_helper_image="worker-image",
                    ),
                    control_plane_client,
                    SimpleNamespace(client=docker_client),
                ),
                control_plane_client,
            )

        def command(operation_id, **overrides):
            return {
                "operation_id": operation_id,
                "worker_id": "worker",
                "target_id": "target",
                "config_revision": "revision",
                "operation": "entries",
                "mount_source": "volume-a",
                "path": "/",
                "limit": 10,
                **overrides,
            }

        with tempfile.TemporaryDirectory() as root, self.assertLogs(
            "src.worker_agent.application.services.worker_agent_service", level="WARNING"
        ) as captured:
            service, client = make_service(_FakeLiveDockerClient(root))
            service._process_live_request("worker", command("missing-source", mount_source="missing-volume"))
            self.assertEqual(
                client.send_live_response.call_args.args[2],
                {"status": 503, "code": "live_worker_unavailable", "reason": "source_unavailable"},
            )

            service, client = make_service(_FakeLiveDockerClient(root))
            service._process_live_request("worker", command("invalid-source", mount_source="/var/lib/docker/volumes"))
            self.assertEqual(
                client.send_live_response.call_args.args[2],
                {"status": 400, "code": "live_request_rejected", "reason": "invalid_source"},
            )

            docker_client = _FakeLiveDockerClient(root)
            docker_client.containers.run.side_effect = RuntimeError("permission denied: /host/secret")
            service, client = make_service(docker_client)
            service._process_live_request("worker", command("helper-start"))
            self.assertEqual(
                client.send_live_response.call_args.args[2],
                {"status": 503, "code": "live_worker_unavailable", "reason": "helper_start_failed"},
            )

            docker_client = _FakeLiveDockerClient(root)
            helper = _FakeLiveHelperContainer(root)
            helper.exec_run = Mock(return_value=(1, (b"", b"helper failure: /host/secret")))
            docker_client.containers.run.side_effect = None
            docker_client.containers.run.return_value = helper
            service, client = make_service(docker_client)
            service._process_live_request("worker", command("helper-request"))
            self.assertEqual(
                client.send_live_response.call_args.args[2],
                {"status": 503, "code": "live_worker_unavailable", "reason": "helper_request_failed"},
            )

            service, client = make_service(_FakeLiveDockerClient(root))

            def fail_runtime(_value):
                raise RuntimeError("credential contains /host/secret")

            service.live_sessions.runtime_factory = fail_runtime
            service._process_live_request("worker", command("session"))
            self.assertEqual(
                client.send_live_response.call_args.args[2],
                {"status": 503, "code": "live_worker_unavailable", "reason": "live_session_unavailable"},
            )

        self.assertEqual(len(captured.records), 5)
        for record in captured.records:
            self.assertIn("operation=entries", record.getMessage())
            self.assertIn("target_id=target", record.getMessage())
            self.assertIn("code=", record.getMessage())
            self.assertIn("reason=", record.getMessage())
            self.assertNotIn("/host/secret", record.getMessage())

    def test_same_key_attach_reuses_and_orphan_cleanup_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []; manager = LiveTargetSessionManager(root_resolver=lambda target: root, runtime_factory=lambda path: calls.append(path) or LiveFileRuntime(path, "s" * 32)); key = LiveTargetSessionKey("target", "revision", "worker"); first, second = manager.attach(key), manager.attach(key); self.assertIs(first.runtime, second.runtime); self.assertEqual(len(calls), 1); first.release(); second.release(); client = Mock(); orphan = Mock(id="orphan", status="exited", labels={LiveFileRuntime.ORPHAN_LABEL: "true"}); client.containers.list.return_value = [orphan]; self.assertEqual(manager.cleanup_orphaned(client)["removed"], 1); manager.invalidate(key); self.assertEqual(manager.session_count, 0)

    def test_named_volume_uses_read_only_helper_when_daemon_mountpoint_is_absent(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "safe.txt").write_bytes(b"volume-data")
            docker_client = _FakeLiveDockerClient(root)
            control_plane_client = SimpleNamespace(
                credential_store=SimpleNamespace(load=lambda: SimpleNamespace(secret="s" * 32)),
                send_live_response=Mock(),
                send_live_change=Mock(return_value={}),
            )
            service = WorkerAgentService(
                WorkerAgentConfig(
                    "http://control-plane",
                    "worker",
                    "host",
                    worker_id="worker",
                    live_helper_image="worker-image",
                ),
                control_plane_client,
                SimpleNamespace(client=docker_client),
            )
            service.live_sessions.runtime_factory = lambda value: LiveFileRuntime(value, "s" * 32, watch_interval=0.01)

            source = service._resolve_live_source("volume-a")
            self.assertIsInstance(source, LiveFileSource)
            self.assertFalse(os.path.exists(docker_client.volume.attrs["Mountpoint"]))
            self.assertEqual(source.source, "volume-a")
            bind_source = service._resolve_live_source(os.path.join(root, "host-only-bind"))
            self.assertEqual((bind_source.kind, bind_source.source), ("bind", os.path.join(root, "host-only-bind")))
            with self.assertRaisesRegex(ValueError, "invalid|unsafe"):
                service._resolve_live_source("/var/lib/docker/volumes")

            command = {
                "operation_id": "operation-1",
                "worker_id": "worker",
                "target_id": "target",
                "config_revision": "revision",
                "operation": "entries",
                "mount_source": "volume-a",
                "path": "/",
                "limit": 10,
            }
            service._process_live_request("worker", command)
            result = control_plane_client.send_live_response.call_args.args[2]
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["entries"][0]["name"], "safe.txt")
            self.assertEqual(docker_client.containers.run.call_count, 1)
            self.assertEqual(docker_client.run_kwargs["volumes"], {"volume-a": {"bind": "/target", "mode": "ro"}})
            self.assertTrue(docker_client.run_kwargs["read_only"])
            self.assertTrue(docker_client.run_kwargs["network_disabled"])
            self.assertNotIn("/var/lib/docker/volumes", docker_client.run_kwargs["volumes"])

            service._process_live_request("worker", {**command, "operation_id": "operation-2"})
            self.assertEqual(docker_client.containers.run.call_count, 1)
            service._process_live_request(
                "worker",
                {**command, "operation_id": "operation-3", "operation": "watch"},
            )
            Path(root, "safe.txt").write_bytes(b"volume-data-updated")
            for _ in range(100):
                if control_plane_client.send_live_change.called:
                    break
                time.sleep(0.01)
            self.assertTrue(control_plane_client.send_live_change.called)
            self.assertEqual(control_plane_client.send_live_change.call_args.kwargs["path"], "/safe.txt")
            service._process_live_request(
                "worker",
                {**command, "operation_id": "operation-4", "operation": "unwatch"},
            )
            service.live_sessions.close()
            self.assertTrue(docker_client.helper.stopped)
            self.assertTrue(docker_client.helper.removed)

    def test_multiple_named_and_bind_sources_share_one_virtual_read_only_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            named_root = Path(directory, "named")
            bind_root = Path(directory, "bind")
            named_root.mkdir()
            bind_root.mkdir()
            (named_root / "README.txt").write_bytes(b"named-data")
            (bind_root / "status.txt").write_bytes(b"bind-data")
            source_roots = {"volume-demo": str(named_root), str(bind_root): str(bind_root)}
            docker_client = _MultiSourceFakeDockerClient(source_roots)
            control_plane_client = SimpleNamespace(
                credential_store=SimpleNamespace(load=lambda: SimpleNamespace(secret="s" * 32)),
                send_live_response=Mock(),
                send_live_chunk=Mock(),
                send_live_change=Mock(return_value=True),
            )
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker", live_helper_image="worker-image"),
                control_plane_client,
                SimpleNamespace(client=docker_client),
            )
            service.live_sessions.runtime_factory = lambda value: LiveFileRuntime(value, "s" * 32, watch_interval=0.01)
            descriptors = [
                {"source": "volume-demo", "folder": "demo-files"},
                {"source": str(bind_root), "folder": "var-lib-postgresql-data"},
            ]
            command = {
                "operation_id": "multi-entries",
                "worker_id": "worker",
                "target_id": "target",
                "config_revision": "revision",
                "operation": "entries",
                "mount_sources": descriptors,
                "path": "/",
                "limit": 10,
            }

            service._process_live_request("worker", command)
            result = control_plane_client.send_live_response.call_args.args[2]
            self.assertEqual((result["status"], [entry["name"] for entry in result["entries"]]), (200, ["demo-files", "var-lib-postgresql-data"]))
            self.assertEqual(docker_client.containers.run.call_count, 1)
            self.assertEqual(
                docker_client.run_kwargs["volumes"],
                {
                    "volume-demo": {"bind": "/target/demo-files", "mode": "ro"},
                    str(bind_root): {"bind": "/target/var-lib-postgresql-data", "mode": "ro"},
                },
            )
            self.assertTrue(docker_client.run_kwargs["read_only"])
            self.assertTrue(docker_client.run_kwargs["network_disabled"])
            self.assertNotIn("/var/lib/docker/volumes", docker_client.run_kwargs["volumes"])

            service._process_live_request(
                "worker",
                {**command, "operation_id": "multi-file", "operation": "file", "path": "/demo-files/README.txt", "max_bytes": 64},
            )
            chunks = [call.args[2] for call in control_plane_client.send_live_chunk.call_args_list]
            self.assertEqual(chunks, [b"named-data", b""])

            service._process_live_request(
                "worker",
                {**command, "operation_id": "multi-watch", "operation": "watch"},
            )
            (bind_root / "status.txt").write_bytes(b"bind-data-updated")
            for _ in range(100):
                if control_plane_client.send_live_change.called:
                    break
                time.sleep(0.01)
            self.assertTrue(control_plane_client.send_live_change.called)
            self.assertEqual(control_plane_client.send_live_change.call_args.kwargs["path"], "/var-lib-postgresql-data/status.txt")
            service._process_live_request(
                "worker",
                {**command, "operation_id": "multi-unwatch", "operation": "unwatch"},
            )
            service.live_sessions.close()
            self.assertTrue(docker_client.helper.stopped)
            self.assertTrue(docker_client.helper.removed)

    def test_multi_source_watch_starts_without_inspecting_the_host_root(self):
        with tempfile.TemporaryDirectory() as directory:
            named_root = Path(directory, "named")
            bind_root = Path(directory, "bind")
            named_root.mkdir()
            bind_root.mkdir()
            (named_root / "README.txt").write_bytes(b"named-data")
            (bind_root / "status.txt").write_bytes(b"bind-data")
            source_roots = {"volume-demo": str(named_root), str(bind_root): str(bind_root)}
            docker_client = _MultiSourceFakeDockerClient(source_roots)
            sources = (
                LiveFileSource("volume", "volume-demo", docker_client, "worker-image", folder="demo-files"),
                LiveFileSource("bind", str(bind_root), docker_client, "worker-image", folder="bind-files"),
            )
            runtime = LiveFileRuntime(sources, "s" * 32, watch_interval=0.01)
            stop_event = threading.Event()
            stop_event.set()
            ready_event = threading.Event()
            original_realpath = os.path.realpath

            def guarded_realpath(path):
                if path == runtime.root:
                    raise AssertionError("multi-source watch inspected the daemon root")
                return original_realpath(path)

            with patch(
                "src.worker_agent.infrastructure.adapters.live_file_runtime.os.path.realpath",
                side_effect=guarded_realpath,
            ) as realpath:
                runtime.watch_changes(stop_event, Mock(), ready_event)

            self.assertTrue(ready_event.is_set())
            self.assertNotIn(runtime.root, [call.args[0] for call in realpath.call_args_list])
            self.assertEqual(docker_client.containers.run.call_count, 1)
            self.assertEqual(
                docker_client.run_kwargs["volumes"],
                {
                    "volume-demo": {"bind": "/target/demo-files", "mode": "ro"},
                    str(bind_root): {"bind": "/target/bind-files", "mode": "ro"},
                },
            )
            self.assertTrue(docker_client.run_kwargs["read_only"])
            self.assertTrue(docker_client.run_kwargs["network_disabled"])
            runtime.cancel()

    def test_cursors_gaps_queues_raw_limits_and_revocation_fail_closed(self):
        service = LiveFileService(cursor_secret="c" * 32, max_events=2, max_subscribers=2, queue_size=1, max_chunk_bytes=4, max_stream_bytes=8); key = LiveSessionKey("target", "revision", "worker"); first, second = service.attach(key), service.attach(key); event = service.publish_change(key, "modified", "/safe.txt", "file", 4, 1); self.assertNotIn("content", event.projection()); self.assertEqual(first.get(timeout=1)["event"]["path"], "/safe.txt")
        for index in range(2, 4): service.publish_change(key, "modified", f"/f{index}", "file")
        self.assertTrue(service.replay(key, service.issue_cursor(key, 0))["resync_required"]); self.assertEqual(second.get(timeout=1)["type"], "resync_required"); stream = service.open_raw_stream(key); stream.push(b"1234"); stream.close(); self.assertEqual(list(stream), [b"1234"])
        with self.assertRaises(LiveLimitError): service.open_raw_stream(key).push(b"12345")
        stale = service.open_raw_stream(key); stale.push(b"old"); service.invalidate(key, "revoked"); self.assertEqual(list(stale), []); self.assertTrue(first.closed and second.closed)
        with self.assertRaises(Exception): service.attach(key)
if __name__ == "__main__": unittest.main()
