import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-dispatch.yml"


class ReleaseAssetWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_stays_manual_and_splits_prepare_native_image_publish_jobs(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)
        for job in ("prepare:", "native-linux:", "native-windows:", "image-build:", "publish-release:"):
            self.assertRegex(self.workflow, rf"(?m)^  {re.escape(job)}")
        self.assertRegex(self.workflow, r"publish-release:\n(?:.|\n)*?needs:\n(?:.|\n)*?- prepare\n(?:.|\n)*?- native-linux\n(?:.|\n)*?- native-windows\n(?:.|\n)*?- image-build")

    def test_linux_matrix_uses_native_runners_manylinux_and_python_311(self):
        self.assertIn("ubuntu-24.04", self.workflow)
        self.assertIn("ubuntu-24.04-arm", self.workflow)
        self.assertIn("quay.io/pypa/manylinux2014_x86_64", self.workflow)
        self.assertIn("quay.io/pypa/manylinux2014_aarch64", self.workflow)
        self.assertIn("/opt/python/cp311-cp311/bin/python", self.workflow)
        self.assertIn("requirements-build.txt", self.workflow)
        self.assertIn("PyInstaller build/vaultline-worker.spec", self.workflow)
        self.assertIn("scripts/build_worker_deb.sh", self.workflow)

    def test_windows_build_names_smokes_and_uploads_exact_exe(self):
        self.assertIn("runs-on: windows-2022", self.workflow)
        self.assertIn("python-version: '3.11'", self.workflow)
        self.assertIn("vaultline-worker_${{ needs.prepare.outputs.new_version }}_windows-amd64.exe", self.workflow)
        self.assertRegex(self.workflow, r"vaultline-worker(?:\.exe)? --help")
        self.assertIn("actions/upload-artifact@v4", self.workflow)

    def test_publish_validates_assets_generates_checksums_and_uses_release_notes(self):
        for name in (
            "vaultline-worker_${VERSION}_amd64.deb",
            "vaultline-worker_${VERSION}_arm64.deb",
            "vaultline-worker_${VERSION}_windows-amd64.exe",
        ):
            self.assertIn(name, self.workflow)
        self.assertIn("vaultline-worker_${VERSION}_SHA256SUMS", self.workflow)
        self.assertIn("sha256sum", self.workflow)
        self.assertIn("--notes-file release_body.md", self.workflow)
        self.assertIn("--title \"v${VERSION}\"", self.workflow)
        self.assertIn("${{ inputs.prerelease && '--prerelease' || '' }}", self.workflow)
        self.assertRegex(self.workflow, r"gh release create \"\$\{VERSION\}\"(?:.|\n)*?\"dist/release-assets/vaultline-worker_\$\{VERSION\}_SHA256SUMS\"")

    def test_tag_and_release_are_only_in_final_publish_job_after_dependencies(self):
        before_publish = self.workflow.split("  publish-release:", 1)[0]
        self.assertNotIn("git tag -a", before_publish)
        self.assertNotIn("gh release create", before_publish)
        self.assertIn("git tag -a \"${VERSION}\"", self.workflow)
        self.assertIn("git push origin \"${VERSION}\"", self.workflow)

    def test_existing_image_tags_platforms_and_worker_options_are_preserved(self):
        self.assertGreaterEqual(self.workflow.count("platforms: linux/amd64,linux/arm64"), 3)
        for target in ("backup-runtime", "control-plane", "worker"):
            self.assertIn(f"target: {target}", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository }}", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-control-plane", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-worker", self.workflow)
        self.assertIn("type=semver,pattern={{version}},value=${{ needs.prepare.outputs.new_version }}", self.workflow)
        self.assertIn("type=raw,value=latest", self.workflow)
        self.assertIn("INSTALL_DOCKER_CLI=true", self.workflow)


if __name__ == "__main__":
    unittest.main()
