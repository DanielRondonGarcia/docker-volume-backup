import subprocess
import os
import sys
import json
import logging
import shutil
import stat
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from src.app.domain.models import BackupConfig, BackupResult, RestoreConfig, RestoreResult
from src.app.application.ports.ports import BackupStrategy, RestoreStrategy
from src.app.domain.restore_ownership import RestoreOwnershipPolicy, parse_uid_gid

logger = logging.getLogger(__name__)

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
UNSUPPORTED_METADATA = ["acl", "xattr", "timestamps", "special metadata"]


class OwnershipNormalizationError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]):
        self.report = report
        self.category = report.get("category", "ownership_normalization_failed")
        super().__init__(message)


class ReadOnlyRestoreTargetError(ValueError):
    category = "readonly_target"


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _ownership_policy(config: RestoreConfig) -> RestoreOwnershipPolicy:
    value = getattr(config, "restore_ownership", None)
    if value is None and config.chown:
        return RestoreOwnershipPolicy(mode="map", default_mapping=config.chown, confirmation="confirmed", source="RESTORE_CHOWN")
    if value is None:
        return RestoreOwnershipPolicy()
    policy = value if isinstance(value, RestoreOwnershipPolicy) else RestoreOwnershipPolicy.from_dict(value)
    return policy.require_confirmation()


def _normalization_targets(config: RestoreConfig, policy: RestoreOwnershipPolicy) -> list[tuple[str, str, str]]:
    if policy.mode != "map":
        return []
    targets: list[tuple[str, str, str]] = []
    for scope in getattr(config, "volume_scopes", ()) or ():
        key = _field(scope, "volume_key") or _field(scope, "stable_key")
        path = _field(scope, "target_path") or _field(scope, "destination")
        mapping = policy.mapping_text(key) if key else None
        if key and path and mapping:
            targets.append((str(path), mapping, str(key)))
    if targets:
        return targets
    if policy.default_mapping:
        return [(config.target_path, policy.default_mapping, "default")]
    return [(config.target_path, next(iter(policy.mappings.values())), "direct")] if len(policy.mappings) == 1 and not _should_use_backup_dir_layout(config) else []


def _apply_chown(target_path: str, chown: str) -> dict[str, Any]:
    uid, gid = parse_uid_gid(chown)
    report: dict[str, Any] = {"state": "running", "changed_count": 0, "unsupported_metadata": list(UNSUPPORTED_METADATA)}
    pending = [Path(target_path)]
    current_path = target_path
    try:
        while pending:
            current = pending.pop()
            current_path = str(current)
            current_stat = os.lstat(current)
            if not report["changed_count"] and stat.S_ISLNK(current_stat.st_mode):
                raise ValueError("refusing to normalize a symlink target")
            os.lchown(current_path, uid, gid)
            report["changed_count"] += 1
            if stat.S_ISDIR(current_stat.st_mode):
                with os.scandir(current) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
    except (AttributeError, OSError, ValueError) as exc:
        report.update({"state": "partial" if report["changed_count"] else "failed", "category": "ownership_normalization_failed", "failed_path": current_path, "detail": str(exc)})
        raise OwnershipNormalizationError(f"Ownership normalization failed at {current_path}: {exc}", report) from exc
    report.update({"state": "complete", "changed": bool(report["changed_count"])})
    return report


def normalize_ownership(config: RestoreConfig, policy: RestoreOwnershipPolicy) -> dict[str, Any]:
    base = {"changed_count": 0, "changed": False, "unsupported_metadata": list(UNSUPPORTED_METADATA)}
    if policy.mode != "map" or (not policy.mappings and not policy.default_mapping):
        return {**base, "state": "preserved", "category": "preserved"}
    targets = _normalization_targets(config, policy)
    if not targets:
        raise OwnershipNormalizationError("No stable target scope is available for ownership mapping", {**base, "state": "failed", "category": "unknown_volume_scope"})
    for path, mapping, _ in targets:
        try:
            report = _apply_chown(path, mapping)
        except OwnershipNormalizationError as exc:
            exc.report["changed_count"] += base["changed_count"]
            raise
        base["changed_count"] += report["changed_count"]
    return {**base, "state": "complete", "category": "ok", "changed": bool(base["changed_count"])}


def _attach_restore_evidence(result: RestoreResult, policy: RestoreOwnershipPolicy, normalization: dict[str, Any], destructive_state: str, category: str) -> RestoreResult:
    policy_data = policy.to_dict()
    policy_data.update({key: value for key, value in (("source", policy.source), ("default_mapping", policy.default_mapping)) if value})
    result.category, result.policy, result.normalization = category, policy_data, normalization
    result.unsupported_metadata = normalization.get("unsupported_metadata", list(UNSUPPORTED_METADATA))
    result.destructive_state, result.partial = destructive_state, destructive_state in {"partial", "unknown"}
    return result

def _clear_target_contents(target_path: str) -> None:
    target = Path(target_path)
    if str(target.resolve()) == "/":
        raise ValueError("Refusing to restore into filesystem root")
    if target.is_symlink():
        raise ValueError("Refusing to restore through a symlink target")
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

def _sqlite_warnings(target_path: str) -> list[str]:
    warnings = []
    for root, _, files in os.walk(target_path):
        for name in files:
            if name.lower().endswith(SQLITE_SUFFIXES):
                db_path = os.path.join(root, name)
                warnings.append(
                    f"SQLite-like file restored at {db_path}; ensure the DB file and parent directory "
                    "are writable by the runtime UID/GID for journal, WAL, and temp files."
                )
    return warnings

def _should_use_backup_dir_layout(config: RestoreConfig) -> bool:
    layout = (config.layout or "auto").lower()
    if layout == "backup-dir":
        return True
    if layout == "auto":
        return os.path.normpath(config.target_path) == "/backup"
    return False

def _replace_path(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    if source.is_symlink():
        target_is_directory = stat.S_ISDIR(os.lstat(source).st_mode)
        os.symlink(os.readlink(source), destination, target_is_directory=target_is_directory)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)

def _copy_directory_contents(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        _replace_path(item, target_dir / item.name)

def _resolve_backup_root(restore_root: str) -> Path:
    root = Path(restore_root)
    backup_root = root / "backup"
    if backup_root.is_dir():
        return backup_root
    return root


def _path_is_within(path: str | Path, root: str | Path) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(os.fspath(path)))
        normalized_root = os.path.normcase(os.path.abspath(os.fspath(root)))
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except (TypeError, ValueError):
        return False


def _read_only_paths(config: RestoreConfig) -> tuple[str, ...]:
    paths = getattr(config, "read_only_paths", ()) or ()
    return tuple(path for path in paths if isinstance(path, str) and path)


def _read_only_mount_covers_entry(target_entry: Path, read_only_paths: tuple[str, ...]) -> bool:
    return any(
        _path_is_within(target_entry, mount_path) or _path_is_within(mount_path, target_entry)
        for mount_path in read_only_paths
    )


def _ensure_direct_restore_target_is_writable(config: RestoreConfig) -> None:
    if _should_use_backup_dir_layout(config):
        return
    for mount_path in _read_only_paths(config):
        if _path_is_within(config.target_path, mount_path):
            raise ReadOnlyRestoreTargetError(
                f"Direct restore target is read-only: {config.target_path}; refusing to clear or write"
            )

def _restore_backup_dir_layout(restore_root: str, config: RestoreConfig, actions: list[str]) -> None:
    target_root = Path(config.target_path)
    target_root.mkdir(parents=True, exist_ok=True)
    source_root = _resolve_backup_root(restore_root)
    logger.info(f"Resolved backup source root: {source_root}")

    children = list(target_root.iterdir())
    if not children:
        msg = (
            "RESTORE_LAYOUT=backup-dir expects mounted entries inside the target path, "
            "for example /backup/storage_data"
        )
        logger.error(msg)
        raise ValueError(msg)

    actions.append(f"Resolved backup source root: {source_root}")
    logger.info(f"Found {len(children)} target entries to restore: {[c.name for c in children]}")
    restored_entries = []
    missing_entries = []
    read_only_paths = _read_only_paths(config)

    for child in children:
        target_entry = target_root / child.name
        if _read_only_mount_covers_entry(target_entry, read_only_paths):
            message = f"Read-only mount skipped intentionally: {target_entry}"
            actions.append(message)
            logger.warning(message)
            continue
        logger.info(f"Clearing target entry: {target_entry}")
        if target_entry.is_dir() and not target_entry.is_symlink():
            _clear_target_contents(str(target_entry))
        elif target_entry.exists() or target_entry.is_symlink():
            target_entry.unlink()

        source_entry = source_root / child.name
        if not source_entry.exists():
            logger.warning(f"No matching source entry for: {child.name}")
            missing_entries.append(child.name)
            continue

        if source_entry.is_dir() and not source_entry.is_symlink():
            logger.info(f"Copying directory {source_entry} -> {target_entry}")
            _copy_directory_contents(source_entry, target_entry)
        else:
            logger.info(f"Replacing file {source_entry} -> {target_entry}")
            _replace_path(source_entry, target_entry)
        restored_entries.append(child.name)
        logger.info(f"Restored entry: {child.name}")

    if restored_entries:
        actions.append(f"Restore matching entries under {config.target_path}: {', '.join(sorted(restored_entries))}")
        logger.info(f"Restore complete. Restored {len(restored_entries)} entries: {sorted(restored_entries)}")
    if missing_entries:
        actions.append(
            f"Mounted entries without matching backup content were left empty: {', '.join(sorted(missing_entries))}"
        )
        logger.warning(f"Missing entries (no backup content): {sorted(missing_entries)}")


class TarballBackupStrategy(BackupStrategy, RestoreStrategy):
    def perform_backup(self, config: BackupConfig) -> BackupResult:
        timestamp = datetime.now()
        filename = timestamp.strftime(config.backup_filename)
        sources = config.source_paths
        
        logger.info(f"Creating tarball: {filename} from {sources}")
        cmd = ["tar", "-czvf", filename] + sources
        
        try:
            subprocess.run(cmd, check=True)
            
            if config.gpg_passphrase:
                logger.info("Encrypting backup")
                gpg_filename = f"{filename}.gpg"
                gpg_cmd = [
                    "gpg", "--symmetric", "--cipher-algo", "aes256", "--batch", 
                    "--passphrase", config.gpg_passphrase, "-o", gpg_filename, filename
                ]
                subprocess.run(gpg_cmd, check=True)
                os.remove(filename)
                filename = gpg_filename
            
            size = os.path.getsize(filename)
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=size,
                success=True,
                artifact_path=os.path.abspath(filename)
            )
        except Exception as e:
            logger.error(f"Tarball backup failed: {e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=str(e)
            )

    def restore(self, source_path: str, config: RestoreConfig) -> RestoreResult:
        timestamp = datetime.now()
        actions = ["Replace target contents before tarball extraction"]
        archive_path = source_path
        temp_dir = None
        policy = RestoreOwnershipPolicy()
        normalization = {"state": "not_started", "unsupported_metadata": list(UNSUPPORTED_METADATA)}
        destructive_state = "none"
        try:
            _ensure_direct_restore_target_is_writable(config)
            policy = _ownership_policy(config)
            if source_path.endswith(".gpg"):
                passphrase = config.gpg_passphrase or os.environ.get("GPG_PASSPHRASE")
                if not passphrase:
                    raise ValueError("Encrypted tarball restore requires GPG_PASSPHRASE")
                temp_dir = tempfile.mkdtemp(prefix="restore-gpg-")
                archive_path = os.path.join(temp_dir, Path(source_path).stem)
                subprocess.run([
                    "gpg", "--decrypt", "--batch", "--passphrase", passphrase,
                    "-o", archive_path, source_path
                ], check=True)
                actions.append("Decrypt encrypted tarball before extraction")

            if _should_use_backup_dir_layout(config):
                if not temp_dir:
                    temp_dir = tempfile.mkdtemp(prefix="restore-tar-")
                extract_dir = os.path.join(temp_dir, "extract")
                os.makedirs(extract_dir, exist_ok=True)
                subprocess.run(["tar", "-xzpf", archive_path, "-C", extract_dir], check=True)
                actions.append("Extract tarball into temporary workspace for backup-dir restore")
                if Path(config.target_path).exists() and any(Path(config.target_path).iterdir()):
                    destructive_state = "partial"
                _restore_backup_dir_layout(extract_dir, config, actions)
            else:
                destructive_state = "partial"
                _clear_target_contents(config.target_path)
                subprocess.run(["tar", "-xzpf", archive_path, "-C", config.target_path], check=True)
                actions.append("Extract tarball into target path")

            normalization = normalize_ownership(config, policy)
            if normalization["state"] == "preserved":
                actions.append("Preserved archive ownership where supported by tar extraction")
            else:
                actions.append(f"Normalized ownership without following symlinks: {policy.to_dict()}")

            warnings = _sqlite_warnings(config.target_path)
            for warning in warnings:
                logger.warning(warning)

            destructive_state = "complete"
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=True,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                permission_warnings=warnings
            ), policy, normalization, destructive_state, "ok")
        except OwnershipNormalizationError as e:
            normalization = e.report
            logger.error(f"Tarball restore ownership normalization failed: {e}")
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp, duration=0, success=False, target_path=config.target_path,
                dry_run=False, force_overwrite=True, planned_actions=actions, error=str(e)
            ), policy, normalization, destructive_state or "unknown", e.category)
        except Exception as e:
            logger.error(f"Tarball restore failed: {e}")
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=False,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                error=str(e)
            ), policy, normalization, destructive_state, getattr(e, "category", "restore_failed"))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

class ResticBackupStrategy(BackupStrategy, RestoreStrategy):
    def inspect_metadata(self, snapshot: str, config: RestoreConfig, policy: RestoreOwnershipPolicy, scopes=None):
        from src.app.infrastructure.adapters.restic_metadata import ResticMetadataInspectorAdapter

        return ResticMetadataInspectorAdapter().inspect(snapshot, config, scopes, policy)
    @staticmethod
    def _emit_process_line(line: Any, output_stream, callback: Optional[Callable[[str], None]]) -> str:
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line or "")
        if callback is not None:
            try:
                callback(text)
            except Exception:
                logger.debug("Restic output callback failed", exc_info=True)
        try:
            output_stream.write(text)
            output_stream.flush()
        except Exception:
            pass
        return text

    def _run_incremental_backup(
        self,
        command: list[str],
        env: dict[str, str],
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, str, int]:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def consume(stream, lines: list[str], output_stream) -> None:
            if stream is None:
                return
            readline = getattr(stream, "readline", None)
            if callable(readline):
                while True:
                    raw_line = readline()
                    if raw_line in ("", b""):
                        break
                    text = self._emit_process_line(raw_line, output_stream, output_callback)
                    lines.append(text)
                return
            else:
                iterator = iter(stream)
            for raw_line in iterator:
                text = self._emit_process_line(raw_line, output_stream, output_callback)
                lines.append(text)

        stdout_thread = threading.Thread(
            target=consume,
            args=(getattr(process, "stdout", None), stdout_lines, sys.stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=consume,
            args=(getattr(process, "stderr", None), stderr_lines, sys.stderr),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        return_code = process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if not isinstance(return_code, int):
            return_code = getattr(process, "returncode", 1)
        return "".join(stdout_lines), "".join(stderr_lines), int(return_code or 0)

    def perform_backup(
        self,
        config: BackupConfig,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> BackupResult:
        timestamp = datetime.now()
        env = os.environ.copy()
        if config.restic_repository:
            env["RESTIC_REPOSITORY"] = config.restic_repository
        if config.restic_password:
            env["RESTIC_PASSWORD"] = config.restic_password
            
        # Rclone config support via env vars if needed, usually handled by user volume mount or envs
            
        sources = config.source_paths
        logger.info(f"Running restic backup for {sources}")
        
        try:
            # Check if repo is initialized
            init_check_cmd = ["restic", "snapshots", "--json", "--latest", "1"]
            try:
                subprocess.run(init_check_cmd, env=env, capture_output=True, check=True)
            except subprocess.CalledProcessError:
                logger.info("Restic repository not initialized or not accessible. Attempting to initialize...")
                # Try to init
                try:
                    init_cmd = ["restic", "init"]
                    subprocess.run(init_cmd, env=env, check=True)
                    logger.info("Restic repository initialized successfully.")
                except subprocess.CalledProcessError as e:
                    # If init fails, maybe it was a connection error or something else, but we can't proceed
                    logger.error(f"Failed to initialize restic repository: {e}")
                    raise e

            cmd = ["restic", "backup", "--json"] + sources
            stdout, stderr, return_code = self._run_incremental_backup(cmd, env, output_callback)
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd, output=stdout, stderr=stderr)

            # Parse streamed JSON output for the final summary.
            # Restic outputs multiple JSON objects, one per line.
            # The last one usually has summary.
            lines = stdout.strip().split("\n")
            summary = {}
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("message_type") == "summary":
                        summary = data
                except (TypeError, ValueError):
                    pass
            
            size = summary.get("data_added", 0)
            
            # Prune old snapshots
            logger.info("Pruning old snapshots...")
            prune_cmd = [
                "restic", "forget", "--prune",
                "--keep-daily", str(config.restic_keep_daily),
                "--keep-weekly", str(config.restic_keep_weekly),
                "--keep-monthly", str(config.restic_keep_monthly),
                "--keep-yearly", str(config.restic_keep_yearly)
            ]
            try:
                subprocess.run(prune_cmd, env=env, check=True)
                logger.info("Pruning finished successfully.")
            except subprocess.CalledProcessError as e:
                logger.error(f"Pruning failed: {e}")
                # We don't fail the whole backup if prune fails, but we log it.
            
            return BackupResult(
                timestamp=timestamp,
                duration=summary.get("total_duration", 0),
                size=size,
                success=True,
                artifact_path=None
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Restic backup failed: {e.stderr or e.output or e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=f"Restic failed: {e.stderr or e.output or e}"
            )
        except Exception as e:
            logger.error(f"Restic backup error: {e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=str(e)
            )

    def restore(self, source_path: str, config: RestoreConfig) -> RestoreResult:
        timestamp = datetime.now()
        actions = ["Replace target contents before restic restore"]
        env = os.environ.copy()
        temp_dir = None
        policy = RestoreOwnershipPolicy()
        normalization = {"state": "not_started", "unsupported_metadata": list(UNSUPPORTED_METADATA)}
        destructive_state = "none"
        repository = config.restic_repository or os.environ.get("RESTIC_REPOSITORY")
        password = config.restic_password or os.environ.get("RESTIC_PASSWORD")
        if repository:
            env["RESTIC_REPOSITORY"] = repository
        if password:
            env["RESTIC_PASSWORD"] = password

        try:
            _ensure_direct_restore_target_is_writable(config)
            policy = _ownership_policy(config)
            snapshot = source_path or config.source or "latest"
            if _should_use_backup_dir_layout(config):
                temp_dir = tempfile.mkdtemp(prefix="restore-restic-")
                logger.info(f"Restoring restic snapshot {snapshot} into temp dir: {temp_dir}")
                sys.stderr.flush()
                subprocess.run(["restic", "restore", snapshot, "--target", temp_dir], env=env, check=True)
                actions.append(f"Restore restic snapshot into temporary workspace: {snapshot}")
                logger.info("Restic restore complete, copying entries to target volumes")
                sys.stderr.flush()
                if Path(config.target_path).exists() and any(Path(config.target_path).iterdir()):
                    destructive_state = "partial"
                _restore_backup_dir_layout(temp_dir, config, actions)
            else:
                logger.info(f"Clearing target contents at {config.target_path}")
                destructive_state = "partial"
                _clear_target_contents(config.target_path)
                logger.info(f"Restoring restic snapshot {snapshot} directly to {config.target_path}")
                sys.stderr.flush()
                subprocess.run(["restic", "restore", snapshot, "--target", config.target_path], env=env, check=True)
                actions.append(f"Restore restic snapshot: {snapshot}")

            normalization = normalize_ownership(config, policy)
            if normalization["state"] == "preserved":
                actions.append("Preserved snapshot ownership where supported by restic extraction")
            else:
                actions.append(f"Normalized ownership without following symlinks: {policy.to_dict()}")

            warnings = _sqlite_warnings(config.target_path)
            for warning in warnings:
                logger.warning(warning)

            destructive_state = "complete"
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=True,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                permission_warnings=warnings
            ), policy, normalization, destructive_state, "ok")
        except OwnershipNormalizationError as e:
            normalization = e.report
            logger.error(f"Restic restore ownership normalization failed: {e}")
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp, duration=0, success=False, target_path=config.target_path,
                dry_run=False, force_overwrite=True, planned_actions=actions, error=str(e)
            ), policy, normalization, destructive_state or "unknown", e.category)
        except Exception as e:
            logger.error(f"Restic restore failed: {e}")
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=False,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                error=str(e)
            ), policy, normalization, destructive_state, getattr(e, "category", "restore_failed"))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
