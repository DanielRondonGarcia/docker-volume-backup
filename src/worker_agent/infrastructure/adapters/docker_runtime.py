import os
import shutil
import tempfile
import json
import math
from typing import Any, Dict, List

import logging

try:
    import docker
except ModuleNotFoundError:
    docker = None

logger = logging.getLogger(__name__)


class DockerRuntimeAdapter:
    DEFAULT_RUNTIME_TIMEOUT_SECONDS = 1800.0
    _SHELL_METACHARACTERS = frozenset(";|&`$><\\\"'\n\r\x00")
    _SECRET_ENV_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY", "ACCESS_KEY", "CREDENTIAL")

    def __init__(self, timeout_seconds: float | None = None):
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else os.environ.get(
            "WORKER_RUNTIME_TIMEOUT_SECONDS", str(self.DEFAULT_RUNTIME_TIMEOUT_SECONDS)
        )
        if docker is None:
            self.client = None
            return
        try:
            self.client = docker.from_env()
        except Exception:
            self.client = None

    @classmethod
    def _timeout_seconds(cls, value: Any) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            raise ValueError("runtime timeout_seconds must be a finite positive number") from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 86400.0:
            raise ValueError("runtime timeout_seconds is outside the permitted bounds")
        return timeout

    @classmethod
    def _runtime_command_argv(cls, command: Any) -> List[str]:
        if command is None or command == "":
            argv = ["/root/backup.sh"]
        elif isinstance(command, (list, tuple)) and command and all(isinstance(item, str) for item in command):
            argv = list(command)
        elif isinstance(command, str):
            if any(character in command for character in cls._SHELL_METACHARACTERS):
                raise ValueError("runtime command contains shell metacharacters")
            argv = command.split()
        else:
            raise ValueError("runtime command must be a supported string or argv list")
        if tuple(argv) in {
            ("/root/backup.sh",),
            ("restic", "snapshots", "--json"),
            ("restic", "stats", "--mode", "raw-data", "--json"),
        }:
            return argv
        if not argv or argv[0] != "restic":
            raise ValueError("unsupported runtime executable")
        if len(argv) in (4, 5) and argv[1:3] == ["ls", "--json"]:
            if not cls._safe_runtime_token(argv[3]) or (len(argv) == 5 and not cls._safe_runtime_token(argv[4], True)):
                raise ValueError("unsafe restic ls bounds")
            return argv
        if len(argv) == 4 and argv[1] == "dump" and cls._safe_runtime_token(argv[2]) and cls._safe_runtime_token(argv[3], True):
            return argv
        if len(argv) >= 3 and argv[1] == "forget":
            index = 2
            while index < len(argv):
                if argv[index] == "--prune":
                    index += 1
                elif argv[index] in {"--keep-last", "--keep-hourly", "--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-yearly"} and index + 1 < len(argv) and argv[index + 1].isdigit():
                    index += 2
                else:
                    raise ValueError("unsupported or unbounded restic retention command")
            return argv
        raise ValueError("unsupported runtime command")

    @staticmethod
    def _safe_runtime_token(value: Any, path: Any = False) -> bool:
        if not isinstance(value, str) or not value or ".." in value.split("/"):
            return False
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
        return (not path or value.startswith("/")) and all(character in allowed + ("/" if path else "") for character in value)

    def _validate_runtime_volumes(self, volumes: Any) -> Dict[str, Dict[str, str]]:
        if not isinstance(volumes, dict):
            raise ValueError("runtime volumes must be an object")
        normalized = {}
        for source, spec in volumes.items():
            destination = spec.get("bind") if isinstance(spec, dict) else None
            mode = spec.get("mode", "ro") if isinstance(spec, dict) else None
            if not isinstance(source, str) or not isinstance(destination, str) or mode not in {"ro", "rw"}:
                raise ValueError("runtime mount specification is invalid")
            if self._is_ignored_bind_source(source) or self._is_ignored_bind_destination(destination):
                raise ValueError("unsafe runtime bind mount rejected")
            normalized[source] = {"bind": destination, "mode": mode}
        return normalized

    @classmethod
    def _collect_secret_values(cls, payload: Any) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        environment = payload.get("environment") or {}
        environment = environment if isinstance(environment, dict) else {}
        env_secrets = {value for key, value in environment.items() if isinstance(value, str) and value and (key == "RCLONE_CONF_CONTENT" or any(marker in str(key).upper() for marker in cls._SECRET_ENV_MARKERS))}
        file_secrets = {item["content"] for item in payload.get("resolved_files") or [] if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"]}
        return env_secrets | file_secrets

    @staticmethod
    def _redact_text(value: Any, secrets: set[str]) -> str:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

    @staticmethod
    def _write_secret(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(path, 0o600)

    @staticmethod
    def _cleanup_temp_dirs(temp_dirs: List[str] | None) -> None:
        for temp_dir in temp_dirs or []:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _cleanup_container(container: Any) -> None:
        try:
            container.remove(force=True)
        except Exception:
            pass

    @staticmethod
    def _is_timeout_error(error: Exception) -> bool:
        return isinstance(error, TimeoutError) or error.__class__.__name__.lower() in {"readtimeout", "connecttimeout"} or "timed out" in str(error).lower()

    def _failure_result(self, message: str, binary: bool, secrets: set[str], status_code: int = 1) -> Dict[str, Any]:
        message = self._redact_text(message, secrets)
        return {"success": False, "status_code": status_code, "error": message, **({"stdout_bytes": b"", "stderr": message} if binary else {"logs": message, "stderr": ""})}

    def _prepare_runtime(self, payload: Dict[str, Any], binary: bool):
        environment = dict(payload.get("environment") or {})
        volumes = self._validate_runtime_volumes(payload.get("volumes") or {})
        command = self._runtime_command_argv(payload.get("command"))
        timeout = self._timeout_seconds(payload.get("timeout_seconds", getattr(self, "timeout_seconds", self.DEFAULT_RUNTIME_TIMEOUT_SECONDS)))
        network_mode = payload.get("network_mode") or ("none" if binary else None)
        if network_mode not in {None, "none", "bridge"}: raise ValueError("unsupported runtime network mode")
        resolved_files = payload.get("resolved_files") or []
        if os.path.exists("/var/run/docker.sock"): volumes["/var/run/docker.sock"] = {"bind": "/var/run/docker.sock", "mode": "rw"}
        temp_dirs = []
        try:
            rclone_content = environment.get("RCLONE_CONF_CONTENT", "")
            needs_rclone = environment.get("RESTIC_REPOSITORY", "").startswith("rclone:")
            rclone_written = False
            secret_files = []
            rclone_files = []
            for index, file_spec in enumerate(resolved_files, start=1):
                is_rclone = "rclone" in str(file_spec.get("secret_name", "")).lower() or "rclone.conf" in str(file_spec.get("container_path", "")).lower()
                secret_files.append((index, file_spec, is_rclone))
                if is_rclone:
                    rclone_files.append((index, file_spec))

            secrets_dir = None
            rclone_dir = None
            if resolved_files or rclone_content:
                temp_dir = tempfile.mkdtemp(prefix="worker-job-secrets-", dir=tempfile.gettempdir())
                temp_dirs.append(temp_dir)
                os.chmod(temp_dir, 0o700)
                secrets_dir = temp_dir
            if rclone_files or rclone_content:
                temp_dir = tempfile.mkdtemp(prefix="worker-job-rclone-config-", dir=tempfile.gettempdir())
                temp_dirs.append(temp_dir)
                os.chmod(temp_dir, 0o700)
                rclone_dir = temp_dir

            for index, file_spec, is_rclone in secret_files:
                local_path = os.path.join(secrets_dir, "rclone.conf" if is_rclone else f"secret_{index}")
                self._write_secret(local_path, file_spec["content"])
            for _, file_spec in rclone_files:
                self._write_secret(os.path.join(rclone_dir, "rclone.conf"), file_spec["content"])
                rclone_written = True
            if rclone_content and not rclone_written:
                self._write_secret(os.path.join(secrets_dir, "rclone.conf"), rclone_content)
                self._write_secret(os.path.join(rclone_dir, "rclone.conf"), rclone_content)
                rclone_written = True
            if secrets_dir:
                volumes[secrets_dir] = {"bind": "/run/secrets", "mode": "ro"}
            if rclone_dir:
                volumes[rclone_dir] = {"bind": "/run/rclone-config", "mode": "rw"}
                environment["RCLONE_CONFIG"] = "/run/rclone-config/rclone.conf"
            if needs_rclone and not rclone_written:
                raise ValueError("rclone repository requires an rclone.conf secret")
            environment.pop("RCLONE_CONF_CONTENT", None)
            return environment, volumes, command, network_mode, timeout, temp_dirs
        except Exception:
            self._cleanup_temp_dirs(temp_dirs)
            raise

    def _pull_image(self, image: str) -> None:
        if not self.client or not image:
            return
        if "/" not in image or "." not in image.split("/")[0]:
            return
        if ":" not in image and "@" not in image:
            image = f"{image}:latest"
        try:
            self.client.images.pull(image)
            logger.info("Pulled runtime image %s", image)
        except Exception as exc:
            logger.warning("Failed to pull runtime image %s: %s", image, exc)

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
        "/rootfs",
        "/host/proc",
        "/host/sys",
        "/host/dev",
        "/host/rootfs",
    }

    _IGNORED_BIND_SOURCES = {
        "/",
        "/proc",
        "/sys",
        "/dev",
        "/host/proc", "/host/sys", "/host/dev", "/host/rootfs",
    }

    @staticmethod
    def _is_ignored_bind_destination(bind_path: str) -> bool:
        normalized = bind_path.rstrip("/") or "/"
        if normalized in DockerRuntimeAdapter._IGNORED_BIND_DESTINATIONS:
            return True
        if normalized.startswith("/proc/") or normalized.startswith("/sys/") or normalized.startswith("/dev/"):
            return True
        if normalized.endswith(".sock") or normalized.endswith(".socket") or ".." in normalized.split("/"):
            return True
        return False

    @staticmethod
    def _is_ignored_bind_source(source: str) -> bool:
        normalized = source.rstrip("/") or "/"
        if normalized in DockerRuntimeAdapter._IGNORED_BIND_SOURCES:
            return True
        if normalized.startswith("/proc/") or normalized.startswith("/sys/") or normalized.startswith("/dev/"):
            return True
        if normalized.endswith(".sock") or normalized.endswith(".socket") or ".." in normalized.split("/"):
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
        if mount_type == "bind" and DockerRuntimeAdapter._is_ignored_bind_source(source):
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
        secrets = self._collect_secret_values(payload)
        temp_dirs = None
        container = None
        try:
            environment, volumes, command, network_mode, timeout, temp_dirs = self._prepare_runtime(payload, binary=False)
            self._pull_image(image)
            container = self.client.containers.run(
                image=image,
                command=command,
                environment=environment,
                volumes=volumes,
                network_mode=network_mode,
                detach=True,
                remove=False,
            )
            try:
                result = container.wait(timeout=timeout)
            except Exception as exc:
                if not self._is_timeout_error(exc):
                    raise
                return self._failure_result(f"runtime timed out after {timeout:g} seconds", False, secrets, 124)
            combined = self._redact_text(container.logs(stdout=True, stderr=True, timestamps=False), secrets)
            status_code = result.get("StatusCode", 1)
            return {"success": status_code == 0, "status_code": status_code, "logs": combined, "stderr": ""}
        except Exception as exc:
            return self._failure_result(f"runtime execution failed: {exc}", False, secrets)
        finally:
            if container is not None:
                self._cleanup_container(container)
            self._cleanup_temp_dirs(temp_dirs)

    def list_restic_snapshots(self, image: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        summary = self.run_runtime_job(image=image, payload=payload)
        logs = summary.get("logs", "")
        snapshots = []
        if summary.get("success"):
            json_candidates = []
            for line in logs.splitlines():
                line = line.strip()
                if line.startswith("[") or line.startswith("{"):
                    json_candidates.append(line)
            if json_candidates:
                try:
                    parsed = json.loads("".join(json_candidates))
                    snapshots = parsed if isinstance(parsed, list) else [parsed]
                except json.JSONDecodeError:
                    summary["success"] = False
                    summary["error"] = "failed to parse restic snapshots JSON"
            else:
                try:
                    parsed = json.loads(logs or "[]")
                    snapshots = parsed if isinstance(parsed, list) else [parsed]
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
        if self.client is None:
            return {"success": False, "error": "docker unavailable"}
        secrets = self._collect_secret_values(payload)
        temp_dirs = None
        container = None
        try:
            environment, volumes, command, network_mode, timeout, temp_dirs = self._prepare_runtime(payload, binary=True)
            self._pull_image(image)
            container = self.client.containers.run(
                image=image,
                command=command,
                environment=environment,
                volumes=volumes,
                network_mode=network_mode,
                detach=True,
                remove=False,
            )
            try:
                result = container.wait(timeout=timeout)
            except Exception as exc:
                if not self._is_timeout_error(exc):
                    raise
                return self._failure_result(f"runtime timed out after {timeout:g} seconds", True, secrets, 124)
            stdout_bytes = container.logs(stdout=True, stderr=False)
            stderr_text = self._redact_text(container.logs(stdout=False, stderr=True), secrets)
            status_code = result.get("StatusCode", 1)
            return {"success": status_code == 0, "status_code": status_code, "stdout_bytes": stdout_bytes, "stderr": stderr_text}
        except Exception as exc:
            return self._failure_result(f"runtime execution failed: {exc}", True, secrets)
        finally:
            if container is not None:
                self._cleanup_container(container)
            self._cleanup_temp_dirs(temp_dirs)
