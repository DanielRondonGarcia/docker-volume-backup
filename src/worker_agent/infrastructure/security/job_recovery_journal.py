import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class WorkerJobRecoveryJournal:
    """Atomic, metadata-only journal for the one job a worker executes at a time."""

    VERSION = 1
    MAX_RECORD_BYTES = 4096
    MAX_AGE_SECONDS = 7 * 24 * 60 * 60
    _TEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    def __init__(self, path: str):
        self.path = Path(path)

    @classmethod
    def _bounded_text(cls, value: Any, field: str, maximum: int = 128) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum or not cls._TEXT_PATTERN.fullmatch(value):
            raise ValueError(f"invalid recovery journal {field}")
        return value

    @classmethod
    def _normalize_record(cls, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict) or value.get("version") != cls.VERSION:
            return None
        job_id = value.get("job_id")
        worker_id = value.get("worker_id")
        command = value.get("command")
        lease_token = value.get("lease_token")
        created_at = value.get("created_at")
        try:
            job_id = cls._bounded_text(job_id, "job_id")
            worker_id = cls._bounded_text(worker_id, "worker_id")
            command = cls._bounded_text(command, "command")
            if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 512:
                return None
            if not isinstance(created_at, str) or len(created_at) > 64:
                return None
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            if age > cls.MAX_AGE_SECONDS or age < -300:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        return {
            "version": cls.VERSION,
            "job_id": job_id,
            "worker_id": worker_id,
            "command": command,
            "lease_token": lease_token,
            "created_at": created_at,
        }

    def write(self, job_id: str, worker_id: str, command: str, lease_token: str) -> Dict[str, Any]:
        record = {
            "version": self.VERSION,
            "job_id": self._bounded_text(job_id, "job_id"),
            "worker_id": self._bounded_text(worker_id, "worker_id"),
            "command": self._bounded_text(command, "command"),
            "lease_token": lease_token if isinstance(lease_token, str) and 0 < len(lease_token) <= 512 else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if record["lease_token"] is None:
            raise ValueError("invalid recovery journal lease_token")
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > self.MAX_RECORD_BYTES:
            raise ValueError("recovery journal record is too large")

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(parent))
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
            try:
                directory_descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return record

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            if self.path.stat().st_size > self.MAX_RECORD_BYTES:
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None
        return self._normalize_record(value)

    def clear(self, expected_job_id: Optional[str] = None) -> bool:
        if expected_job_id is not None:
            record = self.load()
            if record is None or record["job_id"] != expected_job_id:
                return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
