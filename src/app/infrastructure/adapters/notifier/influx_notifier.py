import os
import requests
import logging
from src.app.domain.models import BackupResult
from src.app.application.ports.ports import NotifierPort

logger = logging.getLogger(__name__)

class InfluxNotifier(NotifierPort):
    def __init__(self):
        self.url = os.environ.get("INFLUXDB_URL")
        self.db = os.environ.get("INFLUXDB_DB")
        self.credentials = os.environ.get("INFLUXDB_CREDENTIALS")
        self.api_token = os.environ.get("INFLUXDB_API_TOKEN")
        self.org = os.environ.get("INFLUXDB_ORGANIZATION")
        self.bucket = os.environ.get("INFLUXDB_BUCKET")
        self.measurement = os.environ.get("INFLUXDB_MEASUREMENT", "docker_volume_backup")
        self.host = os.environ.get("BACKUP_HOSTNAME", os.environ.get("HOSTNAME", "unknown"))

    def send_metrics(self, result: BackupResult) -> None:
        if not self.url:
            return

        # Line Protocol: measurement,tag_set field_set timestamp
        tags = f"host={self.host}"
        fields = (
            f"size_compressed_bytes={result.size},"
            f"duration_seconds={result.duration},"
            f"success={1 if result.success else 0}"
        )
        
        line = f"{self.measurement},{tags} {fields}"
        
        try:
            if self.credentials:
                # InfluxDB v1
                auth = tuple(self.credentials.split(":")) if ":" in self.credentials else None
                response = requests.post(
                    f"{self.url}/write",
                    params={"db": self.db},
                    data=line,
                    auth=auth
                )
            elif self.api_token:
                # InfluxDB v2
                headers = {"Authorization": f"Token {self.api_token}"}
                params = {"org": self.org, "bucket": self.bucket}
                response = requests.post(
                    f"{self.url}/api/v2/write",
                    headers=headers,
                    params=params,
                    data=line
                )
            else:
                # No auth?
                response = requests.post(
                    f"{self.url}/write",
                    params={"db": self.db},
                    data=line
                )
            
            if response.status_code >= 400:
                logger.error(f"InfluxDB error: {response.text}")
            else:
                logger.info("Metrics sent to InfluxDB")
                
        except Exception as e:
            logger.error(f"Failed to send metrics: {e}")
