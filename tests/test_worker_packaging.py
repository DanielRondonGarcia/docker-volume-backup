import re
import unittest
from pathlib import Path


class WorkerPackagingTests(unittest.TestCase):
    def test_vaultline_worker_pyinstaller_spec_declares_one_file_worker_cli(self):
        spec_path = Path("build/vaultline-worker.spec")

        self.assertTrue(spec_path.exists(), "vaultline-worker PyInstaller spec is required")
        spec = spec_path.read_text(encoding="utf-8")

        self.assertIn("name='vaultline-worker'", spec)
        self.assertIn("console=True", spec)
        self.assertIn("src/worker_agent/cli.py", spec.replace("\\\\", "/"))
        self.assertIn("src.app.main", spec)
        self.assertIn("src.worker_agent.cli", spec)
        self.assertNotIn(".worker_credentials", spec)
        self.assertNotIn("WORKER_SECRET", spec)
        self.assertNotIn("restic", spec.lower())
        self.assertNotIn("tar.exe", spec.lower())

    def test_build_requirements_pin_pyinstaller_for_ci_python_version(self):
        requirements_path = Path("requirements-build.txt")

        self.assertTrue(requirements_path.exists(), "build-only requirements file is required")
        requirements = requirements_path.read_text(encoding="utf-8")

        match = re.search(r"^pyinstaller==([0-9]+)\.([0-9]+)\.([0-9]+)$", requirements, re.MULTILINE | re.IGNORECASE)
        self.assertIsNotNone(match, "PyInstaller must be exactly pinned")
        major, minor, patch = map(int, match.groups())
        self.assertGreaterEqual((major, minor, patch), (6, 15, 0))
        self.assertRegex(requirements, r"(?im)^-r\s+requirements\.txt$")


if __name__ == "__main__":
    unittest.main()
