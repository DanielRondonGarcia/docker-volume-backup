import ast
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

    def test_vaultline_worker_spec_adds_backports_hiddenimports_only_on_windows(self):
        spec_path = Path("build/vaultline-worker.spec")
        spec = spec_path.read_text(encoding="utf-8")
        tree = ast.parse(spec)
        backports_imports = {"backports", "backports.tarfile"}

        def string_constants(node):
            return {
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }

        def is_win32_platform_check(test):
            return (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Attribute)
                and isinstance(test.left.value, ast.Name)
                and test.left.value.id == "sys"
                and test.left.attr == "platform"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "win32"
            )

        win32_blocks = [node for node in tree.body if isinstance(node, ast.If) and is_win32_platform_check(node.test)]
        self.assertEqual(1, len(win32_blocks), 'spec must gate backports imports with sys.platform == "win32"')
        self.assertTrue(backports_imports.issubset(string_constants(win32_blocks[0])))

        for node in tree.body:
            if node is win32_blocks[0]:
                continue
            self.assertTrue(
                backports_imports.isdisjoint(string_constants(node)),
                "backports hidden imports must not be declared outside the Windows-only block",
            )

    def test_vaultline_worker_spec_resolves_cli_from_pyinstaller_spec_dir(self):
        spec_path = Path("build/vaultline-worker.spec")
        spec = spec_path.read_text(encoding="utf-8")
        root_match = re.search(r"^ROOT\s*=\s*(.+)$", spec, re.MULTILINE)
        self.assertIsNotNone(root_match, "spec must define ROOT from PyInstaller SPECPATH")

        namespace = {"Path": Path, "SPECPATH": str(spec_path.parent.resolve())}
        exec(f"ROOT = {root_match.group(1)}", namespace)

        root = namespace["ROOT"]
        self.assertEqual(Path.cwd().resolve(), root)
        self.assertTrue((root / "src/worker_agent/cli.py").exists())

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
