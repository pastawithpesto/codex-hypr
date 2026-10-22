"""Local JSON-RPC broker. No TCP listener and no remote grant-creation API."""
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import stat
import struct
import threading
import time

from . import __version__
from .desktop import Desktop, NAVIGATION_KEYS, Stop, session_environment
from .policy import Denied, Grants, private_directory

MAX_REQUEST = 65536
MAX_IMAGE = 24 * 1024 * 1024


class RpcError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class GuardedDesktop(Desktop):
    def __init__(self, env, state, guard):
        super().__init__(env, state=state)
        self.guard = guard

    def observe(self):
        self.guard()
        result = super().observe()
        self.guard()
        return result

    def check_target(self, expected, receipt, require_active=True):
        grant = self.guard()
        if expected.get("class") not in grant["app_classes"]:
            raise Denied("Target app class is not granted")
        return super().check_target(expected, receipt, require_active)

    def before_input(self, target):
        grant = self.guard()
        if target.get("class") not in grant["app_classes"]:
            raise Denied("Target app class is not granted")


def desktop_factory(state, guard):
    # Delayed until an authenticated desktop request. Starting/pinging the
    # service does not query Hyprland or capture anything.
    return GuardedDesktop(session_environment(), state, guard)


class Broker:
    def __init__(self, runtime, factory=desktop_factory):
        self.runtime = private_directory(runtime)
        self.observations = private_directory(self.runtime / "observations")
        self.grants = Grants(self.runtime)
        self.factory = factory
        self.operation = threading.Lock()
        self.audit_lock = threading.Lock()

    def audit(self, method, session, ok):
        # Deliberately exclude tokens, titles, text, screenshots and request bodies.
        entry = {"time": time.time(), "method": method, "session": session, "ok": ok}
        with self.audit_lock:
            path = self.runtime / "audit.jsonl"
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(self.runtime / "audit.previous.jsonl")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(entry) + "\n")

    def cleanup(self):
        # Bound captured data. Losing an old receipt makes its next input fail
        # closed; clients must inspect a new screenshot rather than retry input.
        directories = []
        for session in self.observations.iterdir():
            if not session.is_dir() or session.is_symlink():
                continue
            for item in session.iterdir():
                if item.is_dir() and not item.is_symlink():
                    size = sum(f.stat().st_size for f in item.iterdir() if f.is_file())
                    directories.append((item.stat().st_mtime, size, item))
        total = sum(size for _, size, _ in directories)
        for modified, size, path in sorted(directories):
            if time.time() - modified > 300 or total > 128 * 1024 * 1024:
                shutil.rmtree(path)
                total -= size

    def export(self, value):
        if isinstance(value, list):
            return [self.export(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {k: self.export(v) for k, v in value.items() if k != "screenshot"}
        if "screenshot" in value:
            shot = Path(value["screenshot"]).resolve()
            if not shot.is_relative_to(self.observations.resolve()):
                raise Stop("Screenshot path is outside service storage")
            if shot.stat().st_size > MAX_IMAGE:
                raise Stop("Screenshot exceeds protocol size limit")
            result["image"] = {"mime_type": "image/png",
                               "base64": base64.b64encode(shot.read_bytes()).decode("ascii")}
        return result

    def call(self, method, params):
        if method == "ping":
            if params:
                raise RpcError(-32602, "ping takes no parameters")
            return {"ok": True, "service": "linux-computer-use", "version": __version__,
                    "backend": "hyprland", "desktop_tested": False,
                    "native_openai_integration": False}
        mapping = {"observe": "observe", "focus": "focus", "click": "click", "type_text": "type",
                   "press_key": "press_key"}
        if method not in mapping and method != "session.status":
            raise RpcError(-32601, "Method not supported")
        auth = params.get("auth")
        action = "observe" if method == "session.status" else method
        grant = self.grants.authenticate(auth, action)
        if method == "session.status":
            return {k: grant[k] for k in ("session", "expires", "actions", "app_classes", "label")}
        allowed = {"auth"} if method == "observe" else {"auth", "observation", "window", "reviewed"}
        if method == "click":
            allowed |= {"x", "y"}
        if method == "type_text":
            allowed.add("text")
        if method == "press_key":
            allowed.add("key")
        if set(params) - allowed:
            raise RpcError(-32602, "Unexpected parameters")
        if method == "press_key" and (not isinstance(params.get("key"), str)
                                       or params["key"] not in NAVIGATION_KEYS):
            raise RpcError(-32602, "Unsupported navigation key")
        if not self.operation.acquire(blocking=False):
            raise RpcError(-32002, "Another desktop operation is running; inspect before continuing")
        try:
            self.cleanup()
            state = private_directory(self.observations / grant["session"])
            guard = lambda: self.grants.authenticate(auth, action)
            desktop = self.factory(state, guard)
            with desktop.locked():
                if method == "observe":
                    result = {"ok": True, "observation": desktop.observe()}
                else:
                    obs, window = params.get("observation"), params.get("window")
                    if not isinstance(obs, str) or not isinstance(window, str) or params.get("reviewed") is not True:
                        raise RpcError(-32602, "Fresh observation, window and reviewed=true are required")
                    receipt = desktop.load(obs)
                    target = next((c for c in receipt["clients"] if c.get("address") == window), None)
                    if target is None or target.get("class") not in grant["app_classes"]:
                        raise Denied("Target window app is not granted")
                    if method == "click" and any(type(params.get(k)) is not int for k in ("x", "y")):
                        raise RpcError(-32602, "Click coordinates must be integer screenshot pixels")
                    if method == "type_text" and not isinstance(params.get("text"), str):
                        raise RpcError(-32602, "Text must be a string")
                    arguments = {"reviewed": True, "px": params.get("x"),
                                 "py": params.get("y"), "text": params.get("text")}
                    if method == "press_key":
                        arguments["key"] = params["key"]
                    result = desktop.act(mapping[method], obs, window, **arguments)
                try:
                    guard()
                except Denied as e:
                    if method == "observe":
                        raise
                    return {"ok": False, "session_ended": True,
                            "input_may_have_executed": True, "do_not_retry": True,
                            "characters_submitted": result.get("characters_submitted", 0),
                            "error": str(e)}
                return self.export(result)
        finally:
            self.operation.release()

    def respond(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        method, session, ok = "invalid", None, False
        try:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or type(request_id) not in (str, int):
                raise RpcError(-32600, "Expected one JSON-RPC 2.0 request with an ID")
            method, params = request.get("method"), request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError(-32600, "Invalid method or params")
            auth = params.get("auth")
            if isinstance(auth, dict) and isinstance(auth.get("session"), str) and re.fullmatch(r"[a-f0-9]{32}", auth["session"]):
                session = auth["session"]
            result = self.call(method, params)
            ok = not isinstance(result, dict) or result.get("ok", True)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as e:
            code, message = e.code, str(e)
        except Denied as e:
            code, message = -32001, str(e)
        except (Stop, OSError, ValueError, KeyError) as e:
            code, message = -32003, str(e)
        except Exception:
            code, message = -32603, "Internal service error"
        finally:
            # Bound untrusted method labels and do not let audit errors mask an
            # input result (which would encourage an unsafe retry).
            try:
                known = {"ping", "session.status", "observe", "focus", "click", "type_text", "press_key"}
                self.audit(method if isinstance(method, str) and method in known else "invalid", session, ok)
            except OSError:
                pass
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message, "data": {"do_not_retry_input": True}}}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        line = self.rfile.readline(MAX_REQUEST + 1)
        if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request too large or incomplete"}}
        else:
            try:
                request = json.loads(line)
            except (ValueError, UnicodeError):
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
            else:
                reply = self.server.broker.respond(request)
        try:
            self.wfile.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
        except (OSError, TimeoutError):
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    # Wait for active requests on orderly stop, so a mouse-down can reach its
    # finally/release path before the service exits.
    daemon_threads = False
    block_on_close = True
    request_queue_size = 8

    def __init__(self, path, broker):
        self.broker = broker
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(str(path), Handler, bind_and_activate=False)
        self.server_bind()
        os.chmod(path, 0o600)
        self.server_activate()

    def verify_request(self, request, client_address):
        _, uid, _ = struct.unpack("3i", request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == os.getuid()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def notify_ready():
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect(address)
            notify.sendall(b"READY=1")


def serve(runtime):
    runtime = private_directory(runtime)
    lock_fd = os.open(runtime / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Denied("Service is already running") from e
        path = runtime / "control.sock"
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise Denied("Refusing to replace a non-socket or foreign socket")
            path.unlink()
        try:
            with Server(path, Broker(runtime)) as server:
                def stop(signum, frame):
                    raise SystemExit(0)
                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                notify_ready()
                print(f"linux-computer-use {__version__}: listening on {path}; explicit grants required", flush=True)
                server.serve_forever(poll_interval=0.2)
        finally:
            path.unlink(missing_ok=True)
