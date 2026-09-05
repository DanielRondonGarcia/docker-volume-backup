"""Restic metadata inspection without restore-target side effects."""

import os
import subprocess
from typing import Any

from src.app.application.ports.ports import ResticMetadataInspectorPort
from src.app.domain.restore_metadata import MAX_OUTPUT_BYTES, ResticMetadataEvidence, parse_restic_ls_long
from src.app.domain.restore_ownership import RestoreOwnershipPolicy


def _value(config: Any, *names: str) -> Any:
    for name in names:
        value = config.get(name) if isinstance(config, dict) else getattr(config, name, None)
        if value is not None: return value
    return None


class ResticMetadataInspectorAdapter(ResticMetadataInspectorPort):
    def __init__(self, runner=None, max_output_bytes: int = MAX_OUTPUT_BYTES):
        self.runner, self.max_output_bytes = runner or subprocess.run, max_output_bytes

    def inspect(self, snapshot: str, config: Any = None, scopes=None, policy: Any = None) -> ResticMetadataEvidence:
        try:
            selected = policy or _value(config, "restore_ownership") or RestoreOwnershipPolicy()
            if not isinstance(selected, RestoreOwnershipPolicy): selected = RestoreOwnershipPolicy.from_dict(selected)
            selected.require_confirmation()
            env = os.environ.copy()
            repository, password = _value(config, "restic_repository", "repository"), _value(config, "restic_password", "password")
            if repository is not None: env["RESTIC_REPOSITORY"] = str(repository)
            if password is not None: env["RESTIC_PASSWORD"] = str(password)
            result = self.runner(["restic", "ls", "--long", snapshot], env=env, capture_output=True, text=True, check=False)
            if getattr(result, "returncode", 1) != 0:
                return ResticMetadataEvidence(snapshot=snapshot, category="restic_metadata_inspection_failed", detail="restic ls --long failed")
            return parse_restic_ls_long(result.stdout, snapshot=snapshot, creator=_value(config, "snapshot_creator", "creator"), policy=selected, max_output_bytes=self.max_output_bytes)
        except ValueError as exc:
            return ResticMetadataEvidence(snapshot=snapshot, category=getattr(exc, "category", "restic_metadata_inspection_failed"), detail=str(exc))
        except (OSError, subprocess.SubprocessError):
            return ResticMetadataEvidence(snapshot=snapshot, category="restic_metadata_inspection_failed", detail="restic ls --long could not run")


ResticMetadataInspector = ResticMetadataInspectorAdapter
