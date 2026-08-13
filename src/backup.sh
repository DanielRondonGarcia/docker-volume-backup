#!/bin/bash

# Cronjobs don't inherit their env, and restore mode reuses this runner for one-shot execution.
# Load from the entrypoint-generated env file in both cases.
# We use set -a to export all variables to child processes (python)
set -a
[ -f /root/env.sh ] && . /root/env.sh 2>/dev/null || true
set +a

# Also check /root/.config/rclone/rclone.conf (storage profile mount path)
# and copy to /run/secrets/ so the rest of the script finds it there.
if [ ! -f /run/secrets/rclone.conf ] && [ -f /root/.config/rclone/rclone.conf ]; then
  mkdir -p /run/secrets
  cp /root/.config/rclone/rclone.conf /run/secrets/rclone.conf
fi

# If rclone.conf is not on disk but was passed as env var (RCLONE_CONF_CONTENT),
# write it to /run/secrets/rclone.conf so rclone finds it.
if [ ! -f /run/secrets/rclone.conf ] && [ -n "$RCLONE_CONF_CONTENT" ]; then
  mkdir -p /run/secrets
  printf '%s' "$RCLONE_CONF_CONTENT" > /run/secrets/rclone.conf
fi

# If rclone.conf is mounted at /run/secrets/, copy it to a writable
# location so rclone can save config changes without "read-only file system" errors.
# Docker bind mounts marked :ro may still report as writable to [ -w ], so we
# always copy when the source file exists.
if [ -f /run/secrets/rclone.conf ]; then
  mkdir -p /tmp/rclone
  cp /run/secrets/rclone.conf /tmp/rclone/rclone.conf
  export RCLONE_CONFIG="/tmp/rclone/rclone.conf"
fi

# Run the python application.
# cron uses a minimal PATH and may not include /usr/local/bin, where the
# python base image installs python3.
cd /app
exec /usr/local/bin/python3 -u -m src.app.main
