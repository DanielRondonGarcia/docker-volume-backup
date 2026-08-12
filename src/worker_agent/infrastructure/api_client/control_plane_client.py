import json
import ssl
from pathlib import Path
from typing import Any, Dict, List
from urllib import request


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 15,
        ca_file: str | None = None,
        client_cert_file: str | None = None,
        client_key_file: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ca_file = ca_file
        self.client_cert_file = client_cert_file
        self.client_key_file = client_key_file

    def register_worker(
        self,
        name: str,
        host_name: str,
        version: str,
        labels: Dict[str, str],
        worker_id: str | None = None,
    ) -> Dict[str, Any]:
        return self._post(
            "/api/v1/workers/register",
            {
                "name": name,
                "host_name": host_name,
                "version": version,
                "labels": labels,
                "worker_id": worker_id,
            },
        )

    def enroll_worker(self, token: str, csr_pem: str, version: str, labels: Dict[str, str]) -> Dict[str, Any]:
        return self._post(
            "/api/v1/worker-enrollments/sign",
            {
                "token": token,
                "csr_pem": csr_pem,
                "version": version,
                "labels": labels,
            },
            include_client_certificate=False,
        )

    def send_heartbeat(self, worker_id: str, version: str, labels: Dict[str, str]) -> Dict[str, Any]:
        return self._post(f"/api/v1/workers/{worker_id}/heartbeat", {"version": version, "labels": labels})

    def sync_inventory(self, worker_id: str, inventory: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/api/v1/workers/{worker_id}/inventory", {"inventory": inventory})

    def fetch_jobs(self, worker_id: str) -> List[Dict[str, Any]]:
        return self._post(f"/api/v1/workers/{worker_id}/jobs/fetch", {}).get("items", [])

    def update_job_status(
        self,
        worker_id: str,
        job_id: str,
        status: str,
        result_summary: Dict[str, Any],
        log_lines: List[str],
    ) -> Dict[str, Any]:
        return self._post(
            f"/api/v1/workers/{worker_id}/jobs/{job_id}/status",
            {
                "status": status,
                "result_summary": result_summary,
                "log_lines": log_lines,
            },
        )

    def _post(self, path: str, payload: Dict[str, Any], include_client_certificate: bool = True) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url=f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        ssl_context = self._build_ssl_context(include_client_certificate=include_client_certificate)
        with request.urlopen(http_request, timeout=self.timeout_seconds, context=ssl_context) as response:
            body = response.read().decode("utf-8")
        return json.loads(body) if body else {}

    def _build_ssl_context(self, include_client_certificate: bool) -> ssl.SSLContext | None:
        if not self.base_url.lower().startswith("https://"):
            return None
        if self.ca_file and Path(self.ca_file).exists():
            context = ssl.create_default_context(cafile=self.ca_file)
        else:
            context = ssl.create_default_context()
        if (
            include_client_certificate
            and self.client_cert_file
            and self.client_key_file
            and Path(self.client_cert_file).exists()
            and Path(self.client_key_file).exists()
        ):
            context.load_cert_chain(certfile=self.client_cert_file, keyfile=self.client_key_file)
        return context
