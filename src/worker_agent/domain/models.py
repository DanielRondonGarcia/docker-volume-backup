from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class WorkerAgentConfig:
    control_plane_url: str
    name: str
    host_name: str
    version: str = "dev"
    labels: Dict[str, str] = field(default_factory=dict)
    worker_id: Optional[str] = None
    backup_runtime_image: str = "ghcr.io/danielrondongarcia/docker-volume-backup"
    enrollment_token: Optional[str] = None


@dataclass
class WorkerJobExecutionResult:
    status: str
    result_summary: Dict[str, object] = field(default_factory=dict)
    log_lines: List[str] = field(default_factory=list)
