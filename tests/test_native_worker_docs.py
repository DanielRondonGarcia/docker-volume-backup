import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
QUICKSTART = ROOT / "doc" / "control-plane-quickstart.md"


class NativeWorkerDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = README.read_text(encoding="utf-8")
        cls.quickstart = QUICKSTART.read_text(encoding="utf-8")

    def test_quickstart_documents_secure_native_worker_installation_flow(self):
        for marker in (
            "V2 seguro",
            "V1 legacy",
            "vaultline-worker enroll",
            "prompt oculto",
            "dvb-worker",
            "/etc/docker-volume-backup/worker.env",
            "/var/lib/docker-volume-backup",
            "docker-volume-backup-worker.service",
            "Task Scheduler",
            "portable x64",
            "No usa Windows SCM",
            "glibc >= 2.17",
            "SmartScreen",
            "restic, tar, gpg, rclone, aws, ssh y scp",
            "no arranca ni enrola automaticamente",
        ):
            self.assertIn(marker, self.quickstart)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN=", self.quickstart)

    def test_readme_links_to_native_worker_package_guidance_concisely(self):
        self.assertIn("Worker nativo", self.readme)
        self.assertIn("doc/control-plane-quickstart.md#worker-nativo-empaquetado-v2-seguro", self.readme)


if __name__ == "__main__":
    unittest.main()
