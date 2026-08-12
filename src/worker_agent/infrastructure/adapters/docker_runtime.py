import os
import shutil
import tempfile
import json
from typing import Any, Dict, List

try:
    import docker
except ModuleNotFoundError:
    docker = None


class DockerRuntimeAdapter:
    def __init__(self):
        if docker is None:
            self.client = None
            return
        try:
            self.client = docker.from_env()
        except Exception:
            self.client = None

    def collect_inventory(self) -> Dict[str, Any]:
        if self.client is None:
            return {
                "docker_available": False,
                "docker_info": {},
                "compose_projects": [],
                "compose_project_details": [],
                "containers": [],
                "volumes": [],
                "networks": [],
            }

        containers = self.client.containers.list(all=True)
        volumes = self.client.volumes.list()
        networks = self.client.networks.list()
        compose_project_map: Dict[str, Dict[str, Any]] = {}
        container_items = []

        for container in containers:
            labels = container.labels
            mounts = container.attrs.get("Mounts", [])
            compose_project = labels.get("com.docker.compose.project")
            compose_service = labels.get("com.docker.compose.service")
            container_item = {
                "id": container.id,
                "name": container.name,
                "image": container.image.tags,
                "status": container.status,
                "labels": labels,
                "mounts": mounts,
                "compose_project": compose_project,
                "compose_service": compose_service,
            }
            container_items.append(container_item)
            if not compose_project:
                continue

            project_item = compose_project_map.setdefault(
                compose_project,
                {
                    "name": compose_project,
                    "containers": [],
                    "volume_mounts": [],
                    "volume_targets": [],
                    "runtime_volumes": {},
                },
            )
            project_item["containers"].append(
                {
                    "id": container.id,
                    "name": container.name,
                    "service": compose_service,
                    "status": container.status,
                }
            )
            for mount in mounts:
                runtime_volume = self._mount_to_runtime_volume(mount)
                if runtime_volume is None:
                    continue
                project_item["volume_mounts"].append(runtime_volume["mount"])
                bind_path = runtime_volume["bind_path"]
                if bind_path not in project_item["volume_targets"]:
                    project_item["volume_targets"].append(bind_path)
                project_item["runtime_volumes"].setdefault(
                    runtime_volume["source"],
                    {
                        "bind": bind_path,
                        "mode": runtime_volume["mode"],
                    },
                )

        compose_projects = sorted({
            container.labels.get("com.docker.compose.project")
            for container in containers
            if container.labels.get("com.docker.compose.project")
        })

        return {
            "docker_available": True,
            "docker_info": self.client.info(),
            "compose_projects": compose_projects,
            "compose_project_details": [
                compose_project_map[name]
                for name in sorted(compose_project_map)
            ],
            "containers": container_items,
            "volumes": [
                {
                    "name": volume.name,
                    "mountpoint": volume.attrs.get("Mountpoint"),
                    "labels": volume.attrs.get("Labels") or {},
                }
                for volume in volumes
            ],
            "networks": [
                {
                    "id": network.id,
                    "name": network.name,
                    "driver": network.attrs.get("Driver"),
                    "scope": network.attrs.get("Scope"),
                }
                for network in networks
            ],
        }

    _IGNORED_BIND_DESTINATIONS = {
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/proc",
        "/sys",
        "/dev",
        "/dev/mqueue",
        "/dev/shm",
        "/etc/hostname",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/mtab",
    }

    @staticmethod
    def _is_ignored_bind_destination(bind_path: str) -> bool:
        normalized = bind_path.rstrip("/")
        if normalized in DockerRuntimeAdapter._IGNORED_BIND_DESTINATIONS:
            return True
        if normalized.startswith("/proc/") or normalized.startswith("/sys/") or normalized.startswith("/dev/"):
            return True
        if normalized.endswith(".sock") or normalized.endswith(".socket"):
            return True
        return False

    @staticmethod
    def _mount_to_runtime_volume(mount: Dict[str, Any]) -> Dict[str, Any] | None:
        mount_type = (mount.get("Type") or "").lower()
        if mount_type not in {"bind", "volume"}:
            return None
        original_dest = mount.get("Destination")
        if not original_dest:
            return None
        if mount_type == "bind" and DockerRuntimeAdapter._is_ignored_bind_destination(original_dest):
            return None
        if mount_type == "volume":
            source = mount.get("Name") or mount.get("Source")
        else:
            source = mount.get("Source")
        if not source:
            return None
        mode = "rw" if mount.get("RW", False) else "ro"
        return {
            "source": source,
            "bind_path": original_dest,
            "mode": mode,
            "mount": {
                "type": mount_type,
                "source": source,
                "destination": original_dest,
                "mode": mode,
                "name": mount.get("Name"),
            },
        }

    def self_check(self) -> Dict[str, Any]:
        inventory = self.collect_inventory()
        return {
            "docker_available": inventory.get("docker_available", False),
            "container_count": len(inventory.get("containers", [])),
            "volume_count": len(inventory.get("volumes", [])),
            "compose_project_count": len(inventory.get("compose_projects", [])),
        }

    def stop_containers(self, container_ids: List[str]) -> Dict[str, Any]:
        if self.client is None:
            return {"stopped": [], "errors": ["docker unavailable"]}
        stopped = []
        errors = []
        for container_id in container_ids:
            try:
                self.client.containers.get(container_id).stop()
                stopped.append(container_id)
            except Exception as exc:
                errors.append(f"{container_id}: {exc}")
        return {"stopped": stopped, "errors": errors}

    def start_containers(self, container_ids: List[str]) -> Dict[str, Any]:
        if self.client is None:
            return {"started": [], "errors": ["docker unavailable"]}
        started = []
        errors = []
        for container_id in container_ids:
            try:
                self.client.containers.get(container_id).start()
                started.append(container_id)
            except Exception as exc:
                errors.append(f"{container_id}: {exc}")
        return {"started": started, "errors": errors}

    def restart_containers(self, container_ids: List[str]) -> Dict[str, Any]:
        if self.client is None:
            return {"restarted": [], "errors": ["docker unavailable"]}
        restarted = []
        errors = []
        for container_id in container_ids:
            try:
                self.client.containers.get(container_id).restart()
                restarted.append(container_id)
            except Exception as exc:
                errors.append(f"{container_id}: {exc}")
        return {"restarted": restarted, "errors": errors}

    def run_runtime_job(self, image: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.client is None:
            return {"success": False, "error": "docker unavailable"}

        environment = payload.get("environment") or {}
        volumes = dict(payload.get("volumes") or {})
        command = payload.get("command") or "/root/backup.sh"
        network_mode = payload.get("network_mode")
        resolved_files = payload.get("resolved_files") or []
        temp_dir = None

        if resolved_files:
            temp_dir = tempfile.mkdtemp(prefix="worker-job-secrets-", dir="/tmp")
            for index, file_spec in enumerate(resolved_files, start=1):
                is_rclone = "rclone" in file_spec.get("secret_name", "").lower() or "rclone.conf" in file_spec.get("container_path", "")
                filename = "rclone.conf" if is_rclone else f"secret_{index}"
                local_path = os.path.join(temp_dir, filename)
                with open(local_path, "w", encoding="utf-8") as handle:
                    handle.write(file_spec["content"])
            volumes[temp_dir] = {
                "bind": "/run/secrets",
                "mode": "ro",
            }
            rclone_spec = next((f for f in resolved_files if "rclone" in f.get("secret_name", "").lower() or "rclone.conf" in f.get("container_path", "")), None)
            if rclone_spec:
                environment["RCLONE_CONFIG"] = "/run/secrets/rclone.conf"

        try:
            container = self.client.containers.run(
                image=image,
                command=command,
                environment=environment,
                volumes=volumes,
                network_mode=network_mode,
                detach=True,
                remove=False,
            )
            result = container.wait()
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            try:
                container.remove(force=True)
            except Exception:
                pass

            return {
                "success": result.get("StatusCode", 1) == 0,
                "status_code": result.get("StatusCode", 1),
                "logs": logs,
            }
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def list_restic_snapshots(self, image: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        summary = self.run_runtime_job(image=image, payload=payload)
        logs = summary.get("logs", "")
        snapshots = []
        if summary.get("success"):
            try:
                snapshots = json.loads(logs or "[]")
            except json.JSONDecodeError:
                summary["success"] = False
                summary["error"] = "failed to parse restic snapshots JSON"
        summary["snapshots"] = snapshots
        return summary

    def get_restic_stats(self, image: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        summary = self.run_runtime_job(image=image, payload=payload)
        logs = summary.get("logs", "")
        stats = {}
        if summary.get("success"):
            try:
                stats = json.loads(logs or "{}")
            except json.JSONDecodeError:
                summary["success"] = False
                summary["error"] = "failed to parse restic stats JSON"
        summary["stats"] = stats
        return summary

    def run_runtime_job_binary(self, image: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        environment = dict(payload.get("environment") or {})
        volumes = dict(payload.get("volumes") or {})
        command = payload.get("command") or "/root/backup.sh"
        network_mode = payload.get("network_mode") or "none"

        resolved_files = payload.get("resolved_files") or []
        temp_dir = None

        if resolved_files:
            temp_dir = tempfile.mkdtemp(prefix="worker-job-secrets-", dir="/tmp")
            for index, file_spec in enumerate(resolved_files, start=1):
                is_rclone = "rclone" in file_spec.get("secret_name", "").lower() or "rclone.conf" in file_spec.get("container_path", "")
                filename = "rclone.conf" if is_rclone else f"secret_{index}"
                local_path = os.path.join(temp_dir, filename)
                with open(local_path, "w", encoding="utf-8") as handle:
                    handle.write(file_spec["content"])
            volumes[temp_dir] = {"bind": "/run/secrets", "mode": "ro"}
            rclone_spec = next((f for f in resolved_files if "rclone" in f.get("secret_name", "").lower() or "rclone.conf" in f.get("container_path", "")), None)
            if rclone_spec:
                environment["RCLONE_CONFIG"] = "/run/secrets/rclone.conf"

        try:
            container = self.client.containers.run(
                image=image,
                command=command,
                environment=environment,
                volumes=volumes,
                network_mode=network_mode,
                detach=True,
                remove=False,
            )
            result = container.wait()
            stdout_bytes = container.logs(stdout=True, stderr=False)
            stderr_text = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            try:
                container.remove(force=True)
            except Exception:
                pass

            return {
                "success": result.get("StatusCode", 1) == 0,
                "status_code": result.get("StatusCode", 1),
                "stdout_bytes": stdout_bytes,
                "stderr": stderr_text,
            }
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
