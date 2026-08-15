import base64
import json
import logging
from typing import Any, Dict, List

from src.control_plane.domain.models import JobStatus
from src.worker_agent.domain.models import WorkerAgentConfig, WorkerJobExecutionResult
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient

logger = logging.getLogger(__name__)


class WorkerAgentService:
    def __init__(
        self,
        config: WorkerAgentConfig,
        control_plane_client: ControlPlaneClient,
        docker_runtime: DockerRuntimeAdapter,
    ):
        self.config = config
        self.control_plane_client = control_plane_client
        self.docker_runtime = docker_runtime

    def ensure_registered(self) -> str:
        if self.config.worker_id and self.control_plane_client.credential_store and self.control_plane_client.credential_store.load():
            return self.config.worker_id
        response = self.control_plane_client.register_worker(
            name=self.config.name,
            host_name=self.config.host_name,
            version=self.config.version,
            labels=self.config.labels,
            worker_id=self.config.worker_id,
        )
        self.config.worker_id = response["worker_id"]
        return self.config.worker_id

    def send_heartbeat(self):
        worker_id = self.ensure_registered()
        return self.control_plane_client.send_heartbeat(
            worker_id=worker_id,
            version=self.config.version,
            labels=self.config.labels,
        )

    @staticmethod
    def _parse_snapshot_ls_entries(logs: str) -> List[Dict[str, Any]]:
        if not isinstance(logs, str) or not logs:
            return []

        decoder = json.JSONDecoder()
        parsed_values: List[Any] = []
        offset = 0
        while offset < len(logs):
            while offset < len(logs) and logs[offset].isspace():
                offset += 1
            if offset >= len(logs):
                break
            try:
                value, end = decoder.raw_decode(logs, offset)
            except json.JSONDecodeError:
                newline = logs.find("\n", offset)
                if newline == -1:
                    break
                offset = newline + 1
                continue
            parsed_values.append(value)
            offset = end

        candidates: List[Dict[str, Any]] = []
        for value in parsed_values:
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                candidates.append(value)
        return [
            entry
            for entry in candidates
            if entry.get("struct_type") == "node" or entry.get("type") in ("file", "dir")
        ]

    def sync_inventory(self):
        worker_id = self.ensure_registered()
        inventory = self.docker_runtime.collect_inventory()
        return self.control_plane_client.sync_inventory(worker_id, inventory)

    def poll_once(self):
        worker_id = self.ensure_registered()
        jobs = self.control_plane_client.fetch_jobs(worker_id)
        results = []
        for job in jobs:
            execution = self.execute_job(job)
            updated = self.control_plane_client.update_job_status(
                worker_id=worker_id,
                job_id=job["id"],
                status=execution.status,
                result_summary=execution.result_summary,
                log_lines=execution.log_lines,
                lease_token=job["lease_token"],
            )
            results.append(updated)
        return results

    def execute_job(self, job: Dict[str, Any]) -> WorkerJobExecutionResult:
        command = job.get("command")
        payload = job.get("payload") or {}
        logger.info("Executing worker job %s (%s)", job.get("id"), command)

        try:
            if command == "inventory.refresh":
                inventory = self.docker_runtime.collect_inventory()
                self.control_plane_client.sync_inventory(self.config.worker_id, inventory)
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED,
                    result_summary={"docker_available": inventory.get("docker_available", False)},
                    log_lines=["Inventory synchronized"],
                )

            if command == "worker.self_check":
                summary = self.docker_runtime.self_check()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED,
                    result_summary=summary,
                    log_lines=["Self check completed"],
                )

            if command == "containers.stop":
                summary = self.docker_runtime.stop_containers(payload.get("container_ids") or [])
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if not summary["errors"] else JobStatus.FAILED,
                    result_summary=summary,
                    log_lines=["Stop containers executed"],
                )

            if command == "containers.start":
                summary = self.docker_runtime.start_containers(payload.get("container_ids") or [])
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if not summary["errors"] else JobStatus.FAILED,
                    result_summary=summary,
                    log_lines=["Start containers executed"],
                )

            if command == "containers.restart":
                summary = self.docker_runtime.restart_containers(payload.get("container_ids") or [])
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if not summary["errors"] else JobStatus.FAILED,
                    result_summary=summary,
                    log_lines=["Restart containers executed"],
                )

            if command == "backup.run":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.run_runtime_job(image=image, payload=payload)
                combined_logs = (summary.get("logs", "") + "\n" + summary.get("stderr", "")).strip()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "compose_project": payload.get("compose_project"),
                    },
                    log_lines=combined_logs.splitlines()[-1000:],
                )

            if command == "snapshots.list":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.list_restic_snapshots(image=image, payload=payload)
                combined_logs = (summary.get("logs", "") + "\n" + summary.get("stderr", "")).strip()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "snapshots": summary.get("snapshots", []),
                    },
                    log_lines=combined_logs.splitlines()[-1000:],
                )

            if command == "snapshot.ls":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.run_runtime_job(image=image, payload=payload)
                entries = self._parse_snapshot_ls_entries(summary.get("logs", ""))
                combined_logs = (summary.get("logs", "") + "\n" + summary.get("stderr", "")).strip()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "entries": entries,
                    },
                    log_lines=combined_logs.splitlines()[-1000:],
                )

            if command == "snapshot.dump":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.run_runtime_job_binary(image=image, payload=payload)
                stdout_bytes = summary.get("stdout_bytes", b"")
                b64_content = base64.b64encode(stdout_bytes).decode("ascii") if stdout_bytes else ""
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "b64_content": b64_content,
                        "stderr": summary.get("stderr", ""),
                    },
                    log_lines=summary.get("stderr", "").splitlines()[-1000:],
                )

            if command == "stats.get":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.get_restic_stats(image=image, payload=payload)
                combined_logs = (summary.get("logs", "") + "\n" + summary.get("stderr", "")).strip()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "stats": summary.get("stats", {}),
                    },
                    log_lines=combined_logs.splitlines()[-1000:],
                )

            if command == "retention.run":
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.run_runtime_job(image=image, payload=payload)
                combined_logs = (summary.get("logs", "") + "\n" + summary.get("stderr", "")).strip()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "retention_command": payload.get("command"),
                    },
                    log_lines=combined_logs.splitlines()[-1000:],
                )

            if command in ("restore.dry_run", "restore.run"):
                image = payload.get("image") or self.config.backup_runtime_image
                summary = self.docker_runtime.run_runtime_job(image=image, payload=payload)
                combined_logs = (summary.get("logs", "") + "\n" + summary.get("stderr", "")).strip()
                return WorkerJobExecutionResult(
                    status=JobStatus.SUCCEEDED if summary.get("success") else JobStatus.FAILED,
                    result_summary={
                        "status_code": summary.get("status_code"),
                        "target_id": payload.get("target_id"),
                        "dry_run": command == "restore.dry_run",
                    },
                    log_lines=combined_logs.splitlines()[-1000:],
                )

            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"error": f"unsupported command: {command}"},
                log_lines=[f"Unsupported command: {command}"],
            )
        except Exception as exc:
            logger.exception("Worker job failed")
            return WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={"error": str(exc), "command": command},
                log_lines=[str(exc)],
            )
