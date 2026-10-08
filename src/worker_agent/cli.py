"""Small stdlib-only CLI wrapper for native worker operations."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import socket
import sys
import uuid
from pathlib import Path
from typing import Sequence

from src.app import main as app_main
from src.security.hmac_protocol import digest_secret
from src.worker_agent import main as worker_main
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


def _env_present(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _file_exists_from_env(name: str, default: str | None = None) -> bool:
    raw = os.environ.get(name)
    value = raw if raw is not None else default
    if not value or not value.strip():
        return False
    return Path(value).exists()


def _configuration_presence() -> dict[str, bool]:
    """Return redacted configuration presence without exposing values or paths."""

    return {
        "control_plane_url_configured": _env_present("CONTROL_PLANE_URL"),
        "worker_name_configured": _env_present("WORKER_NAME"),
        "worker_id_configured": _env_present("WORKER_ID"),
        "enrollment_token_configured": _env_present("WORKER_ENROLLMENT_TOKEN")
        or _env_present("WORKER_SECRET"),
        "credential_file_configured": _env_present("WORKER_CREDENTIAL_FILE"),
        "credential_file_exists": _file_exists_from_env(
            "WORKER_CREDENTIAL_FILE",
            ".worker_credentials.json",
        ),
        "ca_file_configured": _env_present("CONTROL_PLANE_CA_FILE"),
        "ca_file_exists": _file_exists_from_env("CONTROL_PLANE_CA_FILE"),
    }


def _self_check(_args: argparse.Namespace) -> int:
    payload = {
        "configuration": _configuration_presence(),
        "runtime": NativeRuntimeAdapter().self_check(),
        "notes": [
            "Executable availability is reported for diagnostics; feature-dependent tools must be installed manually.",
            "Configuration values, URLs, tokens, credentials, signatures, and stored credential contents are intentionally redacted.",
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _daemon(args: argparse.Namespace) -> int:
    os.environ["WORKER_RUNTIME"] = "native"
    os.environ["WORKER_RUN_ONCE"] = "true" if args.once else "false"
    worker_main.main()
    return 0


def _backup_engine(_args: argparse.Namespace) -> int:
    try:
        app_main.main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


def _enrollment_labels() -> dict[str, str]:
    return {
        "runtime_kind": "native",
        "runtime_type": "native",
        "supported_runtimes": "native",
        "host_name": socket.gethostname(),
    }


def _new_pending_enrollment(store: WorkerCredentialStore, token_digest: str):
    attempt_id = f"attempt-{uuid.uuid4()}"
    durable_credential = secrets.token_urlsafe(48)
    return store.save_pending_enrollment(attempt_id, durable_credential, token_digest)


def _enroll(args: argparse.Namespace) -> int:
    store = WorkerCredentialStore(os.environ.get("WORKER_CREDENTIAL_FILE", ".worker_credentials.json"))
    if args.reset_pending:
        store.delete_pending_enrollment()
        print("Pending enrollment reset. Run enroll again with a valid bootstrap token to start over.")
        return 0

    bootstrap_token = getpass.getpass("Bootstrap token: ")
    token_digest = digest_secret(bootstrap_token)
    pending = store.load_pending_enrollment()
    if pending is None:
        pending = _new_pending_enrollment(store, token_digest)
    elif pending.token_digest != token_digest:
        print(
            "A pending enrollment already exists for a different bootstrap token. "
            "Re-run with the original token, or run 'enroll --reset-pending' if you intentionally want to discard the pending attempt.",
            file=sys.stderr,
        )
        return 2

    client = ControlPlaneClient(
        os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8080"),
        ca_file=os.environ.get("CONTROL_PLANE_CA_FILE") or None,
        credential_store=store,
    )
    try:
        response = client.complete_worker_enrollment_v2(
            bootstrap_token,
            pending.attempt_id,
            pending.durable_credential,
            labels=_enrollment_labels(),
        )
    except Exception as exc:
        print(
            "Enrollment did not complete. The pending attempt was kept; re-run enroll with the same bootstrap token to retry.",
            file=sys.stderr,
        )
        print(f"Enrollment error type: {exc.__class__.__name__}", file=sys.stderr)
        return 1

    if store.load() is None:
        store.save(response["worker_id"], pending.durable_credential, response["credential_version"])
    store.delete_pending_enrollment()
    print("Enrollment completed. Durable worker credentials were stored for daemon use.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.worker_agent.cli",
        description="Native filesystem worker CLI.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    enroll = subcommands.add_parser(
        "enroll",
        help="Securely complete native worker first-run enrollment using a hidden bootstrap token prompt.",
    )
    enroll.add_argument(
        "--reset-pending",
        action="store_true",
        help="Discard a pending native enrollment attempt without prompting for a bootstrap token.",
    )
    enroll.set_defaults(handler=_enroll)

    daemon = subcommands.add_parser(
        "daemon",
        help="Run the native worker in the foreground with continuous polling by default.",
    )
    daemon.add_argument(
        "--once",
        action="store_true",
        help="Debug mode: run a single worker polling cycle instead of the foreground daemon loop.",
    )
    daemon.set_defaults(handler=_daemon)

    self_check = subcommands.add_parser(
        "self-check",
        help="Print a redacted native worker diagnostic payload.",
    )
    self_check.set_defaults(handler=_self_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["backup-engine"]:
        return _backup_engine(argparse.Namespace(command="backup-engine"))
    parser = build_parser()
    args = parser.parse_args(arguments)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
