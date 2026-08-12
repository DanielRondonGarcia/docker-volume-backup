import base64
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ModuleNotFoundError:
    Fernet = None


class SecretCodec:
    def __init__(self, key: bytes, key_version: str = "v1"):
        if Fernet is None:
            self._fernet = None
        else:
            self._fernet = Fernet(key)
        self.key_version = key_version

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("cryptography is required to encrypt secrets; install requirements first")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("cryptography is required to decrypt secrets; install requirements first")
        return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")

    @classmethod
    def from_runtime(cls, key_file_path: str, env_key: str | None = None):
        if Fernet is None:
            return cls(b"")
        if env_key:
            raw = env_key.encode("utf-8")
            if len(raw) == 44:
                return cls(raw)
            return cls(base64.urlsafe_b64encode(raw.ljust(32, b"0")[:32]))

        key_file = Path(key_file_path)
        if key_file.exists():
            return cls(key_file.read_text(encoding="utf-8").strip().encode("utf-8"))

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_text(key.decode("utf-8"), encoding="utf-8")
        return cls(key)
