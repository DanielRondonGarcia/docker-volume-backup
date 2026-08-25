import hashlib
import json
import unittest
from unittest.mock import Mock

from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.redis_cache import RedisSnapshotCache


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeRedis:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expiry = {}
        self.sorted_sets = {}
        self.set_calls = []
        self.available = True

    def _purge(self, key):
        expires_at = self.expiry.get(key)
        if expires_at is not None and expires_at <= self.clock():
            self.values.pop(key, None)
            self.expiry.pop(key, None)
            self.sorted_sets.pop(key, None)

    def ping(self):
        if not self.available:
            raise TimeoutError("redis://user:password@example.invalid timed out")
        return True

    def get(self, key):
        self._purge(key)
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False, px=None):
        self._purge(key)
        self.set_calls.append({"key": key, "value": value, "ex": ex, "nx": nx, "px": px})
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expiry[key] = self.clock() + ex
        elif px is not None:
            self.expiry[key] = self.clock() + (px / 1000.0)
        else:
            self.expiry.pop(key, None)
        return True

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            self._purge(key)
            if key in self.values:
                deleted += 1
            self.values.pop(key, None)
            self.expiry.pop(key, None)
        return deleted

    def zadd(self, key, mapping):
        self._purge(key)
        members = self.sorted_sets.setdefault(key, {})
        for member, score in mapping.items():
            members[member] = score
        return len(mapping)

    def expire(self, key, seconds):
        self.expiry[key] = self.clock() + seconds
        return True

    def zcard(self, key):
        self._purge(key)
        return len(self.sorted_sets.get(key, {}))

    def zrange(self, key, start, stop):
        self._purge(key)
        members = sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: (item[1], item[0]))
        return [member for member, _ in members[start : stop + 1]]

    def zrem(self, key, *members):
        values = self.sorted_sets.get(key, {})
        removed = 0
        for member in members:
            if member in values:
                removed += 1
                del values[member]
        return removed

    def eval(self, _script, _numkeys, key, token):
        if self.get(key) == token:
            return self.delete(key)
        return 0


class RedisSnapshotCacheTests(unittest.TestCase):
    REPOSITORY = "s3://user:password@example.invalid/private/repository"

    def make_context(
        self,
        operation="snapshot.ls",
        path="/",
        query=None,
        snapshot_id="snap-1",
        cache_generation=0,
        max_log_bytes=None,
    ):
        context = {
            "target_id": "target-a",
            "repository": self.REPOSITORY,
            "operation": operation,
            "snapshot_id": snapshot_id,
            "path": path,
            "query": query,
            "cache_generation": cache_generation,
        }
        if max_log_bytes is not None:
            context["max_log_bytes"] = max_log_bytes
        return context

    def make_cache(self, **kwargs):
        clock = kwargs.pop("clock", FakeClock())
        client = kwargs.pop("client", FakeRedis(clock))
        cache = RedisSnapshotCache(client=client, clock=clock, sleep_fn=clock.sleep, **kwargs)
        return cache, client, clock

    @staticmethod
    def successful_value(target_id="target-a"):
        return {
            "schema_version": 1,
            "status": JobStatus.SUCCEEDED,
            "status_code": 0,
            "target_id": target_id,
            "entries": [{"type": "file", "path": "/README.md"}],
        }

    def test_versioned_keys_isolate_target_repository_and_hide_material(self):
        cache, _, _ = self.make_cache()
        context = self.make_context(operation="snapshot.search", path="/private", query="password hunter2")

        key = cache.key_for(context)
        repository_fingerprint = hashlib.sha256(self.REPOSITORY.encode()).hexdigest()

        self.assertTrue(key.startswith("sx:v4:target-a:"))
        self.assertIn(repository_fingerprint, key)
        self.assertIn(":entry:snapshot.search:", key)
        self.assertNotIn(self.REPOSITORY, key)
        self.assertNotIn("password hunter2", key)
        self.assertIn(repository_fingerprint, cache._index_key(context))
        self.assertIn(repository_fingerprint, cache._lock_key(context))

    def test_old_cache_namespace_values_are_not_reused(self):
        cache, client, _ = self.make_cache()
        context = self.make_context()
        new_key = cache.key_for(context)
        old_key = new_key.replace("sx:v4:", "sx:v3:", 1)
        client.values[old_key] = json.dumps(self.successful_value()).encode("utf-8")

        self.assertIsNone(cache.get(context))
        self.assertIsNotNone(client.get(old_key))

    def test_cache_generation_changes_entry_and_index_keys(self):
        cache, _, _ = self.make_cache()
        first = self.make_context(cache_generation=0)
        second = self.make_context(cache_generation=1)

        self.assertNotEqual(cache.key_for(first), cache.key_for(second))
        self.assertNotEqual(cache._index_key(first), cache._index_key(second))

    def test_snapshot_about_cache_is_isolated_and_accepts_only_projected_stats(self):
        cache, client, _ = self.make_cache()
        context = self.make_context(operation="snapshot.about", snapshot_id="abcdef12")
        other_snapshot = self.make_context(operation="snapshot.about", snapshot_id="abcdef13")
        other_target = {**context, "target_id": "target-b"}
        other_repository = {**context, "repository": "local:/other-repository"}
        value = {
            "schema_version": 1,
            "status": JobStatus.SUCCEEDED,
            "status_code": 0,
            "target_id": "target-a",
            "stats": {"total_size": 2048, "total_file_count": 3, "snapshots_count": 1},
        }

        calls = []
        first = cache.get_or_compute(context, lambda: calls.append("compute") or value, cacheable=lambda item: True)
        second = cache.get_or_compute(context, lambda: calls.append("compute-again") or value, cacheable=lambda item: True)

        self.assertEqual(first, (value, False, "restic"))
        self.assertEqual(second, (value, True, "redis"))
        self.assertEqual(calls, ["compute"])
        self.assertNotEqual(cache.key_for(context), cache.key_for(other_snapshot))
        self.assertNotEqual(cache.key_for(context), cache.key_for(other_target))
        self.assertNotEqual(cache.key_for(context), cache.key_for(other_repository))
        self.assertTrue(cache.set(context, value))
        for unsafe in (
            {**value, "stats": {"total_size": 1, "total_file_count": 1, "snapshots_count": 1, "logs": "secret"}},
            {**value, "stats": {"total_size": 1, "total_file_count": "1", "snapshots_count": 1}},
            {**value, "repository": "local:/private"},
        ):
            self.assertFalse(cache.set(context, unsafe))
        stored = json.loads(client.get(cache.key_for(context)).decode("utf-8"))
        self.assertEqual(stored["stats"], value["stats"])

    def test_snapshot_about_cache_generation_changes_entry_key(self):
        cache, _, _ = self.make_cache()
        first = self.make_context(operation="snapshot.about", snapshot_id="abcdef12", cache_generation=0)
        second = self.make_context(operation="snapshot.about", snapshot_id="abcdef12", cache_generation=1)

        self.assertNotEqual(cache.key_for(first), cache.key_for(second))

    def test_listing_output_limit_changes_snapshot_entry_key(self):
        cache, _, _ = self.make_cache()
        default = self.make_context(max_log_bytes=4 * 1024 * 1024)
        expanded = self.make_context(max_log_bytes=8 * 1024 * 1024)

        self.assertNotEqual(cache.key_for(default), cache.key_for(expanded))

    def test_empty_environment_url_disables_cache(self):
        self.assertIsNone(RedisSnapshotCache.from_env({"SNAPSHOT_EXPLORER_REDIS_URL": ""}))
        self.assertIsNone(
            RedisSnapshotCache.from_env(
                {
                    "SNAPSHOT_EXPLORER_REDIS_URL": "redis://redis:6379/0",
                    "SNAPSHOT_EXPLORER_REDIS_URL_SET": "true",
                    "SNAPSHOT_EXPLORER_REDIS_URL_NONEMPTY": "",
                }
            )
        )

    def test_default_ttl_is_durable_but_bounded(self):
        self.assertEqual(RedisSnapshotCache.DEFAULT_TTL_SECONDS, 86400)
        self.assertLessEqual(
            RedisSnapshotCache.DEFAULT_TTL_SECONDS,
            RedisSnapshotCache.MAX_TTL_SECONDS,
        )

    def test_cache_miss_then_hit_preserves_value_and_reports_source(self):
        cache, client, _ = self.make_cache()
        calls = []
        value = self.successful_value()

        first = cache.get_or_compute(
            self.make_context(),
            lambda: calls.append("computed") or value,
            cacheable=lambda result: result["status"] == JobStatus.SUCCEEDED,
        )
        second = cache.get_or_compute(
            self.make_context(),
            lambda: calls.append("computed-again") or value,
            cacheable=lambda result: result["status"] == JobStatus.SUCCEEDED,
        )

        self.assertEqual(first, (value, False, "restic"))
        self.assertEqual(second, (value, True, "redis"))
        self.assertEqual(calls, ["computed"])
        self.assertTrue(any(key.startswith("sx:v4:") for key in client.values))
        self.assertTrue(all(self.REPOSITORY not in key for key in client.values))

    def test_singleflight_lock_uses_nx_px_and_release_is_token_safe(self):
        cache, client, _ = self.make_cache(lock_ttl_ms=500)
        context = self.make_context()

        owner_token = cache.acquire(context)
        other_token = cache.acquire(context)

        self.assertIsNotNone(owner_token)
        self.assertIsNone(other_token)
        lock_calls = [call for call in client.set_calls if call["nx"]]
        self.assertTrue(lock_calls)
        self.assertEqual(lock_calls[0]["px"], 500)
        self.assertFalse(cache.release(context, "wrong-token"))
        self.assertIsNotNone(client.get(cache._lock_key(context)))
        self.assertTrue(cache.release(context, owner_token))
        self.assertIsNone(client.get(cache._lock_key(context)))

    def test_singleflight_wait_is_bounded_and_computes_directly(self):
        cache, _, clock = self.make_cache(lock_wait_ms=40, lock_poll_ms=10)
        context = self.make_context()
        owner_token = cache.acquire(context)
        calls = []

        value, cache_hit, source = cache.get_or_compute(
            context,
            lambda: calls.append("computed") or self.successful_value(),
            cacheable=lambda result: True,
        )

        self.assertIsNotNone(owner_token)
        self.assertEqual(value["entries"][0]["path"], "/README.md")
        self.assertFalse(cache_hit)
        self.assertEqual(source, "restic-fallback")
        self.assertEqual(calls, ["computed"])
        self.assertGreaterEqual(clock.value, 0.04)
        self.assertLess(clock.value, 0.2)
        cache.release(context, owner_token)

    def test_wait_returns_another_owners_cached_value(self):
        cache, _, clock = self.make_cache(lock_wait_ms=50, lock_poll_ms=10)
        context = self.make_context()
        owner_token = cache.acquire(context)
        published = [False]

        def sleep_and_publish(seconds):
            clock.sleep(seconds)
            if not published[0]:
                cache.set(context, self.successful_value())
                published[0] = True

        cache._sleep = sleep_and_publish
        calls = []
        value, cache_hit, source = cache.get_or_compute(
            context,
            lambda: calls.append("computed") or self.successful_value(),
            cacheable=lambda result: True,
        )

        self.assertEqual(value, self.successful_value())
        self.assertTrue(cache_hit)
        self.assertEqual(source, "redis")
        self.assertEqual(calls, [])
        cache.release(context, owner_token)

    def test_ttl_and_per_target_cardinality_are_enforced(self):
        cache, client, clock = self.make_cache(ttl_seconds=5, max_entries=2)
        contexts = [self.make_context(path=f"/{name}") for name in ("a", "b", "c")]

        for context in contexts:
            self.assertTrue(cache.set(context, self.successful_value()))
            clock.value += 1

        index_key = cache._index_key(contexts[0])
        self.assertEqual(client.zcard(index_key), 2)
        self.assertIsNone(client.get(cache.key_for(contexts[0])))
        self.assertIsNotNone(client.get(cache.key_for(contexts[1])))
        self.assertIsNotNone(client.get(cache.key_for(contexts[2])))

        clock.value += 5
        self.assertIsNone(cache.get(contexts[2]))

    def test_unavailable_and_malformed_redis_fall_back_without_breaking_reads(self):
        cache, client, _ = self.make_cache()
        client.available = False
        calls = []
        with self.assertLogs("src.worker_agent.infrastructure.adapters.redis_cache", level="WARNING") as logs:
            result = cache.get_or_compute(
                self.make_context(),
                lambda: calls.append("computed") or self.successful_value(),
            )
        self.assertEqual(result[1:], (False, "restic-fallback"))
        self.assertEqual(calls, ["computed"])
        self.assertNotIn("redis://", "\n".join(logs.output))
        self.assertNotIn("password@example", "\n".join(logs.output))

        client.available = True
        malformed_key = cache.key_for(self.make_context(path="/malformed"))
        client.values[malformed_key] = b"not-json"
        result = cache.get_or_compute(
            self.make_context(path="/malformed"),
            lambda: self.successful_value(),
            cacheable=lambda value: True,
        )
        self.assertEqual(result[1:], (False, "restic"))

        malformed_url_cache = RedisSnapshotCache(url="not-a-redis-url")
        fallback = malformed_url_cache.get_or_compute(
            self.make_context(), lambda: self.successful_value()
        )
        self.assertEqual(fallback[1:], (False, "restic-fallback"))

    def test_oversized_secret_and_failed_values_are_not_cached(self):
        cache, client, _ = self.make_cache(max_value_bytes=64)
        context = self.make_context()
        calls = []
        oversized = {
            "status": JobStatus.SUCCEEDED,
            "entries": [{"type": "file", "path": "/" + ("x" * 200)}],
        }
        for value in (oversized, {"status": JobStatus.SUCCEEDED, "entries": [{"content": "secret"}]}, {"status": JobStatus.FAILED, "entries": []}):
            cache.get_or_compute(
                context,
                lambda value=value: calls.append(value) or value,
                cacheable=lambda result: result.get("status") == JobStatus.SUCCEEDED,
            )
        self.assertEqual(len(calls), 3)
        self.assertFalse(any(key.startswith("sx:v4:") for key in client.values))

    def test_canceled_result_is_not_cached(self):
        cache, client, _ = self.make_cache()
        context = self.make_context()
        value = self.successful_value()
        result = cache.get_or_compute(
            context,
            lambda: value,
            cacheable=lambda item: True,
            cancel_check=lambda: True,
        )
        self.assertEqual(result[1:], (False, "restic-fallback"))
        self.assertFalse(any(key.startswith("sx:v4:") for key in client.values))

    def test_worker_metadata_reads_use_redis_but_dump_stays_uncached(self):
        clock = FakeClock()
        client = FakeRedis(clock)
        cache = RedisSnapshotCache(client=client, clock=clock, sleep_fn=clock.sleep)
        runtime = Mock()
        entry = {"name": "README.md", "type": "file", "size": 13}
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": json.dumps({"nodes": [entry]}),
            "stderr": "",
        }
        runtime.run_runtime_job_binary.return_value = {
            "success": True,
            "status_code": 0,
            "stdout_bytes": b"binary-content",
            "stderr": "",
        }
        runtime.get_restic_snapshot_stats.return_value = {
            "success": True,
            "status_code": 0,
            "stats": {"total_size": 2048, "total_file_count": 3, "snapshots_count": 1},
            "logs": "",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
            snapshot_cache=cache,
        )
        payload = {
            "target_id": "target-a",
            "snapshot_id": "snap-1",
            "path": "/",
            "environment": {"RESTIC_REPOSITORY": self.REPOSITORY},
        }

        first = service.execute_job({"command": "snapshot.ls", "payload": payload})
        second = service.execute_job({"command": "snapshot.ls", "payload": payload})
        dump = service.execute_job({"command": "snapshot.dump", "payload": payload})

        self.assertEqual(first.status, JobStatus.SUCCEEDED)
        self.assertFalse(first.result_summary["cache_hit"])
        self.assertEqual(first.result_summary["source"], "restic")
        self.assertTrue(second.result_summary["cache_hit"])
        self.assertEqual(second.result_summary["source"], "redis")
        self.assertEqual(first.result_summary["entries"], second.result_summary["entries"])
        self.assertEqual(runtime.run_runtime_job.call_count, 1)
        self.assertEqual(dump.result_summary["b64_content"], "YmluYXJ5LWNvbnRlbnQ=")
        self.assertNotIn("cache_hit", dump.result_summary)

        about_first = service.execute_job({"command": "snapshot.about", "payload": payload})
        about_second = service.execute_job({"command": "snapshot.about", "payload": payload})

        self.assertEqual(about_first.status, JobStatus.SUCCEEDED)
        self.assertFalse(about_first.result_summary["cache_hit"])
        self.assertEqual(about_first.result_summary["source"], "restic")
        self.assertTrue(about_second.result_summary["cache_hit"])
        self.assertEqual(about_second.result_summary["source"], "redis")
        self.assertEqual(about_first.result_summary["stats"], about_second.result_summary["stats"])
        self.assertEqual(runtime.get_restic_snapshot_stats.call_count, 1)

    def test_worker_snapshot_catalog_is_cached_and_unavailable_cache_falls_back(self):
        clock = FakeClock()
        client = FakeRedis(clock)
        client.available = False
        cache = RedisSnapshotCache(client=client, clock=clock, sleep_fn=clock.sleep)
        runtime = Mock()
        runtime.list_restic_snapshots.return_value = {
            "success": True,
            "status_code": 0,
            "snapshots": [{"short_id": "abc", "time": "2026-01-01T00:00:00Z"}],
            "logs": "",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
            snapshot_cache=cache,
        )
        result = service.execute_job(
            {
                "command": "snapshots.list",
                "payload": {
                    "target_id": "target-a",
                    "environment": {"RESTIC_REPOSITORY": self.REPOSITORY},
                },
            }
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertFalse(result.result_summary["cache_hit"])
        self.assertEqual(result.result_summary["source"], "restic-fallback")
        self.assertEqual(runtime.list_restic_snapshots.call_count, 1)


if __name__ == "__main__":
    unittest.main()
