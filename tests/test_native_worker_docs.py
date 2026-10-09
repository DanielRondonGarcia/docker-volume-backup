import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
QUICKSTART = ROOT / "doc" / "control-plane-quickstart.md"
# Historical release evidence intentionally remains outside this active-doc audit.
DOCS_TO_AUDIT = (
    ROOT / "README.md",
    ROOT / "doc" / "control-plane-quickstart.md",
    ROOT / "doc" / "control-plane-spec.md",
    ROOT / "deploy" / "control-plane" / "README.md",
    ROOT / "deploy" / "worker" / "README.md",
    ROOT / "examples" / "restic-rclone" / "docker-compose.yml",
)


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

    def test_public_docs_use_canonical_vaultline_repository_and_images(self):
        docs_by_path = {path: path.read_text(encoding="utf-8") for path in DOCS_TO_AUDIT}
        combined_docs = "\n".join(docs_by_path.values())

        self.assertIn("https://github.com/DanielRondonGarcia/vaultline/releases", combined_docs)
        for image in (
            "ghcr.io/danielrondongarcia/vaultline",
            "ghcr.io/danielrondongarcia/vaultline-control-plane",
            "ghcr.io/danielrondongarcia/vaultline-worker",
        ):
            self.assertIn(image, combined_docs)

        self.assertIn(
            "image: ghcr.io/danielrondongarcia/vaultline",
            docs_by_path[ROOT / "examples" / "restic-rclone" / "docker-compose.yml"],
        )
        for legacy_reference in (
            "github.com/DanielRondonGarcia/docker-volume-backup",
            "ghcr.io/danielrondongarcia/docker-volume-backup",
            "danielrondongarcia/docker-volume-backup",
        ):
            with self.subTest(legacy_reference=legacy_reference):
                self.assertNotIn(legacy_reference, combined_docs)


if __name__ == "__main__":
    unittest.main()
