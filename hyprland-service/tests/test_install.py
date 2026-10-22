import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "install-user-service.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Installation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        for directory in ("linux_cua", "bin", "systemd"):
            (self.source / directory).mkdir(parents=True)
        (self.source / "linux_cua/__init__.py").write_text("")
        (self.source / "linux_cua/__main__.py").write_text('def entry(): print("installed runtime")\n')
        self.driver = self.source / "bin/linux-cua-wtype"
        self.driver.write_text("#!/bin/sh\nexit 0\n")
        self.driver.chmod(0o755)
        (self.source / "systemd/linux-computer-use.service.in").write_text("[Service]\nWorkingDirectory=@SOURCE_DIR@\n")
        self.destination = self.root / "stable installation"
        self.unit = self.root / "user/service"
        self.client = self.root / "bin/linux-cua"

    def install(self):
        return installer.install(self.source, self.destination, self.unit, self.client, reload=False)

    def test_installed_client_survives_source_move_and_excludes_credentials(self):
        (self.source / "credentials.json").write_text("secret fixture")
        installed = self.install()
        self.source.rename(self.root / "source-moved")
        result = subprocess.run([str(self.client)], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "installed runtime")
        self.assertFalse((Path(installed["release"]) / "credentials.json").exists())
        self.assertIn("stable\\x20installation/current", self.unit.read_text())

    def test_missing_driver_rejected_before_install(self):
        self.driver.unlink()
        with self.assertRaisesRegex(RuntimeError, "Build the private driver"):
            self.install()
        self.assertFalse(self.unit.exists())

    def test_unrelated_client_is_preserved(self):
        self.client.parent.mkdir()
        self.client.write_text("unrelated")
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            self.install()
        self.assertEqual(self.client.read_text(), "unrelated")
        self.assertFalse(self.unit.exists())

    def test_reinstall_reuses_identical_release(self):
        first = self.install()
        second = self.install()
        self.assertEqual(first["release"], second["release"])
        self.assertTrue((self.destination / "current").is_symlink())

    def test_modified_release_rejected(self):
        first = self.install()
        (Path(first["release"]) / "linux_cua/__main__.py").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "release changed"):
            self.install()


if __name__ == "__main__":
    unittest.main()
