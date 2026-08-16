"""Optional, bounded Redis cache for Snapshot Explorer metadata reads."""

import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any, Callable, Optional

try:
    import redis as redis_module
except (ImportError, ModuleNotFoundError):
    redis_module = None


logger = logging.getLogger(__name__)


class RedisSnapshotCache:
    """A best-effort Redis cache with bounded values, keys, locks, and cardinality."""

    KEY_PREFIX = "sx:v1"
    CACHEABLE_OPERATIONS = frozenset(
        {"snapshots.list", "snapshot.ls", "snapshot.search", "snapshot.find"}
    )
    DEFAULT_TTL_SECONDS = 86400
    DEFAULT_MAX_ENTRIES = 1000
    DEFAULT_MAX_VALUE_BYTES = 256 * 1024
    DEFAULT_LOCK_TTL_MS = 5000
    DEFAULT_LOCK_WAIT_MS = 1000
    DEFAULT_LOCK_POLL_MS = 25
    DEFAULT_SOCKET_TIMEOUT_SECONDS = 1.0
    DEFAULT_SOCKET_CONNECT_TIMEOUT_SECONDS = 1.0
    MAX_TTL_SECONDS = 86400
    MAX_ENTRIES = 10000
    MAX_VALUE_BYTES = 1024 * 1024
    MAX_LOCK_TTL_MS = 30000
    MAX_LOCK_WAIT_MS = 10000
    MAX_LOCK_POLL_MS = 1000
    MAX_CONTEXT_MATERIAL_CHARS = 8192
    MAX_STRING_CHARS = 16384
    MAX_NESTING = 16
    MAX_LIST_ITEMS = 10000
    _SAFE_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
    _FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
    _SENSITIVE_KEY_PATTERN = re.compile(
        r"(?i)(password|secret|token|credential|authorization|repository|rclone|config|content)"
    )
    _URL_PATTERN = re.compile(
        r"(?i)(?:[a-z][a-z0-9+.-]*://|(?:s3|gs|azure|rclone|local|file):)[^\s\"']+"
    )
    _RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
    _INVALID = object()

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        client: Any = None,
        ttl_seconds: Any = DEFAULT_TTL_SECONDS,
        max_entries: Any = DEFAULT_MAX_ENTRIES,
        max_value_bytes: Any = DEFAULT_MAX_VALUE_BYTES,
        lock_ttl_ms: Any = DEFAULT_LOCK_TTL_MS,
        lock_wait_ms: Any = DEFAULT_LOCK_WAIT_MS,
        lock_poll_ms: Any = DEFAULT_LOCK_POLL_MS,
        socket_timeout: Any = DEFAULT_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout: Any = DEFAULT_SOCKET_CONNECT_TIMEOUT_SECONDS,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.ttl_seconds = self._bounded_int(
            ttl_seconds, self.DEFAULT_TTL_SECONDS, 1, self.MAX_TTL_SECONDS, "TTL"
        )
        self.max_entries = self._bounded_int(
            max_entries, self.DEFAULT_MAX_ENTRIES, 1, self.MAX_ENTRIES, "entry cap"
        )
        self.max_value_bytes = self._bounded_int(
            max_value_bytes, self.DEFAULT_MAX_VALUE_BYTES, 1, self.MAX_VALUE_BYTES, "value size"
        )
        self.lock_ttl_ms = self._bounded_int(
            lock_ttl_ms, self.DEFAULT_LOCK_TTL_MS, 100, self.MAX_LOCK_TTL_MS, "lock TTL"
        )
        self.lock_wait_ms = self._bounded_int(
            lock_wait_ms, self.DEFAULT_LOCK_WAIT_MS, 0, self.MAX_LOCK_WAIT_MS, "lock wait"
        )
        self.lock_poll_ms = self._bounded_int(
            lock_poll_ms, self.DEFAULT_LOCK_POLL_MS, 1, self.MAX_LOCK_POLL_MS, "lock poll"
        )
        self.socket_timeout = self._bounded_float(
            socket_timeout, self.DEFAULT_SOCKET_TIMEOUT_SECONDS, 0.1, 30.0, "socket timeout"
        )
        self.socket_connect_timeout = self._bounded_float(
            socket_connect_timeout,
            self.DEFAULT_SOCKET_CONNECT_TIMEOUT_SECONDS,
            0.1,
            30.0,
            "socket connect timeout",
        )
        self._sleep = sleep_fn or time.sleep
        self._clock = clock or time.monotonic
        self._score_clock = clock or time.time
        self._configured = bool(url) or client is not None
        self.client = client
        self._available = False

        if self.client is None and url:
            if redis_module is None:
                self._diagnose("dependency", RuntimeError("redis package is not installed"))
            else:
                try:
                    self.client = redis_module.Redis.from_url(
                        url,
                        socket_timeout=self.socket_timeout,
                        socket_connect_timeout=self.socket_connect_timeout,
                    )
                except Exception as exc:
                    self._diagnose("configuration", exc)
        if self.client is not None:
            self._available = self._health_check()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional["RedisSnapshotCache"]:
        """Build the cache from worker environment variables, or disable it when empty."""

        values = os.environ if env is None else env
        url = values.get("SNAPSHOT_EXPLORER_REDIS_URL")
        explicit_url = values.get("SNAPSHOT_EXPLORER_REDIS_URL_SET")
        nonempty_url = values.get("SNAPSHOT_EXPLORER_REDIS_URL_NONEMPTY")
        if (
            str(explicit_url or "").strip().lower() in {"1", "true", "yes", "on"}
            and str(nonempty_url or "").strip().lower() not in {"1", "true", "yes", "on"}
        ):
            return None
        if not url:
            return None

        return cls(
            url=url,
            ttl_seconds=values.get("SNAPSHOT_EXPLORER_REDIS_TTL_SECONDS", cls.DEFAULT_TTL_SECONDS),
            max_entries=values.get("SNAPSHOT_EXPLORER_REDIS_MAX_ENTRIES", cls.DEFAULT_MAX_ENTRIES),
            max_value_bytes=values.get(
                "SNAPSHOT_EXPLORER_REDIS_MAX_VALUE_BYTES", cls.DEFAULT_MAX_VALUE_BYTES
            ),
            lock_ttl_ms=values.get("SNAPSHOT_EXPLORER_REDIS_LOCK_TTL_MS", cls.DEFAULT_LOCK_TTL_MS),
            lock_wait_ms=values.get("SNAPSHOT_EXPLORER_REDIS_LOCK_WAIT_MS", cls.DEFAULT_LOCK_WAIT_MS),
            lock_poll_ms=values.get("SNAPSHOT_EXPLORER_REDIS_LOCK_POLL_MS", cls.DEFAULT_LOCK_POLL_MS),
        )

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int, label: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            logger.warning("Snapshot Redis cache %s is invalid; using bounded default", label)
            return default
        if parsed < minimum or parsed > maximum:
            logger.warning("Snapshot Redis cache %s is outside its bounds; using bounded default", label)
            return default
        return parsed

    @staticmethod
    def _bounded_float(value: Any, default: float, minimum: float, maximum: float, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            logger.warning("Snapshot Redis cache %s is invalid; using bounded default", label)
            return default
        if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
            logger.warning("Snapshot Redis cache %s is outside its bounds; using bounded default", label)
            return default
        return parsed

    @property
    def enabled(self) -> bool:
        return self._configured

    @staticmethod
    def _diagnostic_text(error: Exception) -> str:
        text = str(error or "")
        text = RedisSnapshotCache._URL_PATTERN.sub("<redacted-url>", text)
        text = re.sub(
            r"(?i)(password|secret|token|credential)=([^&\s]+)",
            r"\1=<redacted>",
            text,
        )
        text = text.strip()
        return text[:200] or error.__class__.__name__

    def _diagnose(self, operation: str, error: Exception) -> None:
        logger.warning(
            "Snapshot Redis cache %s unavailable: %s",
            operation,
            self._diagnostic_text(error),
        )

    def _health_check(self) -> bool:
        if self.client is None:
            self._available = False
            return False
        try:
            if not self.client.ping():
                raise RuntimeError("ping returned false")
            self._available = True
            return True
        except Exception as exc:
            self._available = False
            self._diagnose("health check", exc)
            return False

    @classmethod
    def _target_component(cls, value: Any) -> str:
        if value is None:
            raise ValueError("target ID is required for Redis cache isolation")
        text = str(value)
        if cls._SAFE_TARGET_PATTERN.fullmatch(text):
            return text
        if not text or len(text) > cls.MAX_CONTEXT_MATERIAL_CHARS:
            text = text[: cls.MAX_CONTEXT_MATERIAL_CHARS]
        return "t-" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]

    @classmethod
    def _repository_fingerprint(cls, context: Mapping[str, Any]) -> str:
        supplied = context.get("repository_fingerprint")
        if isinstance(supplied, str) and cls._FINGERPRINT_PATTERN.fullmatch(supplied):
            return supplied
        repository = context.get("repository")
        if not isinstance(repository, str) or not repository or "\x00" in repository:
            raise ValueError("repository fingerprint is required for Redis cache isolation")
        return hashlib.sha256(repository.encode("utf-8")).hexdigest()

    @classmethod
    def _material_hash(cls, *values: Any) -> str:
        material = []
        for value in values:
            if value is None:
                text = ""
            elif isinstance(value, str):
                text = value
            else:
                try:
                    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
                except Exception:
                    text = str(value)
            material.append(text[: cls.MAX_CONTEXT_MATERIAL_CHARS])
        return hashlib.sha256("\x00".join(material).encode("utf-8", errors="replace")).hexdigest()[:32]

    def _key_parts(self, context: Mapping[str, Any]) -> tuple[str, str, str]:
        target = self._target_component(context.get("target_id"))
        repository = self._repository_fingerprint(context)
        operation = context.get("operation")
        if operation not in self.CACHEABLE_OPERATIONS:
            raise ValueError("operation is not cacheable")
        return target, repository, operation

    def key_for(self, context: Mapping[str, Any]) -> str:
        """Return a bounded data key without embedding repository or query material."""

        target, repository, operation = self._key_parts(context)
        generation_hash = self._material_hash(context.get("cache_generation", 0))
        snapshot_hash = self._material_hash(context.get("snapshot_id"))
        path_hash = self._material_hash(
            context.get("path", "/"), context.get("query"), context.get("max_entries")
        )
        return f"{self.KEY_PREFIX}:{target}:{repository}:g{generation_hash}:entry:{operation}:{snapshot_hash}:{path_hash}"

    def _index_key(self, context: Mapping[str, Any]) -> str:
        target, repository, _ = self._key_parts(context)
        generation_hash = self._material_hash(context.get("cache_generation", 0))
        return f"{self.KEY_PREFIX}:{target}:{repository}:g{generation_hash}:index"

    def _lock_key(self, context: Mapping[str, Any]) -> str:
        return f"{self.key_for(context)}:lock"

    @classmethod
    def _sanitized_value(cls, value: Any, repository: Optional[str], depth: int = 0, key: str = "") -> Any:
        if depth > cls.MAX_NESTING:
            return cls._INVALID
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else cls._INVALID
        if isinstance(value, str):
            if len(value) > cls.MAX_STRING_CHARS:
                return cls._INVALID
            if cls._URL_PATTERN.search(value) or (repository and repository in value):
                return cls._INVALID
            return value
        if isinstance(value, list):
            if len(value) > cls.MAX_LIST_ITEMS:
                return cls._INVALID
            sanitized = []
            for item in value:
                item_value = cls._sanitized_value(item, repository, depth + 1, key)
                if item_value is cls._INVALID:
                    return cls._INVALID
                sanitized.append(item_value)
            return sanitized
        if isinstance(value, dict):
            if len(value) > 128:
                return cls._INVALID
            sanitized = {}
            for item_key, item in value.items():
                if not isinstance(item_key, str) or len(item_key) > 128:
                    return cls._INVALID
                if cls._SENSITIVE_KEY_PATTERN.search(item_key):
                    return cls._INVALID
                item_value = cls._sanitized_value(item, repository, depth + 1, item_key)
                if item_value is cls._INVALID:
                    return cls._INVALID
                sanitized[item_key] = item_value
            return sanitized
        return cls._INVALID

    def _encoded_value(self, context: Mapping[str, Any], value: Any) -> Optional[bytes]:
        if not isinstance(value, dict):
            return None
        allowed = {"schema_version", "status", "status_code", "target_id", "entries", "snapshots"}
        if not set(value).issubset(allowed):
            return None
        if "entries" not in value and "snapshots" not in value:
            return None
        for collection_name in ("entries", "snapshots"):
            if collection_name in value and not isinstance(value[collection_name], list):
                return None
        if "status" in value and value.get("status") != "succeeded":
            return None
        target_id = value.get("target_id")
        context_target = context.get("target_id")
        if target_id is not None and context_target is not None and str(target_id) != str(context_target):
            return None

        repository = context.get("repository")
        repository = repository if isinstance(repository, str) else None
        sanitized = self._sanitized_value(value, repository)
        if sanitized is self._INVALID:
            return None
        try:
            encoded = json.dumps(
                sanitized,
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        if len(encoded) > self.max_value_bytes:
            return None
        return encoded

    def get(self, context: Mapping[str, Any]) -> Any:
        if not self._health_check():
            return None
        try:
            key = self.key_for(context)
            raw = self.client.get(key)
            if raw is None:
                return None
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                value = json.loads(raw)
            except (UnicodeDecodeError, TypeError, ValueError):
                self.client.delete(key)
                return None
            encoded = self._encoded_value(context, value)
            if encoded is None:
                self.client.delete(key)
                return None
            return value
        except Exception as exc:
            self._available = False
            self._diagnose("read", exc)
            return None

    def _evict_oldest(self, index_key: str) -> None:
        count = int(self.client.zcard(index_key))
        excess = count - self.max_entries
        if excess <= 0:
            return
        old_keys = self.client.zrange(index_key, 0, excess - 1)
        if not old_keys:
            return
        self.client.delete(*old_keys)
        self.client.zrem(index_key, *old_keys)

    def set(self, context: Mapping[str, Any], value: Any) -> bool:
        if not self._health_check():
            return False
        encoded = self._encoded_value(context, value)
        if encoded is None:
            return False
        key = None
        try:
            key = self.key_for(context)
            index_key = self._index_key(context)
            stored = self.client.set(key, encoded, ex=self.ttl_seconds)
            if stored is False:
                return False
            self.client.zadd(index_key, {key: self._score_clock()})
            self.client.expire(index_key, self.ttl_seconds)
            self._evict_oldest(index_key)
            self._available = True
            return True
        except Exception as exc:
            self._available = False
            try:
                if key is None:
                    raise RuntimeError("cache key was not created")
                self.client.delete(key)
            except Exception:
                pass
            self._diagnose("write", exc)
            return False

    def acquire(self, context: Mapping[str, Any]) -> Optional[str]:
        if not self._health_check():
            return None
        try:
            token = uuid.uuid4().hex
            acquired = self.client.set(
                self._lock_key(context),
                token,
                nx=True,
                px=self.lock_ttl_ms,
            )
            return token if acquired else None
        except Exception as exc:
            self._available = False
            self._diagnose("lock", exc)
            return None

    def release(self, context: Mapping[str, Any], token: str) -> bool:
        if not token or self.client is None:
            return False
        try:
            result = self.client.eval(self._RELEASE_SCRIPT, 1, self._lock_key(context), token)
            return bool(result)
        except Exception as exc:
            self._diagnose("unlock", exc)
            return False

    @staticmethod
    def _is_canceled(cancel_check: Optional[Callable[[], bool]]) -> bool:
        if not callable(cancel_check):
            return False
        try:
            return bool(cancel_check())
        except Exception:
            return False

    def get_or_compute(
        self,
        context: Mapping[str, Any],
        compute_fn: Callable[[], Any],
        *,
        cacheable: Optional[Callable[[Any], bool]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[Any, bool, str]:
        """Return ``(value, cache_hit, source)`` while never making Redis required."""

        def compute(source: str, allow_cache: bool) -> tuple[Any, bool, str]:
            value = compute_fn()
            should_cache = allow_cache and not self._is_canceled(cancel_check)
            if should_cache and cacheable is not None:
                try:
                    should_cache = bool(cacheable(value))
                except Exception as exc:
                    should_cache = False
                    self._diagnose("cacheability", exc)
            if should_cache and not self._is_canceled(cancel_check):
                stored = self.set(context, value)
                if not stored and not self._available:
                    source = "restic-fallback"
            return value, False, source

        try:
            self.key_for(context)
        except (TypeError, ValueError):
            return compute("restic-fallback", False)

        if self._is_canceled(cancel_check):
            return compute("restic-fallback", False)
        if not self._health_check():
            return compute("restic-fallback", False)

        cached = self.get(context)
        if cached is not None:
            return cached, True, "redis"

        token = self.acquire(context)
        if token:
            try:
                cached = self.get(context)
                if cached is not None:
                    return cached, True, "redis"
                source = "restic" if self._available else "restic-fallback"
                return compute(source, True)
            finally:
                self.release(context, token)

        deadline = self._clock() + (self.lock_wait_ms / 1000.0)
        while self._clock() < deadline:
            if self._is_canceled(cancel_check):
                break
            cached = self.get(context)
            if cached is not None:
                return cached, True, "redis"
            retry_token = self.acquire(context)
            if retry_token:
                try:
                    cached = self.get(context)
                    if cached is not None:
                        return cached, True, "redis"
                    source = "restic" if self._available else "restic-fallback"
                    return compute(source, True)
                finally:
                    self.release(context, retry_token)
            remaining = max(0.0, deadline - self._clock())
            if remaining:
                self._sleep(min(self.lock_poll_ms / 1000.0, remaining))

        return compute("restic-fallback", False)
