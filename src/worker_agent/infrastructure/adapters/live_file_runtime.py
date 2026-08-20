import json
import errno
import os
import re
import stat as stat_module
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.worker_agent.live_file_helper import (
    PROTECTED_VOLUME_EXIT_CODE,
    ProtectedVolumeError,
    confined_path,
    open_confined,
    sign_request,
    verify_request,
)


class LiveRuntimeError(RuntimeError):
    code = "live_worker_unavailable"
    reason = "live_session_unavailable"
    status = 503

    def __init__(self, message="live operation failed", *, code=None, reason=None, status=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if reason is not None:
            self.reason = reason
        if status is not None:
            self.status = status


class LiveAccessDeniedError(LiveRuntimeError):
    code = "live_access_denied"
    reason = "protected_volume"
    status = 403

    def __init__(self, message="live access denied"):
        super().__init__(message, code=self.code, reason=self.reason, status=self.status)


def _is_permission_error(exc):
    return isinstance(exc, (PermissionError, ProtectedVolumeError)) or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)


def _raise_live_access_denied(exc):
    if _is_permission_error(exc):
        raise LiveAccessDeniedError("live access denied") from exc


LIVE_MOUNT_FOLDER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class LiveFileSource:
    """A server-selected source that may need a Docker mount boundary."""

    kind: str
    source: str
    client: Any = field(compare=False, repr=False)
    image: str
    folder: str = ""

    def __post_init__(self):
        if self.kind not in {"bind", "volume"} or not isinstance(self.source, str) or not self.source:
            raise ValueError("live source is invalid")
        if len(self.source) > 4096 or "\x00" in self.source:
            raise ValueError("live source is invalid")
        if self.kind == "bind" and not os.path.isabs(self.source):
            raise ValueError("live bind source must be absolute")
        if self.kind == "bind":
            normalized = self.source.rstrip("/") or "/"
            if (
                normalized in {"/", "/var/lib/docker", "/var/lib/docker/volumes"}
                or normalized.startswith(("/proc/", "/sys/", "/dev/"))
                or normalized.endswith((".sock", ".socket"))
            ):
                raise ValueError("live bind source is unsafe")
        if self.kind == "volume" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", self.source):
            raise ValueError("live volume source is invalid")
        if self.client is None or not isinstance(self.image, str) or not self.image or len(self.image) > 512:
            raise ValueError("live helper configuration is invalid")
        if not isinstance(self.folder, str) or (self.folder and not LIVE_MOUNT_FOLDER_PATTERN.fullmatch(self.folder)):
            raise ValueError("live mount folder is invalid")

    @property
    def identity(self):
        return self.kind, self.source, self.image, self.folder


class _LiveHelperContainer:
    def __init__(self, source: LiveFileSource, kwargs: dict[str, Any]):
        self.sources = tuple(source) if isinstance(source, (list, tuple)) else (source,)
        first_source = self.sources[0]
        containers = getattr(first_source.client, "containers", None)
        run = getattr(containers, "run", None)
        if not callable(run):
            raise LiveRuntimeError("live helper runtime is unavailable", reason="helper_start_failed")
        try:
            self.container = run(image=first_source.image, **kwargs)
        except Exception as exc:
            if _is_permission_error(exc):
                raise LiveAccessDeniedError() from exc
            raise LiveRuntimeError("live helper container could not be started", reason="helper_start_failed") from exc
        if self.container is None:
            raise LiveRuntimeError("live helper container could not be started", reason="helper_start_failed")
        self.closed = False

    @property
    def id(self):
        return getattr(self.container, "id", None)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            stop = getattr(self.container, "stop", None)
            if callable(stop):
                try:
                    stop(timeout=1)
                except TypeError:
                    stop()
        except Exception:
            pass
        try:
            remove = getattr(self.container, "remove", None)
            if callable(remove):
                remove(force=True)
        except Exception:
            pass


class LiveFileRuntime:
    ORPHAN_LABEL, MAX_ENTRIES, MAX_WATCH_ENTRIES = "docker-volume-backup.live-file.temporary", 1000, 4096
    WATCH_INTERVAL_SECONDS = 0.25
    HELPER_CONTAINER_SCRIPT = "/app/src/worker_agent/live_file_helper.py"
    MAX_HELPER_METADATA_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        target_root,
        hmac_secret,
        *,
        max_chunk_bytes=64 * 1024,
        max_file_bytes=64 * 1024 * 1024,
        idle_ttl=300.0,
        max_age=3600.0,
        watch_interval=WATCH_INTERVAL_SECONDS,
        max_watch_entries=MAX_WATCH_ENTRIES,
        clock=None,
    ):
        if isinstance(target_root, LiveFileSource):
            self.sources = (target_root,)
        elif isinstance(target_root, (list, tuple)) and target_root and all(
            isinstance(source, LiveFileSource) for source in target_root
        ):
            self.sources = tuple(target_root)
        else:
            self.sources = None
        if self.sources is None:
            self.source = None
            raw_root = os.fspath(target_root)
            if not os.path.isabs(raw_root) or os.path.islink(raw_root) or not os.path.isdir(raw_root):
                raise ValueError("live target root must be an existing server-derived directory")
            self.root = os.path.realpath(raw_root)
        else:
            if len(self.sources) > 1 and any(not source.folder for source in self.sources):
                raise ValueError("live mount folders are required")
            source_keys = {(source.kind, source.source) for source in self.sources}
            folders = {source.folder for source in self.sources}
            if len(source_keys) != len(self.sources) or len(folders) != len(self.sources):
                raise ValueError("live mount sources are ambiguous")
            self.source = self.sources[0] if len(self.sources) == 1 else None
            self.root = self.sources[0].source
        if not isinstance(hmac_secret, (str, bytes)) or len(hmac_secret) < 32:
            raise ValueError("live helper secret is too short")
        if not isinstance(watch_interval, (int, float)) or watch_interval <= 0:
            raise ValueError("live watcher interval must be positive")
        if not isinstance(max_watch_entries, int) or max_watch_entries <= 0:
            raise ValueError("live watcher entry limit must be positive")
        self.hmac_secret = hmac_secret
        self.max_chunk_bytes = max_chunk_bytes
        self.max_file_bytes = max_file_bytes
        self.watch_interval = float(watch_interval)
        self.max_watch_entries = min(max_watch_entries, self.MAX_WATCH_ENTRIES)
        self.idle_ttl, self.max_age, self.clock = idle_ttl, max_age, clock or time.monotonic
        self.created_at = self.last_activity = self.clock()
        self._cancel = threading.Event()
        self._helper_container = None
        if self.sources is not None:
            self._helper_container = _LiveHelperContainer(self.sources, self._helper_run_kwargs())

    def _helper_command(self, operation, *arguments):
        return [
            sys.executable,
            self.HELPER_CONTAINER_SCRIPT,
            "--root",
            "/target",
            "--read-only",
            "--max-chunk-bytes",
            str(self.max_chunk_bytes),
            operation,
            *[str(argument) for argument in arguments],
        ]

    def _helper_run_kwargs(self):
        if self.sources is None:
            raise LiveRuntimeError("live helper runtime is unavailable")
        volumes = {}
        for source in self.sources:
            if source.source in volumes:
                raise LiveRuntimeError("live mount sources are ambiguous")
            bind = "/target" if not source.folder else f"/target/{source.folder}"
            volumes[source.source] = {"bind": bind, "mode": "ro"}
        return {
            "command": self._helper_command("serve"),
            "volumes": volumes,
            "read_only": True,
            "network_disabled": True,
            "detach": True,
            "stdin_open": False,
            "tty": False,
            "remove": False,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "labels": {self.ORPHAN_LABEL: "true"},
        }

    def helper_argv(self):
        if self.sources is not None:
            return self._helper_command("serve")
        helper = Path(__file__).resolve().parents[2] / "live_file_helper.py"
        return [
            sys.executable,
            str(helper),
            "--root",
            self.root,
            "--read-only",
            "--max-chunk-bytes",
            str(self.max_chunk_bytes),
        ]

    def docker_run_kwargs(self):
        if self.sources is not None:
            return self._helper_run_kwargs()
        return {
            "command": self.helper_argv(),
            "volumes": {self.root: {"bind": "/target", "mode": "ro"}},
            "read_only": True,
            "network_disabled": True,
            "detach": True,
            "stdin_open": True,
            "labels": {self.ORPHAN_LABEL: "true"},
        }

    @property
    def helper_container_id(self):
        return self._helper_container.id if self._helper_container is not None else None

    def _helper_exec(self, operation, *arguments, output_limit):
        if self._helper_container is None or self._helper_container.closed:
            raise LiveRuntimeError("live helper runtime is unavailable", reason="helper_request_failed")
        try:
            result = self._helper_container.container.exec_run(
                cmd=self._helper_command(operation, *arguments),
                stdout=True,
                stderr=True,
                tty=False,
                demux=True,
            )
        except Exception as exc:
            if _is_permission_error(exc):
                raise LiveAccessDeniedError() from exc
            raise LiveRuntimeError("live helper request failed", reason="helper_request_failed") from exc

        if isinstance(result, tuple) and len(result) == 2:
            exit_code, output = result
        else:
            exit_code = getattr(result, "exit_code", None)
            output = getattr(result, "output", None)
        if exit_code == PROTECTED_VOLUME_EXIT_CODE:
            raise LiveAccessDeniedError()
        if exit_code != 0:
            raise LiveRuntimeError("live helper request failed", reason="helper_request_failed")
        if isinstance(output, tuple):
            stdout = output[0] or b""
        else:
            stdout = output or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if not isinstance(stdout, bytes) or len(stdout) > output_limit:
            raise LiveRuntimeError("live helper response exceeded the permitted bound", reason="helper_request_failed")
        return stdout

    def _helper_json(self, operation, *arguments):
        try:
            result = json.loads(
                self._helper_exec(
                    operation,
                    *arguments,
                    output_limit=self.MAX_HELPER_METADATA_BYTES,
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiveRuntimeError("live helper response is invalid", reason="helper_request_failed") from exc
        if not isinstance(result, dict):
            raise LiveRuntimeError("live helper response is invalid", reason="helper_request_failed")
        return result

    def sign(self, operation, path, nonce, timestamp):
        return sign_request(
            self.hmac_secret,
            {"operation": operation, "path": path, "nonce": nonce, "timestamp": timestamp},
        )

    def verify(self, operation, path, nonce, timestamp, signature):
        return verify_request(
            self.hmac_secret,
            {"operation": operation, "path": path, "nonce": nonce, "timestamp": timestamp},
            signature,
        )

    def list_entries(self, path="/", limit=100, cursor=None):
        limit = min(int(limit), self.MAX_ENTRIES)
        if limit <= 0:
            raise ValueError("live entry limit must be positive")
        if self.sources is not None:
            result = self._helper_json(
                "list",
                "--path",
                path,
                "--limit",
                limit,
                "--cursor",
                cursor or "",
            )
            entries = result.get("entries")
            next_cursor = result.get("next_cursor")
            if not isinstance(entries, list) or (next_cursor is not None and not isinstance(next_cursor, str)):
                raise LiveRuntimeError("live helper response is invalid", reason="helper_request_failed")
            self.last_activity = self.clock()
            return {"entries": entries, "next_cursor": next_cursor}

        try:
            directory = confined_path(self.root, path)
            metadata = os.stat(directory, follow_symlinks=False)
            if not stat_module.S_ISDIR(metadata.st_mode):
                raise LiveRuntimeError("live path is not a directory", reason="invalid_request", status=400)
            entries, after = [], cursor or ""
            with os.scandir(directory) as scan:
                for scanned, entry in enumerate(scan):
                    if scanned >= limit + 1:
                        break
                    if entry.name <= after or entry.is_symlink():
                        continue
                    stat, is_dir = entry.stat(follow_symlinks=False), entry.is_dir(follow_symlinks=False)
                    if not is_dir and not entry.is_file(follow_symlinks=False):
                        continue
                    relative = "/" + "/".join([part for part in (path.strip("/"), entry.name) if part])
                    entries.append(
                        {
                            "name": entry.name,
                            "path": relative,
                            "type": "dir" if is_dir else "file",
                            "size": None if is_dir else stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    )
                    if len(entries) >= limit:
                        break
        except OSError as exc:
            _raise_live_access_denied(exc)
            raise
        self.last_activity = self.clock()
        return {"entries": entries, "next_cursor": entries[-1]["name"] if len(entries) == limit else None}

    def read_file(self, path, offset=0, max_bytes=None, cancel_check=None):
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("live file offset must be non-negative")
        limit = min(self.max_file_bytes, max_bytes if max_bytes is not None else self.max_file_bytes)
        if limit < 0:
            raise LiveRuntimeError("live file size bound is invalid", reason="invalid_request", status=400)
        if self.sources is not None:
            raw = self._helper_exec(
                "read",
                "--path",
                path,
                "--offset",
                offset,
                "--max-bytes",
                limit,
                output_limit=limit,
            )
            for start in range(0, len(raw), self.max_chunk_bytes):
                if self._cancel.is_set() or (callable(cancel_check) and cancel_check()):
                    raise LiveRuntimeError("live file read canceled", reason="live_session_unavailable")
                chunk = raw[start : start + self.max_chunk_bytes]
                self.last_activity = self.clock()
                yield chunk
            return

        try:
            fd = open_confined(self.root, path)
        except OSError as exc:
            _raise_live_access_denied(exc)
            raise
        try:
            size = os.fstat(fd).st_size
            if size - offset > limit:
                raise LiveRuntimeError("live file exceeds the permitted bound")
            os.lseek(fd, offset, os.SEEK_SET)
            remaining = max(0, size - offset)
            while remaining:
                if self._cancel.is_set() or (callable(cancel_check) and cancel_check()):
                    raise LiveRuntimeError("live file read canceled")
                chunk = os.read(fd, min(self.max_chunk_bytes, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self.last_activity = self.clock()
                yield chunk
        except OSError as exc:
            _raise_live_access_denied(exc)
            raise
        finally:
            os.close(fd)

    def _assert_watch_root(self):
        if self._cancel.is_set():
            raise LiveRuntimeError("live watcher root is unavailable", reason="source_unavailable")
        if self.sources is not None:
            if self._helper_container is None or self._helper_container.closed:
                raise LiveRuntimeError("live watcher root is unavailable", reason="source_unavailable")
            return
        if os.path.realpath(self.root) != self.root:
            raise LiveRuntimeError("live watcher root is unavailable", reason="source_unavailable")
        try:
            metadata = os.stat(self.root, follow_symlinks=False)
        except OSError as exc:
            _raise_live_access_denied(exc)
            raise
        if not stat_module.S_ISDIR(metadata.st_mode):
            raise LiveRuntimeError("live watcher root is unavailable", reason="source_unavailable")

    def _watch_snapshot(self):
        self._assert_watch_root()
        if self.sources is not None:
            payload = self._helper_json("snapshot", "--max-entries", self.max_watch_entries)
            raw_entries, complete = payload.get("entries"), payload.get("complete")
            if not isinstance(raw_entries, dict) or not isinstance(complete, bool):
                raise LiveRuntimeError("live helper response is invalid", reason="helper_request_failed")
            entries = {}
            for path, value in raw_entries.items():
                if (
                    not isinstance(path, str)
                    or not path.startswith("/")
                    or not isinstance(value, list)
                    or len(value) != 3
                    or value[0] not in {"dir", "file"}
                ):
                    raise LiveRuntimeError("live helper response is invalid", reason="helper_request_failed")
                entries[path] = (value[0], value[1], value[2])
            return entries, complete

        entries, pending, scanned = {}, [("", self.root)], 0
        while pending:
            relative, directory = pending.pop()
            try:
                with os.scandir(directory) as scan:
                    for entry in scan:
                        if self._cancel.is_set():
                            raise LiveRuntimeError("live watcher canceled", reason="live_session_unavailable")
                        if scanned >= self.max_watch_entries:
                            return entries, False
                        scanned += 1
                        if entry.is_symlink():
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            is_file = entry.is_file(follow_symlinks=False)
                            stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if not is_dir and not is_file:
                            continue
                        path = "/" + "/".join(part for part in (relative, entry.name) if part)
                        entries[path] = (
                            "dir" if is_dir else "file",
                            None if is_dir else stat.st_size,
                            None if is_dir else stat.st_mtime_ns,
                        )
                        if is_dir:
                            pending.append((path, entry.path))
            except OSError as exc:
                if not relative:
                    _raise_live_access_denied(exc)
                    raise LiveRuntimeError("live watcher root is unavailable", reason="source_unavailable") from exc
        return entries, True

    def watch_changes(self, stop_event, on_change, ready_event=None):
        previous, complete = self._watch_snapshot()
        if ready_event is not None:
            ready_event.set()
        if not complete:
            on_change("resync_required", "/", "dir", None, None)
            return
        while not stop_event.wait(self.watch_interval) and not self._cancel.is_set():
            current, complete = self._watch_snapshot()
            if not complete:
                on_change("resync_required", "/", "dir", None, None)
                return
            for path in sorted(set(previous) | set(current)):
                before, after = previous.get(path), current.get(path)
                if before == after:
                    continue
                value = after or before
                kind = "created" if before is None else "deleted" if after is None else "modified"
                if on_change(kind, path, value[0], value[1], value[2]) is False:
                    return
            previous = current

    def expired(self, now=None, idle=False):
        now = self.clock() if now is None else now
        return now - self.created_at >= self.max_age or (idle and now - self.last_activity >= self.idle_ttl)

    def cancel(self):
        self._cancel.set()
        if self._helper_container is not None:
            self._helper_container.close()

    close = cancel

    @classmethod
    def cleanup_orphaned_helpers(cls, client, active_ids=()):
        result = {"inspected": 0, "removed": 0, "skipped": 0, "failed": 0, "removed_ids": []}
        for container in client.containers.list(all=True, filters={"label": f"{cls.ORPHAN_LABEL}=true"}):
            result["inspected"] += 1
            cid = getattr(container, "id", None)
            labels = getattr(container, "labels", None)
            if not isinstance(labels, dict) or labels.get(cls.ORPHAN_LABEL) != "true":
                result["skipped"] += 1
                continue
            if cid in active_ids:
                result["skipped"] += 1
                continue
            try:
                container.remove(force=True)
                result["removed"] += 1
                if len(result["removed_ids"]) < 20:
                    result["removed_ids"].append(cid)
            except Exception:
                result["failed"] += 1
        return result
