#!/bin/bash

# Cronjobs don't inherit their env, so load from file
# We use set -a to export all variables to child processes (python)
set -a
source /root/env.sh
set +a

# Run the python application
cd /app
python3 -m src.app.main
