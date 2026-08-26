import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.control_plane.domain.models import JobRecord
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository
from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
from src.control_plane.infrastructure.sqlite_runtime import (
    SQLITE_BUSY_TIMEOUT_MILLISECONDS,
    SQLITE_BUSY_TIMEOUT_SECONDS,
    SQLiteConnection,
)


class SQLiteRuntimeTests(unittest.TestCase):
    def test_file_connection_uses_explicit_bounded_busy_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "control-plane.db")

            with SQLiteConnection(database_path) as connection:
                self.assertGreater(SQLITE_BUSY_TIMEOUT_SECONDS, 0)
                self.assertIs(connection.row_factory, sqlite3.Row)
                self.assertEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    SQLITE_BUSY_TIMEOUT_MILLISECONDS,
                )

    def test_repository_connection_serializes_persisted_auth_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "control-plane.db")
            auth = WorkerAuthState(database_path)
            repository = SQLiteJobRepository(database_path)
            held_connection = repository._connect()
            held_connection.execute("BEGIN IMMEDIATE")
            acquire_attempted = threading.Event()
            completed = threading.Event()
            errors = []
            original_acquire = held_connection._coordinator.acquire

            def tracked_acquire(*args, **kwargs):
                acquire_attempted.set()
                return original_acquire(*args, **kwargs)

            def persist_enrollment():
                try:
                    auth.create_enrollment(
                        "worker-a",
                        "host-a",
                        {},
                        "s" * 32,
                        worker_id="worker-a",
                    )
                except BaseException as error:
                    errors.append(error)
                finally:
                    completed.set()

            try:
                with patch.object(held_connection._coordinator, "acquire", tracked_acquire):
                    thread = threading.Thread(target=persist_enrollment)
                    thread.start()
                    self.assertTrue(acquire_attempted.wait(timeout=2))
                    self.assertFalse(completed.is_set())
                    held_connection.close()
                    self.assertTrue(completed.wait(timeout=2))
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
            finally:
                held_connection.close()

            self.assertEqual(errors, [])
            with SQLiteConnection(database_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM auth_enrollments").fetchone()[0],
                    1,
                )

    def test_connection_lock_is_released_after_context_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "control-plane.db")
            repository = SQLiteJobRepository(database_path)

            with self.assertRaisesRegex(RuntimeError, "forced transaction failure"):
                with repository._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    raise RuntimeError("forced transaction failure")

            job = JobRecord(worker_id="worker-a", command="worker.self_check")
            repository.save(job)

            self.assertIsNotNone(repository.get(job.id))


if __name__ == "__main__":
    unittest.main()
