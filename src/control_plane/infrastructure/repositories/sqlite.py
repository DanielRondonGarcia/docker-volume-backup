import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.application.ports.ports import (
    CacheRepository,
    IndexRepository,
    InventoryRepository,
    JobRepository,
    RetentionPolicyRepository,
    SecretRepository,
    SettingsRepository,
    SnapshotRepository,
    StorageProfileRepository,
    TargetStatsRepository,
    TargetRepository,
    WorkerEnrollmentRepository,
    WorkerRepository,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    CacheGenerationRecord,
    IndexStatusRecord,
    InventorySnapshot,
    JobRecord,
    JobStatus,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    WorkerEnrollmentRecord,
    WorkerRecord,
    utcnow,
)


class _SQLiteConnection:
    def __init__(self, database_path: str):
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row

    def __enter__(self):
        self._connection.__enter__()
        return self._connection

    def __exit__(self, *args):
        try:
            return self._connection.__exit__(*args)
        finally:
            self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _json_load(value: Optional[str], fallback):
    if not value:
        return fallback
    return json.loads(value)


class SQLiteRepositoryBase:
    SCHEMA_VERSION = 1

    def __init__(self, database_path: str):
        self.database_path = database_path
        self._lock = Lock()
        self._initialize()

    def _connect(self):
        return _SQLiteConnection(self.database_path)

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );

                CREATE TABLE IF NOT EXISTS worker_enrollments (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    host_name TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS inventories (
                    worker_id TEXT PRIMARY KEY,
                    inventory_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    compose_project TEXT,
                    volume_targets_json TEXT NOT NULL,
                    backup_mode TEXT NOT NULL,
                    backup_strategy TEXT NOT NULL,
                    runtime_image TEXT,
                    runtime_command TEXT,
                    runtime_environment_json TEXT NOT NULL,
                    runtime_volumes_json TEXT NOT NULL,
                    runtime_network_mode TEXT,
                    storage_profile_id TEXT,
                    path_storage TEXT,
                    retention_policy_id TEXT,
                    execution_policy_id TEXT,
                    restic_password_secret_id TEXT,
                    restore_defaults_json TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    live_access_enabled INTEGER NOT NULL DEFAULT 0,
                    cron_expression TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    target_id TEXT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_worker_id TEXT,
                    lease_token TEXT,
                    lease_issued_at TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    result_summary_json TEXT,
                    log_lines_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS storage_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    backend_type TEXT NOT NULL,
                    environment_json TEXT NOT NULL,
                    secret_refs_json TEXT NOT NULL,
                    file_secret_refs_json TEXT NOT NULL,
                    runtime_volumes_json TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secrets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    secret_type TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    key_version TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    hostname TEXT,
                    paths_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS retention_policies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    keep_last INTEGER,
                    keep_hourly INTEGER,
                    keep_daily INTEGER,
                    keep_weekly INTEGER,
                    keep_monthly INTEGER,
                    keep_yearly INTEGER,
                    prune INTEGER NOT NULL,
                    labels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS target_stats (
                    id TEXT PRIMARY KEY,
                    target_id TEXT UNIQUE NOT NULL,
                    worker_id TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    id TEXT PRIMARY KEY,
                    restic_repository_base TEXT NOT NULL DEFAULT '',
                    restic_password_secret_id TEXT,
                    rclone_conf_secret_id TEXT,
                    global_cron_expression TEXT,
                    control_plane_public_url TEXT NOT NULL DEFAULT '',
                    snapshot_explorer_listing_max_output_bytes INTEGER NOT NULL DEFAULT 4194304,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS explorer_cache_generations (
                    target_id TEXT NOT NULL,
                    repository_fingerprint TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (target_id, repository_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS explorer_index_status (
                    target_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (target_id, snapshot_id)
                );
                """
            )
            self._ensure_column(connection, "settings", "rclone_conf_secret_id", "TEXT")
            self._ensure_column(connection, "settings", "global_cron_expression", "TEXT")
            self._ensure_column(connection, "settings", "control_plane_public_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                connection,
                "settings",
                "snapshot_explorer_listing_max_output_bytes",
                "INTEGER NOT NULL DEFAULT 4194304",
            )
            self._ensure_column(connection, "targets", "cron_expression", "TEXT")
            self._ensure_column(connection, "targets", "live_access_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "targets", "path_storage", "TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_submitted_at_id_desc "
                "ON jobs (submitted_at DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_lease_expires_at "
                "ON jobs (status, lease_expires_at)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    def get_schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name not in existing:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


class SQLiteWorkerRepository(SQLiteRepositoryBase, WorkerRepository):
    def save(self, worker: WorkerRecord) -> WorkerRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO workers (
                    id, name, host_name, version, labels_json, status, created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker.id,
                    worker.name,
                    worker.host_name,
                    worker.version,
                    json.dumps(worker.labels),
                    worker.status,
                    worker.created_at.isoformat(),
                    worker.updated_at.isoformat(),
                    worker.last_seen_at.isoformat() if worker.last_seen_at else None,
                ),
            )
        return worker

    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return self._row_to_worker(row) if row else None

    def find_by_name(self, name: str) -> Optional[WorkerRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workers WHERE name = ? ORDER BY created_at ASC LIMIT 1", (name,)).fetchone()
        return self._row_to_worker(row) if row else None

    def list(self) -> List[WorkerRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM workers ORDER BY created_at ASC").fetchall()
        return [self._row_to_worker(row) for row in rows]

    def delete(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_worker(row: sqlite3.Row) -> WorkerRecord:
        return WorkerRecord(
            id=row["id"],
            name=row["name"],
            host_name=row["host_name"],
            version=row["version"],
            labels=_json_load(row["labels_json"], {}),
            status=row["status"],
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
            last_seen_at=_dt(row["last_seen_at"]),
        )

class SQLiteWorkerEnrollmentRepository(SQLiteRepositoryBase, WorkerEnrollmentRepository):
    def save(self, enrollment: WorkerEnrollmentRecord) -> WorkerEnrollmentRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_enrollments (
                    id, worker_id, token_hash, name, host_name, labels_json, created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    enrollment.id,
                    enrollment.worker_id,
                    enrollment.token_hash,
                    enrollment.name,
                    enrollment.host_name,
                    json.dumps(enrollment.labels),
                    enrollment.created_at.isoformat(),
                    enrollment.expires_at.isoformat(),
                    enrollment.used_at.isoformat() if enrollment.used_at else None,
                ),
            )
        return enrollment

    def get(self, enrollment_id: str) -> Optional[WorkerEnrollmentRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM worker_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        return self._row_to_enrollment(row) if row else None

    def get_by_token_hash(self, token_hash: str) -> Optional[WorkerEnrollmentRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_enrollments WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return self._row_to_enrollment(row) if row else None

    def list(self) -> List[WorkerEnrollmentRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM worker_enrollments ORDER BY created_at DESC").fetchall()
        return [self._row_to_enrollment(row) for row in rows]

    @staticmethod
    def _row_to_enrollment(row: sqlite3.Row) -> WorkerEnrollmentRecord:
        return WorkerEnrollmentRecord(
            id=row["id"],
            worker_id=row["worker_id"],
            token_hash=row["token_hash"],
            name=row["name"],
            host_name=row["host_name"],
            labels=_json_load(row["labels_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            expires_at=_dt(row["expires_at"]) or datetime.utcnow(),
            used_at=_dt(row["used_at"]),
        )


class SQLiteInventoryRepository(SQLiteRepositoryBase, InventoryRepository):
    def save(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO inventories (
                    worker_id, inventory_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.worker_id,
                    json.dumps(snapshot.inventory),
                    snapshot.created_at.isoformat(),
                    snapshot.updated_at.isoformat(),
                ),
            )
        return snapshot

    def get_by_worker(self, worker_id: str) -> Optional[InventorySnapshot]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM inventories WHERE worker_id = ?", (worker_id,)).fetchone()
        if not row:
            return None
        return InventorySnapshot(
            worker_id=row["worker_id"],
            inventory=_json_load(row["inventory_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM inventories WHERE worker_id = ?", (worker_id,))
            return cursor.rowcount > 0


class SQLiteTargetRepository(SQLiteRepositoryBase, TargetRepository):
    def save(self, target: BackupTargetRecord) -> BackupTargetRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO targets (
                    id, name, worker_id, compose_project, volume_targets_json, backup_mode, backup_strategy,
                    runtime_image, runtime_command, runtime_environment_json, runtime_volumes_json, runtime_network_mode,
                    storage_profile_id, path_storage, retention_policy_id, execution_policy_id, restic_password_secret_id, restore_defaults_json, labels_json,
                    enabled, live_access_enabled, cron_expression, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    target.id,
                    target.name,
                    target.worker_id,
                    target.compose_project,
                    json.dumps(target.volume_targets),
                    target.backup_mode,
                    target.backup_strategy,
                    target.runtime_image,
                    target.runtime_command,
                    json.dumps(target.runtime_environment),
                    json.dumps(target.runtime_volumes),
                    target.runtime_network_mode,
                    target.storage_profile_id,
                    target.path_storage,
                    target.retention_policy_id,
                    target.execution_policy_id,
                    target.restic_password_secret_id,
                    json.dumps(target.restore_defaults),
                    json.dumps(target.labels),
                    1 if target.enabled else 0,
                    1 if target.live_access_enabled else 0,
                    target.cron_expression,
                    target.created_at.isoformat(),
                    target.updated_at.isoformat(),
                ),
            )
        return target

    def get(self, target_id: str) -> Optional[BackupTargetRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return self._row_to_target(row) if row else None

    def list(self) -> List[BackupTargetRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM targets ORDER BY created_at ASC").fetchall()
        return [self._row_to_target(row) for row in rows]

    def delete(self, target_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM targets WHERE id = ?", (target_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_target(row: sqlite3.Row) -> BackupTargetRecord:
        return BackupTargetRecord(
            id=row["id"],
            name=row["name"],
            worker_id=row["worker_id"],
            compose_project=row["compose_project"],
            volume_targets=_json_load(row["volume_targets_json"], []),
            backup_mode=row["backup_mode"],
            backup_strategy=row["backup_strategy"],
            runtime_image=row["runtime_image"],
            runtime_command=row["runtime_command"],
            runtime_environment=_json_load(row["runtime_environment_json"], {}),
            runtime_volumes=_json_load(row["runtime_volumes_json"], {}),
            runtime_network_mode=row["runtime_network_mode"],
            storage_profile_id=row["storage_profile_id"],
            path_storage=row["path_storage"] if "path_storage" in row.keys() else None,
            retention_policy_id=row["retention_policy_id"],
            execution_policy_id=row["execution_policy_id"],
            restic_password_secret_id=row["restic_password_secret_id"],
            restore_defaults=_json_load(row["restore_defaults_json"], {}),
            labels=_json_load(row["labels_json"], {}),
            enabled=bool(row["enabled"]),
            live_access_enabled=bool(row["live_access_enabled"]) if "live_access_enabled" in row.keys() else False,
            cron_expression=row["cron_expression"] if "cron_expression" in row.keys() else None,
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteJobRepository(SQLiteRepositoryBase, JobRepository):
    MAX_LOG_LINES = 1000
    MAX_LOG_CHARS = 512 * 1024

    def save(self, job: JobRecord) -> JobRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    id, worker_id, command, payload_json, requested_by, target_id, trigger, status,
                    owner_worker_id, lease_token, lease_issued_at, lease_expires_at, attempt_count,
                    result_summary_json, log_lines_json, submitted_at, started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.worker_id,
                    job.command,
                    json.dumps(job.payload),
                    job.requested_by,
                    job.target_id,
                    job.trigger,
                    JobStatus.normalize(job.status),
                    job.owner_worker_id,
                    job.lease_token,
                    job.lease_issued_at.isoformat() if job.lease_issued_at else None,
                    job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                    job.attempt_count,
                    json.dumps(job.result_summary) if job.result_summary is not None else None,
                    json.dumps(job.log_lines),
                    job.submitted_at.isoformat(),
                    job.started_at.isoformat() if job.started_at else None,
                    job.finished_at.isoformat() if job.finished_at else None,
                    job.updated_at.isoformat(),
                ),
            )
            job.status = JobStatus.normalize(job.status)
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self) -> List[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY submitted_at DESC").fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_for_listing(self, limit: Optional[int] = None, offset: int = 0) -> Tuple[List[JobRecord], int]:
        start = max(0, offset)
        columns = """
            id, worker_id, command, requested_by, target_id, trigger, status,
            attempt_count, submitted_at, started_at, finished_at, updated_at
        """
        query = f"SELECT {columns} FROM jobs ORDER BY submitted_at DESC, id DESC"
        parameters: List[int] = []
        if limit is not None and limit > 0:
            query += " LIMIT ? OFFSET ?"
            parameters.extend((limit, start))
        elif start > 0:
            query += " LIMIT -1 OFFSET ?"
            parameters.append(start)
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()["total"]
            rows = connection.execute(query, parameters).fetchall()
        return [self._row_to_listing_job(row) for row in rows], total

    @staticmethod
    def _row_to_listing_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            worker_id=row["worker_id"],
            command=row["command"],
            requested_by=row["requested_by"],
            target_id=row["target_id"],
            trigger=row["trigger"],
            status=JobStatus.normalize(row["status"]),
            attempt_count=row["attempt_count"] or 0,
            submitted_at=_dt(row["submitted_at"]) or datetime.utcnow(),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    @staticmethod
    def _reconcile_expired_leases_locked(connection, now) -> int:
        rows = connection.execute(
            """
            SELECT id, result_summary_json, log_lines_json, lease_expires_at
            FROM jobs
            WHERE status = ? AND lease_expires_at IS NOT NULL
            """,
            (JobStatus.IN_PROGRESS,),
        ).fetchall()
        interruption_log = "Worker lease expired before terminal status was reported."
        reconciled = 0
        for row in rows:
            lease_expires_at = _dt(row["lease_expires_at"])
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if not lease_expires_at or lease_expires_at > now:
                continue
            result_summary = _json_load(row["result_summary_json"], None)
            result_summary = dict(result_summary) if isinstance(result_summary, dict) else {}
            result_summary.setdefault("error", "worker lease expired before the job reported a terminal result")
            result_summary["recovery"] = "worker_interrupted"
            log_lines = _json_load(row["log_lines_json"], [])
            log_lines = list(log_lines) if isinstance(log_lines, list) else []
            if interruption_log not in log_lines:
                log_lines.append(interruption_log)
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, owner_worker_id = NULL, lease_token = NULL,
                    lease_issued_at = NULL, lease_expires_at = NULL,
                    result_summary_json = ?, log_lines_json = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.FAILED,
                    json.dumps(result_summary),
                    json.dumps(log_lines),
                    now.isoformat(),
                    now.isoformat(),
                    row["id"],
                    JobStatus.IN_PROGRESS,
                ),
            )
            reconciled += updated.rowcount
        return reconciled

    def reconcile_expired_leases(self) -> int:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                reconciled = self._reconcile_expired_leases_locked(connection, utcnow())
                connection.commit()
                return reconciled
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_duration_seconds: int = 300,
    ) -> Optional[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                lease_expires_at = _dt(row["lease_expires_at"]) if row else None
                if lease_expires_at and lease_expires_at.tzinfo:
                    lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
                if (
                    row is None
                    or JobStatus.normalize(row["status"]) != JobStatus.IN_PROGRESS
                    or row["owner_worker_id"] != worker_id
                    or row["lease_token"] != lease_token
                    or not lease_expires_at
                    or lease_expires_at <= now
                ):
                    connection.commit()
                    return None
                expires_at = now + timedelta(seconds=lease_duration_seconds)
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND owner_worker_id = ?
                      AND lease_token = ? AND lease_expires_at > ?
                    """,
                    (
                        expires_at.isoformat(),
                        now.isoformat(),
                        job_id,
                        JobStatus.IN_PROGRESS,
                        worker_id,
                        lease_token,
                        now.isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    connection.commit()
                    return None
                renewed = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                connection.commit()
                return self._row_to_job(renewed) if renewed else None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def update_progress(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        sequence: int,
        progress: Dict[str, Any],
        log_lines: List[str],
        result_summary: Optional[Dict[str, Any]] = None,
    ) -> Optional[JobRecord]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                lease_expires_at = _dt(row["lease_expires_at"]) if row else None
                if lease_expires_at and lease_expires_at.tzinfo:
                    lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
                if (
                    row is None
                    or JobStatus.normalize(row["status"]) != JobStatus.IN_PROGRESS
                    or row["owner_worker_id"] != worker_id
                    or not hmac.compare_digest(row["lease_token"] or "", lease_token or "")
                    or not lease_expires_at
                    or lease_expires_at <= now
                ):
                    connection.commit()
                    return None

                current_summary = _json_load(row["result_summary_json"], None)
                current_summary = dict(current_summary) if isinstance(current_summary, dict) else {}
                try:
                    current_sequence = int(current_summary.get("progress_sequence", 0) or 0)
                except (TypeError, ValueError):
                    current_sequence = 0
                if sequence <= current_sequence:
                    connection.commit()
                    return self._row_to_job(row)

                if isinstance(result_summary, dict):
                    current_summary.update(result_summary)
                current_summary["progress"] = dict(progress)
                current_summary["progress_sequence"] = sequence
                current_logs = _json_load(row["log_lines_json"], [])
                current_logs = list(current_logs) if isinstance(current_logs, list) else []
                current_logs = (current_logs + list(log_lines or []))[-self.MAX_LOG_LINES :]
                while current_logs and sum(len(line) for line in current_logs if isinstance(line, str)) > self.MAX_LOG_CHARS:
                    current_logs.pop(0)
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET result_summary_json = ?, log_lines_json = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND owner_worker_id = ?
                      AND lease_token = ? AND lease_expires_at > ?
                    """,
                    (
                        json.dumps(current_summary),
                        json.dumps(current_logs),
                        now.isoformat(),
                        job_id,
                        JobStatus.IN_PROGRESS,
                        worker_id,
                        lease_token,
                        now.isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    connection.commit()
                    return None
                saved = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                connection.commit()
                return self._row_to_job(saved) if saved else None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def claim_pending_for_worker(self, worker_id: str, lease_duration_seconds: int = 300) -> List[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                self._reconcile_expired_leases_locked(connection, now)
                rows = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE worker_id = ? AND status = ?
                    ORDER BY submitted_at ASC
                    """,
                    (worker_id, JobStatus.PENDING),
                ).fetchall()
                expires_at = now + timedelta(seconds=lease_duration_seconds)
                claimed = []
                for row in rows:
                    lease_token = secrets.token_urlsafe(32)
                    attempt_count = (row["attempt_count"] or 0) + 1
                    updated = connection.execute(
                        """
                        UPDATE jobs
                        SET status = ?, owner_worker_id = ?, lease_token = ?,
                            lease_issued_at = ?, lease_expires_at = ?, attempt_count = ?,
                            started_at = ?, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            JobStatus.IN_PROGRESS,
                            worker_id,
                            lease_token,
                            now.isoformat(),
                            expires_at.isoformat(),
                            attempt_count,
                            now.isoformat(),
                            now.isoformat(),
                            row["id"],
                            JobStatus.PENDING,
                        ),
                    )
                    if updated.rowcount != 1:
                        continue
                    job = self._row_to_job(row)
                    job.status = JobStatus.IN_PROGRESS
                    job.owner_worker_id = worker_id
                    job.lease_token = lease_token
                    job.lease_issued_at = now
                    job.lease_expires_at = expires_at
                    job.attempt_count = attempt_count
                    job.started_at = now
                    job.updated_at = now
                    claimed.append(job)
                connection.commit()
                return claimed
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            worker_id=row["worker_id"],
            command=row["command"],
            payload=_json_load(row["payload_json"], {}),
            requested_by=row["requested_by"],
            target_id=row["target_id"],
            trigger=row["trigger"],
            status=JobStatus.normalize(row["status"]),
            owner_worker_id=row["owner_worker_id"],
            lease_token=row["lease_token"],
            lease_issued_at=_dt(row["lease_issued_at"]),
            lease_expires_at=_dt(row["lease_expires_at"]),
            attempt_count=row["attempt_count"] or 0,
            result_summary=_json_load(row["result_summary_json"], None),
            log_lines=_json_load(row["log_lines_json"], []),
            submitted_at=_dt(row["submitted_at"]) or datetime.utcnow(),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteStorageProfileRepository(SQLiteRepositoryBase, StorageProfileRepository):
    def save(self, profile: StorageProfileRecord) -> StorageProfileRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO storage_profiles (
                    id, name, backend_type, environment_json, secret_refs_json, file_secret_refs_json,
                    runtime_volumes_json, labels_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.id,
                    profile.name,
                    profile.backend_type,
                    json.dumps(profile.environment),
                    json.dumps(profile.secret_refs),
                    json.dumps(profile.file_secret_refs),
                    json.dumps(profile.runtime_volumes),
                    json.dumps(profile.labels),
                    profile.created_at.isoformat(),
                    profile.updated_at.isoformat(),
                ),
            )
        return profile

    def get(self, profile_id: str) -> Optional[StorageProfileRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM storage_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._row_to_profile(row) if row else None

    def list(self) -> List[StorageProfileRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM storage_profiles ORDER BY created_at ASC").fetchall()
        return [self._row_to_profile(row) for row in rows]

    def delete(self, profile_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM storage_profiles WHERE id = ?", (profile_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> StorageProfileRecord:
        return StorageProfileRecord(
            id=row["id"],
            name=row["name"],
            backend_type=row["backend_type"],
            environment=_json_load(row["environment_json"], {}),
            secret_refs=_json_load(row["secret_refs_json"], {}),
            file_secret_refs=_json_load(row["file_secret_refs_json"], {}),
            runtime_volumes=_json_load(row["runtime_volumes_json"], {}),
            labels=_json_load(row["labels_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteSecretRepository(SQLiteRepositoryBase, SecretRepository):
    def save(self, secret: SecretRecord) -> SecretRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO secrets (
                    id, name, scope, secret_type, ciphertext, key_version, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secret.id,
                    secret.name,
                    secret.scope,
                    secret.secret_type,
                    secret.ciphertext,
                    secret.key_version,
                    json.dumps(secret.metadata),
                    secret.created_at.isoformat(),
                    secret.updated_at.isoformat(),
                ),
            )
        return secret

    def get(self, secret_id: str) -> Optional[SecretRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM secrets WHERE id = ?", (secret_id,)).fetchone()
        return self._row_to_secret(row) if row else None

    def list(self) -> List[SecretRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM secrets ORDER BY created_at ASC").fetchall()
        return [self._row_to_secret(row) for row in rows]

    def delete(self, secret_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM secrets WHERE id = ?", (secret_id,))

    @staticmethod
    def _row_to_secret(row: sqlite3.Row) -> SecretRecord:
        return SecretRecord(
            id=row["id"],
            name=row["name"],
            scope=row["scope"],
            secret_type=row["secret_type"],
            ciphertext=row["ciphertext"],
            key_version=row["key_version"],
            metadata=_json_load(row["metadata_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteSnapshotRepository(SQLiteRepositoryBase, SnapshotRepository):
    def replace_for_target(self, target_id: str, snapshots: List[SnapshotRecord]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM snapshots WHERE target_id = ?", (target_id,))
            for snapshot in snapshots:
                connection.execute(
                    """
                    INSERT INTO snapshots (
                        id, target_id, worker_id, snapshot_id, created_at, hostname, paths_json, tags_json, summary_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.id,
                        snapshot.target_id,
                        snapshot.worker_id,
                        snapshot.snapshot_id,
                        snapshot.created_at.isoformat(),
                        snapshot.hostname,
                        json.dumps(snapshot.paths),
                        json.dumps(snapshot.tags),
                        json.dumps(snapshot.summary),
                        snapshot.updated_at.isoformat(),
                    ),
                )

    def list_by_target(self, target_id: str) -> List[SnapshotRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE target_id = ? ORDER BY created_at DESC",
                (target_id,),
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> SnapshotRecord:
        return SnapshotRecord(
            id=row["id"],
            target_id=row["target_id"],
            worker_id=row["worker_id"],
            snapshot_id=row["snapshot_id"],
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            hostname=row["hostname"],
            paths=_json_load(row["paths_json"], []),
            tags=_json_load(row["tags_json"], []),
            summary=_json_load(row["summary_json"], {}),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteRetentionPolicyRepository(SQLiteRepositoryBase, RetentionPolicyRepository):
    def save(self, policy: RetentionPolicyRecord) -> RetentionPolicyRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO retention_policies (
                    id, name, keep_last, keep_hourly, keep_daily, keep_weekly, keep_monthly, keep_yearly,
                    prune, labels_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.id,
                    policy.name,
                    policy.keep_last,
                    policy.keep_hourly,
                    policy.keep_daily,
                    policy.keep_weekly,
                    policy.keep_monthly,
                    policy.keep_yearly,
                    1 if policy.prune else 0,
                    json.dumps(policy.labels),
                    policy.created_at.isoformat(),
                    policy.updated_at.isoformat(),
                ),
            )
        return policy

    def get(self, policy_id: str) -> Optional[RetentionPolicyRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM retention_policies WHERE id = ?", (policy_id,)).fetchone()
        return self._row_to_policy(row) if row else None

    def list(self) -> List[RetentionPolicyRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM retention_policies ORDER BY created_at ASC").fetchall()
        return [self._row_to_policy(row) for row in rows]

    @staticmethod
    def _row_to_policy(row: sqlite3.Row) -> RetentionPolicyRecord:
        return RetentionPolicyRecord(
            id=row["id"],
            name=row["name"],
            keep_last=row["keep_last"],
            keep_hourly=row["keep_hourly"],
            keep_daily=row["keep_daily"],
            keep_weekly=row["keep_weekly"],
            keep_monthly=row["keep_monthly"],
            keep_yearly=row["keep_yearly"],
            prune=bool(row["prune"]),
            labels=_json_load(row["labels_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteTargetStatsRepository(SQLiteRepositoryBase, TargetStatsRepository):
    def save(self, stats_record: TargetStatsRecord) -> TargetStatsRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO target_stats (
                    id, target_id, worker_id, stats_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    stats_record.id,
                    stats_record.target_id,
                    stats_record.worker_id,
                    json.dumps(stats_record.stats),
                    stats_record.updated_at.isoformat(),
                ),
            )
        return stats_record

    def get_by_target(self, target_id: str) -> Optional[TargetStatsRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM target_stats WHERE target_id = ?", (target_id,)).fetchone()
        if not row:
            return None
        return TargetStatsRecord(
            id=row["id"],
            target_id=row["target_id"],
            worker_id=row["worker_id"],
            stats=_json_load(row["stats_json"], {}),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM target_stats WHERE worker_id = ?", (worker_id,))
            return cursor.rowcount > 0


class SQLiteSettingsRepository(SQLiteRepositoryBase, SettingsRepository):
    def get(self) -> Optional[SettingsRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settings WHERE id = 'default'").fetchone()
        if not row:
            return None
        stored_listing_limit = (
            row["snapshot_explorer_listing_max_output_bytes"]
            if "snapshot_explorer_listing_max_output_bytes" in row.keys()
            else None
        )
        if (
            isinstance(stored_listing_limit, bool)
            or not isinstance(stored_listing_limit, int)
            or stored_listing_limit < SettingsRecord.MIN_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
            or stored_listing_limit > SettingsRecord.MAX_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
        ):
            stored_listing_limit = SettingsRecord.DEFAULT_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
        return SettingsRecord(
            id=row["id"],
            restic_repository_base=row["restic_repository_base"] or "",
            restic_password_secret_id=row["restic_password_secret_id"],
            rclone_conf_secret_id=row["rclone_conf_secret_id"],
            global_cron_expression=row["global_cron_expression"] if "global_cron_expression" in row.keys() else None,
            control_plane_public_url=row["control_plane_public_url"] if "control_plane_public_url" in row.keys() else "",
            snapshot_explorer_listing_max_output_bytes=stored_listing_limit,
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def save(self, settings: SettingsRecord) -> SettingsRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO settings (
                    id, restic_repository_base, restic_password_secret_id, rclone_conf_secret_id, global_cron_expression, control_plane_public_url, snapshot_explorer_listing_max_output_bytes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settings.id,
                    settings.restic_repository_base,
                    settings.restic_password_secret_id,
                    settings.rclone_conf_secret_id,
                    settings.global_cron_expression,
                    settings.control_plane_public_url,
                    settings.snapshot_explorer_listing_max_output_bytes,
                    settings.updated_at.isoformat(),
                ),
            )
        return settings


class SQLiteCacheRepository(SQLiteRepositoryBase, CacheRepository):
    def get_generation(self, target_id: str, repository_fingerprint: str) -> Optional[CacheGenerationRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM explorer_cache_generations WHERE target_id = ? AND repository_fingerprint = ?",
                (target_id, repository_fingerprint),
            ).fetchone()
        if not row:
            return None
        return CacheGenerationRecord(
            target_id=row["target_id"],
            repository_fingerprint=row["repository_fingerprint"],
            generation=row["generation"] or 0,
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def bump_generation(self, target_id: str, repository_fingerprint: str) -> CacheGenerationRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT generation FROM explorer_cache_generations WHERE target_id = ? AND repository_fingerprint = ?",
                (target_id, repository_fingerprint),
            ).fetchone()
            generation = (row["generation"] if row else 0) + 1
            now = utcnow()
            connection.execute(
                """
                INSERT OR REPLACE INTO explorer_cache_generations (target_id, repository_fingerprint, generation, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (target_id, repository_fingerprint, generation, now.isoformat()),
            )
        return CacheGenerationRecord(
            target_id=target_id,
            repository_fingerprint=repository_fingerprint,
            generation=generation,
            updated_at=now,
        )

    def cleanup_orphaned(self, active_keys: List[Tuple[str, str]]) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT target_id, repository_fingerprint FROM explorer_cache_generations"
            ).fetchall()
            active = set(active_keys)
            removed = 0
            for row in rows:
                key = (row["target_id"], row["repository_fingerprint"])
                if key not in active:
                    connection.execute(
                        "DELETE FROM explorer_cache_generations WHERE target_id = ? AND repository_fingerprint = ?",
                        key,
                    )
                    removed += 1
        return removed


class SQLiteIndexRepository(SQLiteRepositoryBase, IndexRepository):
    def upsert_status(self, record: IndexStatusRecord) -> IndexStatusRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO explorer_index_status (target_id, snapshot_id, status, entry_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.target_id,
                    record.snapshot_id,
                    record.status,
                    record.entry_count,
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_status(self, target_id: str, snapshot_id: str) -> Optional[IndexStatusRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM explorer_index_status WHERE target_id = ? AND snapshot_id = ?",
                (target_id, snapshot_id),
            ).fetchone()
        if not row:
            return None
        return IndexStatusRecord(
            target_id=row["target_id"],
            snapshot_id=row["snapshot_id"],
            status=row["status"],
            entry_count=row["entry_count"] or 0,
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def list_by_target(self, target_id: str) -> List[IndexStatusRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM explorer_index_status WHERE target_id = ? ORDER BY snapshot_id ASC",
                (target_id,),
            ).fetchall()
        return [
            IndexStatusRecord(
                target_id=row["target_id"],
                snapshot_id=row["snapshot_id"],
                status=row["status"],
                entry_count=row["entry_count"] or 0,
                updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
            )
            for row in rows
        ]

    def delete_for_target(self, target_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM explorer_index_status WHERE target_id = ?", (target_id,))

    def cleanup_orphaned(self, active_target_ids: List[str]) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT target_id FROM explorer_index_status").fetchall()
            active = set(active_target_ids)
            removed = 0
            for row in rows:
                if row["target_id"] not in active:
                    connection.execute("DELETE FROM explorer_index_status WHERE target_id = ?", (row["target_id"],))
                    removed += 1
        return removed
