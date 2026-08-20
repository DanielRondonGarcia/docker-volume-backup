import os
import unittest
from unittest.mock import patch

from src.worker_agent.main import _resolve_live_helper_image


class LiveHelperImageResolutionTests(unittest.TestCase):
    def test_explicit_live_helper_image_precedes_worker_image(self):
        with patch.dict(
            os.environ,
            {
                "LIVE_FILE_HELPER_IMAGE": "registry.example/helper:custom",
                "WORKER_IMAGE": "registry.example/worker:custom",
                "APP_VERSION": "3.3.1",
            },
            clear=True,
        ):
            self.assertEqual(_resolve_live_helper_image(), "registry.example/helper:custom")

    def test_worker_image_precedes_version_inference(self):
        with patch.dict(
            os.environ,
            {
                "WORKER_IMAGE": "registry.example/worker:custom",
                "WORKER_VERSION": "3.3.1",
            },
            clear=True,
        ):
            self.assertEqual(_resolve_live_helper_image(), "registry.example/worker:custom")

    def test_concrete_worker_or_app_version_infers_published_worker_image(self):
        for variable in ("WORKER_VERSION", "APP_VERSION"):
            with self.subTest(variable=variable):
                with patch.dict(os.environ, {variable: "3.3.1"}, clear=True):
                    self.assertEqual(
                        _resolve_live_helper_image(),
                        "ghcr.io/danielrondongarcia/docker-volume-backup-worker:3.3.1",
                    )

    def test_non_concrete_versions_keep_local_fallback(self):
        for version in ("dev", "latest", "ghcr", "docker", "3.3"):
            with self.subTest(version=version):
                with patch.dict(os.environ, {"APP_VERSION": version}, clear=True):
                    self.assertEqual(_resolve_live_helper_image(), "docker-volume-backup-worker-local:dev")


if __name__ == "__main__":
    unittest.main()
