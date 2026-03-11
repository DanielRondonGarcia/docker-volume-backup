import docker
import logging
from typing import List, Optional
from src.app.application.ports.ports import ContainerPort

logger = logging.getLogger(__name__)

class DockerAdapter(ContainerPort):
    def __init__(self):
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.error(f"Failed to connect to Docker: {e}")
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
        if not self.client: return
        for cid in container_ids:
            try:
                container = self.client.containers.get(cid)
                logger.info(f"Starting container {cid}")
                container.start()
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error starting container {cid}: {e}")

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
