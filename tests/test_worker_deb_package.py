import re
import unittest
from pathlib import Path


PACKAGING_ROOT = Path("packaging/debian")
CONTROL = PACKAGING_ROOT / "control.in"
POSTINST = PACKAGING_ROOT / "postinst"
SERVICE = PACKAGING_ROOT / "lib/systemd/system/docker-volume-backup-worker.service"
ENV_EXAMPLE = PACKAGING_ROOT / "usr/share/doc/vaultline-worker/worker.env.example"
BUILDER = Path("scripts/build_worker_deb.sh")


class WorkerDebPackageTests(unittest.TestCase):
    def read_text(self, path: Path) -> str:
        self.assertTrue(path.exists(), f"{path} is required")
        return path.read_text(encoding="utf-8")

    def test_control_metadata_uses_package_arch_and_runtime_dependencies(self):
        control = self.read_text(CONTROL)

        self.assertRegex(control, r"(?m)^Package: vaultline-worker$")
        self.assertRegex(control, r"(?m)^Version: @VERSION@$")
        self.assertRegex(control, r"(?m)^Architecture: @ARCH@$")
        self.assertRegex(control, r"(?m)^Depends: .*libc6 \(>= 2\.17\)")
        self.assertRegex(control, r"(?m)^Depends: .*adduser")
        self.assertRegex(control, r"(?m)^Depends: .*systemd")
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", control)
        self.assertNotIn("WORKER_SECRET", control)

    def test_builder_validates_semver_and_arch_and_names_assets_deterministically(self):
        builder = self.read_text(BUILDER)

        self.assertIn("dpkg-deb --build --root-owner-group", builder)
        self.assertRegex(builder, r"\[\[ \$\{VERSION\} =~ \^\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+")
        self.assertRegex(builder, r"case \"\$\{ARCH\}\" in\s+amd64\|arm64\)")
        self.assertIn('vaultline-worker_${VERSION}_${ARCH}.deb', builder)
        self.assertIn("install -m 0755", builder)
        self.assertIn("install -m 0644", builder)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", builder)
        self.assertNotIn("WORKER_SECRET", builder)
        for tool in ("restic", "rclone", "gpg", "aws", "ssh", "scp"):
            self.assertNotIn(tool, builder.lower())

    def test_package_service_preserves_identifiers_paths_and_binary_execstart(self):
        service = self.read_text(SERVICE)

        self.assertIn("User=dvb-worker", service)
        self.assertIn("Group=dvb-worker", service)
        self.assertIn("EnvironmentFile=-/etc/docker-volume-backup/worker.env", service)
        self.assertIn("ReadWritePaths=/var/lib/docker-volume-backup", service)
        self.assertIn("ExecStart=/usr/bin/vaultline-worker daemon", service)
        self.assertIn("ProtectHome=read-only", service)
        self.assertIn("restore", service.lower())
        self.assertNotIn("/opt/docker-volume-backup", service)
        self.assertNotIn("python3 -m", service)

    def test_postinst_prepares_state_safely_without_enabling_or_starting_service(self):
        postinst = self.read_text(POSTINST)

        self.assertRegex(postinst, r"addgroup.*dvb-worker")
        self.assertRegex(postinst, r"adduser.*dvb-worker")
        self.assertIn("/etc/docker-volume-backup", postinst)
        self.assertIn("/var/lib/docker-volume-backup", postinst)
        self.assertRegex(postinst, r"install -d -m 0750 .* /etc/docker-volume-backup")
        self.assertRegex(postinst, r"install -d -m 0700 .* /var/lib/docker-volume-backup")
        self.assertNotIn("worker.env", postinst)
        self.assertNotRegex(postinst, r"systemctl\s+(enable|start|restart)")
        self.assertIn("systemctl daemon-reload", postinst)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", postinst)
        self.assertNotIn("WORKER_SECRET", postinst)

    def test_package_contains_only_non_secret_environment_example(self):
        example = self.read_text(ENV_EXAMPLE)

        self.assertIn("CONTROL_PLANE_URL=", example)
        self.assertIn("WORKER_NAME=", example)
        self.assertIn("WORKER_CREDENTIAL_FILE=/var/lib/docker-volume-backup/worker_credentials.json", example)
        self.assertIn("WORKER_RUNTIME=native", example)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", example)
        self.assertNotIn("WORKER_SECRET", example)
        self.assertNotIn("change-me", example.lower())
        self.assertNotIn("password", example.lower())

    def test_builder_stages_expected_debian_tree_without_active_secret_config_or_tools(self):
        builder = self.read_text(BUILDER)

        self.assertIn("DEBIAN/control", builder)
        self.assertIn("DEBIAN/postinst", builder)
        self.assertIn("usr/bin/vaultline-worker", builder)
        self.assertIn("lib/systemd/system/docker-volume-backup-worker.service", builder)
        self.assertIn("usr/share/doc/vaultline-worker/worker.env.example", builder)
        self.assertNotIn("etc/docker-volume-backup/worker.env", builder)
        self.assertNotIn("worker.env.example etc/", builder)
        self.assertIn("test -x", builder)
        self.assertIn("test -f", builder)


if __name__ == "__main__":
    unittest.main()
