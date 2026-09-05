import shutil
import subprocess
import unittest
from pathlib import Path


COMPOSE = Path(__file__).resolve().parents[1] / "test" / "restore-ownership" / "docker-compose.yml"


class RestoreOwnershipComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = COMPOSE.read_text(encoding="utf-8")

    def test_compose_config_is_valid_without_starting_services(self):
        if not shutil.which("docker"):
            self.skipTest("Docker is not installed")
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixture_is_disposable_and_covers_preserve_and_distinct_numeric_mappings(self):
        for marker in (
            "n8n_data:", "redis_data:", "chmod 600", "chown -R 0:0", "600:0:0",
            "profiles: [preserve]", "profiles: [map]", "1000:1000", "2000:2000",
            "compose:restore-ownership:n8n_data", "compose:restore-ownership:redis_data",
            "RESTORE_OWNERSHIP_JSON", "confirmation",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("docker compose up", self.source)


if __name__ == "__main__":
    unittest.main()
