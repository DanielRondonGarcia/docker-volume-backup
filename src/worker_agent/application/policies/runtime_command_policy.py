import hashlib
import posixpath
import re
from typing import Any, Dict, List


class RuntimeCommandPolicy:
    """Shared validation for runtime argv and snapshot scope."""

    _SHELL_METACHARACTERS = frozenset(';|&`$><\\"\'\n\r\x00(){}[]*?!')
    _READ_ONLY_OPERATIONS = frozenset({"snapshots", "ls", "cat", "dump", "find", "stats"})
    _WRITE_OPERATIONS = frozenset({"backup", "restore", "forget", "prune"})
    _SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8,64}$", re.IGNORECASE)
    _SAFE_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    _RCLONE_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*:$")

    @classmethod
    def validate(cls, command: Any) -> List[str]:
        """Return a safe argv list or raise a fail-closed validation error."""
        if command is None or command == "":
            argv = ["/root/backup.sh"]
        elif isinstance(command, (list, tuple)) and command and all(isinstance(item, str) for item in command):
            argv = list(command)
        elif isinstance(command, str):
            if any(character in command for character in cls._SHELL_METACHARACTERS):
                raise ValueError("runtime command contains shell metacharacters")
            argv = command.split()
        else:
            raise ValueError("runtime command must be a supported string or argv list")

        if any(not item or any(character in item for character in cls._SHELL_METACHARACTERS) for item in argv):
            raise ValueError("runtime command contains shell metacharacters")

        explicit_no_lock = "--no-lock" in argv
        if explicit_no_lock:
            argv = [item for item in argv if item != "--no-lock"]
            if not cls.is_read_only(argv):
                raise ValueError("runtime write command cannot disable locks")

        if tuple(argv) == ("/root/backup.sh",):
            return argv
        if not argv:
            raise ValueError("unsupported runtime executable")
        if argv[0] == "rclone":
            if len(argv) != 4 or argv[1] != "about" or argv[3] != "--json":
                raise ValueError("unsupported rclone command")
            argv = ["rclone", "about", cls.validated_rclone_remote(argv[2]), "--json"]
            return argv
        if argv[0] != "restic":
            raise ValueError("unsupported runtime executable")

        operation = argv[1] if len(argv) > 1 else ""
        if operation == "snapshots" and argv == ["restic", "snapshots", "--json"]:
            return argv
        if operation == "stats" and argv == ["restic", "stats", "--mode", "raw-data", "--json"]:
            return argv
        if operation == "stats" and argv == ["restic", "stats", "--mode", "blobs-per-file", "--json"]:
            return argv
        if operation == "stats" and len(argv) == 6 and argv[2:5] == ["--mode", "restore-size", "--json"]:
            cls.validate_snapshot_id(argv[5])
            return argv
        if operation == "cat" and len(argv) == 4 and argv[2] == "tree":
            snapshot_id, path = cls.snapshot_tree_arguments(argv[3])
            argv[3] = snapshot_id if path == "/" else f"{snapshot_id}:{path}"
            return argv
        if operation == "ls" and len(argv) in (4, 5) and argv[2] == "--json":
            cls.validate_snapshot_id(argv[3])
            if len(argv) == 5:
                argv[4] = cls.normalize_snapshot_path(argv[4])
            return argv
        if operation == "find" and len(argv) in (4, 5) and argv[2] == "--json":
            cls.validate_snapshot_id(argv[3])
            if len(argv) == 5:
                argv[4] = cls.normalize_snapshot_path(argv[4])
            return argv
        if operation == "dump":
            if len(argv) == 4:
                cls.validate_snapshot_id(argv[2])
                argv[3] = cls.normalize_snapshot_path(argv[3])
                return argv
            if len(argv) == 6 and argv[2] in {"-a", "--archive"} and argv[3] == "zip":
                cls.validate_snapshot_id(argv[4])
                argv[5] = cls.normalize_snapshot_path(argv[5])
                return argv
        if operation == "forget" and len(argv) >= 3:
            index = 2
            while index < len(argv):
                if argv[index] == "--prune":
                    index += 1
                elif (
                    argv[index]
                    in {"--keep-last", "--keep-hourly", "--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-yearly"}
                    and index + 1 < len(argv)
                    and argv[index + 1].isdigit()
                ):
                    index += 2
                else:
                    raise ValueError("unsupported or unbounded restic retention command")
            return argv
        if operation == "prune" and len(argv) == 2:
            return argv
        raise ValueError("unsupported runtime command")

    @classmethod
    def runtime_command_argv(cls, command: Any) -> List[str]:
        return cls.validate(command)

    @classmethod
    def _runtime_command_argv(cls, command: Any) -> List[str]:
        return cls.validate(command)

    @classmethod
    def is_read_only(cls, argv: List[str]) -> bool:
        return bool(len(argv) > 1 and argv[0] == "restic" and argv[1] in cls._READ_ONLY_OPERATIONS)

    @classmethod
    def _is_read_only_argv(cls, argv: List[str]) -> bool:
        return cls.is_read_only(argv)

    @classmethod
    def apply_lock_policy(cls, argv: List[str], no_lock: bool) -> List[str]:
        if not no_lock or not cls.is_read_only(argv) or "--no-lock" in argv:
            return argv
        return [*argv, "--no-lock"]

    @classmethod
    def _apply_lock_policy(cls, argv: List[str], no_lock: bool) -> List[str]:
        return cls.apply_lock_policy(argv, no_lock)

    @classmethod
    def validate_snapshot_id(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls._SNAPSHOT_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid snapshot ID")
        return value

    @classmethod
    def normalize_snapshot_path(cls, value: Any) -> str:
        if value is None or value == "":
            return "/"
        if not isinstance(value, str):
            raise ValueError("snapshot path must be a POSIX string")
        if len(value) > 4096 or "\x00" in value:
            raise ValueError("snapshot path is invalid")
        if "\\" in value or any(ord(character) < 32 for character in value):
            raise ValueError("snapshot path must use POSIX separators")
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("snapshot path must be absolute within the snapshot")
        parts = value.split("/")
        if ".." in parts:
            raise ValueError("snapshot path traversal is not allowed")
        normalized = posixpath.normpath(value)
        if normalized == ".":
            return "/"
        return normalized

    @classmethod
    def snapshot_tree_arguments(cls, value: Any) -> tuple[str, str]:
        if not isinstance(value, str) or not value:
            raise ValueError("invalid snapshot tree target")
        snapshot_id, separator, raw_path = value.partition(":")
        cls.validate_snapshot_id(snapshot_id)
        if not separator:
            return snapshot_id, "/"
        path = cls.normalize_snapshot_path(raw_path)
        if path == "/":
            raise ValueError("snapshot tree target must use the snapshot ID for the root")
        return snapshot_id, path

    @classmethod
    def _snapshot_tree_arguments(cls, value: Any) -> tuple[str, str]:
        return cls.snapshot_tree_arguments(value)

    @staticmethod
    def safe_runtime_token(value: Any, path: Any = False) -> bool:
        if not isinstance(value, str) or not value or ".." in value.split("/"):
            return False
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-"
        return (not path or value.startswith("/")) and all(character in allowed + ("/" if path else "") for character in value)

    @staticmethod
    def _safe_runtime_token(value: Any, path: Any = False) -> bool:
        return RuntimeCommandPolicy.safe_runtime_token(value, path)

    @classmethod
    def validated_rclone_remote(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls._RCLONE_REMOTE_PATTERN.fullmatch(value):
            raise ValueError("invalid rclone remote")
        return value

    @classmethod
    def _validated_rclone_remote(cls, value: Any) -> str:
        return cls.validated_rclone_remote(value)

    @staticmethod
    def repository_fingerprint(repository: Any) -> str:
        if not isinstance(repository, str) or not repository or "\x00" in repository:
            raise ValueError("repository fingerprint requires a non-empty repository")
        return hashlib.sha256(repository.encode("utf-8")).hexdigest()

    @classmethod
    def validate_target_scope(cls, payload: Dict[str, Any]) -> None:
        target_id = payload.get("target_id")
        if target_id is not None and not cls._SAFE_TARGET_PATTERN.fullmatch(str(target_id)):
            raise ValueError("invalid target ID")

        snapshot_target_id = payload.get("snapshot_target_id")
        snapshot_metadata = payload.get("snapshot") or payload.get("snapshot_record")
        if isinstance(snapshot_metadata, dict):
            snapshot_target_id = snapshot_target_id or snapshot_metadata.get("target_id")
        if snapshot_target_id is not None and not cls._SAFE_TARGET_PATTERN.fullmatch(str(snapshot_target_id)):
            raise ValueError("invalid snapshot target ID")
        if target_id is not None and snapshot_target_id is not None and str(target_id) != str(snapshot_target_id):
            raise ValueError("snapshot belongs to a different target")

    @classmethod
    def _validate_target_scope(cls, payload: Dict[str, Any]) -> None:
        cls.validate_target_scope(payload)

    @classmethod
    def snapshot_arguments(cls, argv: List[str]) -> tuple[str | None, str | None]:
        argv = [item for item in argv if item != "--no-lock"]
        if len(argv) < 2 or argv[0] != "restic":
            return None, None
        operation_index = 3 if len(argv) >= 4 and argv[1] == "--cache-dir" else 1
        if len(argv) <= operation_index:
            return None, None
        operation = argv[operation_index]
        if operation in {"ls", "find"}:
            if len(argv) not in {operation_index + 3, operation_index + 4}:
                return None, None
            return argv[operation_index + 2], argv[operation_index + 3] if len(argv) == operation_index + 4 else "/"
        if operation == "cat" and len(argv) == operation_index + 3 and argv[operation_index + 1] == "tree":
            return cls.snapshot_tree_arguments(argv[operation_index + 2])
        if operation == "dump":
            if len(argv) == operation_index + 3:
                return argv[operation_index + 1], argv[operation_index + 2]
            if len(argv) == operation_index + 5:
                return argv[operation_index + 3], argv[operation_index + 4]
        if (
            operation == "stats"
            and len(argv) == operation_index + 5
            and argv[operation_index + 1 : operation_index + 4] == ["--mode", "restore-size", "--json"]
        ):
            return argv[operation_index + 4], "/"
        return None, None

    @classmethod
    def _snapshot_arguments(cls, argv: List[str]) -> tuple[str | None, str | None]:
        return cls.snapshot_arguments(argv)

    @classmethod
    def validate_snapshot_scope(cls, payload: Dict[str, Any], argv: List[str]) -> None:
        cls.validate_target_scope(payload)
        snapshot_id, path = cls.snapshot_arguments(argv)
        if snapshot_id is None:
            return
        cls.validate_snapshot_id(snapshot_id)
        normalized_path = cls.normalize_snapshot_path(path)
        requested_snapshot_id = payload.get("snapshot_id")
        if requested_snapshot_id is not None and str(requested_snapshot_id) != snapshot_id:
            raise ValueError("snapshot ID does not match the request")
        requested_path = payload.get("path")
        if requested_path is not None and cls.normalize_snapshot_path(requested_path) != normalized_path:
            raise ValueError("snapshot path does not match the request")

    @classmethod
    def _validate_snapshot_scope(cls, payload: Dict[str, Any], argv: List[str]) -> None:
        cls.validate_snapshot_scope(payload, argv)
