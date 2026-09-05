import docker
import logging
import os
from typing import List, Optional
from src.app.application.ports.ports import ContainerPort

logger = logging.getLogger(__name__)

class DockerAdapter(ContainerPort):
    def __init__(self):
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.warning(f"Docker socket not available (container stop/start disabled): {e}")
            self.client = None

    def stop_containers(self, container_ids: List[str]) -> List[str]:
        if not self.client: return []
        stopped = []
        for cid in container_ids:
            try:
                container = self.client.containers.get(cid)
                logger.info(f"Stopping container {cid}")
                container.stop()
                stopped.append(cid)
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error stopping container {cid}: {e}")
        return stopped

    def start_containers(self, container_ids: List[str]) -> None:
        if not container_ids:
            return
        if not self.client:
            raise RuntimeError("Docker client unavailable while restarting cold-restore containers")
        errors = []
        for cid in container_ids:
            try:
                container = self.client.containers.get(cid)
                logger.info(f"Starting container {cid}")
                container.start()
            except docker.errors.NotFound:
                errors.append(f"{cid}: container not found")
            except Exception as e:
                logger.error(f"Error starting container {cid}: {e}")
                errors.append(f"{cid}: {e}")
        if errors:
            raise RuntimeError("Cold-restore container restart failed: " + "; ".join(errors))

    def exec_command(self, container_id: str, command: str) -> None:
        if not self.client: return
        try:
            container = self.client.containers.get(container_id)
            logger.info(f"Exec command in {container_id}: {command}")
            exit_code, output = container.exec_run(command)
            if exit_code != 0:
                logger.error(f"Command failed with exit code {exit_code}: {output.decode('utf-8')}")
            else:
                logger.info(f"Command output: {output.decode('utf-8')}")
        except docker.errors.NotFound:
            pass
        except Exception as e:
            logger.error(f"Error executing command in {container_id}: {e}")

    def get_containers_by_labels(self, labels: List[str]) -> List[str]:
        if not self.client: return []
        try:
            containers = self.client.containers.list() # Only running containers by default
            result = []
            for c in containers:
                match = True
                for required_label in labels:
                    if "=" in required_label:
                        k, v = required_label.split("=", 1)
                        if c.labels.get(k) != v:
                            match = False
                            break
                    else:
                        if required_label not in c.labels:
                            match = False
                            break
                if match:
                    result.append(c.id)
            return result
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            return []

    def get_label_value(self, container_id: str, label: str) -> Optional[str]:
        if not self.client: return None
        try:
            container = self.client.containers.get(container_id)
            return container.labels.get(label)
        except docker.errors.NotFound:
            return None
        except Exception as e:
            logger.error(f"Error getting label for {container_id}: {e}")
            return None

    def get_container_name(self, container_id: str) -> str:
        if not self.client: return container_id
        try:
            container = self.client.containers.get(container_id)
            return container.name
        except Exception:
            return container_id

    def find_containers_using_volume(self, target_path: str) -> List[str]:
        if not self.client: return []
        try:
            containers = self.client.containers.list(all=True)
            result = []
            for container in containers:
                mounts = container.attrs.get("Mounts", [])
                for mount in mounts:
                    if target_path in (mount.get("Name"), mount.get("Source"), mount.get("Destination")):
                        result.append(container.id)
                        break
            return result
        except Exception as e:
            logger.error(f"Error finding containers using {target_path}: {e}")
            return []

    def find_containers_using_runtime_volumes(self) -> List[str]:
        if not self.client: return []
        try:
            runtime_id = os.environ.get("HOSTNAME")
            if not runtime_id:
                logger.warning("HOSTNAME env var not set; cannot identify runtime container")
                return []
            runtime = self.client.containers.get(runtime_id)
            runtime_mounts = runtime.attrs.get("Mounts", [])
            backup_sources = set()
            for mount in runtime_mounts:
                dest = mount.get("Destination", "")
                if dest.startswith("/backup/"):
                    source = mount.get("Source") or mount.get("Name")
                    if source:
                        backup_sources.add(source)
            logger.info(f"Runtime container {runtime_id} has {len(backup_sources)} backup volume source(s): {list(backup_sources)}")
            if not backup_sources:
                return []
            containers = self.client.containers.list()
            result = []
            for container in containers:
                if container.id == runtime.id:
                    continue
                mounts = container.attrs.get("Mounts", [])
                for mount in mounts:
                    source = mount.get("Source") or mount.get("Name")
                    if source and source in backup_sources:
                        result.append(container.id)
                        break
            return result
        except Exception as e:
            logger.error(f"Error finding containers using runtime volumes: {e}")
            return []
