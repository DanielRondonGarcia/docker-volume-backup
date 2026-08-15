import hashlib, hmac
from urllib.parse import parse_qsl, urlencode, urlsplit
def canonical_path_query(path):
    p = urlsplit(path or "/"); q = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)), doseq=True)
    return f"{p.path or '/'}?{q}" if q else p.path or "/"
def canonical_request_bytes(method, path, body, timestamp, nonce, worker_id, credential_version):
    return "\n".join((method.upper(), canonical_path_query(path), hashlib.sha256(body).hexdigest(), str(timestamp), nonce, worker_id, str(credential_version))).encode()
def digest_secret(secret):
    return hashlib.sha256(secret.encode() if isinstance(secret, str) else secret).hexdigest()
def sign_request(secret, method, path, body, timestamp, nonce, worker_id, credential_version):
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(key, canonical_request_bytes(method, path, body, timestamp, nonce, worker_id, credential_version), hashlib.sha256).hexdigest()
def verify_request(secret, signature, method, path, body, timestamp, nonce, worker_id, credential_version):
    return hmac.compare_digest(sign_request(secret, method, path, body, timestamp, nonce, worker_id, credential_version), signature or "")
