import json, os
from dataclasses import dataclass
from pathlib import Path
@dataclass(frozen=True)
class StoredWorkerCredential:
    worker_id: str
    secret: str
    version: str = "1"
class WorkerCredentialStore:
    def __init__(self, path): self.path = Path(path)
    def load(self):
        if not self.path.exists(): return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not data.get("worker_id") or not data.get("secret"): raise ValueError("worker credential file is incomplete")
        return StoredWorkerCredential(str(data["worker_id"]), str(data["secret"]), str(data.get("version", "1")))
    def save(self, worker_id, secret, version="1"):
        if len(secret.encode()) < 32: raise ValueError("worker secret must contain at least 32 bytes")
        self.path.parent.mkdir(parents=True, exist_ok=True); temp = self.path.with_name(f".{self.path.name}.tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"worker_id": worker_id, "secret": secret, "version": str(version)}, stream, separators=(",", ":")); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temp, 0o600); os.replace(temp, self.path); os.chmod(self.path, 0o600)
        return StoredWorkerCredential(worker_id, secret, str(version))
