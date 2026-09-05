"""Bounded, read-only Restic ownership evidence."""

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Optional

from src.app.domain.restore_ownership import RestoreOwnershipPolicy

MAX_OUTPUT_BYTES = 256 * 1024
MAX_RECORDS = 2048
_RECORD = re.compile(
    r"^(?P<mode>[-?bcCdDlLpPs][rwxXsStT-]{9})\s+(?P<uid>\d+)\s+(?P<gid>\d+)\s+(?P<size>\d+)\s+"
    r"(?P<mtime>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<path>.+)$"
)


class ResticMetadataError(ValueError):
    def __init__(self, message: str, category: str = "restic_metadata_inspection_failed"):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class ResticFileMetadata:
    path: str
    mode: str
    uid: int
    gid: int
    size: int
    mtime: datetime

    @property
    def owner(self) -> str: return f"{self.uid}:{self.gid}"
    @property
    def owner_label(self) -> str: return "file_owner"


@dataclass(frozen=True)
class ResticMetadataEvidence:
    snapshot: str = ""
    files: tuple[ResticFileMetadata, ...] = ()
    creator_uid: Optional[int] = None
    creator_gid: Optional[int] = None
    policy: RestoreOwnershipPolicy = field(default_factory=RestoreOwnershipPolicy)
    category: str = "ok"
    detail: Optional[str] = None

    @property
    def success(self) -> bool: return self.category == "ok"
    @property
    def owners(self) -> tuple[str, ...]: return tuple(sorted({item.owner for item in self.files}))
    @property
    def ownership_classification(self) -> str:
        return "empty" if not self.files else ("dominant" if len(self.owners) == 1 else "mixed")
    @property
    def creator_owner(self) -> Optional[str]:
        return None if self.creator_uid is None or self.creator_gid is None else f"{self.creator_uid}:{self.creator_gid}"
    @property
    def backup_creator_label(self) -> str: return "backup_creator"
    @property
    def inferred_mapping(self) -> None: return None
    @property
    def restored_metadata_proven(self) -> bool: return False


def _policy(value: Any) -> RestoreOwnershipPolicy:
    try:
        result = value if isinstance(value, RestoreOwnershipPolicy) else RestoreOwnershipPolicy.from_dict(value or {})
        return result.require_confirmation()
    except ValueError as exc:
        raise ResticMetadataError(str(exc), getattr(exc, "category", "confirmation_required")) from exc


def parse_restic_ls_long(
    output: str | bytes, *, snapshot: str = "", creator: Optional[tuple[int, int]] = None,
    policy: Any = None, max_output_bytes: int = MAX_OUTPUT_BYTES, max_records: int = MAX_RECORDS,
) -> ResticMetadataEvidence:
    if isinstance(output, bytes):
        try: text = output.decode("utf-8")
        except UnicodeDecodeError as exc: raise ResticMetadataError("Restic metadata output is not valid UTF-8") from exc
    elif isinstance(output, str): text = output
    else: raise ResticMetadataError("Restic metadata output is incomplete")
    if len(text.encode("utf-8")) > max_output_bytes:
        raise ResticMetadataError("Restic metadata output exceeded the bounded limit")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > max_records:
        raise ResticMetadataError("Restic metadata inspection output is incomplete or over the record bound")
    records = []
    for line in lines:
        match = _RECORD.fullmatch(line)
        if match is None: raise ResticMetadataError("Restic metadata inspection output contains a malformed record")
        try:
            values = match.groupdict()
            records.append(ResticFileMetadata(values["path"], values["mode"], int(values["uid"]), int(values["gid"]), int(values["size"]), datetime.fromisoformat(values["mtime"])))
        except (TypeError, ValueError) as exc:
            raise ResticMetadataError("Restic metadata inspection output contains an invalid record") from exc
    creator_uid, creator_gid = creator or (None, None)
    return ResticMetadataEvidence(snapshot, tuple(records), creator_uid, creator_gid, _policy(policy))
