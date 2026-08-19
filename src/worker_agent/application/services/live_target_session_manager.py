import os, threading, time
from dataclasses import dataclass
from src.worker_agent.infrastructure.adapters.live_file_runtime import LiveFileRuntime, LiveFileSource
@dataclass(frozen=True)
class LiveTargetSessionKey:
    target_id: str
    config_revision: str
    worker_id: str
@dataclass
class _Session:
    runtime: LiveFileRuntime
    subscribers: int = 0
    watchers: int = 0
    watch_stop: object = None
    watch_ready: object = None
    watch_thread: object = None
class LiveTargetSessionHandle:
    def __init__(self, manager, key, session): self.manager, self.key, self.runtime, self._session, self._released = manager, key, session.runtime, session, False
    def list_entries(self, *args, **kwargs): return self.runtime.list_entries(*args, **kwargs)
    def read_file(self, *args, **kwargs): return self.runtime.read_file(*args, **kwargs)
    def release(self):
        if not self._released: self._released = True; self.manager._release(self.key, self._session)
    close = release
class LiveTargetSessionManager:
    """One read-only helper/runtime per target, revision, and worker."""
    def __init__(self, root_resolver=None, runtime_factory=LiveFileRuntime, max_sessions=32, clock=None, change_publisher=None): self.root_resolver, self.runtime_factory, self.max_sessions, self.clock, self.change_publisher, self._lock, self._sessions = root_resolver, runtime_factory, max_sessions, clock or time.monotonic, change_publisher, threading.RLock(), {}
    @staticmethod
    def _key(value): return value if isinstance(value, LiveTargetSessionKey) else LiveTargetSessionKey(*value)
    def _expected_root(self, key, target_root):
        expected = self.root_resolver(key.target_id) if self.root_resolver else target_root
        if not expected or (target_root is not None and self._source_identity(target_root) != self._source_identity(expected)): raise ValueError("live root must come from the server inventory")
        return expected
    @staticmethod
    def _source_identity(source):
        if source is None: return None
        if isinstance(source, LiveFileSource): return ("helper", source.identity)
        if isinstance(source, (list, tuple)) and source and all(isinstance(item, LiveFileSource) for item in source):
            return ("helper", tuple(item.identity for item in source))
        return ("local", os.path.realpath(os.fspath(source)))
    def _start_watcher(self, key, session):
        if not callable(self.change_publisher) or session.watch_thread is not None: return
        stop, failed, ready = threading.Event(), threading.Event(), threading.Event(); session.watch_stop, session.watch_ready = stop, ready
        def publish(kind, path, entry_type, size, mtime_ns):
            try:
                if self.change_publisher(key, kind, path, entry_type, size, mtime_ns) is False: failed.set(); return False
                return True
            except Exception:
                failed.set(); return False
        def run():
            try: session.runtime.watch_changes(stop, publish, ready)
            except Exception: failed.set()
            finally:
                if failed.is_set(): self.invalidate(key)
                with self._lock:
                    if self._sessions.get(key) is session: session.watch_thread = None
        session.watch_thread = threading.Thread(target=run, daemon=True, name="live-target-watcher"); session.watch_thread.start()
    def _get_or_create(self, key, expected):
        session = self._sessions.get(key)
        if session is None:
            if len(self._sessions) >= self.max_sessions: raise RuntimeError("live session limit reached")
            session = self._sessions[key] = _Session(self.runtime_factory(expected))
        self._start_watcher(key, session)
        return session
    def attach(self, key, target_root=None):
        key = self._key(key); expected = self._expected_root(key, target_root)
        with self._lock:
            session = self._get_or_create(key, expected)
            session.subscribers += 1; return LiveTargetSessionHandle(self, key, session)
    def begin_watch(self, key, target_root=None):
        key = self._key(key); expected = self._expected_root(key, target_root)
        with self._lock:
            session = self._get_or_create(key, expected); session.watchers += 1
        if session.watch_ready is not None: session.watch_ready.wait(timeout=.5)
        return True
    def end_watch(self, key):
        key = self._key(key)
        with self._lock:
            session = self._sessions.get(key)
            if session is None: return False
            session.watchers = max(0, session.watchers - 1)
        return True
    def _release(self, key, session):
        with self._lock:
            if self._sessions.get(key) is session: session.subscribers = max(0, session.subscribers - 1)
    def invalidate(self, key):
        with self._lock: session = self._sessions.pop(self._key(key), None)
        if session:
            if session.watch_stop is not None: session.watch_stop.set()
            session.runtime.cancel()
            if session.watch_thread is not None and session.watch_thread is not threading.current_thread(): session.watch_thread.join(timeout=.2)
            return True
        return False
    def cleanup(self, now=None):
        now = self.clock() if now is None else now
        with self._lock: expired = [key for key, session in self._sessions.items() if session.runtime.expired(now, idle=session.subscribers == 0 and session.watchers == 0)]
        for key in expired: self.invalidate(key)
        return len(expired)
    def active_helper_ids(self):
        with self._lock:
            return tuple(
                helper_id
                for session in self._sessions.values()
                if (helper_id := getattr(session.runtime, "helper_container_id", None))
            )
    def cleanup_orphaned(self, client, active_ids=None):
        if active_ids is None: active_ids = self.active_helper_ids()
        return LiveFileRuntime.cleanup_orphaned_helpers(client, active_ids)
    def close(self):
        with self._lock: keys = list(self._sessions)
        for key in keys: self.invalidate(key)
    @property
    def session_count(self):
        with self._lock: return len(self._sessions)
