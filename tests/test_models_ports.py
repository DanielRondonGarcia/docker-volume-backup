"""RED coverage for Snapshot Explorer v2 Phase 1 foundation contracts.

Covers typed read/cache/index contracts, schema versioning, cache/index
repository ports, the interactive job port, in-memory parity, and the
SNAPSHOT_EXPLORER_* feature-flag environment handling.
"""

import os
import tempfile
import unittest

from src.control_plane.application.ports.ports import (
    CacheRepository,
    IndexRepository,
    InteractiveJobPort,
)
from src.control_plane.domain.models import (
    CacheGenerationRecord,
    IndexStatusRecord,
    SnapshotExplorerConfig,
    SnapshotReadRequest,
    SnapshotReadResponse,
)
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryCacheRepository,
    InMemoryIndexRepository,
)
from src.control_plane.infrastructure.repositories.sqlite import (
    SQLiteCacheRepository,
    SQLiteIndexRepository,
    SQLiteRepositoryBase,
)

_EXPLORER_ENV_KEYS = (
    "SNAPSHOT_EXPLORER_NO_LOCK",
    "SNAPSHOT_EXPLORER_CACHE_DIR",
    "SNAPSHOT_EXPLORER_REDIS_URL",
    "SNAPSHOT_EXPLORER_EAGER_INDEX",
)


class SnapshotReadContractTests(unittest.TestCase):
    def test_read_request_defaults_to_schema_version_1(self):
        request = SnapshotReadRequest(snapshot_id="abc123", path="/", operation="browse")
        self.assertEqual(request.schema_version, 1)
        self.assertTrue(request.request_id)

    def test_read_response_carries_source_and_cache_hit(self):
        response = SnapshotReadResponse(
            request_id="req-1",
            job_id="job-1",
            status="succeeded",
            source="restic",
            cache_hit=True,
            entries=[{"path": "/"}],
        )
        self.assertEqual(response.schema_version, 1)
        self.assertIsNone(response.error)

    def test_cache_generation_record_defaults(self):
        record = CacheGenerationRecord(target_id="t1", repository_fingerprint="fp1")
        self.assertEqual(record.generation, 0)
        self.assertIsNotNone(record.updated_at)

    def test_index_status_record_defaults(self):
        record = IndexStatusRecord(target_id="t1", snapshot_id="s1")
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.entry_count, 0)


class RepositoryPortContractTests(unittest.TestCase):
    def test_cache_repository_port_contract(self):
        self.assertTrue(hasattr(CacheRepository, "get_generation"))
        self.assertTrue(hasattr(CacheRepository, "bump_generation"))
        self.assertTrue(hasattr(CacheRepository, "cleanup_orphaned"))

    def test_index_repository_port_contract(self):
        self.assertTrue(hasattr(IndexRepository, "upsert_status"))
        self.assertTrue(hasattr(IndexRepository, "get_status"))
        self.assertTrue(hasattr(IndexRepository, "list_by_target"))
        self.assertTrue(hasattr(IndexRepository, "delete_for_target"))
        self.assertTrue(hasattr(IndexRepository, "cleanup_orphaned"))

    def test_interactive_job_port_contract(self):
        self.assertTrue(hasattr(InteractiveJobPort, "submit"))
        self.assertTrue(hasattr(InteractiveJobPort, "get_result"))
        self.assertTrue(hasattr(InteractiveJobPort, "cancel"))


class SchemaVersioningTests(unittest.TestCase):
    def test_sqlite_base_exposes_schema_version(self):
        self.assertGreaterEqual(SQLiteRepositoryBase.SCHEMA_VERSION, 1)

    def test_sqlite_schema_meta_records_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteCacheRepository(os.path.join(tmp, "test.db"))
            self.assertEqual(repo.get_schema_version(), SQLiteRepositoryBase.SCHEMA_VERSION)


class CacheRepositoryBehaviorTests(unittest.TestCase):
    def test_generation_bumps_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteCacheRepository(os.path.join(tmp, "test.db"))
            first = repo.bump_generation("t1", "fp1")
            second = repo.bump_generation("t1", "fp1")
            self.assertEqual(second.generation, first.generation + 1)
            loaded = repo.get_generation("t1", "fp1")
            self.assertEqual(loaded.generation, second.generation)

    def test_cleanup_orphaned_removes_stale_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteCacheRepository(os.path.join(tmp, "test.db"))
            repo.bump_generation("t1", "fp1")
            repo.bump_generation("t2", "fp2")
            removed = repo.cleanup_orphaned([("t1", "fp1")])
            self.assertEqual(removed, 1)
            self.assertIsNone(repo.get_generation("t2", "fp2"))
            self.assertIsNotNone(repo.get_generation("t1", "fp1"))

    def test_in_memory_cache_repository_mirrors_sqlite(self):
        repo = InMemoryCacheRepository()
        first = repo.bump_generation("t1", "fp1")
        second = repo.bump_generation("t1", "fp1")
        self.assertEqual(second.generation, first.generation + 1)
        self.assertEqual(repo.get_generation("t1", "fp1").generation, second.generation)
        self.assertEqual(repo.cleanup_orphaned([]), 1)
        self.assertIsNone(repo.get_generation("t1", "fp1"))


class IndexRepositoryBehaviorTests(unittest.TestCase):
    def test_upsert_and_get_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteIndexRepository(os.path.join(tmp, "test.db"))
            repo.upsert_status(
                IndexStatusRecord(target_id="t1", snapshot_id="s1", status="indexed", entry_count=42)
            )
            loaded = repo.get_status("t1", "s1")
            self.assertEqual(loaded.status, "indexed")
            self.assertEqual(loaded.entry_count, 42)

    def test_delete_for_target_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteIndexRepository(os.path.join(tmp, "test.db"))
            repo.upsert_status(IndexStatusRecord(target_id="t1", snapshot_id="s1"))
            repo.upsert_status(IndexStatusRecord(target_id="t2", snapshot_id="s2"))
            repo.delete_for_target("t1")
            self.assertIsNone(repo.get_status("t1", "s1"))
            removed = repo.cleanup_orphaned(["t2"])
            self.assertEqual(removed, 0)
            self.assertIsNotNone(repo.get_status("t2", "s2"))

    def test_in_memory_index_repository_mirrors_sqlite(self):
        repo = InMemoryIndexRepository()
        repo.upsert_status(IndexStatusRecord(target_id="t1", snapshot_id="s1", status="lazy"))
        self.assertEqual(repo.get_status("t1", "s1").status, "lazy")
        self.assertEqual(len(repo.list_by_target("t1")), 1)
        repo.delete_for_target("t1")
        self.assertEqual(len(repo.list_by_target("t1")), 0)


class FeatureFlagEnvTests(unittest.TestCase):
    def test_from_env_defaults(self):
        saved = {key: os.environ.pop(key, None) for key in _EXPLORER_ENV_KEYS}
        try:
            config = SnapshotExplorerConfig.from_env()
            self.assertFalse(config.no_lock)
            self.assertIsNone(config.cache_dir)
            self.assertIsNone(config.redis_url)
            self.assertFalse(config.eager_index)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_from_env_parses_values(self):
        saved = {key: os.environ.pop(key, None) for key in _EXPLORER_ENV_KEYS}
        try:
            os.environ["SNAPSHOT_EXPLORER_NO_LOCK"] = "true"
            os.environ["SNAPSHOT_EXPLORER_CACHE_DIR"] = "/var/cache/restic"
            os.environ["SNAPSHOT_EXPLORER_REDIS_URL"] = "redis://localhost:6379/0"
            os.environ["SNAPSHOT_EXPLORER_EAGER_INDEX"] = "1"
            config = SnapshotExplorerConfig.from_env()
            self.assertTrue(config.no_lock)
            self.assertEqual(config.cache_dir, "/var/cache/restic")
            self.assertEqual(config.redis_url, "redis://localhost:6379/0")
            self.assertTrue(config.eager_index)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
