"""Small stdlib-only CLI wrapper for native worker operations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from src.worker_agent import main as worker_main
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.worker_agent.cli",
        description="Native filesystem worker CLI.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
