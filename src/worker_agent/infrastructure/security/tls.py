from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ModuleNotFoundError:
    x509 = None
    hashes = None
    serialization = None
    rsa = None
    NameOID = None


def _require_cryptography() -> None:
    if x509 is None or hashes is None or serialization is None or rsa is None:
        raise RuntimeError("cryptography is required to manage worker TLS identity; install requirements first")


class WorkerTLSIdentityManager:
    def __init__(self, tls_dir: str, ca_file: str, cert_file: str, key_file: str):
        _require_cryptography()
        self.tls_dir = Path(tls_dir)
        self.tls_dir.mkdir(parents=True, exist_ok=True)
        self.ca_file = Path(ca_file)
        self.cert_file = Path(cert_file)
        self.key_file = Path(key_file)
        self.ca_file.parent.mkdir(parents=True, exist_ok=True)
        self.cert_file.parent.mkdir(parents=True, exist_ok=True)
        self.key_file.parent.mkdir(parents=True, exist_ok=True)

    def has_client_certificate(self) -> bool:
        return self.cert_file.exists() and self.key_file.exists() and self.ca_file.exists()

    def create_csr(self, name: str, host_name: str) -> str:
        key = self._load_or_create_private_key()
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.COMMON_NAME, name or host_name or "worker"),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "docker-volume-backup-worker"),
                    ]
                )
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(name or host_name or "worker"),
                        x509.DNSName(host_name or name or "worker"),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def persist_signed_materials(self, certificate_pem: str, ca_certificate_pem: str) -> None:
        self.cert_file.write_text(certificate_pem, encoding="utf-8")
        self.ca_file.write_text(ca_certificate_pem, encoding="utf-8")

    def _load_or_create_private_key(self):
        if self.key_file.exists():
            return serialization.load_pem_private_key(self.key_file.read_bytes(), password=None)
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        self.key_file.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return key
