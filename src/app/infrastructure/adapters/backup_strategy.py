import subprocess
import os
import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from src.app.domain.models import BackupConfig, BackupResult, RestoreConfig, RestoreResult
from src.app.application.ports.ports import BackupStrategy, RestoreStrategy

logger = logging.getLogger(__name__)

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")

def _clear_target_contents(target_path: str) -> None:
    target = Path(target_path)
    if str(target.resolve()) == "/":
        raise ValueError("Refusing to restore into filesystem root")
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

def _apply_chown(target_path: str, chown: str) -> None:
    uid_text, gid_text = chown.split(":", 1)
    uid, gid = int(uid_text), int(gid_text)
    for root, dirs, files in os.walk(target_path):
        os.chown(root, uid, gid)
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid)

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
        os.symlink(os.readlink(source), destination, target_is_directory=source.is_dir())
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

def _restore_backup_dir_layout(restore_root: str, config: RestoreConfig, actions: list[str]) -> None:
    target_root = Path(config.target_path)
    target_root.mkdir(parents=True, exist_ok=True)
    source_root = _resolve_backup_root(restore_root)

    children = list(target_root.iterdir())
    if not children:
        raise ValueError(
            "RESTORE_LAYOUT=backup-dir expects mounted entries inside the target path, "
            "for example /backup/storage_data"
        )

    actions.append(f"Resolved backup source root: {source_root}")
    restored_entries = []
    missing_entries = []

    for child in children:
        target_entry = target_root / child.name
        if target_entry.is_dir() and not target_entry.is_symlink():
            _clear_target_contents(str(target_entry))
        elif target_entry.exists() or target_entry.is_symlink():
            target_entry.unlink()

        source_entry = source_root / child.name
        if not source_entry.exists():
            missing_entries.append(child.name)
            continue

        if source_entry.is_dir() and not source_entry.is_symlink():
            _copy_directory_contents(source_entry, target_entry)
        else:
            _replace_path(source_entry, target_entry)
        restored_entries.append(child.name)

    if restored_entries:
        actions.append(f"Restore matching entries under {config.target_path}: {', '.join(sorted(restored_entries))}")
    if missing_entries:
        actions.append(
            f"Mounted entries without matching backup content were left empty: {', '.join(sorted(missing_entries))}"
        )

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
        try:
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
                _restore_backup_dir_layout(extract_dir, config, actions)
            else:
                _clear_target_contents(config.target_path)
                subprocess.run(["tar", "-xzpf", archive_path, "-C", config.target_path], check=True)
                actions.append("Extract tarball into target path")

            if config.chown:
                _apply_chown(config.target_path, config.chown)
                actions.append(f"Apply RESTORE_CHOWN recursively after extraction: {config.chown}")
            else:
                actions.append("Preserved archive ownership where supported by tar extraction")

            warnings = _sqlite_warnings(config.target_path)
            for warning in warnings:
                logger.warning(warning)

            return RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=True,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                permission_warnings=warnings
            )
        except Exception as e:
            logger.error(f"Tarball restore failed: {e}")
            return RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=False,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                error=str(e)
            )
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

class ResticBackupStrategy(BackupStrategy, RestoreStrategy):
    def perform_backup(self, config: BackupConfig) -> BackupResult:
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
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
            
            # Parse last line of JSON output for summary
            # Restic outputs multiple JSON objects, one per line.
            # The last one usually has summary.
            lines = result.stdout.strip().split('\n')
            summary = {}
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("message_type") == "summary":
                        summary = data
                except:
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
            logger.error(f"Restic backup failed: {e.stderr}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=f"Restic failed: {e.stderr}"
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
        repository = config.restic_repository or os.environ.get("RESTIC_REPOSITORY")
        password = config.restic_password or os.environ.get("RESTIC_PASSWORD")
        if repository:
            env["RESTIC_REPOSITORY"] = repository
        if password:
            env["RESTIC_PASSWORD"] = password

        try:
            snapshot = source_path or config.source or "latest"
            if _should_use_backup_dir_layout(config):
                temp_dir = tempfile.mkdtemp(prefix="restore-restic-")
                subprocess.run(["restic", "restore", snapshot, "--target", temp_dir], env=env, check=True)
                actions.append(f"Restore restic snapshot into temporary workspace: {snapshot}")
                _restore_backup_dir_layout(temp_dir, config, actions)
            else:
                _clear_target_contents(config.target_path)
                subprocess.run(["restic", "restore", snapshot, "--target", config.target_path], env=env, check=True)
                actions.append(f"Restore restic snapshot: {snapshot}")

            if config.chown:
                _apply_chown(config.target_path, config.chown)
                actions.append(f"Apply RESTORE_CHOWN recursively after restic restore: {config.chown}")

            warnings = _sqlite_warnings(config.target_path)
            for warning in warnings:
                logger.warning(warning)

            return RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=True,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                permission_warnings=warnings
            )
        except Exception as e:
            logger.error(f"Restic restore failed: {e}")
            return RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=False,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=True,
                planned_actions=actions,
                error=str(e)
            )
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
