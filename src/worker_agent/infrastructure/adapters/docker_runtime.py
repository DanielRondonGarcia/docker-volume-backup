import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import logging

from src.worker_agent.application.policies.runtime_command_policy import RuntimeCommandPolicy
from src.worker_agent.application.ports.runtime_port import RuntimePort

try:
    import docker
except ModuleNotFoundError:
    docker = None

logger = logging.getLogger(__name__)


class DockerRuntimeAdapter(RuntimePort):
    runtime_kind = "docker"
    DEFAULT_RUNTIME_TIMEOUT_SECONDS = 1800.0
    DEFAULT_RESTORE_RUNTIME_TIMEOUT_SECONDS = 6 * 60 * 60
    MAX_RUNTIME_TIMEOUT_SECONDS = 24 * 60 * 60
    DEFAULT_CACHE_DIR = "/var/cache/restic-explorer"
    CACHE_MOUNT_PATH = "/root/.cache/restic"
    RUNTIME_TEMPORARY_LABEL = "docker-volume-backup.runtime.temporary"
    RUNTIME_JOB_ID_LABEL = "docker-volume-backup.runtime.job_id"
    RUNTIME_TEMPORARY_VALUE = "true"
    CLEANUP_REMOVE_ATTEMPTS = 2
    MAX_CLEANUP_REPORT_IDS = 20
    MAX_LOG_BYTES = 4 * 1024 * 1024
    MAX_SNAPSHOT_METADATA_LOG_BYTES = 16 * 1024 * 1024
    MAX_DUMP_BYTES = 16 * 1024 * 1024
    MAX_ZIP_BYTES = 64 * 1024 * 1024
    MAX_SNAPSHOT_ENTRIES = 10_000
    MAX_SNAPSHOT_STATS_VALUE = (1 << 63) - 1
    MAX_TARGET_STATS_FIELDS = 64
    MAX_RECOVERY_LOG_BYTES = 512 * 1024
    MAX_SNAPSHOT_PATH_LENGTH = 4096
    RESTORE_RESULT_MOUNT_PATH = "/run/restore-result"
    RESTORE_RESULT_FILENAME = "restore-result.json"
    MAX_RESTORE_RESULT_BYTES = 64 * 1024
    _SHELL_METACHARACTERS = frozenset(";|&`$><\\\"'\n\r\x00(){}[]*?!")
    _SECRET_ENV_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY", "ACCESS_KEY", "CREDENTIAL")
    _READ_ONLY_OPERATIONS = frozenset({"snapshots", "ls", "cat", "dump", "find", "stats"})
    _WRITE_OPERATIONS = frozenset({"backup", "restore", "forget", "prune"})
    _SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8,64}$", re.IGNORECASE)
    _STATS_FIELD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    _TARGET_STATS_MODES = frozenset({"raw-data", "blobs-per-file"})
    _SAFE_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    _RCLONE_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*:$")
    _REPOSITORY_URL_PATTERN = re.compile(r"(?i)\b(?:https?|s3|gs|az|swift)://[^\s\"'<>]+")
    _RCLONE_REPOSITORY_PATTERN = re.compile(r"(?i)\brclone:[^\s\"'<>]+")
    _LOCAL_REPOSITORY_PATTERN = re.compile(r"(?i)\blocal:[^\s\"'<>]+")
    _SAFE_LABEL_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    _RECOVERY_SECRET_PATTERN = re.compile(
        r"(?i)\b(password|passphrase|secret|token|private[_-]?key|access[_-]?key|credential)\b"
        r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    )

    def __init__(self, timeout_seconds: float | None = None):
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else os.environ.get(
            "WORKER_RUNTIME_TIMEOUT_SECONDS", str(self.DEFAULT_RUNTIME_TIMEOUT_SECONDS)
        )
        self.restore_timeout_seconds = os.environ.get(
            "WORKER_RESTORE_RUNTIME_TIMEOUT_SECONDS", str(self.DEFAULT_RESTORE_RUNTIME_TIMEOUT_SECONDS)
        )
        self.no_lock = self._env_flag("SNAPSHOT_EXPLORER_NO_LOCK")
        self.cache_dir = os.environ.get("SNAPSHOT_EXPLORER_CACHE_DIR") or None
        if docker is None:
            self.client = None
            return
        try:
            self.client = docker.from_env()
        except Exception:
            self.client = None

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _timeout_seconds(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("runtime timeout_seconds must be a finite positive number")
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            raise ValueError("runtime timeout_seconds must be a finite positive number") from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > cls.MAX_RUNTIME_TIMEOUT_SECONDS:
            raise ValueError("runtime timeout_seconds is outside the permitted bounds")
        return timeout

    @classmethod
    def _is_restore_payload(cls, payload: Dict[str, Any], command: List[str]) -> bool:
        environment = payload.get("environment") if isinstance(payload, dict) else None
        restore_mode = environment.get("RESTORE_MODE") if isinstance(environment, dict) else None
        if isinstance(restore_mode, str) and restore_mode.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        operation_index = 3 if len(command) >= 4 and command[:2] == ["restic", "--cache-dir"] else 1
        return len(command) > operation_index and command[operation_index] == "restore"

    def _default_runtime_timeout(self, payload: Dict[str, Any], command: List[str]) -> Any:
        if self._is_restore_payload(payload, command):
            return getattr(self, "restore_timeout_seconds", self.DEFAULT_RESTORE_RUNTIME_TIMEOUT_SECONDS)
        return getattr(self, "timeout_seconds", self.DEFAULT_RUNTIME_TIMEOUT_SECONDS)

    @classmethod
    def _runtime_command_argv(cls, command: Any) -> List[str]:
        return RuntimeCommandPolicy.validate(command)

    @classmethod
    def _is_read_only_argv(cls, argv: List[str]) -> bool:
        return RuntimeCommandPolicy.is_read_only(argv)

    @classmethod
    def _apply_lock_policy(cls, argv: List[str], no_lock: bool) -> List[str]:
        return RuntimeCommandPolicy.apply_lock_policy(argv, no_lock)

    @classmethod
    def validate_snapshot_id(cls, value: Any) -> str:
        return RuntimeCommandPolicy.validate_snapshot_id(value)

    @classmethod
    def normalize_snapshot_path(cls, value: Any) -> str:
        return RuntimeCommandPolicy.normalize_snapshot_path(value)

    @classmethod
    def _snapshot_tree_arguments(cls, value: Any) -> tuple[str, str]:
        return RuntimeCommandPolicy.snapshot_tree_arguments(value)

    @staticmethod
    def _safe_runtime_token(value: Any, path: Any = False) -> bool:
        return RuntimeCommandPolicy.safe_runtime_token(value, path)

    @classmethod
    def _validated_rclone_remote(cls, value: Any) -> str:
        return RuntimeCommandPolicy.validated_rclone_remote(value)

    @classmethod
    def repository_fingerprint(cls, repository: Any) -> str:
        return RuntimeCommandPolicy.repository_fingerprint(repository)

    @classmethod
    def _validate_target_scope(cls, payload: Dict[str, Any]) -> None:
        RuntimeCommandPolicy.validate_target_scope(payload)

    @classmethod
    def _snapshot_arguments(cls, argv: List[str]) -> tuple[str | None, str | None]:
        return RuntimeCommandPolicy.snapshot_arguments(argv)

    @classmethod
    def _validate_snapshot_scope(cls, payload: Dict[str, Any], argv: List[str]) -> None:
        RuntimeCommandPolicy.validate_snapshot_scope(payload, argv)

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
        env_secrets = {value for key, value in environment.items() if isinstance(value, str) and value and (key == "RCLONE_CONF_CONTENT" or key == "RCLONE_REMOTE" or any(marker in str(key).upper() for marker in cls._SECRET_ENV_MARKERS))}
        file_secrets = {item["content"] for item in payload.get("resolved_files") or [] if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"]}
        repository = environment.get("RESTIC_REPOSITORY")
        repository_values = {repository} if isinstance(repository, str) and repository else set()
        nested_secrets: set[str] = set()

        def collect_nested(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    collect_nested(nested_value, str(nested_key))
            elif isinstance(value, (list, tuple)):
                for nested_value in value:
                    collect_nested(nested_value, key)
            elif isinstance(value, str) and value and any(marker in key.upper() for marker in cls._SECRET_ENV_MARKERS + ("PLAINTEXT", "PAYLOAD")):
                nested_secrets.add(value)

        collect_nested(payload)
        return env_secrets | file_secrets | repository_values | nested_secrets

    @staticmethod
    def _redact_text(value: Any, secrets: set[str]) -> str:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        text = DockerRuntimeAdapter._REPOSITORY_URL_PATTERN.sub("<redacted-repository>", text)
        text = DockerRuntimeAdapter._RCLONE_REPOSITORY_PATTERN.sub("<redacted-repository>", text)
        text = DockerRuntimeAdapter._LOCAL_REPOSITORY_PATTERN.sub("<redacted-repository>", text)
        return text

    @classmethod
    def _safe_label_value(cls, value: Any, default: str = "unknown") -> str:
        if isinstance(value, str) and cls._SAFE_LABEL_VALUE_PATTERN.fullmatch(value):
            return value
        return default

    @classmethod
    def _runtime_job_id(cls, payload: Dict[str, Any]) -> str:
        value = payload.get("_job_id")
        if value is None:
            value = payload.get("job_id")
        return cls._safe_label_value(value)

    @classmethod
    def _runtime_container_labels(cls, payload: Dict[str, Any]) -> Any:
        temporary_labels = {
            cls.RUNTIME_TEMPORARY_LABEL: cls.RUNTIME_TEMPORARY_VALUE,
            cls.RUNTIME_JOB_ID_LABEL: cls._runtime_job_id(payload),
        }
        user_labels = payload.get("labels")
        if isinstance(user_labels, dict):
            labels = dict(user_labels)
            labels.update(temporary_labels)
            return labels
        if isinstance(user_labels, (list, tuple)):
            reserved = set(temporary_labels)
            labels = [
                item
                for item in user_labels
                if not isinstance(item, str) or item.split("=", 1)[0] not in reserved
            ]
            labels.extend(f"{key}={value}" for key, value in temporary_labels.items())
            return labels
        return temporary_labels

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
    def _container_labels(container: Any) -> Dict[str, Any]:
        try:
            labels = getattr(container, "labels", None)
        except Exception:
            labels = None
        if isinstance(labels, dict):
            return labels
        try:
            attrs = getattr(container, "attrs", None)
        except Exception:
            attrs = None
        if isinstance(attrs, dict):
            config = attrs.get("Config")
            if isinstance(config, dict) and isinstance(config.get("Labels"), dict):
                return config["Labels"]
        return {}

    @staticmethod
    def _container_status(container: Any) -> str | None:
        try:
            status = getattr(container, "status", None)
        except Exception:
            status = None
        if isinstance(status, str):
            return status.casefold()
        try:
            attrs = getattr(container, "attrs", None)
        except Exception:
            attrs = None
        if isinstance(attrs, dict):
            state = attrs.get("State")
            if isinstance(state, dict) and isinstance(state.get("Status"), str):
                return state["Status"].casefold()
        return None

    @classmethod
    def _redact_recovery_text(cls, value: Any) -> str:
        text = cls._redact_text(value, set())

        def replace(match: re.Match[str]) -> str:
            value = match.group(3)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = f"{value[0]}<redacted>{value[-1]}"
            else:
                value = "<redacted>"
            return f"{match.group(1)}{match.group(2)}{value}"

        return cls._RECOVERY_SECRET_PATTERN.sub(replace, text)

    def inspect_runtime_container(self, container: Any) -> Dict[str, Any]:
        """Read exit evidence before an orphaned container is removed."""
        result: Dict[str, Any] = {
            "container_id": self._container_identifier(container),
            "status": self._container_status(container),
        }
        try:
            reload_container = getattr(container, "reload", None)
            if callable(reload_container):
                reload_container()
            result["status"] = self._container_status(container)
        except Exception:
            result["inspect_error"] = "container refresh failed"
            return result

        if result["status"] not in {"exited", "dead"}:
            return result

        state = None
        try:
            attrs = getattr(container, "attrs", None)
            if isinstance(attrs, dict) and isinstance(attrs.get("State"), dict):
                state = attrs["State"]
        except Exception:
            state = None
        status_code = state.get("ExitCode") if isinstance(state, dict) else None
        if not isinstance(status_code, int) or isinstance(status_code, bool):
            try:
                try:
                    waited = container.wait(timeout=1)
                except TypeError:
                    waited = container.wait()
                status_code = waited.get("StatusCode") if isinstance(waited, dict) else None
            except Exception:
                result["inspect_error"] = "container exit status unavailable"
                return result
        if not isinstance(status_code, int) or isinstance(status_code, bool) or not -1 <= status_code <= 255:
            result["inspect_error"] = "container exit status unavailable"
            return result
        result["status_code"] = status_code

        try:
            try:
                raw_logs = container.logs(stdout=True, stderr=True, timestamps=False)
            except TypeError:
                raw_logs = container.logs()
            bounded_logs, exceeded = self._bounded_bytes(raw_logs, self.MAX_RECOVERY_LOG_BYTES)
        except Exception:
            result["inspect_error"] = "container logs unavailable"
            return result
        result["logs"] = self._redact_recovery_text(bounded_logs)
        result["logs_truncated"] = exceeded
        return result

    @classmethod
    def _container_identifier(cls, container: Any) -> str:
        try:
            value = getattr(container, "id", None)
        except Exception:
            value = None
        return cls._safe_label_value(value)

    @staticmethod
    def _is_container_gone_error(error: Exception) -> bool:
        error_name = error.__class__.__name__.casefold()
        message = str(error).casefold()
        return "notfound" in error_name or "no such container" in message or "not found" in message

    def _cleanup_container(
        self,
        container: Any,
        already_exited: bool | None = None,
        job_id: Any = None,
    ) -> Dict[str, Any]:
        container_id = self._container_identifier(container)
        labels = self._container_labels(container)
        cleanup_job_id = self._safe_label_value(
            job_id if job_id is not None else labels.get(self.RUNTIME_JOB_ID_LABEL)
        )
        status = self._container_status(container)
        exited = already_exited is True or status in {"exited", "dead"}
        stop_attempted = False
        if not exited:
            stop_attempted = True
            try:
                container.stop(timeout=1)
            except Exception as exc:
                logger.debug(
                    "Runtime container stop did not complete (container_id=%s job_id=%s error_type=%s)",
                    container_id,
                    cleanup_job_id,
                    exc.__class__.__name__,
                )

        remove_attempts = 0
        last_error = None
        for attempt in range(1, self.CLEANUP_REMOVE_ATTEMPTS + 1):
            remove_attempts = attempt
            try:
                container.remove(force=True)
                return {
                    "removed": True,
                    "container_id": container_id,
                    "job_id": cleanup_job_id,
                    "stop_attempted": stop_attempted,
                    "remove_attempts": remove_attempts,
                    "fallback_used": False,
                }
            except Exception as exc:
                if self._is_container_gone_error(exc):
                    return {
                        "removed": True,
                        "container_id": container_id,
                        "job_id": cleanup_job_id,
                        "stop_attempted": stop_attempted,
                        "remove_attempts": remove_attempts,
                        "fallback_used": False,
                    }
                last_error = exc
                logger.debug(
                    "Runtime container remove attempt failed (container_id=%s job_id=%s attempt=%d error_type=%s)",
                    container_id,
                    cleanup_job_id,
                    attempt,
                    exc.__class__.__name__,
                )

        fallback_used = False
        api = getattr(getattr(self, "client", None), "api", None)
        remove_container = getattr(api, "remove_container", None)
        if callable(remove_container) and container_id != "unknown":
            fallback_used = True
            try:
                remove_container(container_id, force=True)
                return {
                    "removed": True,
                    "container_id": container_id,
                    "job_id": cleanup_job_id,
                    "stop_attempted": stop_attempted,
                    "remove_attempts": remove_attempts,
                    "fallback_used": True,
                }
            except Exception as exc:
                if self._is_container_gone_error(exc):
                    return {
                        "removed": True,
                        "container_id": container_id,
                        "job_id": cleanup_job_id,
                        "stop_attempted": stop_attempted,
                        "remove_attempts": remove_attempts,
                        "fallback_used": True,
                    }
                last_error = exc

        logger.warning(
            "Runtime container cleanup failed (container_id=%s job_id=%s remove_attempts=%d fallback=%s error_type=%s)",
            container_id,
            cleanup_job_id,
            remove_attempts,
            fallback_used,
            last_error.__class__.__name__ if last_error is not None else "unknown",
        )
        return {
            "removed": False,
            "container_id": container_id,
            "job_id": cleanup_job_id,
            "stop_attempted": stop_attempted,
            "remove_attempts": remove_attempts,
            "fallback_used": fallback_used,
        }

    def cleanup_orphaned_runtime_jobs(self, recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None) -> Dict[str, Any]:
        return self.cleanup_orphaned_runtime_containers(recover_callback=recover_callback)

    def cleanup_orphaned_runtime_containers(self, recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "inspected": 0,
            "removed": 0,
            "failed": 0,
            "skipped": 0,
            "retained": 0,
            "removed_ids": [],
            "failed_ids": [],
            "retained_ids": [],
        }
        if self.client is None:
            summary["error"] = "docker unavailable"
            logger.warning("Runtime orphan sweep unavailable: Docker client is not configured")
            return summary

        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"{self.RUNTIME_TEMPORARY_LABEL}={self.RUNTIME_TEMPORARY_VALUE}"},
            )
            for container in containers or []:
                summary["inspected"] += 1
                labels = self._container_labels(container)
                if labels.get(self.RUNTIME_TEMPORARY_LABEL) != self.RUNTIME_TEMPORARY_VALUE:
                    summary["skipped"] += 1
                    continue
                if self._container_status(container) not in {"exited", "dead"}:
                    summary["skipped"] += 1
                    continue
                job_id = labels.get(self.RUNTIME_JOB_ID_LABEL)
                if recover_callback is not None:
                    inspection = self.inspect_runtime_container(container)
                    if inspection.get("inspect_error"):
                        action = "retain"
                    else:
                        try:
                            action = recover_callback(job_id, inspection)
                        except Exception as exc:
                            logger.warning(
                                "Runtime orphan recovery failed (container_id=%s error_type=%s)",
                                self._container_identifier(container),
                                exc.__class__.__name__,
                            )
                            action = "retain"
                    if action != "remove":
                        summary["retained"] += 1
                        if len(summary["retained_ids"]) < self.MAX_CLEANUP_REPORT_IDS:
                            summary["retained_ids"].append(self._container_identifier(container))
                        continue
                result = self._cleanup_container(container, already_exited=True, job_id=job_id)
                container_id = result["container_id"]
                if result["removed"]:
                    summary["removed"] += 1
                    if len(summary["removed_ids"]) < self.MAX_CLEANUP_REPORT_IDS:
                        summary["removed_ids"].append(container_id)
                else:
                    summary["failed"] += 1
                    if len(summary["failed_ids"]) < self.MAX_CLEANUP_REPORT_IDS:
                        summary["failed_ids"].append(container_id)
        except Exception as exc:
            summary["error"] = "sweep failed"
            logger.warning("Runtime orphan sweep failed (error_type=%s)", exc.__class__.__name__)

        logger.info(
            "Runtime orphan sweep inspected=%d removed=%d failed=%d skipped=%d",
            summary["inspected"],
            summary["removed"],
            summary["failed"],
            summary["skipped"],
        )
        return summary

    @staticmethod
    def _is_timeout_error(error: Exception) -> bool:
        return isinstance(error, TimeoutError) or error.__class__.__name__.lower() in {"readtimeout", "connecttimeout"} or "timed out" in str(error).lower()

    def _failure_result(self, message: str, binary: bool, secrets: set[str], status_code: int = 1) -> Dict[str, Any]:
        message = self._redact_text(message, secrets)
        return {
            "success": False,
            "status_code": status_code,
            "error": message,
            "canceled": status_code == 130,
            **({"stdout_bytes": b"", "stderr": message} if binary else {"logs": message, "stderr": ""}),
        }

    @classmethod
    def _bounded_limit(cls, value: Any, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            raise ValueError("runtime output limit must be a positive integer")
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError("runtime output limit must be a positive integer") from None
        if limit <= 0 or limit > maximum:
            raise ValueError("runtime output limit is outside the permitted bounds")
        return limit

    @classmethod
    def _is_snapshot_metadata_command(cls, command: List[str]) -> bool:
        argv = [item for item in command if item != "--no-lock"]
        if len(argv) < 3 or argv[0] != "restic":
            return False
        operation_index = 3 if len(argv) >= 4 and argv[1] == "--cache-dir" else 1
        if len(argv) <= operation_index:
            return False
        operation = argv[operation_index]
        if operation == "snapshots":
            return (
                argv[operation_index : operation_index + 2] == ["snapshots", "--json"]
                and len(argv) == operation_index + 2
            )
        if operation == "cat":
            return (
                argv[operation_index : operation_index + 2] == ["cat", "tree"]
                and len(argv) == operation_index + 3
            )
        if operation == "stats":
            return (
                argv[operation_index : operation_index + 4] == ["stats", "--mode", "restore-size", "--json"]
                and len(argv) == operation_index + 5
            )
        return (
            operation in {"ls", "find"}
            and len(argv) in {operation_index + 3, operation_index + 4}
            and argv[operation_index + 1] == "--json"
        )

    @classmethod
    def _runtime_output_limit(cls, payload: Dict[str, Any], command: List[str]) -> int:
        maximum = (
            cls.MAX_SNAPSHOT_METADATA_LOG_BYTES
            if cls._is_snapshot_metadata_command(command)
            else cls.MAX_LOG_BYTES
        )
        return cls._bounded_limit(payload.get("max_log_bytes"), cls.MAX_LOG_BYTES, maximum)

    @staticmethod
    def _bounded_bytes(value: Any, limit: int) -> tuple[bytes, bool]:
        raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8", errors="replace")
        return raw[:limit], len(raw) > limit

    @classmethod
    def _restore_result_unavailable(cls, detail: str) -> Dict[str, Any]:
        detail = cls._redact_text(detail, set())[:512]
        return {"schema_version": 1, "status": "failed", "category": "result_unavailable", "error": detail, "detail": detail, "partial": True, "destructive_state": "unknown"}

    @classmethod
    def _read_restore_result(cls, path: str, secrets: set[str]) -> tuple[Dict[str, Any], Optional[str]]:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(cls.MAX_RESTORE_RESULT_BYTES + 1)
        except OSError:
            return cls._restore_result_unavailable("restore result is missing"), "restore result is missing"
        if len(raw) > cls.MAX_RESTORE_RESULT_BYTES:
            return cls._restore_result_unavailable("restore result exceeded the permitted limit"), "restore result exceeded the permitted limit"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return cls._restore_result_unavailable("restore result is malformed"), "restore result is malformed"
        sections = ("policy", "metadata", "capability", "normalization", "restart")
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("status") not in {"succeeded", "failed", "blocked"} or ("partial" in value and not isinstance(value["partial"], bool)) or ("destructive_state" in value and value["destructive_state"] not in {"none", "partial", "complete", "unknown"}) or any(key in value and not isinstance(value[key], dict) for key in sections):
            return cls._restore_result_unavailable("restore result is malformed"), "restore result is malformed"
        allowed = {key: value[key] for key in ("schema_version", "status", "category", "error", "detail", "partial", "destructive_state", *sections) if key in value}
        for key in ("category", "error", "detail"):
            if isinstance(allowed.get(key), str): allowed[key] = cls._redact_text(allowed[key], secrets)[:2048]
        return allowed, None

    @staticmethod
    def _capture_binary_output(container: Any) -> tuple[Any, Any]:
        attached = container.attach(
            stdout=True,
            stderr=True,
            stream=False,
            logs=True,
            demux=True,
        )
        if isinstance(attached, tuple) and len(attached) == 2:
            return attached
        return attached, b""

    @classmethod
    def _safe_cache_component(cls, value: Any, label: str) -> str:
        if not isinstance(value, str) or not cls._SAFE_TARGET_PATTERN.fullmatch(value):
            raise ValueError(f"invalid {label}")
        return value

    def _cache_mount(self, payload: Dict[str, Any], environment: Dict[str, Any]) -> str | None:
        cache_root = payload.get("cache_dir") or getattr(self, "cache_dir", None) or os.environ.get("SNAPSHOT_EXPLORER_CACHE_DIR")
        target_id = payload.get("target_id")
        repository = environment.get("RESTIC_REPOSITORY")
        if not cache_root or not target_id or not repository:
            return None
        if not isinstance(cache_root, str) or "\x00" in cache_root:
            raise ValueError("cache directory is invalid")
        target_component = self._safe_cache_component(target_id, "target ID")
        fingerprint = self.repository_fingerprint(repository)
        cache_path = os.path.join(cache_root, target_component, fingerprint)
        os.makedirs(cache_path, mode=0o700, exist_ok=True)
        try:
            os.chmod(cache_path, 0o700)
        except OSError:
            pass
        environment["RESTIC_CACHE_DIR"] = self.CACHE_MOUNT_PATH
        return cache_path

    @staticmethod
    def _callback_is_true(callback: Callable[[], bool] | None) -> bool:
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _wait_for_container(
        self,
        container: Any,
        timeout: float,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[Dict[str, Any] | None, bool]:
        if cancel_check is None:
            return container.wait(timeout=timeout), False

        deadline = time.monotonic() + timeout
        while True:
            if self._callback_is_true(cancel_check):
                return None, True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("container wait timed out")
            try:
                result = container.wait(timeout=min(0.5, remaining))
            except Exception as exc:
                if self._is_timeout_error(exc):
                    continue
                raise
            return result, False

    @staticmethod
    def _iter_output_chunks(stream: Any):
        if isinstance(stream, (bytes, str)):
            yield stream
            return
        try:
            iterator = iter(stream)
        except TypeError:
            if stream is not None:
                yield stream
            return
        for chunk in iterator:
            yield chunk

    def _start_log_stream(
        self,
        container: Any,
        secrets: set[str],
        limit: int,
        output_callback: Callable[[str], None] | None,
    ) -> tuple[Dict[str, Any], threading.Thread]:
        state: Dict[str, Any] = {
            "started": False,
            "exceeded": False,
            "error": None,
            "logs": "",
            "stream": None,
        }

        def consume() -> None:
            pending = ""
            safe_parts: List[str] = []
            raw_size = 0
            max_secret_tail = max((len(secret) - 1 for secret in secrets if secret), default=0)

            def safe_cutoff(value: str) -> int:
                hold = 0
                for secret in secrets:
                    if not secret:
                        continue
                    maximum_overlap = min(len(secret) - 1, max_secret_tail, len(value))
                    for overlap in range(maximum_overlap, 0, -1):
                        if value.endswith(secret[:overlap]):
                            hold = max(hold, overlap)
                            break
                cutoff = len(value) - hold

                # A complete secret may overlap the suffix held for another
                # secret. Keep it whole so _redact_text can remove it atomically.
                while True:
                    crossing_end = cutoff
                    for secret in secrets:
                        if not secret:
                            continue
                        start = value.find(secret)
                        while start >= 0:
                            end = start + len(secret)
                            if start < cutoff < end:
                                crossing_end = max(crossing_end, end)
                            start = value.find(secret, start + 1)
                    if crossing_end == cutoff:
                        return max(0, cutoff)
                    cutoff = crossing_end

            try:
                stream = container.logs(stdout=True, stderr=True, timestamps=False, stream=True, follow=True)
                state["stream"] = stream
                state["started"] = True
                for chunk in self._iter_output_chunks(stream):
                    raw = chunk if isinstance(chunk, bytes) else str(chunk or "").encode("utf-8", errors="replace")
                    if raw_size >= limit:
                        state["exceeded"] = True
                        break
                    remaining = limit - raw_size
                    bounded = raw[:remaining]
                    raw_size += len(bounded)
                    text = bounded.decode("utf-8", errors="replace")
                    pending += text
                    cutoff = safe_cutoff(pending)
                    if cutoff:
                        emit = pending[:cutoff]
                        pending = pending[cutoff:]
                        safe = self._redact_text(emit, secrets)
                        safe_parts.append(safe)
                        if output_callback and safe:
                            try:
                                output_callback(safe)
                            except Exception:
                                logger.debug("Runtime output callback failed", exc_info=True)
                    if len(bounded) < len(raw):
                        state["exceeded"] = True
                        break
                safe = self._redact_text(pending, secrets)
                safe_parts.append(safe)
                if output_callback and safe:
                    try:
                        output_callback(safe)
                    except Exception:
                        logger.debug("Runtime output callback failed", exc_info=True)
                state["logs"] = "".join(safe_parts)
            except Exception as exc:
                state["error"] = exc
                state["logs"] = "".join(safe_parts) + self._redact_text(pending, secrets)

        thread = threading.Thread(target=consume, daemon=True, name="runtime-log-stream")
        thread.start()
        return state, thread

    @staticmethod
    def _finish_log_stream(state: Optional[Dict[str, Any]], thread: Optional[threading.Thread]) -> None:
        if thread is None:
            return
        thread.join(timeout=2)
        if thread.is_alive():
            stream = state.get("stream") if isinstance(state, dict) else None
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            thread.join(timeout=0.5)

    def _fallback_container_logs(
        self,
        container: Any,
        secrets: set[str],
        limit: int,
        output_callback: Callable[[str], None] | None,
    ) -> tuple[str, bool]:
        try:
            raw_logs = container.logs(stdout=True, stderr=True, timestamps=False)
            bounded, exceeded = self._bounded_bytes(raw_logs, limit)
            combined = self._redact_text(bounded, secrets)
            if output_callback and combined:
                try:
                    output_callback(combined)
                except Exception:
                    logger.debug("Runtime output callback failed", exc_info=True)
            return combined, exceeded
        except Exception:
            return "", False

    def _prepare_runtime(self, payload: Dict[str, Any], binary: bool):
        environment = dict(payload.get("environment") or {})
        volumes = self._validate_runtime_volumes(payload.get("volumes") or {})
        command = RuntimeCommandPolicy.validate(payload.get("command"))
        RuntimeCommandPolicy.validate_snapshot_scope(payload, command)
        command = RuntimeCommandPolicy.apply_lock_policy(command, bool(getattr(self, "no_lock", False)))
        timeout_value = payload["timeout_seconds"] if "timeout_seconds" in payload else self._default_runtime_timeout(payload, command)
        timeout = self._timeout_seconds(timeout_value)
        network_mode = payload.get("network_mode") or ("none" if binary else None)
        if network_mode not in {None, "none", "bridge"}:
            raise ValueError("unsupported runtime network mode")
        cache_path = self._cache_mount(payload, environment)
        if cache_path:
            volumes[cache_path] = {"bind": self.CACHE_MOUNT_PATH, "mode": "rw"}
            if command and command[0] == "restic":
                command = ["restic", "--cache-dir", self.CACHE_MOUNT_PATH, *command[1:]]
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

    @staticmethod
    def _is_missing_image_error(error: Exception) -> bool:
        docker_errors = getattr(docker, "errors", None) if docker is not None else None
        not_found = getattr(docker_errors, "NotFound", None)
        if not_found is not None and isinstance(error, not_found):
            return True
        detail = str(error).lower()
        return "no such image" in detail or ("404" in detail and "image" in detail)

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
        volume_candidate_maps: Dict[str, Dict[tuple[str, str], Dict[str, Any]]] = {}
        volume_metadata = {
            volume.name: {
                "labels": volume.attrs.get("Labels") or {},
            }
            for volume in volumes
        }
        container_items = []

        for container in containers:
            labels = container.labels
            mounts = container.attrs.get("Mounts", [])
            compose_project = labels.get("com.docker.compose.project")
            compose_service = labels.get("com.docker.compose.service")
            try:
                image_tags = container.image.tags
            except Exception as exc:
                if not self._is_missing_image_error(exc):
                    raise
                image_tags = []
            container_item = {
                "id": container.id,
                "name": container.name,
                "image": image_tags,
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
                    "volume_candidates": [],
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
                candidate_key = (runtime_volume["source"], bind_path)
                candidate_map = volume_candidate_maps.setdefault(compose_project, {})
                candidate = candidate_map.get(candidate_key)
                if candidate is None:
                    source_metadata = volume_metadata.get(runtime_volume["source"], {})
                    candidate = self._mount_to_volume_candidate(
                        runtime_volume,
                        source_metadata.get("labels") or {},
                        compose_project,
                    )
                    candidate_map[candidate_key] = candidate
                    project_item["volume_candidates"].append(candidate)
                self._append_unique(candidate["services"], compose_service)
                self._append_unique(candidate["containers"], container.name)

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

    @staticmethod
    def _append_unique(items: List[str], value: Any) -> None:
        if isinstance(value, str) and value and value not in items:
            items.append(value)

    @staticmethod
    def _volume_name(source: str) -> str:
        normalized = (source or "").rstrip("/")
        if normalized.endswith("/_data"):
            normalized = normalized[: -len("/_data")]
        return normalized.rsplit("/", 1)[-1]

    @classmethod
    def _is_generated_volume_name(cls, source: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{32,64}", cls._volume_name(source), re.IGNORECASE))

    @classmethod
    def _mount_to_volume_candidate(
        cls,
        runtime_volume: Dict[str, Any],
        volume_labels: Dict[str, Any],
        compose_project: str,
    ) -> Dict[str, Any]:
        mount = runtime_volume["mount"]
        mount_type = mount["type"]
        source = runtime_volume["source"]
        compose_volume = volume_labels.get("com.docker.compose.volume")
        compose_volume_project = volume_labels.get("com.docker.compose.project")
        has_compose_identity = bool(
            compose_volume
            and (not compose_volume_project or compose_volume_project == compose_project)
        )
        anonymous = mount_type == "volume" and (
            bool(runtime_volume.get("anonymous"))
            or cls._is_generated_volume_name(source)
            or (not has_compose_identity and not mount.get("name"))
        )
        display_name = compose_volume if isinstance(compose_volume, str) and compose_volume else source
        return {
            "source": source,
            "name": display_name,
            "compose_volume": compose_volume,
            "bind": runtime_volume["bind_path"],
            "mode": runtime_volume["mode"],
            "mount_type": mount_type,
            "type": mount_type,
            "anonymous": anonymous,
            "services": [],
            "containers": [],
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
            "anonymous": mount.get("Anonymous") in (True, "true", "True"),
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

    def run_runtime_job(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        if self.client is None:
            return {"success": False, "error": "docker unavailable"}
        secrets = self._collect_secret_values(payload)
        temp_dirs = None
        container = None
        container_exited = False
        stream_state = None
        stream_thread = None
        restore_result_path = None
        try:
            environment, volumes, command, network_mode, timeout, temp_dirs = self._prepare_runtime(payload, binary=False)
            if payload.get("_restore_result_transport") and self._is_restore_payload(payload, command):
                result_dir = tempfile.mkdtemp(prefix="worker-restore-result-", dir=tempfile.gettempdir())
                os.chmod(result_dir, 0o700); temp_dirs.append(result_dir)
                restore_result_path = os.path.join(result_dir, self.RESTORE_RESULT_FILENAME); environment["RESTORE_RESULT_FILE"] = f"{self.RESTORE_RESULT_MOUNT_PATH}/{self.RESTORE_RESULT_FILENAME}"; volumes[result_dir] = {"bind": self.RESTORE_RESULT_MOUNT_PATH, "mode": "rw"}
            output_limit = self._runtime_output_limit(payload, command)
            if self._callback_is_true(cancel_check):
                return self._failure_result("runtime canceled before launch", False, secrets, 130)
            self._pull_image(image)
            container = self.client.containers.run(
                image=image,
                command=command,
                environment=environment,
                volumes=volumes,
                network_mode=network_mode,
                detach=True,
                remove=False,
                labels=self._runtime_container_labels(payload),
            )
            stream_state, stream_thread = self._start_log_stream(
                container,
                secrets,
                output_limit,
                output_callback,
            )
            try:
                result, canceled = self._wait_for_container(container, timeout, cancel_check)
            except Exception as exc:
                if not self._is_timeout_error(exc):
                    raise
                self._finish_log_stream(stream_state, stream_thread)
                return self._failure_result(f"runtime timed out after {timeout:g} seconds", False, secrets, 124)
            container_exited = not canceled
            if canceled:
                self._finish_log_stream(stream_state, stream_thread)
                return self._failure_result("runtime canceled", False, secrets, 130)
            self._finish_log_stream(stream_state, stream_thread)
            if stream_state and stream_state.get("started") and not stream_state.get("error"):
                combined = stream_state.get("logs", "")
                exceeded = bool(stream_state.get("exceeded"))
            else:
                combined, exceeded = self._fallback_container_logs(
                    container,
                    secrets,
                    output_limit,
                    output_callback,
                )
            restore_evidence, restore_error = self._read_restore_result(restore_result_path, secrets) if restore_result_path else (None, None)
            if exceeded:
                failure = self._failure_result("runtime logs exceeded the permitted limit", False, secrets, 413)
                if restore_evidence: failure["restore_ownership"] = restore_evidence
                return failure
            status_code = result.get("StatusCode", 1)
            if restore_error:
                failure = self._failure_result(restore_error, False, secrets, status_code or 1)
                failure["restore_ownership"] = restore_evidence
                return failure
            success = status_code == 0 and (not restore_evidence or restore_evidence.get("status") == "succeeded")
            output = {"success": success, "status_code": status_code, "logs": combined, "stderr": ""}
            if restore_evidence:
                output["restore_ownership"] = restore_evidence
                if not success: output["error"] = restore_evidence.get("error") or f"runtime exited with status code {status_code}"
            return output
        except Exception as exc:
            return self._failure_result(f"runtime execution failed: {exc}", False, secrets)
        finally:
            self._finish_log_stream(stream_state, stream_thread)
            if container is not None:
                self._cleanup_container(
                    container,
                    already_exited=container_exited,
                    job_id=self._runtime_job_id(payload),
                )
            self._cleanup_temp_dirs(temp_dirs)

    def list_restic_snapshots(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        kwargs = {}
        if cancel_check:
            kwargs["cancel_check"] = cancel_check
        if output_callback:
            kwargs["output_callback"] = output_callback
        summary = self.run_runtime_job(image=image, payload=payload, **kwargs)
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
        max_entries = self._bounded_limit(payload.get("max_entries"), self.MAX_SNAPSHOT_ENTRIES, self.MAX_SNAPSHOT_ENTRIES)
        if len(snapshots) > max_entries:
            summary["success"] = False
            summary["error"] = "snapshot listing exceeded the permitted entry limit"
            snapshots = []
        summary["snapshots"] = snapshots[:max_entries]
        return summary

    def get_restic_stats(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        requested_modes = payload.get("stats_modes", ["raw-data"])
        if (
            not isinstance(requested_modes, (list, tuple))
            or not requested_modes
            or len(requested_modes) > len(self._TARGET_STATS_MODES)
            or any(not isinstance(mode, str) or mode not in self._TARGET_STATS_MODES for mode in requested_modes)
            or len(set(requested_modes)) != len(requested_modes)
        ):
            return {
                "success": False,
                "status_code": 1,
                "error": "unsupported restic stats mode",
                "logs": "",
                "stderr": "",
                "stats": {},
                "stats_by_mode": {},
            }

        summaries = []
        stats_by_mode: Dict[str, Dict[str, int | float]] = {}
        for mode in requested_modes:
            mode_payload = dict(payload)
            mode_payload["command"] = ["restic", "stats", "--mode", mode, "--json"]
            kwargs = {}
            if cancel_check:
                kwargs["cancel_check"] = cancel_check
            if output_callback:
                kwargs["output_callback"] = output_callback
            summary = self.run_runtime_job(image=image, payload=mode_payload, **kwargs)
            summaries.append(summary)
            if not summary.get("success"):
                return {
                    **summary,
                    "logs": "\n".join(str(item.get("logs", "") or "") for item in summaries if item.get("logs")),
                    "stderr": "\n".join(str(item.get("stderr", "") or "") for item in summaries if item.get("stderr")),
                    "stats": {},
                    "stats_by_mode": {},
                    "failed_mode": mode,
                }
            try:
                parsed = json.loads(summary.get("logs", "") or "{}")
                stats_by_mode[mode] = self.project_restic_stats(parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                return {
                    **summary,
                    "success": False,
                    "error": "failed to parse restic stats JSON",
                    "stats": {},
                    "stats_by_mode": {},
                    "failed_mode": mode,
                }

        logs = "\n".join(str(item.get("logs", "") or "") for item in summaries if item.get("logs"))
        stderr = "\n".join(str(item.get("stderr", "") or "") for item in summaries if item.get("stderr"))
        legacy_stats = stats_by_mode.get("raw-data") or next(iter(stats_by_mode.values()), {})
        return {
            "success": True,
            "status_code": summaries[-1].get("status_code", 0),
            "logs": logs,
            "stderr": stderr,
            "stats": legacy_stats,
            "stats_by_mode": stats_by_mode,
        }

    @classmethod
    def project_restic_stats(cls, value: Any) -> Dict[str, int | float]:
        if not isinstance(value, dict) or not value or len(value) > cls.MAX_TARGET_STATS_FIELDS:
            raise ValueError("restic stats JSON must be a bounded non-empty object")
        projected: Dict[str, int | float] = {}
        for field, raw in value.items():
            if not isinstance(field, str) or not cls._STATS_FIELD_PATTERN.fullmatch(field):
                raise ValueError("restic stats JSON contains an invalid field")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                raise ValueError("restic stats JSON contains invalid numeric fields")
            if raw < 0 or raw > cls.MAX_SNAPSHOT_STATS_VALUE:
                raise ValueError("restic stats JSON contains out-of-range numeric fields")
            projected[field] = raw
        return projected

    @classmethod
    def project_snapshot_stats(cls, value: Any) -> Dict[str, int]:
        if not isinstance(value, dict):
            raise ValueError("snapshot stats JSON must be an object")
        projected: Dict[str, int] = {}
        for field in ("total_size", "total_file_count", "snapshots_count"):
            raw = value.get(field)
            if (
                isinstance(raw, bool)
                or not isinstance(raw, int)
                or raw < 0
                or raw > cls.MAX_SNAPSHOT_STATS_VALUE
            ):
                raise ValueError("snapshot stats JSON contains invalid numeric fields")
            projected[field] = raw
        return projected

    def get_restic_snapshot_stats(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        output_callback: Callable[[str], None] | None = None,
    ) -> Dict[str, Any]:
        kwargs = {}
        if cancel_check:
            kwargs["cancel_check"] = cancel_check
        if output_callback:
            kwargs["output_callback"] = output_callback
        summary = self.run_runtime_job(image=image, payload=payload, **kwargs)
        stats: Dict[str, int] = {}
        if summary.get("success"):
            try:
                parsed = json.loads(summary.get("logs", "") or "{}")
                stats = self.project_snapshot_stats(parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                summary["success"] = False
                summary["error"] = "failed to parse restic snapshot stats JSON"
        summary["stats"] = stats
        return summary

    def run_runtime_job_binary(
        self,
        image: str,
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
    ) -> Dict[str, Any]:
        if self.client is None:
            return {"success": False, "error": "docker unavailable"}
        secrets = self._collect_secret_values(payload)
        temp_dirs = None
        container = None
        container_exited = False
        try:
            environment, volumes, command, network_mode, timeout, temp_dirs = self._prepare_runtime(payload, binary=True)
            if self._callback_is_true(cancel_check):
                return self._failure_result("runtime canceled before launch", True, secrets, 130)
            self._pull_image(image)
            container = self.client.containers.run(
                image=image,
                command=command,
                environment=environment,
                volumes=volumes,
                network_mode=network_mode,
                detach=True,
                remove=False,
                labels=self._runtime_container_labels(payload),
            )
            try:
                result, canceled = self._wait_for_container(container, timeout, cancel_check)
            except Exception as exc:
                if not self._is_timeout_error(exc):
                    raise
                return self._failure_result(f"runtime timed out after {timeout:g} seconds", True, secrets, 124)
            container_exited = not canceled
            if canceled:
                return self._failure_result("runtime canceled", True, secrets, 130)
            operation_index = 3 if len(command) > 2 and command[1] == "--cache-dir" else 1
            is_zip = len(command) > operation_index and command[operation_index] == "dump" and "zip" in command
            default_limit = self.MAX_ZIP_BYTES if is_zip else self.MAX_DUMP_BYTES
            output_limit = self._bounded_limit(payload.get("max_output_bytes"), default_limit, default_limit)
            attached_stdout, attached_stderr = self._capture_binary_output(container)
            raw_stdout, stdout_exceeded = self._bounded_bytes(attached_stdout, output_limit)
            raw_stderr, stderr_exceeded = self._bounded_bytes(
                attached_stderr,
                self._bounded_limit(payload.get("max_log_bytes"), self.MAX_LOG_BYTES, self.MAX_LOG_BYTES),
            )
            if stdout_exceeded:
                return self._failure_result("runtime dump exceeded the permitted limit", True, secrets, 413)
            if stderr_exceeded:
                return self._failure_result("runtime logs exceeded the permitted limit", True, secrets, 413)
            stdout_bytes = raw_stdout
            stderr_text = self._redact_text(raw_stderr, secrets)
            status_code = result.get("StatusCode", 1)
            return {"success": status_code == 0, "status_code": status_code, "stdout_bytes": stdout_bytes, "stderr": stderr_text}
        except Exception as exc:
            return self._failure_result(f"runtime execution failed: {exc}", True, secrets)
        finally:
            if container is not None:
                self._cleanup_container(
                    container,
                    already_exited=container_exited,
                    job_id=self._runtime_job_id(payload),
                )
            self._cleanup_temp_dirs(temp_dirs)
