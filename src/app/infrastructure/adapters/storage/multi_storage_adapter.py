import os
import shutil
import subprocess
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from src.app.domain.models import BackupConfig, RestoreCandidate, RestoreConfig
from src.app.application.ports.ports import StoragePort

logger = logging.getLogger(__name__)

class StorageUploadError(RuntimeError):
    def __init__(self, failures: list[str], successes: list[str]):
        self.failures = failures
        self.successes = successes
        details = "; ".join(failures)
        completed = ", ".join(successes) or "none"
        super().__init__(
            f"Upload incomplete; successful destinations: {completed}; failed destinations: {details}"
        )

class MultiStorageAdapter(StoragePort):
    def upload(self, file_path: str, config: BackupConfig) -> None:
        destinations = []
        if config.aws_s3_bucket:
            destinations.append((f"s3://{config.aws_s3_bucket}", lambda: self._upload_s3(file_path, config.aws_s3_bucket)))
        if config.aws_glacier_vault:
            destinations.append((f"glacier://{config.aws_glacier_vault}", lambda: self._upload_glacier(file_path, config.aws_glacier_vault)))
        if config.scp_host:
            destinations.append((f"scp://{config.scp_host}", lambda: self._upload_scp(file_path, config)))
        if config.rclone_remote:
            destinations.append((f"rclone://{config.rclone_remote}", lambda: self._upload_rclone(file_path, config.rclone_remote)))
        if config.local_archive_path:
            destinations.append((f"local:{config.local_archive_path}", lambda: self._archive_local(file_path, config.local_archive_path)))

        failures = []
        successes = []
        for destination, upload in destinations:
            try:
                upload()
                successes.append(destination)
            except Exception as e:
                logger.error("Upload to %s failed: %s", destination, e)
                failures.append(f"{destination}: {e}")

        if failures or not destinations:
            if not destinations:
                failures.append("no configured upload destination")
            raise StorageUploadError(failures, successes)

    def cleanup(self, file_path: str) -> None:
        if os.path.exists(file_path):
            logger.info(f"Cleaning up {file_path}")
            os.remove(file_path)

    def list_restore_candidates(self, config: RestoreConfig) -> list[RestoreCandidate]:
        candidates = []
        if config.backup_strategy == "restic":
            repository = config.restic_repository or os.environ.get("RESTIC_REPOSITORY")
            if repository:
                candidates.extend(self._list_restic_candidates(config, repository))

        local_archive_path = config.local_archive_path or os.environ.get("BACKUP_ARCHIVE")
        if local_archive_path:
            candidates.extend(self._list_local_candidates(local_archive_path, config.backup_strategy))

        bucket = config.aws_s3_bucket or os.environ.get("AWS_S3_BUCKET_NAME")
        if bucket:
            candidates.extend(self._list_s3_candidates(bucket, config.backup_strategy))

        if config.aws_glacier_vault or os.environ.get("AWS_GLACIER_VAULT_NAME"):
            candidates.append(RestoreCandidate(
                source="glacier",
                strategy=config.backup_strategy,
                available=False,
                unavailable_reason="Glacier archives require delayed retrieval before restore"
            ))

        scp_host = config.scp_host or os.environ.get("SCP_HOST")
        if scp_host:
            candidates.extend(self._list_scp_candidates(config, scp_host))

        rclone_remote = config.rclone_remote or os.environ.get("RCLONE_REMOTE")
        if rclone_remote:
            candidates.extend(self._list_rclone_candidates(rclone_remote, config.backup_strategy))

        return candidates

    def download_restore_candidate(self, candidate: RestoreCandidate, config: RestoreConfig) -> str:
        source = candidate.source
        if source.startswith("s3://"):
            return self._download_to_temp(["aws", "s3", "cp", "--only-show-errors", source])
        if source.startswith("scp://"):
            return self._download_scp(source, config)
        if source.startswith("rclone://"):
            remote_path = source.removeprefix("rclone://")
            return self._download_to_temp(["rclone", "copyto", remote_path])
        if config.backup_strategy == "restic":
            return source or "latest"
        return source

    def _list_local_candidates(self, archive_path: str, strategy: str) -> list[RestoreCandidate]:
        path = Path(archive_path)
        if path.is_file():
            return [RestoreCandidate(str(path), strategy, datetime.fromtimestamp(path.stat().st_mtime), path.stat().st_size)]
        if not path.is_dir():
            return []
        candidates = []
        for item in path.iterdir():
            if item.is_file():
                candidates.append(RestoreCandidate(
                    source=str(item),
                    strategy=strategy,
                    created_at=datetime.fromtimestamp(item.stat().st_mtime),
                    size=item.stat().st_size
                ))
        return candidates

    def _list_restic_candidates(self, config: RestoreConfig, repository: str) -> list[RestoreCandidate]:
        env = os.environ.copy()
        env["RESTIC_REPOSITORY"] = repository
        if config.restic_password or os.environ.get("RESTIC_PASSWORD"):
            env["RESTIC_PASSWORD"] = config.restic_password or os.environ.get("RESTIC_PASSWORD")

        try:
            result = subprocess.run(
                ["restic", "snapshots", "--json"],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            snapshots = __import__("json").loads(result.stdout or "[]")
            candidates = []
            for snapshot in snapshots:
                snapshot_id = snapshot.get("short_id") or snapshot.get("id")
                if not snapshot_id:
                    continue
                created_at = snapshot.get("time")
                candidates.append(RestoreCandidate(
                    source=snapshot_id,
                    strategy="restic",
                    created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")) if created_at else None,
                ))
            return candidates
        except Exception as e:
            logger.error(f"Restic restore listing failed: {e}")
            return []

    def _list_s3_candidates(self, bucket: str, strategy: str) -> list[RestoreCandidate]:
        try:
            result = subprocess.run(["aws", "s3", "ls", f"s3://{bucket}/"], capture_output=True, text=True, check=True)
            candidates = []
            for line in result.stdout.splitlines():
                parts = line.split(maxsplit=3)
                if len(parts) == 4 and parts[2].isdigit():
                    created_at = datetime.fromisoformat(f"{parts[0]}T{parts[1]}")
                    candidates.append(RestoreCandidate(f"s3://{bucket}/{parts[3]}", strategy, created_at, int(parts[2])))
            return candidates
        except Exception as e:
            logger.error(f"S3 restore listing failed: {e}")
            return []

    def _list_scp_candidates(self, config: RestoreConfig, host: str) -> list[RestoreCandidate]:
        try:
            user = config.scp_user or os.environ.get("SCP_USER") or "root"
            remote_dir = config.scp_directory or os.environ.get("SCP_DIRECTORY") or "/tmp"
            result = subprocess.run([
                "ssh", "-o", "StrictHostKeyChecking=no", "-i", "/ssh/id_rsa",
                f"{user}@{host}", f"find {remote_dir} -maxdepth 1 -type f -printf '%T@ %s %f\\n'"
            ], capture_output=True, text=True, check=True)
            candidates = []
            for line in result.stdout.splitlines():
                parts = line.split(maxsplit=2)
                if len(parts) == 3:
                    candidates.append(RestoreCandidate(
                        f"scp://{user}@{host}:{remote_dir.rstrip('/')}/{parts[2]}",
                        config.backup_strategy,
                        datetime.fromtimestamp(float(parts[0])),
                        int(parts[1])
                    ))
            return candidates
        except Exception as e:
            logger.error(f"SCP restore listing failed: {e}")
            return []

    def _list_rclone_candidates(self, remote: str, strategy: str) -> list[RestoreCandidate]:
        try:
            result = subprocess.run(["rclone", "lsjson", remote], capture_output=True, text=True, check=True)
            items = __import__("json").loads(result.stdout or "[]")
            return [
                RestoreCandidate(
                    source=f"rclone://{remote.rstrip('/')}/{item['Path']}",
                    strategy=strategy,
                    created_at=datetime.fromisoformat(item["ModTime"].replace("Z", "+00:00")) if item.get("ModTime") else None,
                    size=item.get("Size")
                )
                for item in items if not item.get("IsDir")
            ]
        except Exception as e:
            logger.error(f"Rclone restore listing failed: {e}")
            return []

    def _download_scp(self, source: str, config: RestoreConfig) -> str:
        remote = source.removeprefix("scp://")
        return self._download_to_temp(["scp", "-o", "StrictHostKeyChecking=no", "-i", "/ssh/id_rsa", remote])

    def _download_to_temp(self, command: list[str]) -> str:
        suffix = Path(command[-1]).suffix
        descriptor, destination = tempfile.mkstemp(prefix="restore-download-", suffix=suffix)
        os.close(descriptor)
        try:
            subprocess.run(command + [destination], check=True)
            return destination
        except Exception:
            try:
                os.remove(destination)
            except OSError as cleanup_error:
                logger.error("Failed to clean restore download %s: %s", destination, cleanup_error)
            raise

    def _upload_s3(self, file_path: str, bucket: str):
        logger.info(f"Uploading to S3 bucket: {bucket}")
        cmd = ["aws", "s3", "cp", "--only-show-errors", file_path, f"s3://{bucket}/"]
        subprocess.run(cmd, check=True)

    def _upload_glacier(self, file_path: str, vault: str):
        logger.info(f"Uploading to Glacier vault: {vault}")
        cmd = ["aws", "glacier", "upload-archive", "--account-id", "-", "--vault-name", vault, "--body", file_path]
        subprocess.run(cmd, check=True)

    def _upload_scp(self, file_path: str, config: BackupConfig):
        logger.info(f"Uploading via SCP to {config.scp_host}")
        ssh_key = "/ssh/id_rsa"
        user = config.scp_user or "root"
        host = config.scp_host
        remote_dir = config.scp_directory or "/tmp"
        cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-i", ssh_key, file_path, f"{user}@{host}:{remote_dir}"]
        subprocess.run(cmd, check=True)

    def _upload_rclone(self, file_path: str, remote: str):
        logger.info(f"Uploading via Rclone to {remote}")
        # Assume config.rclone_remote includes a destination such as "remote:/backups".
        cmd = ["rclone", "copy", file_path, remote]
        subprocess.run(cmd, check=True)

    def _archive_local(self, file_path: str, archive_path: str):
        logger.info(f"Archiving locally to {archive_path}")
        shutil.copy2(file_path, archive_path)
