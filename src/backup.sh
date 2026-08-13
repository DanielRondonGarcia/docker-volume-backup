#!/bin/bash

# Cronjobs don't inherit their env, and restore mode reuses this runner for one-shot execution.
# Load from the entrypoint-generated env file in both cases.
# We use set -a to export all variables to child processes (python)
set -a
if [ -f /root/env.sh ]; then
  source /root/env.sh
fi
set +a

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
