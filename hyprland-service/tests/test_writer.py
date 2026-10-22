"""Real child/pipe supervision tests with a non-desktop driver."""
import os
from pathlib import Path
import tempfile
import subprocess
import unittest

from linux_cua.writer import DRIVER, WriteStopped, write_key, write_text
from linux_cua.desktop import NAVIGATION_KEYS

FAKE = '''#!/usr/bin/python3
import os, sys
assert os.environ["LC_ALL"] == "C.UTF-8"
text = sys.stdin.read()
if sys.argv[1:2] == ["--host-key"]:
    assert text == ""
    text = ["KEY:" + sys.argv[2]]
else:
    assert sys.argv[1:] == ["-"]
permission = int(os.environ["LINUX_CUA_STEP_FD"])
ack = int(os.environ["LINUX_CUA_STEP_ACK_FD"])
for char in text:
    os.write(ack, b"R")
    if os.read(permission, 1) != b"T":
        sys.exit(2)
    with open(os.environ["TEST_LOG"], "a") as stream:
        stream.write(char)
    os.write(ack, b"D")
'''


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.driver = self.root / "driver"
        self.driver.write_text(FAKE)
        self.driver.chmod(0o700)
        self.log = self.root / "pairs"
        self.env = {**os.environ, "TEST_LOG": str(self.log)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_literal_text_and_unicode_use_one_persistent_driver(self):
        text = "KEYBOARD_CONTROL_TEST ñ☃"
        self.env["LC_ALL"] = "C"
        checks = []
        def check():
            checks.append(len(self.log.read_text()) if self.log.exists() else 0)
        count = write_text(text, self.env, check, driver=self.driver)
        self.assertEqual(count, len(text))
        self.assertEqual(self.log.read_text(), text)
        self.assertEqual(checks, [0, *range(len(text)), len(text)])

    def test_revocation_stops_before_next_pair_without_replaying(self):
        def check():
            if self.log.exists() and self.log.read_text() == "AB":
                raise ValueError("session revoked")
        with self.assertRaises(WriteStopped) as caught:
            write_text("ABCD", self.env, check, driver=self.driver)
        self.assertEqual(caught.exception.completed, 2)
        self.assertEqual(self.log.read_text(), "AB")

    def test_missing_driver_fails_before_desktop_check(self):
        with self.assertRaises(WriteStopped):
            write_text("x", self.env, lambda: self.fail("must not access desktop"), driver=self.root / "absent")

    def test_bad_acknowledgement_is_not_reported_as_success(self):
        self.driver.write_text(FAKE.replace('os.write(ack, b"D")', 'os.write(ack, b"?")'))
        with self.assertRaises(WriteStopped) as caught:
            write_text("ABC", self.env, lambda: None, driver=self.driver)
        self.assertEqual(caught.exception.completed, 0)
        self.assertEqual(self.log.read_text(), "A")

    def test_navigation_uses_one_gated_pair_with_fresh_checks(self):
        checks = []
        def check():
            checks.append(self.log.read_text() if self.log.exists() else "")
        self.assertEqual(write_key("Home", self.env, check, driver=self.driver), 1)
        self.assertEqual(self.log.read_text(), "KEY:Home")
        self.assertEqual(checks, ["", "", "KEY:Home"])

    def test_navigation_revoked_before_permission_never_emits_pair(self):
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("session revoked")
        with self.assertRaises(WriteStopped) as caught:
            write_key("Delete", self.env, check, driver=self.driver)
        self.assertEqual(caught.exception.completed, 0)
        self.assertFalse(self.log.exists())

    def test_navigation_rejection_precedes_process_and_desktop_access(self):
        for key in ("Enter", "Return", "Ctrl+Enter", "arbitrary", None):
            with self.subTest(key=key), self.assertRaises(WriteStopped):
                write_key(key, self.env, lambda: self.fail("no desktop check expected"),
                          driver=self.root / "missing")

    def test_setup_delay_revocation_is_checked_after_ready_before_permission(self):
        prepared = self.root / "prepared"
        self.env["TEST_PREPARED"] = str(prepared)
        delayed = FAKE.replace('    os.write(ack, b"R")',
            '    import time\n'
            '    with open(os.environ["TEST_PREPARED"], "w") as stream:\n'
            '        stream.write("setup finished; grant revoked")\n'
            '    time.sleep(0.04)\n'
            '    os.write(ack, b"R")')
        self.driver.write_text(delayed)
        for navigation in (False, True):
            with self.subTest(navigation=navigation):
                prepared.unlink(missing_ok=True)
                checks = []
                def check():
                    checks.append(prepared.exists())
                    if prepared.exists():
                        raise ValueError("grant revoked during preparation")
                with self.assertRaises(WriteStopped) as caught:
                    if navigation:
                        write_key("Home", self.env, check, driver=self.driver)
                    else:
                        write_text("A", self.env, check, driver=self.driver)
                self.assertEqual(caught.exception.completed, 0)
                self.assertEqual(checks, [False, True])
                self.assertFalse(self.log.exists())

    def test_missing_or_unexpected_readiness_never_sends_permission(self):
        for ready in ('os.write(ack, b"?")', 'sys.exit(0)'):
            with self.subTest(ready=ready):
                self.driver.write_text(FAKE.replace('os.write(ack, b"R")', ready))
                checks = []
                with self.assertRaises(WriteStopped) as caught:
                    write_text("A", self.env, lambda: checks.append("check"), driver=self.driver)
                self.assertEqual(caught.exception.completed, 0)
                self.assertEqual(checks, ["check"])
                self.assertFalse(self.log.exists())

    def test_native_navigation_mode_validates_before_wayland_connection(self):
        if not DRIVER.exists():
            self.skipTest("private driver has not been built")
        env = {k: v for k, v in self.env.items()
               if k not in ("LINUX_CUA_STEP_FD", "LINUX_CUA_STEP_ACK_FD")}
        env.update(XDG_RUNTIME_DIR=str(self.root / "no-session"), WAYLAND_DISPLAY="no-display")
        for key in NAVIGATION_KEYS:
            with self.subTest(key=key):
                result = subprocess.run([str(DRIVER), "--host-key", key], capture_output=True,
                                        text=True, env=env, timeout=2)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires handshake descriptors", result.stderr)
        for key in ("Enter", "Return", "Ctrl+Enter", "Home; bad"):
            with self.subTest(key=key):
                result = subprocess.run([str(DRIVER), "--host-key", key], capture_output=True,
                                        text=True, env=env, timeout=2)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsupported host navigation key", result.stderr)


if __name__ == "__main__":
    unittest.main()
