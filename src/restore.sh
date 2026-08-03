#!/bin/bash

set -euo pipefail

# Restore is a one-shot execution of the same Python runner used by backup.sh.
# Default to the centralized /backup layout so existing volume mounts can be
# matched back to their original names during restore.
export RESTORE_MODE="${RESTORE_MODE:-true}"
export RESTORE_TARGET_PATH="${RESTORE_TARGET_PATH:-/backup}"
export RESTORE_LAYOUT="${RESTORE_LAYOUT:-auto}"
export RESTORE_DRY_RUN="${RESTORE_DRY_RUN:-true}"

exec /root/backup.sh
