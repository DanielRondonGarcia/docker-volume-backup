import base64, hashlib, hmac, json, queue, secrets, threading, time
from collections import deque
from dataclasses import dataclass
class LiveSessionError(RuntimeError): pass
class LiveLimitError(LiveSessionError): pass
class LiveCursorError(LiveSessionError): pass
@dataclass(frozen=True)
class LiveSessionKey:
    target_id: str
    config_revision: str
    worker_id: str
@dataclass(frozen=True)
class LiveChange:
    event_id: int
    kind: str
    path: str
    entry_type: str
    size: int | None = None
    mtime_ns: int | None = None
    def projection(self): return {"id": self.event_id, "kind": self.kind, "path": self.path, "entry_type": self.entry_type, "size": self.size, "mtime_ns": self.mtime_ns}
class LiveSubscription:
    def __init__(self, service, key, subscriber_id, size):
        self.service, self.key, self.subscriber_id = service, key, subscriber_id; self._queue, self._closed, self.cursor = queue.Queue(size), threading.Event(), 0
    @property
    def closed(self): return self._closed.is_set()
    def get(self, timeout=None): return self._queue.get(timeout=timeout)
    def acknowledge(self, event_id):
        if not isinstance(event_id, int) or event_id < self.cursor: raise LiveCursorError("cursor must be monotonic")
        self.cursor = event_id
    def close(self): self.service._detach(self)
    def _offer(self, event):
        if self.closed: return
        try: self._queue.put_nowait({"type": "changed", "event": event.projection()}); return
        except queue.Full: pass
        while True:
            try: self._queue.get_nowait()
            except queue.Empty: break
        try: self._queue.put_nowait({"type": "resync_required", "reason": "queue_overflow"})
        except queue.Full: pass
class LiveRawStream:
    def __init__(self, service, key, stream_id, queue_size, max_bytes, max_chunk):
        self.service, self.key, self.stream_id = service, key, stream_id; self._queue, self._max_bytes, self._max_chunk = queue.Queue(queue_size), max_bytes, max_chunk; self._closed, self._bytes = threading.Event(), 0
    @property
    def closed(self): return self._closed.is_set()
    def push(self, chunk):
        if self.closed: return False
        if not isinstance(chunk, (bytes, bytearray, memoryview)) or len(chunk) > self._max_chunk: self.cancel(); raise LiveLimitError("raw chunk exceeds the permitted bound")
        self._bytes += len(chunk)
        if self._bytes > self._max_bytes: self.cancel(); raise LiveLimitError("raw stream exceeds the permitted bound")
        try: self._queue.put_nowait(bytes(chunk))
        except queue.Full as exc: self.cancel(); raise LiveLimitError("raw stream backpressure limit reached") from exc
        return True
    def __iter__(self):
        while not self.closed or not self._queue.empty():
            try: yield self._queue.get(timeout=.05)
            except queue.Empty:
                if self.closed: return
    def close(self):
        if not self._closed.is_set(): self._closed.set(); self.service._close_stream(self)
    def cancel(self):
        if not self._closed.is_set():
            self._closed.set()
            while True:
                try: self._queue.get_nowait()
                except queue.Empty: break
            self.service._close_stream(self)
class _Session:
    def __init__(self, key, max_events, now):
        self.key, self.events = key, deque(maxlen=max_events); self.next_id, self.created_at, self.last_activity = 1, now, now; self.subscribers, self.streams = {}, {}
class LiveFileService:
    """Process-local live state with no JobRepository or durable job projection."""
    def __init__(self, cursor_secret=None, max_sessions=64, max_subscribers=8, queue_size=16, max_events=128, max_streams=4, stream_queue_size=4, max_chunk_bytes=64 * 1024, max_stream_bytes=64 * 1024 * 1024, idle_ttl=300.0, max_age=3600.0, clock=None):
        limits = (max_sessions, max_subscribers, queue_size, max_events, max_streams, stream_queue_size, max_chunk_bytes, max_stream_bytes, idle_ttl, max_age)
        if any(value <= 0 for value in limits): raise ValueError("live limits must be positive")
        self._secret = (cursor_secret.encode() if isinstance(cursor_secret, str) else cursor_secret) or secrets.token_bytes(32); self.max_sessions, self.max_subscribers, self.queue_size = max_sessions, max_subscribers, queue_size; self.max_events, self.max_streams = max_events, max_streams; self.stream_queue_size, self.max_chunk_bytes, self.max_stream_bytes = stream_queue_size, max_chunk_bytes, max_stream_bytes; self.idle_ttl, self.max_age, self.clock = idle_ttl, max_age, clock or time.monotonic; self._lock, self._sessions, self._revoked = threading.RLock(), {}, {}
    @staticmethod
    def _key(key): return key if isinstance(key, LiveSessionKey) else LiveSessionKey(*key)
    def _session(self, key, create=False):
        key = self._key(key)
        if key in self._revoked: raise LiveSessionError(f"live session is {self._revoked[key]}")
        session = self._sessions.get(key)
        if session is None and create:
            if len(self._sessions) >= self.max_sessions: raise LiveLimitError("live session limit reached")
            session = self._sessions[key] = _Session(key, self.max_events, self.clock())
        if session is None: raise LiveSessionError("live session not found")
        session.last_activity = self.clock(); return session
    def attach(self, key):
        with self._lock:
            session = self._session(key, True)
            if len(session.subscribers) >= self.max_subscribers: raise LiveLimitError("live subscriber limit reached")
            subscriber_id = secrets.token_urlsafe(12)
            while subscriber_id in session.subscribers: subscriber_id = secrets.token_urlsafe(12)
            result = LiveSubscription(self, session.key, subscriber_id, self.queue_size); session.subscribers[subscriber_id] = result; return result
    def publish_change(self, key, kind, path, entry_type, size=None, mtime_ns=None):
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096: raise ValueError("live change path is invalid")
        with self._lock:
            session = self._session(key, True); event = LiveChange(session.next_id, str(kind), path, str(entry_type), size, mtime_ns); session.next_id += 1; session.events.append(event); subscribers = tuple(session.subscribers.values())
        for subscriber in subscribers: subscriber._offer(event)
        return event
    def issue_cursor(self, key, event_id):
        key = self._key(key)
        if not isinstance(event_id, int) or event_id < 0: raise LiveCursorError("invalid live cursor")
        body = json.dumps([key.target_id, key.config_revision, key.worker_id, event_id], separators=(",", ":")).encode(); signature = hmac.new(self._secret, body, hashlib.sha256).digest(); return base64.urlsafe_b64encode(body + b"." + signature).decode().rstrip("=")
    def read_cursor(self, key, cursor):
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)); body, signature = raw[:-33], raw[-32:]
            if raw[-33:-32] != b"." or not hmac.compare_digest(hmac.new(self._secret, body, hashlib.sha256).digest(), signature): raise ValueError
            target, revision, worker, event_id = json.loads(body); key = self._key(key)
            if [target, revision, worker] != [key.target_id, key.config_revision, key.worker_id] or not isinstance(event_id, int): raise ValueError
            return event_id
        except (ValueError, TypeError, json.JSONDecodeError, IndexError): raise LiveCursorError("invalid or stale live cursor") from None
    def issue_entry_cursor(self, key, path, entry_name):
        key = self._key(key)
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(entry_name, str) or not entry_name or "/" in entry_name:
            raise LiveCursorError("invalid live entry cursor")
        body = json.dumps(["entry", key.target_id, key.config_revision, key.worker_id, path, entry_name], separators=(",", ":")).encode()
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + b"." + signature).decode().rstrip("=")
    def read_entry_cursor(self, key, path, cursor):
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)); body, signature = raw[:-33], raw[-32:]
            if raw[-33:-32] != b"." or not hmac.compare_digest(hmac.new(self._secret, body, hashlib.sha256).digest(), signature): raise ValueError
            marker, target, revision, worker, signed_path, entry_name = json.loads(body); key = self._key(key)
            if [marker, target, revision, worker, signed_path] != ["entry", key.target_id, key.config_revision, key.worker_id, path] or not isinstance(entry_name, str) or not entry_name or "/" in entry_name: raise ValueError
            return entry_name
        except (ValueError, TypeError, json.JSONDecodeError, IndexError): raise LiveCursorError("invalid or stale live entry cursor") from None
    def replay(self, key, cursor=None):
        with self._lock:
            session = self._session(key); after = self.read_cursor(session.key, cursor) if cursor else 0; oldest = session.events[0].event_id if session.events else session.next_id
            if after < oldest - 1: return {"resync_required": True, "reason": "replay_gap", "events": []}
            events = [event.projection() for event in session.events if event.event_id > after]; return {"resync_required": False, "events": events, "next_cursor": self.issue_cursor(session.key, events[-1]["id"] if events else after)}
    def open_raw_stream(self, key):
        with self._lock:
            session = self._session(key, True)
            if len(session.streams) >= self.max_streams: raise LiveLimitError("raw stream limit reached")
            stream = LiveRawStream(self, session.key, secrets.token_urlsafe(12), self.stream_queue_size, self.max_stream_bytes, self.max_chunk_bytes); session.streams[stream.stream_id] = stream; return stream
    def invalidate(self, key, reason="revoked"):
        key = self._key(key)
        with self._lock:
            session = self._sessions.pop(key, None); self._revoked[key] = reason
            if len(self._revoked) > self.max_sessions * 4: self._revoked.pop(next(iter(self._revoked)))
            if session is None: return False
            items = list(session.subscribers.values()) + list(session.streams.values()); session.subscribers.clear(); session.streams.clear()
        for item in items: item.cancel() if isinstance(item, LiveRawStream) else item._closed.set()
        return True
    def invalidate_target(self, target_id, reason="revoked"):
        with self._lock: keys = [key for key in self._sessions if key.target_id == target_id]
        for key in keys: self.invalidate(key, reason)
        return len(keys)
    def reap(self, now=None):
        now = self.clock() if now is None else now
        with self._lock: expired = [key for key, session in self._sessions.items() if now - session.created_at >= self.max_age or (not session.subscribers and now - session.last_activity >= self.idle_ttl)]
        for key in expired: self.invalidate(key, "expired")
        return len(expired)
    def _detach(self, subscription):
        with self._lock:
            session = self._sessions.get(subscription.key)
            if session: session.subscribers.pop(subscription.subscriber_id, None); subscription._closed.set()
    def _close_stream(self, stream):
        with self._lock:
            session = self._sessions.get(stream.key)
            if session: session.streams.pop(stream.stream_id, None)
    @property
    def session_count(self):
        with self._lock: return len(self._sessions)
