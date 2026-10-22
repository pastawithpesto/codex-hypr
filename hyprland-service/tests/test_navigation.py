"""Scoped navigation through the real broker/desktop code, with no GUI calls."""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from linux_cua.desktop import NAVIGATION_KEYS, SESSION_KEYS
from linux_cua.policy import read_private_json
from linux_cua.server import Broker, GuardedDesktop


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDPsAAAAASUVORK5CYII=")
TARGET = {"address": "0x1", "class": "fixture", "pid": 123,
          "at": [0, 0], "size": [100, 100], "mapped": True, "hidden": False}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backends = []
        self.broker = Broker(self.root / "runtime", self.factory)
        self.credentials = self.root / "credentials.json"
        self.auth = None
        def supervised_key(key, env, check):
            check()
            check()
            check()
            return 1
        self.writer_patch = patch("linux_cua.writer.write_key", side_effect=supervised_key)
        self.write_key = self.writer_patch.start()
        self.addCleanup(self.writer_patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, state, guard):
        backend = GuardedDesktop({k: "test" for k in SESSION_KEYS}, state, guard)
        snapshot = {"clients": [TARGET], "active": TARGET, "layout": []}
        receipt = {**snapshot, "bounds": [0, 0, 100, 100]}
        def load(token):
            (state / token).mkdir(exist_ok=True)
            return receipt
        def observe():
            guard()
            shot = state / "after.png"
            shot.write_bytes(PNG)
            return {"observation": uuid.uuid4().hex, "screenshot": str(shot)}
        backend.load = Mock(side_effect=load)
        backend.snapshot = Mock(return_value=snapshot)
        backend.dispatch = Mock(return_value="ok\n")
        backend.observe = Mock(side_effect=observe)
        self.backends.append(backend)
        return backend

    def grant(self, actions=("observe", "press_key"), apps=("fixture",)):
        self.broker.grants.create(actions, apps, 300, self.credentials)
        self.auth = read_private_json(self.credentials)

    def call(self, key, **extra):
        params = {"auth": self.auth, "observation": uuid.uuid4().hex,
                  "window": "0x1", "reviewed": True, "key": key, **extra}
        return self.broker.respond({"jsonrpc": "2.0", "id": 1,
                                    "method": "press_key", "params": params})

    def test_navigation_needs_its_own_permission(self):
        self.grant(actions=("observe",))
        self.assertEqual(self.call("Tab")["error"]["code"], -32001)
        self.assertEqual(self.backends, [])

    def test_unsupported_keys_and_arbitrary_modifiers_never_construct_desktop(self):
        self.grant()
        for key in ("Enter", "Return", "Ctrl+Return", "Super+W", "tab", "a", "", None,
                    ["Tab"], 'Tab"}); hl.dsp.exec_cmd("bad")'):
            with self.subTest(key=key):
                self.assertEqual(self.call(key)["error"]["code"], -32602)
        self.assertEqual(self.call("Tab", mods="CTRL")["error"]["code"], -32602)
        self.assertEqual(self.backends, [])

    def test_allowlist_is_exact(self):
        self.assertEqual(set(NAVIGATION_KEYS), {"Tab", "Shift+Tab", "Escape", "Left", "Right",
                         "Up", "Down", "Home", "End", "PageUp", "PageDown", "Ctrl+A",
                         "Backspace", "Delete"})

    def test_keys_use_supervised_driver_and_export_final_screenshot(self):
        self.grant()
        for key in ("Shift+Tab", "Ctrl+A", "Home", "PageDown", "Backspace"):
            with self.subTest(key=key):
                reply = self.call(key)
                self.assertTrue(reply["result"]["ok"])
                self.assertEqual(self.write_key.call_args.args[0], key)
                self.assertTrue(callable(self.write_key.call_args.args[2]))
                self.backends[-1].dispatch.assert_not_called()
                self.assertEqual(reply["result"]["key_pairs_submitted"], 1)
                self.assertEqual(reply["result"]["characters_submitted"], 0)
                self.backends[-1].observe.assert_called_once()
                self.assertEqual(reply["result"]["after"]["image"]["mime_type"], "image/png")

    def test_wrong_app_and_missing_review_cannot_dispatch(self):
        self.grant(apps=("other-app",))
        self.assertEqual(self.call("Tab")["error"]["code"], -32001)
        self.backends[-1].dispatch.assert_not_called()
        self.assertEqual(self.call("Tab", reviewed=False)["error"]["code"], -32602)
        self.backends[-1].dispatch.assert_not_called()

    def test_revocation_after_target_queries_prevents_dispatch(self):
        self.grant()
        factory = self.broker.factory
        def revoking_factory(state, guard):
            backend = factory(state, guard)
            original = backend.check_target
            def check_then_revoke(*args, **kwargs):
                result = original(*args, **kwargs)
                self.broker.grants.revoke(self.auth["session"])
                return result
            backend.check_target = check_then_revoke
            return backend
        self.broker.factory = revoking_factory
        reply = self.call("Delete")
        self.assertFalse(reply["result"]["ok"])
        self.assertTrue(reply["result"]["session_ended"])
        self.backends[-1].dispatch.assert_not_called()

    def test_changed_focus_stops_navigation(self):
        self.grant()
        factory = self.broker.factory
        def unfocused_factory(state, guard):
            backend = factory(state, guard)
            backend.snapshot.return_value = {"clients": [TARGET], "active": {}, "layout": []}
            return backend
        self.broker.factory = unfocused_factory
        reply = self.call("Backspace")
        self.assertIn("error", reply)
        self.backends[-1].dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
