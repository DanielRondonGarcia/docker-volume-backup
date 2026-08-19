import json
import re
import ssl
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlsplit

from src.security.hmac_protocol import digest_secret, sign_request
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


_HTTP_ERROR_DETAIL_LIMIT = 512
_HTTP_ERROR_BODY_READ_LIMIT = 4096


def _safe_diagnostic_text(value: Any, limit: int = _HTTP_ERROR_DETAIL_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"(?i)(?:https?|http\+docker)://[^\s]+", "<url>", text)
    text = re.sub(
        r"(?i)\b(?:authorization|bearer|token|secret|password|credential)\b\s*[:=]\s*[^\s,;]+",
        "<redacted>",
        text,
    )
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _safe_endpoint_path(path: str) -> str:
    raw_path = str(path or "/").strip()
    try:
        parsed = urlsplit(raw_path)
        raw_path = parsed.path or raw_path
    except ValueError:
        pass
    raw_path = raw_path.split("?", 1)[0].split("#", 1)[0]
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"
    return _safe_diagnostic_text(raw_path) or "/"


class ControlPlaneHTTPError(HTTPError):
    """A bounded, endpoint-only representation of a Control Plane rejection."""

    def __init__(
        self,
        method: str,
        path: str,
        status: int,
        detail: str = "",
        server_code: Any = None,
    ):
        self.method = str(method).upper()
        self.path = _safe_endpoint_path(path)
        self.detail = _safe_diagnostic_text(detail)
        self.server_code = _safe_diagnostic_text(server_code) if server_code is not None else ""

        message = f"Control Plane rejected {self.method} {self.path}: HTTP {status}"
        if self.server_code:
            message += f" (code={self.server_code})"
        if self.detail:
            message += f": {self.detail}"
        self._diagnostic_message = message
        super().__init__(self.path, status, message, {}, None)
        self.close()

    def __str__(self) -> str:
        return self._diagnostic_message

    @property
    def status(self) -> int:
        return self.code


def _parse_http_error_detail(body: str, reason: Any) -> tuple[str, Any]:
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            pass

    server_code = None
    detail = body
    if isinstance(payload, dict):
        server_code = payload.get("code")
        for key in ("error", "message", "detail"):
            if payload.get(key) is not None:
                detail = payload[key]
                break

    safe_detail = _safe_diagnostic_text(detail)
    if not safe_detail:
        safe_detail = _safe_diagnostic_text(reason) or "no response detail"
    return safe_detail, server_code


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 15,
        ca_file: str | None = None,
        credential_store: WorkerCredentialStore | None = None,
        worker_id: str | None = None,
        enrollment_secret: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ca_file = ca_file
        self.credential_store = credential_store
        self.worker_id = worker_id
        self.enrollment_secret = enrollment_secret

    def register_worker(
        self,
        name: str,
        host_name: str,
        version: str,
        labels: Dict[str, str],
        worker_id: str | None = None,
    ) -> Dict[str, Any]:
        if self.enrollment_secret: response = self.enroll_worker(self.enrollment_secret, "1", labels); self.worker_id = response["worker_id"]; self.credential_store.save(response["worker_id"], self.enrollment_secret, response["credential_version"]); return response
        raise RuntimeError("worker enrollment secret is not configured")

    def enroll_worker(self, token: str, version: str, labels: Dict[str, str]) -> Dict[str, Any]:
        return self._post(
            "/api/v1/worker-enrollments/complete",
            {
                "secret": token,
                "version": version,
                "labels": labels,
            },
            authenticate=False,
        )

    def send_heartbeat(self, worker_id: str, version: str, labels: Dict[str, str]) -> Dict[str, Any]:
        return self._post(f"/api/v1/workers/{worker_id}/heartbeat", {"version": version, "labels": labels})

    def sync_inventory(self, worker_id: str, inventory: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/api/v1/workers/{worker_id}/inventory", {"inventory": inventory})

    def fetch_jobs(self, worker_id: str) -> List[Dict[str, Any]]:
        return self._post(f"/api/v1/workers/{worker_id}/jobs/fetch", {}).get("items", [])

    def fetch_interactive_jobs(self, worker_id: str) -> List[Dict[str, Any]]:
        try:
            return self._post(f"/api/v1/workers/{worker_id}/jobs/fetch-interactive", {}).get("items", [])
        except (AttributeError, NotImplementedError):
            return self.fetch_jobs(worker_id)
        except HTTPError as exc:
            if exc.code not in (404, 405, 501):
                raise
            return self.fetch_jobs(worker_id)

    def is_job_cancelled(self, worker_id: str, job_id: str) -> bool:
        result = self._post(f"/api/v1/workers/{worker_id}/jobs/{job_id}/cancel-status", {})
        return bool(result.get("canceled", False))

    def update_job_status(
        self,
        worker_id: str,
        job_id: str,
        status: str,
        result_summary: Dict[str, Any],
        log_lines: List[str],
        lease_token: str | None = None,
    ) -> Dict[str, Any]:
        return self._post(
            f"/api/v1/workers/{worker_id}/jobs/{job_id}/status",
            {
                "status": status,
                "result_summary": result_summary,
                "log_lines": log_lines,
                "lease_token": lease_token,
            },
        )

    def renew_job_lease(self, worker_id: str, job_id: str, lease_token: str) -> Dict[str, Any]:
        return self._post(
            f"/api/v1/workers/{worker_id}/jobs/{job_id}/renew-lease",
            {"lease_token": lease_token},
        )

    def _post(self, path: str, payload: Dict[str, Any], authenticate: bool = True) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if authenticate:
            credential = self.credential_store.load() if self.credential_store else None
            if credential is None or not self.worker_id:
                raise RuntimeError("worker credential is not configured")
            timestamp = str(int(time.time()))
            nonce = secrets.token_urlsafe(18)
            headers.update(
                {
                    "X-Worker-ID": self.worker_id,
                    "X-Worker-Credential-Version": credential.version,
                    "X-Worker-Timestamp": timestamp,
                    "X-Worker-Nonce": nonce,
                    "X-Worker-Signature": sign_request(
                        digest_secret(credential.secret), "POST", path, data, timestamp, nonce,
                        self.worker_id, credential.version,
                    ),
                }
            )
        http_request = request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers=headers,
        )
        ssl_context = self._build_ssl_context()
        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds, context=ssl_context) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                raw_body = exc.read(_HTTP_ERROR_BODY_READ_LIMIT)
            except Exception:
                raw_body = b""
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            if isinstance(raw_body, bytes):
                error_body = raw_body.decode("utf-8", errors="replace")
            else:
                error_body = str(raw_body or "")
            detail, server_code = _parse_http_error_detail(error_body, exc.reason)
            raise ControlPlaneHTTPError("POST", path, exc.code, detail, server_code) from exc
        return json.loads(body) if body else {}

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not self.base_url.lower().startswith("https://"):
            return None
        if self.ca_file and Path(self.ca_file).exists():
            context = ssl.create_default_context(cafile=self.ca_file)
        else:
            context = ssl.create_default_context()
        return context
