FROM python:3.11-slim-bookworm AS app-base

# Install system dependencies. Docker CLI installation is optional so the
# Kubernetes worker image can use the same multi-architecture base without
# carrying a Docker daemon client when it is not needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    tzdata \
    unzip \
    gnupg \
    restic \
    rclone \
    openssh-client \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

ARG INSTALL_DOCKER_CLI=true
RUN if [ "${INSTALL_DOCKER_CLI}" = "true" ]; then \
    apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*; \
    fi

# Install AWS CLI v2
RUN if [ $(uname -m) = "aarch64" ] || [ $(uname -m) = "x86_64" ] ; then \
    curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "awscliv2.zip" \
    && unzip -q awscliv2.zip \
    && ./aws/install -i /usr/bin -b /usr/bin \
    && rm -rf ./aws awscliv2.zip \
    ; fi

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
# We copy the 'src' directory into /app/src so that 'src.app.main' works
COPY src /app/src

# Set PYTHONPATH to /app so python can find 'src'
ENV PYTHONPATH=/app

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
ENV WORKER_VERSION=${APP_VERSION}

# Scripts
# entrypoint.sh expects backup.sh at /root/backup.sh for cron backups and one-shot restores
# It creates env.sh at /root/env.sh
# So let's symlink or copy them to /root
RUN cp /app/src/entrypoint.sh /root/entrypoint.sh \
    && cp /app/src/backup.sh /root/backup.sh \
    && cp /app/src/restore.sh /root/restore.sh \
    && sed -i 's/\r$//' /root/entrypoint.sh \
    && sed -i 's/\r$//' /root/backup.sh \
    && sed -i 's/\r$//' /root/restore.sh \
    && chmod +x /root/entrypoint.sh /root/backup.sh /root/restore.sh

WORKDIR /root

FROM app-base AS backup-runtime
CMD ["/root/entrypoint.sh"]

FROM app-base AS control-plane
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 CMD python -c "import os, sys, urllib.request; port=os.environ.get('CONTROL_PLANE_PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4); sys.exit(0)"
CMD ["python", "-m", "src.control_plane.main"]

FROM app-base AS worker
EXPOSE 8081
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 CMD python -c "import os, sys, urllib.request; port=os.environ.get('WORKER_HEALTH_PORT', '8081'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4); sys.exit(0)"
CMD ["python", "-m", "src.worker_agent.main"]
