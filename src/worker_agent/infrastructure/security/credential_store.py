import getpass, json, os, subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredWorkerCredential:
    worker_id: str
    secret: str
    version: str = "1"


@dataclass(frozen=True)
class PendingWorkerEnrollment:
    attempt_id: str
    durable_credential: str
    token_digest: str


class WorkerCredentialStore:
    def __init__(self, path):
        self.path = Path(path)
        self.pending_path = self.path.with_name(f".{self.path.stem}.pending.json")

    def load(self):
        if not self.path.exists(): return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not data.get("worker_id") or not data.get("secret"): raise ValueError("worker credential file is incomplete")
        return StoredWorkerCredential(str(data["worker_id"]), str(data["secret"]), str(data.get("version", "1")))

    def save(self, worker_id, secret, version="1"):
        if len(secret.encode()) < 32: raise ValueError("worker secret must contain at least 32 bytes")
        self._atomic_write_json(self.path, {"worker_id": worker_id, "secret": secret, "version": str(version)})
        return StoredWorkerCredential(worker_id, secret, str(version))

    def load_pending_enrollment(self):
        if not self.pending_path.exists(): return None
        data = json.loads(self.pending_path.read_text(encoding="utf-8"))
        if not data.get("attempt_id") or not data.get("durable_credential") or not data.get("token_digest"):
            raise ValueError("pending worker enrollment file is incomplete")
        return PendingWorkerEnrollment(
            str(data["attempt_id"]),
            str(data["durable_credential"]),
            str(data["token_digest"]),
        )

    def save_pending_enrollment(self, attempt_id, durable_credential, token_digest):
        if len((durable_credential or "").encode()) < 32:
            raise ValueError("pending durable credential must contain at least 32 bytes")
        self._atomic_write_json(
            self.pending_path,
            {
                "attempt_id": str(attempt_id),
                "durable_credential": str(durable_credential),
                "token_digest": str(token_digest),
            },
        )
        return PendingWorkerEnrollment(str(attempt_id), str(durable_credential), str(token_digest))

    def delete_pending_enrollment(self):
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass

    def _atomic_write_json(self, path, payload):
        path = Path(path)
        self._prepare_parent(path.parent)
        temp = path.with_name(f".{path.name}.tmp")
        replaced = False
        try:
            if os.name == "nt":
                temp.touch(exist_ok=False)
                self._restrict_file(temp)
                mode = "w"
            else:
                mode = "x"
            with temp.open(mode, encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                self._restrict_file(temp)
            os.replace(temp, path)
            replaced = True
            self._restrict_file(path)
        finally:
            if not replaced:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass

    def _prepare_parent(self, parent):
        parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(parent, 0o700)

    def _restrict_file(self, path):
        if os.name == "nt":
            self._restrict_file_windows(path)
            return
        os.chmod(path, 0o600)

    def _restrict_file_windows(self, path):
        # chmod(0600) is not ACL protection on Windows. Use icacls and fail closed.
        try:
            user = os.getlogin()
        except OSError:
            user = getpass.getuser()
        args = [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{user}:(R,W)",
        ]
        try:
            subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            raise PermissionError("could not restrict worker credential ACL with icacls") from exc
