"""Protocol/policy integration checks. No desktop tools are invoked."""
import base64
import contextlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from linux_cua.client import request, save_images
from linux_cua.desktop import SESSION_KEYS, Stop, validate_text
from linux_cua.policy import Denied, private_directory, read_private_json
from linux_cua.server import Broker, GuardedDesktop, Server, notify_ready

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDPsAAAAASUVORK5CYII=")


class FakeDesktop:
    def __init__(self, state, guard, events):
        self.state, self.guard, self.events = Path(state), guard, events

    def locked(self):
        return contextlib.nullcontext()

    def observe(self):
        self.guard()
        self.events.append("observe")
        shot = self.state / "fake.png"
        shot.write_bytes(PNG)
        return {"observation": "a" * 32, "screenshot": str(shot),
                "clients": [{"address": "0x1", "class": "fixture"}]}

    def load(self, token):
        if token != "a" * 32 or (self.state / "used").exists():
            raise Stop("Receipt unavailable or consumed")
        return {"clients": [{"address": "0x1", "class": "fixture"}]}

    def act(self, kind, token, window, **kwargs):
        self.guard()
        self.load(token)
        if kind == "type":
            validate_text(kwargs["text"])
        (self.state / "used").write_text("used")
        self.events.append(kind)
        return {"ok": True, "after": self.observe()}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = []
        def factory(state, guard):
            self.events.append("factory")
            return FakeDesktop(state, guard, self.events)
        self.broker = Broker(self.root / "runtime", factory)
        self.credential = self.root / "credentials.json"

    def tearDown(self):
        self.tmp.cleanup()

    def grant(self, actions=("observe", "focus", "click", "type_text"), apps=("fixture",)):
        self.broker.grants.create(actions, apps, 300, self.credential)
        return read_private_json(self.credential)

    def call(self, method, auth=None, **params):
        if auth is not None:
            params["auth"] = auth
        return self.broker.respond({"jsonrpc": "2.0", "id": 7, "method": method, "params": params})

    def test_ping_never_constructs_desktop(self):
        self.assertTrue(self.call("ping")["result"]["ok"])
        self.assertEqual(self.events, [])

    def test_unauthenticated_observe_is_denied_before_desktop(self):
        self.assertEqual(self.call("observe")["error"]["code"], -32001)
        self.assertEqual(self.events, [])

    def test_bad_token_and_expired_and_revoked_never_access_desktop(self):
        auth = self.grant()
        bad = {**auth, "token": "wrong"}
        self.assertIn("error", self.call("observe", bad))
        self.broker.grants.clock = lambda: 10 ** 12
        self.assertIn("error", self.call("observe", auth))
        self.broker.grants.revoke(auth["session"])
        self.assertIn("error", self.call("observe", auth))
        self.assertEqual(self.events, [])

    def test_observation_only_grant_cannot_type(self):
        auth = self.grant(("observe",), ())
        reply = self.call("type_text", auth, observation="a" * 32, window="0x1", reviewed=True, text="hello")
        self.assertEqual(reply["error"]["code"], -32001)
        self.assertEqual(self.events, [])

    def test_wrong_app_cannot_receive_input(self):
        auth = self.grant(apps=("different-app",))
        reply = self.call("focus", auth, observation="a" * 32, window="0x1", reviewed=True)
        self.assertEqual(reply["error"]["code"], -32001)
        self.assertNotIn("focus", self.events)

    def test_input_requires_reviewed_true_and_integer_click(self):
        auth = self.grant()
        self.assertIn("error", self.call("focus", auth, observation="a" * 32, window="0x1", reviewed="true"))
        self.assertIn("error", self.call("click", auth, observation="a" * 32, window="0x1", reviewed=True, x=True, y=1))
        self.assertNotIn("focus", self.events)
        self.assertNotIn("click", self.events)

    def test_type_rejects_enter_and_receipt_cannot_replay(self):
        auth = self.grant()
        common = dict(observation="a" * 32, window="0x1", reviewed=True)
        self.assertIn("error", self.call("type_text", auth, text="hello\n", **common))
        self.assertTrue(self.call("type_text", auth, text="hello", **common)["result"]["ok"])
        self.assertIn("error", self.call("type_text", auth, text="again", **common))
        self.assertEqual(self.events.count("type"), 1)

    def test_no_grant_creation_or_arbitrary_command_rpc(self):
        self.assertEqual(self.call("authorize")["error"]["code"], -32601)
        self.assertEqual(self.call("exec", command="anything")["error"]["code"], -32601)
        self.assertEqual(self.events, [])

    def test_credential_files_reject_symlinks_and_public_permissions(self):
        self.grant()
        self.credential.chmod(0o644)
        with self.assertRaises(Denied):
            read_private_json(self.credential)
        self.credential.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(self.credential)
        with self.assertRaises(OSError):
            read_private_json(link)

    def test_audit_does_not_contain_credentials_text_or_method_payload(self):
        auth = self.grant()
        text = "private test payload"
        self.call("type_text", auth, observation="a" * 32, window="0x1", reviewed=True, text=text)
        self.call("secret value as method")
        audit = (self.broker.runtime / "audit.jsonl").read_text()
        self.assertNotIn(auth["token"], audit)
        self.assertNotIn(text, audit)
        self.assertNotIn("secret value", audit)

    def test_real_unix_socket_transport_and_image_export(self):
        auth = self.grant()
        path = self.broker.runtime / "control.sock"
        with Server(path, self.broker) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertTrue(request(self.broker.runtime, "ping", {})["result"]["ok"])
                reply = request(self.broker.runtime, "observe", {}, self.credential)
                exported = save_images(reply["result"], self.root / "caller-images")
                self.assertEqual(Path(exported["observation"]["screenshot"]).read_bytes(), PNG)
                self.assertNotIn("base64", json.dumps(exported))
                denied = request(self.broker.runtime, "type_text", {"text": "x"})
                self.assertEqual(denied["error"]["code"], -32001)
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_busy_service_refuses_second_desktop_operation(self):
        auth = self.grant()
        with self.broker.operation:
            self.assertEqual(self.call("observe", auth)["error"]["code"], -32002)
        self.assertEqual(self.events, [])

    def test_systemd_readiness_notification(self):
        path = str(self.root / "notify.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
            notification.bind(path)
            notification.settimeout(1)
            with patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                notify_ready()
            self.assertEqual(notification.recv(128), b"READY=1")

    def test_socket_rejects_oversized_request_before_backend(self):
        path = self.broker.runtime / "control.sock"
        with Server(path, self.broker) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b"x" * 65537 + b"\n")
                    with client.makefile("rb") as stream:
                        reply = json.loads(stream.readline())
                    self.assertEqual(reply["error"]["code"], -32600)
                self.assertEqual(self.events, [])
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_revocation_during_target_check_stops_next_input(self):
        target = {"address": "0x1", "class": "fixture", "at": [0, 0], "size": [100, 100]}
        receipt = {"clients": [target], "bounds": [0, 0, 100, 100]}
        for kind, action in (("focus", "focus"), ("click", "click"), ("type", "type_text")):
            with self.subTest(kind=kind):
                self.credential.unlink(missing_ok=True)
                auth = self.grant()
                state = private_directory(self.root / kind)
                (state / ("a" * 32)).mkdir()
                backend = GuardedDesktop({k: "test" for k in SESSION_KEYS}, state,
                                         lambda: self.broker.grants.authenticate(auth, action))
                backend.load = Mock(return_value=receipt)
                def revoke_during_query(*args, **kwargs):
                    self.broker.grants.revoke(auth["session"])
                    return target
                backend.check_target = Mock(side_effect=revoke_during_query)
                backend.dispatch = Mock()
                backend.run = Mock()
                backend.observe = Mock(return_value={})
                result = backend.act(kind, "a" * 32, "0x1", reviewed=True, px=1, py=1, text="x")
                self.assertFalse(result["ok"])
                backend.dispatch.assert_not_called()
                backend.run.assert_not_called()

    def test_mouse_release_still_runs_after_revocation(self):
        auth = self.grant()
        state = private_directory(self.root / "release")
        (state / ("a" * 32)).mkdir()
        backend = GuardedDesktop({k: "test" for k in SESSION_KEYS}, state,
                                 lambda: self.broker.grants.authenticate(auth, "click"))
        target = {"address": "0x1", "class": "fixture", "at": [0, 0], "size": [100, 100]}
        backend.load = Mock(return_value={"clients": [target], "bounds": [0, 0, 100, 100]})
        backend.check_target = Mock(return_value=target)
        backend.hypr = Mock(return_value={"x": 1, "y": 1})
        calls = []
        def dispatch(expression):
            calls.append(expression)
            if 'state = "down"' in expression:
                self.broker.grants.revoke(auth["session"])
                raise Stop("simulated press failure")
        backend.dispatch = dispatch
        backend.observe = Mock(return_value={})
        result = backend.act("click", "a" * 32, "0x1", reviewed=True, px=1, py=1)
        self.assertFalse(result["ok"])
        self.assertIn('state = "up"', calls[-1])


if __name__ == "__main__":
    unittest.main()
