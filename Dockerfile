FROM python:3.11-slim-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    unzip \
    gnupg \
    restic \
    rclone \
    openssh-client \
    iputils-ping \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

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

# Scripts
# entrypoint.sh expects backup.sh at /root/backup.sh
# It creates env.sh at /root/env.sh
# So let's symlink or copy them to /root
RUN cp /app/src/entrypoint.sh /root/entrypoint.sh \
    && cp /app/src/backup.sh /root/backup.sh \
    && sed -i 's/\r$//' /root/entrypoint.sh \
    && sed -i 's/\r$//' /root/backup.sh \
    && chmod +x /root/entrypoint.sh /root/backup.sh

WORKDIR /root
CMD ["/root/entrypoint.sh"]
