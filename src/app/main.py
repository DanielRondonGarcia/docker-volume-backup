import os
import sys
import json
import logging
import re
import posixpath
import tempfile
from src.app.domain.models import BackupConfig, ContainerConfig, RestoreConfig
from src.app.application.services.backup_service import BackupService
from src.app.application.services.restore_service import RestoreService
from src.app.infrastructure.adapters.storage.multi_storage_adapter import MultiStorageAdapter
from src.app.infrastructure.adapters.container.docker_adapter import DockerAdapter
from src.app.infrastructure.adapters.notifier.influx_notifier import InfluxNotifier
from src.app.infrastructure.adapters.backup_strategy import TarballBackupStrategy, ResticBackupStrategy

class FlushingStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[FlushingStreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")

def _strategy_for(strategy_name: str):
    if strategy_name == "restic":
        return ResticBackupStrategy()
    return TarballBackupStrategy()

MAX_RESTORE_RESULT_BYTES = 64 * 1024
MAX_RESTORE_READ_ONLY_PATHS = 256
MAX_RESTORE_READ_ONLY_PATH_LENGTH = 4096
MAX_RESTORE_READ_ONLY_PATHS_BYTES = 16 * 1024
_RESTORE_RESULT_SECRET = re.compile(r"(?i)(password|passphrase|secret|token|credential)\s*[:=]\s*[^\s,;]+")

def _restore_safe(value, depth=0):
    if depth > 4:
        return None
    if isinstance(value, dict): return {str(k)[:64]: _restore_safe(v, depth + 1) for k, v in list(value.items())[:64] if not any(m in str(k).upper() for m in ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "CONTENT"))}
    if isinstance(value, (list, tuple)): return [_restore_safe(v, depth + 1) for v in list(value)[:64]]
    if isinstance(value, str): return _RESTORE_RESULT_SECRET.sub(r"\1=<redacted>", " ".join(value.split())[:512])
    return value if isinstance(value, (int, float, bool)) or value is None else _restore_safe(str(value), depth + 1)

def _parse_restore_read_only_paths(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError("RESTORE_READ_ONLY_PATHS must be a JSON array")
    if not value.strip():
        return ()
    raw = value
    if len(raw.encode("utf-8")) > MAX_RESTORE_READ_ONLY_PATHS_BYTES:
        raise ValueError("RESTORE_READ_ONLY_PATHS exceeds the permitted size")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("RESTORE_READ_ONLY_PATHS must be a JSON array") from exc
    if not isinstance(parsed, list) or len(parsed) > MAX_RESTORE_READ_ONLY_PATHS:
        raise ValueError("RESTORE_READ_ONLY_PATHS must be a bounded JSON array")

    normalized_paths = []
    seen = set()
    for path in parsed:
        if not isinstance(path, str):
            raise ValueError("RESTORE_READ_ONLY_PATHS entries must be strings")
        path = path.strip()
        if (
            not path
            or len(path) > MAX_RESTORE_READ_ONLY_PATH_LENGTH
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or not path.startswith("/")
            or path.startswith("//")
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError("RESTORE_READ_ONLY_PATHS contains an invalid path")
        normalized = posixpath.normpath(path)
        if normalized != "/backup" and not normalized.startswith("/backup/"):
            raise ValueError("RESTORE_READ_ONLY_PATHS paths must stay within /backup")
        if normalized not in seen:
            seen.add(normalized)
            normalized_paths.append(normalized)
    return tuple(normalized_paths)

def _restore_metadata(result):
    source = getattr(result, "metadata", {}) or {}
    data = dict(source) if isinstance(source, dict) else {name: getattr(source, name, None) for name in ("snapshot", "creator_owner", "backup_creator_label", "ownership_classification", "restored_metadata_proven", "owners")}
    files = data.pop("files", getattr(source, "files", ()))
    if files: data["samples"] = [{name: (item.get(name) if isinstance(item, dict) else getattr(item, name, None)) for name in ("path", "mode", "uid", "gid", "size", "mtime")} for item in list(files)[:64]]
    return _restore_safe(data)

def _restore_result_payload(result):
    policy = getattr(result, "policy", {}) or {}
    if hasattr(policy, "to_dict"): policy = policy.to_dict()
    destructive = getattr(result, "destructive_state", None) or ("complete" if result.success else "unknown")
    error = getattr(result, "error", None)
    return {"schema_version": 1, "status": "succeeded" if result.success else "failed", "category": _restore_safe(getattr(result, "category", "ok" if result.success else "restore_failed")), "error": _restore_safe(error), "detail": _restore_safe(getattr(result, "detail", None) or error), "partial": bool(getattr(result, "partial", destructive in {"partial", "unknown"})), "destructive_state": _restore_safe(destructive), "policy": _restore_safe(policy), "metadata": _restore_metadata(result), "capability": _restore_safe(getattr(result, "capability", {})), "normalization": _restore_safe(getattr(result, "normalization", {})), "restart": _restore_safe(getattr(result, "restart", {}))}

def _write_restore_result(result):
    path = os.environ.get("RESTORE_RESULT_FILE")
    if not path: return
    if not os.path.isabs(path) or "\x00" in path:
        raise ValueError("RESTORE_RESULT_FILE must be an absolute path")
    payload = json.dumps(_restore_result_payload(result), ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_RESTORE_RESULT_BYTES: raise ValueError("restore result exceeded the permitted limit")
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".restore-result-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def main():
    # Load config from env
    backup_sources = os.environ.get("BACKUP_SOURCES", "/backup").split()
    backup_filename = os.environ.get("BACKUP_FILENAME", "backup-%Y-%m-%dT%H-%M-%S.tar.gz")
    gpg_passphrase = os.environ.get("GPG_PASSPHRASE")

    aws_s3_bucket = os.environ.get("AWS_S3_BUCKET_NAME")
    aws_glacier_vault = os.environ.get("AWS_GLACIER_VAULT_NAME")

    scp_host = os.environ.get("SCP_HOST")
    scp_user = os.environ.get("SCP_USER")
    scp_dir = os.environ.get("SCP_DIRECTORY")

    restic_repo = os.environ.get("RESTIC_REPOSITORY")
    restic_pass = os.environ.get("RESTIC_PASSWORD")

    rclone_remote = os.environ.get("RCLONE_REMOTE")
    local_archive_path = os.environ.get("BACKUP_ARCHIVE")

    restic_keep_daily = int(os.environ.get("RESTIC_KEEP_DAILY", 7))
    restic_keep_weekly = int(os.environ.get("RESTIC_KEEP_WEEKLY", 4))
    restic_keep_monthly = int(os.environ.get("RESTIC_KEEP_MONTHLY", 12))
    restic_keep_yearly = int(os.environ.get("RESTIC_KEEP_YEARLY", 1))

    pre_backup_cmd = os.environ.get("PRE_BACKUP_COMMAND")
    post_backup_cmd = os.environ.get("POST_BACKUP_COMMAND")

    backup_strategy_name = os.environ.get("BACKUP_STRATEGY", "tar").lower()
    restore_strategy_name = os.environ.get("RESTORE_BACKUP_STRATEGY", backup_strategy_name).lower()

    storage_port = MultiStorageAdapter()
    container_port = DockerAdapter()

    if _env_bool("RESTORE_MODE"):
        restore_config = RestoreConfig(
            target_path=os.environ.get("RESTORE_TARGET_PATH", ""),
            source=os.environ.get("RESTORE_SOURCE") or None,
            dry_run=_env_bool("RESTORE_DRY_RUN", True),
            force_overwrite=_env_bool("RESTORE_FORCE_OVERWRITE"),
            stop_containers=_env_bool("RESTORE_STOP_CONTAINERS"),
            chown=os.environ.get("RESTORE_CHOWN") or None,
            backup_strategy=restore_strategy_name,
            layout=os.environ.get("RESTORE_LAYOUT", "auto").lower(),
            gpg_passphrase=gpg_passphrase,
            restic_repository=restic_repo,
            restic_password=restic_pass,
            aws_s3_bucket=aws_s3_bucket,
            aws_glacier_vault=aws_glacier_vault,
            scp_host=scp_host,
            scp_user=scp_user,
            scp_directory=scp_dir,
            rclone_remote=rclone_remote,
            local_archive_path=local_archive_path,
            stop_label=os.environ.get("RESTORE_STOP_LABEL") or None,
            custom_label=os.environ.get("RESTORE_CUSTOM_LABEL") or None,
            read_only_paths=_parse_restore_read_only_paths(os.environ.get("RESTORE_READ_ONLY_PATHS")),
        )

        service = RestoreService(
            storage_port=storage_port,
            container_port=container_port,
            restore_strategy=_strategy_for(restore_strategy_name),
            restore_config=restore_config,
        )
        result = service.execute_restore()
        try: _write_restore_result(result)
        except Exception as exc: logger.error("Restore result unavailable: %s", _restore_safe(exc)); exit(1)

        if result.permission_warnings:
            for warning in result.permission_warnings:
                logger.warning(warning)
        for action in result.planned_actions or []:
            logger.info(action)
        if result.success:
            logger.info(f"Restore completed successfully in {result.duration:.1f}s")
        else:
            logger.error(f"Restore failed: {result.error}")
            exit(1)
        sys.stderr.flush()
        return

    config = BackupConfig(
        source_paths=backup_sources,
        backup_filename=backup_filename,
        gpg_passphrase=gpg_passphrase,
        aws_s3_bucket=aws_s3_bucket,
        aws_glacier_vault=aws_glacier_vault,
        scp_host=scp_host,
        scp_user=scp_user,
        scp_directory=scp_dir,
        restic_repository=restic_repo,
        restic_password=restic_pass,
        rclone_remote=rclone_remote,
        local_archive_path=local_archive_path,
        backup_strategy=backup_strategy_name,
        restic_keep_daily=restic_keep_daily,
        restic_keep_weekly=restic_keep_weekly,
        restic_keep_monthly=restic_keep_monthly,
        restic_keep_yearly=restic_keep_yearly,
        pre_backup_command=pre_backup_cmd,
        post_backup_command=post_backup_cmd
    )

    container_config = ContainerConfig(
        custom_label=os.environ.get("BACKUP_CUSTOM_LABEL"),
        stop_containers=_env_bool("BACKUP_STOP_CONTAINERS", True),
    )

    # Adapters
    notifier_port = InfluxNotifier()
    strategy = _strategy_for(backup_strategy_name)

    service = BackupService(
        storage_port=storage_port,
        container_port=container_port,
        notifier_port=notifier_port,
        backup_strategy=strategy,
        backup_config=config,
        container_config=container_config
    )

    # Execute
    result = service.execute_backup()

    if not result.success:
        exit(1)

if __name__ == "__main__":
    main()
