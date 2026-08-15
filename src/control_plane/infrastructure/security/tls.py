import ipaddress
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ModuleNotFoundError:
    x509 = None
    hashes = None
    serialization = None
    rsa = None
    ExtendedKeyUsageOID = None
    NameOID = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_cryptography() -> None:
    if x509 is None or hashes is None or serialization is None or rsa is None:
        raise RuntimeError("cryptography is required to manage TLS materials; install requirements first")


def _normalize_hostnames(items: Iterable[str]) -> List[str]:
    values = []
    seen = set()
    for item in items:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


class TLSMaterialManager:
    def __init__(self, base_dir: Path, server_hostnames: Iterable[str]):
        _require_cryptography()
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.server_hostnames = _normalize_hostnames(server_hostnames)
        self.ca_cert_path = self.base_dir / "ca-cert.pem"
        self.ca_key_path = self.base_dir / "ca-key.pem"
        self.server_cert_path = self.base_dir / "server-cert.pem"
        self.server_key_path = self.base_dir / "server-key.pem"
        self._ensure_materials()

    def get_ca_certificate_pem(self) -> str:
        return self.ca_cert_path.read_text(encoding="utf-8")

    def get_ca_certificate_path(self) -> str:
        return str(self.ca_cert_path)

    def build_server_ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(self.server_cert_path), keyfile=str(self.server_key_path))
        context.load_verify_locations(cafile=str(self.ca_cert_path))
        context.verify_mode = ssl.CERT_NONE
        return context

    @classmethod
    def from_runtime(cls, base_dir: str, server_hostnames: Iterable[str]):
        return cls(base_dir=Path(base_dir), server_hostnames=server_hostnames)

    def _ensure_materials(self) -> None:
        if not self.ca_cert_path.exists() or not self.ca_key_path.exists():
            self._generate_ca()
        if not self.server_cert_path.exists() or not self.server_key_path.exists():
            self._generate_server_certificate()

    def _generate_ca(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "docker-volume-backup Control Plane CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "docker-volume-backup"),
            ]
        )
        now = _utcnow()
        subject_key_identifier = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(subject_key_identifier, critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(subject_key_identifier), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(private_key=key, algorithm=hashes.SHA256())
        )
        self.ca_key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.ca_cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    def _generate_server_certificate(self) -> None:
        ca_key = serialization.load_pem_private_key(self.ca_key_path.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        ca_subject_key = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        sans = []
        for hostname in self.server_hostnames or ["localhost", "127.0.0.1"]:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(hostname)))
            except ValueError:
                sans.append(x509.DNSName(hostname))
        now = _utcnow()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.COMMON_NAME, self.server_hostnames[0] if self.server_hostnames else "localhost"),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "docker-volume-backup"),
                    ]
                )
            )
            .issuer_name(ca_cert.subject)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_subject_key), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(private_key=ca_key, algorithm=hashes.SHA256())
        )
        self.server_key_path.write_bytes(
            server_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.server_cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
