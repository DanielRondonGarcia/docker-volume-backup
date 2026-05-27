#!/bin/bash

# Cronjobs don't inherit their env, and restore mode reuses this runner for one-shot execution.
# Load from the entrypoint-generated env file in both cases.
# We use set -a to export all variables to child processes (python)
set -a
source /root/env.sh
set +a

# Run the python application.
# Cron uses a minimal PATH and may not include /usr/local/bin, where the
# python base image installs python3.
cd /app
/usr/local/bin/python3 -m src.app.main
