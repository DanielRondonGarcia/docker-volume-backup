import json, time; from contextlib import contextmanager; from dataclasses import dataclass; from datetime import datetime, timedelta; from threading import Lock; from uuid import uuid4; from src.control_plane.domain.models import utcnow; from src.control_plane.infrastructure.sqlite_runtime import SQLiteConnection; from src.security.hmac_protocol import digest_secret, verify_request
@dataclass
class Credential: worker_id: str; version: str; secret_digest: str; status: str = "active"; revoked_at: datetime | None = None
@dataclass
class Enrollment: worker_id: str; secret_digest: str; name: str; host_name: str; labels: dict; expires_at: datetime; used: bool = False
class WorkerAuthState:
    def __init__(self, database_path=None):
        self._lock, self.path = Lock(), database_path if database_path and database_path != ":memory:" else None
        self._credentials, self._nonces, self._enrollments = {}, set(), {}
        if self.path: self._initialize()
    @contextmanager
    def _db(self):
        db = SQLiteConnection(self.path)
        try: yield db
        finally:
            try: db.commit()
            finally: db.close()
    def _initialize(self):
        with self._db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "dvb_auth_schema" not in tables: raise RuntimeError("unsupported persisted state; reset the Control Plane for the new major release")
            db.executescript("CREATE TABLE IF NOT EXISTS dvb_auth_schema (major INTEGER PRIMARY KEY); CREATE TABLE IF NOT EXISTS worker_credentials (worker_id TEXT, version TEXT, secret_digest TEXT, status TEXT, created_at TEXT, revoked_at TEXT, PRIMARY KEY(worker_id,version)); CREATE TABLE IF NOT EXISTS worker_nonces (worker_id TEXT, version TEXT, nonce TEXT, seen_at TEXT, PRIMARY KEY(worker_id,version,nonce)); CREATE TABLE IF NOT EXISTS auth_enrollments (secret_digest TEXT PRIMARY KEY, worker_id TEXT, name TEXT, host_name TEXT, labels_json TEXT, expires_at TEXT, used INTEGER);")
            row = db.execute("SELECT major FROM dvb_auth_schema LIMIT 1").fetchone()
            if row and row[0] != 2: raise RuntimeError("unsupported Control Plane schema version")
            db.execute("INSERT OR IGNORE INTO dvb_auth_schema VALUES (2)")
    def _get(self, worker_id, version):
        if self.path:
            with self._db() as db: row = db.execute("SELECT * FROM worker_credentials WHERE worker_id=? AND version=?", (worker_id, str(version))).fetchone()
            return Credential(row[0], row[1], row[2], row[3], datetime.fromisoformat(row[5]) if row[5] else None) if row else None
        return self._credentials.get((worker_id, str(version)))
    def _current(self, worker_id):
        if self.path:
            with self._db() as db: row = db.execute("SELECT * FROM worker_credentials WHERE worker_id=? AND status='active' ORDER BY CAST(version AS INTEGER) DESC LIMIT 1", (worker_id,)).fetchone()
            return Credential(row[0], row[1], row[2], row[3], datetime.fromisoformat(row[5]) if row and row[5] else None) if row else None
        active = [c for (wid, _), c in self._credentials.items() if wid == worker_id and c.status == "active"]
        return max(active, key=lambda c: int(c.version)) if active else None
    def _save(self, c):
        if self.path:
            with self._db() as db: db.execute("INSERT OR REPLACE INTO worker_credentials VALUES (?,?,?,?,?,?)", (c.worker_id, c.version, c.secret_digest, c.status, utcnow().isoformat(), c.revoked_at.isoformat() if c.revoked_at else None))
        else: self._credentials[(c.worker_id, c.version)] = c
    def _consume_nonce(self, worker_id, version, nonce):
        if self.path:
            with self._db() as db: return db.execute("INSERT OR IGNORE INTO worker_nonces VALUES (?,?,?,?)", (worker_id, str(version), nonce, utcnow().isoformat())).rowcount == 1
        key = (worker_id, str(version), nonce)
        if key in self._nonces: return False
        self._nonces.add(key); return True
    def create_enrollment(self, name, host_name, labels, secret, worker_id=None, ttl_minutes=30, replace_pending=False):
        if ttl_minutes <= 0 or len((secret or "").encode()) < 32: raise ValueError("worker secret and positive TTL are required")
        with self._lock:
            e = Enrollment(worker_id or str(uuid4()), digest_secret(secret), name, host_name, labels or {}, utcnow() + timedelta(minutes=ttl_minutes))
            if replace_pending: self._remove_pending_enrollments(e.worker_id)
            if self.path:
                with self._db() as db: db.execute("INSERT OR REPLACE INTO auth_enrollments VALUES (?,?,?,?,?,?,0)", (e.secret_digest, e.worker_id, e.name, e.host_name, json.dumps(e.labels), e.expires_at.isoformat()))
            else: self._enrollments[e.secret_digest] = e
        return {"enrollment_id": e.worker_id, "worker_id": e.worker_id, "expires_at": e.expires_at}
    def _remove_pending_enrollments(self, worker_id):
        if self.path:
            with self._db() as db: db.execute("DELETE FROM auth_enrollments WHERE worker_id=? AND used=0", (worker_id,))
        else:
            for digest, enrollment in list(self._enrollments.items()):
                if enrollment.worker_id == worker_id and not enrollment.used: self._enrollments.pop(digest, None)
    def complete(self, secret, version="1", labels=None):
        with self._lock:
            e = self._enrollment(digest_secret(secret or ""))
            if not e or e.used or e.expires_at < utcnow(): raise ValueError("invalid or expired worker enrollment")
            current = self._current(e.worker_id)
            if current: current.status, current.revoked_at, version = "revoked", utcnow(), str(int(current.version) + 1); self._save(current)
            self._mark_used(e.secret_digest); self._save(Credential(e.worker_id, str(version), e.secret_digest))
            return {"worker_id": e.worker_id, "credential_version": str(version), "name": e.name, "host_name": e.host_name, "labels": labels or e.labels}
    def _enrollment(self, digest):
        if not self.path: return self._enrollments.get(digest)
        with self._db() as db: row = db.execute("SELECT * FROM auth_enrollments WHERE secret_digest=?", (digest,)).fetchone()
        return Enrollment(row[1], row[0], row[2], row[3], json.loads(row[4]), datetime.fromisoformat(row[5]), bool(row[6])) if row else None
    def _mark_used(self, digest):
        if self.path:
            with self._db() as db: (db.execute("UPDATE auth_enrollments SET used=1 WHERE secret_digest=? AND used=0", (digest,)).rowcount == 1) or (_ for _ in ()).throw(ValueError("worker enrollment already used"))
        else: self._enrollments[digest].used = True
    def authenticate(self, worker_id, method, path, body, timestamp, nonce, signed_worker_id, version, signature):
        if not worker_id or signed_worker_id != worker_id: raise ValueError("worker identity mismatch")
        try: fresh = abs(time.time() - int(timestamp)) <= 300
        except (TypeError, ValueError): fresh = False
        if not fresh: raise ValueError("stale worker request")
        c = self._get(worker_id, version)
        if not c or c.status != "active": raise ValueError("worker credential is invalid or revoked")
        if not verify_request(c.secret_digest, signature, method, path, body, timestamp, nonce, worker_id, version): raise ValueError("invalid worker signature")
        if not nonce or not self._consume_nonce(worker_id, version, nonce): raise ValueError("replayed worker request")

    def rotate(self, worker_id, secret):
        if len((secret or "").encode()) < 32: raise ValueError("worker secret must contain at least 32 bytes")
        c = self._current(worker_id); version = str(int(c.version) + 1) if c else "1"
        if c: c.status, c.revoked_at = "revoked", utcnow(); self._save(c)
        self._save(Credential(worker_id, version, digest_secret(secret)))
        return {"worker_id": worker_id, "credential_version": version}

    def revoke(self, worker_id, version=None):
        c = self._get(worker_id, version) if version else self._current(worker_id)
        if not c: raise ValueError("worker credential not found")
        c.status, c.revoked_at = "revoked", utcnow(); self._save(c)
        return {"worker_id": worker_id, "credential_version": c.version, "status": "revoked"}
    def revoke_all(self, worker_id):
        with self._lock:
            now = utcnow()
            if self.path:
                with self._db() as db:
                    updated = db.execute(
                        "UPDATE worker_credentials SET status='revoked', revoked_at=? WHERE worker_id=? AND status!='revoked'",
                        (now.isoformat(), worker_id),
                    )
                    count = updated.rowcount
            else:
                credentials = [c for (wid, _), c in self._credentials.items() if wid == worker_id and c.status != "revoked"]
                for credential in credentials:
                    credential.status, credential.revoked_at = "revoked", now
                count = len(credentials)
            return {"worker_id": worker_id, "status": "revoked", "credentials_revoked": count}
    def delete_worker(self, worker_id):
        with self._lock:
            if self.path:
                with self._db() as db:
                    revoked = db.execute("SELECT COUNT(*) FROM worker_credentials WHERE worker_id=?", (worker_id,)).fetchone()[0]
                    db.execute("DELETE FROM worker_credentials WHERE worker_id=?", (worker_id,))
                    db.execute("DELETE FROM worker_nonces WHERE worker_id=?", (worker_id,))
                    db.execute("DELETE FROM auth_enrollments WHERE worker_id=?", (worker_id,))
                return {"worker_id": worker_id, "credentials_revoked": revoked}
            credential_keys = [key for key in self._credentials if key[0] == worker_id]
            for key in credential_keys:
                self._credentials.pop(key, None)
            self._nonces = {key for key in self._nonces if key[0] != worker_id}
            enrollment_keys = [digest for digest, enrollment in self._enrollments.items() if enrollment.worker_id == worker_id]
            for digest in enrollment_keys:
                self._enrollments.pop(digest, None)
            return {"worker_id": worker_id, "credentials_revoked": len(credential_keys)}
