"""Shared SQLite connection policy for the Control Plane."""

import os
import sqlite3
from threading import Lock


# SQLite waits for external writers for a bounded five seconds. The process
# coordinator below handles contention between the Control Plane's own lanes.
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)


class _SQLiteProcessCoordinator:
    def __init__(self):
        self._lock = Lock()

    def acquire(self):
        self._lock.acquire()

    def release(self):
        self._lock.release()


_COORDINATORS_LOCK = Lock()
_COORDINATORS = {}


def _database_key(database_path):
    path = os.fspath(database_path)
    if path == ":memory:":
        return None
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _coordinator_for(database_path):
    key = _database_key(database_path)
    if key is None:
        return None
    with _COORDINATORS_LOCK:
        return _COORDINATORS.setdefault(key, _SQLiteProcessCoordinator())


class SQLiteConnection:
    """SQLite connection held under the process-wide file coordinator."""

    def __init__(self, database_path):
        self._coordinator = _coordinator_for(database_path)
        self._acquired = False
        self._closed = False
        self._connection = None
        try:
            if self._coordinator is not None:
                self._coordinator.acquire()
                self._acquired = True
            self._connection = sqlite3.connect(database_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
        except BaseException:
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                self._release()
            raise

    def __enter__(self):
        try:
            self._connection.__enter__()
        except BaseException:
            self.close()
            raise
        return self._connection

    def __exit__(self, *args):
        try:
            return self._connection.__exit__(*args)
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        try:
            if self._connection is not None:
                self._connection.close()
        finally:
            self._closed = True
            self._release()

    def _release(self):
        if self._acquired:
            self._acquired = False
            self._coordinator.release()

    def __getattr__(self, name):
        return getattr(self._connection, name)
