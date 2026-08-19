import argparse
import errno
import hashlib
import hmac
import json
import os
import stat as stat_module
import sys
import time


PROTECTED_VOLUME_EXIT_CODE = 13
MAX_ENTRIES = 1000
MAX_WATCH_ENTRIES = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024


class ProtectedVolumeError(PermissionError):
    pass


def _is_permission_error(exc):
    return isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)


def _raise_if_protected(exc):
    if _is_permission_error(exc):
        raise ProtectedVolumeError("live target is protected") from exc


def _directory_stat(path):
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        _raise_if_protected(exc)
        raise
    if not stat_module.S_ISDIR(metadata.st_mode):
        raise ValueError("live path is not a directory")
    return metadata


def virtual_parts(path):
    if path in ("", "/"):
        return []
    if not isinstance(path, str) or not path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("live path must be virtual and POSIX-style")
    parts = path.split("/")[1:]
    if any(not part or part in (".", "..") for part in parts):
        raise ValueError("live path traversal is not allowed")
    return parts


def confined_path(root, path):
    raw_root = os.fspath(root)
    if os.path.islink(raw_root):
        raise ValueError("live target root cannot be a link")
    current = root_path = os.path.realpath(raw_root)
    for part in virtual_parts(path):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ValueError("live links are not allowed")
    candidate = os.path.realpath(current)
    if os.path.commonpath((root_path, candidate)) != root_path:
        raise ValueError("live path escapes target root")
    return candidate


def open_confined(root, path, flags=os.O_RDONLY):
    parts = virtual_parts(path)
    nofollow, directory = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
    try:
        if os.open in getattr(os, "supports_dir_fd", ()):
            fd = os.open(os.fspath(root), os.O_RDONLY | directory | nofollow)
            try:
                for index, part in enumerate(parts):
                    child = os.open(
                        part,
                        (os.O_RDONLY | directory if index < len(parts) - 1 else flags) | nofollow,
                        dir_fd=fd,
                    )
                    os.close(fd)
                    fd = child
                return fd
            except Exception:
                os.close(fd)
                raise
        return os.open(confined_path(root, path), flags | nofollow)
    except OSError as exc:
        _raise_if_protected(exc)
        raise


def list_entries(root, path="/", limit=100, cursor=None):
    limit = min(int(limit), MAX_ENTRIES)
    if limit <= 0:
        raise ValueError("live entry limit must be positive")
    directory = confined_path(root, path)
    _directory_stat(directory)
    entries, after = [], cursor or ""
    try:
        with os.scandir(directory) as scan:
            for scanned, entry in enumerate(scan):
                if scanned >= limit + 1:
                    break
                if entry.name <= after or entry.is_symlink():
                    continue
                stat, is_dir = entry.stat(follow_symlinks=False), entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue
                relative = "/" + "/".join([part for part in (path.strip("/"), entry.name) if part])
                entries.append(
                    {
                        "name": entry.name,
                        "path": relative,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                )
                if len(entries) >= limit:
                    break
    except OSError as exc:
        _raise_if_protected(exc)
        raise
    return {"entries": entries, "next_cursor": entries[-1]["name"] if len(entries) == limit else None}


def watch_snapshot(root, max_entries=MAX_WATCH_ENTRIES):
    raw_root = os.fspath(root)
    if os.path.islink(raw_root) or os.path.realpath(raw_root) != raw_root:
        raise ValueError("live watcher root is unavailable")
    _directory_stat(raw_root)
    entries, pending, scanned = {}, [("", raw_root)], 0
    while pending:
        relative, directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if scanned >= max_entries:
                        return entries, False
                    scanned += 1
                    if entry.is_symlink():
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not is_dir and not is_file:
                        continue
                    path = "/" + "/".join(part for part in (relative, entry.name) if part)
                    entries[path] = (
                        "dir" if is_dir else "file",
                        None if is_dir else stat.st_size,
                        None if is_dir else stat.st_mtime_ns,
                    )
                    if is_dir:
                        pending.append((path, entry.path))
        except OSError as exc:
            if not relative:
                _raise_if_protected(exc)
                raise ValueError("live watcher root is unavailable") from exc
    return entries, True


def read_file(root, path, offset=0, max_bytes=MAX_FILE_BYTES, max_chunk_bytes=MAX_CHUNK_BYTES):
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("live file offset must be non-negative")
    limit = min(MAX_FILE_BYTES, int(max_bytes))
    if limit < 0:
        raise ValueError("live file size bound is invalid")
    fd = open_confined(root, path)
    try:
        size = os.fstat(fd).st_size
        if size - offset > limit:
            raise ValueError("live file exceeds the permitted bound")
        os.lseek(fd, offset, os.SEEK_SET)
        remaining = max(0, size - offset)
        while remaining:
            chunk = os.read(fd, min(max_chunk_bytes, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    except OSError as exc:
        _raise_if_protected(exc)
        raise
    finally:
        os.close(fd)


def sign_request(secret, payload):
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(
        key,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_request(secret, payload, signature):
    return hmac.compare_digest(sign_request(secret, payload), signature or "")


def _parser():
    parser = argparse.ArgumentParser(description="Read-only live-file helper")
    parser.add_argument("--root", required=True)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--max-chunk-bytes", type=int, default=MAX_CHUNK_BYTES)
    commands = parser.add_subparsers(dest="operation", required=True)

    list_parser = commands.add_parser("list")
    list_parser.add_argument("--path", default="/")
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.add_argument("--cursor", default="")

    snapshot_parser = commands.add_parser("snapshot")
    snapshot_parser.add_argument("--max-entries", type=int, default=MAX_WATCH_ENTRIES)

    read_parser = commands.add_parser("read")
    read_parser.add_argument("--path", required=True)
    read_parser.add_argument("--offset", type=int, default=0)
    read_parser.add_argument("--max-bytes", type=int, default=MAX_FILE_BYTES)

    commands.add_parser("serve")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.read_only:
        parser.error("--read-only is required")
    if (
        not isinstance(args.root, str)
        or not os.path.isabs(args.root)
        or os.path.islink(args.root)
        or not os.path.isdir(args.root)
    ):
        parser.error("root must be an existing absolute directory")
    if args.max_chunk_bytes <= 0 or args.max_chunk_bytes > MAX_CHUNK_BYTES:
        parser.error("max chunk size is outside the permitted bounds")
    try:
        if args.operation == "serve":
            while True:
                time.sleep(3600)
        elif args.operation == "list":
            print(json.dumps(list_entries(args.root, args.path, args.limit, args.cursor), separators=(",", ":")))
        elif args.operation == "snapshot":
            if args.max_entries <= 0 or args.max_entries > MAX_WATCH_ENTRIES:
                raise ValueError("watch entry limit is outside the permitted bounds")
            entries, complete = watch_snapshot(args.root, args.max_entries)
            print(json.dumps({"entries": entries, "complete": complete}, separators=(",", ":")))
        else:
            for chunk in read_file(
                args.root,
                args.path,
                args.offset,
                args.max_bytes,
                args.max_chunk_bytes,
            ):
                sys.stdout.buffer.write(chunk)
    except ProtectedVolumeError:
        print("live helper request failed", file=sys.stderr)
        return PROTECTED_VOLUME_EXIT_CODE
    except PermissionError:
        print("live helper request failed", file=sys.stderr)
        return PROTECTED_VOLUME_EXIT_CODE
    except Exception:
        print("live helper request failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
