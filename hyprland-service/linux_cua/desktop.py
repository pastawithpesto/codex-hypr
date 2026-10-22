#!/usr/bin/env python3
"""Explicit, one-operation Wayland helper; invoked through normal task permissions.

This is not native CUA and does not change sandbox or app permissions.
Observations are safety checks, not an authorization or security boundary.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid

MAX_AGE = 90
MAX_TEXT = 200
# Fixed, reviewed navigation operations only. No user-provided modifiers, Lua,
# Enter/Return or application-specific submission shortcuts are accepted.
NAVIGATION_KEYS = {
    "Tab": ("", "Tab"),
    "Shift+Tab": ("SHIFT", "Tab"),
    "Escape": ("", "Escape"),
    "Left": ("", "Left"),
    "Right": ("", "Right"),
    "Up": ("", "Up"),
    "Down": ("", "Down"),
    "Home": ("", "Home"),
    "End": ("", "End"),
    "PageUp": ("", "Page_Up"),
    "PageDown": ("", "Page_Down"),
    "Ctrl+A": ("CTRL", "a"),
    "Backspace": ("", "BackSpace"),
    "Delete": ("", "Delete"),
}
SESSION_KEYS = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE")
# Keep screenshots and receipts in the task workspace so the delegated task can
# inspect them without widening its writable roots.
STATE = Path(__file__).resolve().parent / "state"


class Stop(RuntimeError):
    pass


def execute(argv, *, env=None, text=None):
    try:
        p = subprocess.run(argv, input=text, text=True, capture_output=True,
                           timeout=12, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Stop(f"{Path(argv[0]).name} failed or timed out; do not retry input") from e
    if p.returncode or p.stdout.lstrip().startswith("error:"):
        diagnostic = ""
        if len(argv) == 3 and list(argv[:2]) == ["/usr/bin/hyprctl", "dispatch"]:
            # These are the service's generated dispatch expressions. Preserve
            # only their bounded error reply; never expose other command output.
            detail = (p.stdout.strip() + "\n" + p.stderr.strip()).strip()[:512]
            if detail:
                diagnostic = f"; compositor reply: {detail}"
        raise Stop(f"{Path(argv[0]).name} failed (exit {p.returncode}); "
                   "input may have partially executed; inspect before continuing" + diagnostic)
    return p.stdout


def session_environment(run=execute):
    env = dict(os.environ)
    # Only documented session variables; never import arbitrary user-service env.
    values = run(["/usr/bin/systemctl", "--user", "show-environment"], env=env)
    for line in values.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in SESSION_KEYS:
            env[key] = value
    if any(not env.get(key) for key in SESSION_KEYS):
        raise Stop("Wayland session variables missing; no display fallback attempted")
    return env


def identity(window):
    return {k: window.get(k) for k in ("address", "pid", "class", "initialClass")}


def geometry(window):
    return {k: window.get(k) for k in ("at", "size", "workspace", "monitor", "mapped", "hidden")}


def layout(monitors):
    result = []
    for m in monitors:
        if m.get("disabled"):
            continue
        scale = float(m["scale"])
        if scale <= 0:
            raise Stop("Invalid monitor scale")
        w, h = m["width"], m["height"]
        if int(m.get("transform", 0)) % 2:
            w, h = h, w
        result.append({"name": m["name"], "x": int(m["x"]), "y": int(m["y"]),
                       # Match Hyprland's positive floating-point round-to-nearest.
                       "width": math.floor(w / scale + 0.5),
                       "height": math.floor(h / scale + 0.5),
                       "scale": scale, "transform": m.get("transform", 0)})
    if not result:
        raise Stop("No active monitors")
    return sorted(result, key=lambda m: m["name"])


def bounds(monitors):
    x, y = min(m["x"] for m in monitors), min(m["y"] for m in monitors)
    return [x, y, max(m["x"] + m["width"] for m in monitors) - x,
            max(m["y"] + m["height"] for m in monitors) - y]


def validate_text(text):
    if not text or len(text) > MAX_TEXT or any(not c.isprintable() for c in text):
        raise Stop(f"Text must contain 1–{MAX_TEXT} printable characters; no Enter, tabs or controls")


class Desktop:
    def __init__(self, env, state=STATE, run=execute, clock=time.time):
        self.env, self.state, self.run, self.clock = env, Path(state), run, clock
        self.session = hashlib.sha256(json.dumps(
            {k: env[k] for k in SESSION_KEYS}, sort_keys=True).encode()).hexdigest()

    def hypr(self, kind):
        return json.loads(self.run(["/usr/bin/hyprctl", "-j", kind], env=self.env))

    def dispatch(self, expression):
        return self.run(["/usr/bin/hyprctl", "dispatch", expression], env=self.env)

    def snapshot(self):
        return {"clients": self.hypr("clients"), "active": self.hypr("activewindow"),
                "layout": layout(self.hypr("monitors"))}

    @contextlib.contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        with (self.state / "operation.lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise Stop("Another helper operation is running") from e
            yield

    def observe(self):
        start = self.clock()
        before = self.snapshot()
        token = uuid.uuid4().hex
        directory = self.state / token
        directory.mkdir(mode=0o700)
        shot = directory / "desktop.png"
        x, y, w, h = bounds(before["layout"])
        try:
            self.run(["/usr/bin/grim", "-s", "1", "-g", f"{x},{y} {w}x{h}", str(shot)], env=self.env)
            os.chmod(shot, 0o600)
            data = shot.read_bytes()
            if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (w, h):
                raise Stop("Screenshot dimensions do not match layout; coordinate actions unavailable")
            after = self.snapshot()
            signature = lambda s: (s["layout"], identity(s["active"]),
                sorted([(str(identity(c)), str(geometry(c))) for c in s["clients"]]))
            if signature(before) != signature(after):
                raise Stop("Desktop changed during capture; take a fresh observation")
            receipt = {"schema": 1, "observation": token, "created": start,
                       "session": self.session, "screenshot": str(shot),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "bounds": [x, y, w, h], **after}
            (directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
            os.chmod(directory / "receipt.json", 0o600)
            return receipt
        except BaseException:
            shutil.rmtree(directory)
            raise

    def load(self, token):
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise Stop("Invalid observation ID")
        directory = self.state / token
        receipt = json.loads((directory / "receipt.json").read_text())
        age = self.clock() - receipt["created"]
        if receipt["session"] != self.session or not 0 <= age <= MAX_AGE:
            raise Stop("Observation expired or belongs to another desktop session")
        if (directory / "used").exists():
            raise Stop("Observation already used; inspect a new screenshot")
        shot = directory / "desktop.png"
        if hashlib.sha256(shot.read_bytes()).hexdigest() != receipt["sha256"]:
            raise Stop("Screenshot no longer matches observation")
        return receipt

    def check_target(self, expected, receipt, require_active=True):
        current = self.snapshot()
        if current["layout"] != receipt["layout"]:
            raise Stop("Monitor layout changed")
        clients_signature = lambda clients: sorted(
            json.dumps([identity(c), geometry(c)], sort_keys=True) for c in clients)
        if clients_signature(current["clients"]) != clients_signature(receipt["clients"]):
            raise Stop("Window set or geometry changed; inspect a fresh screenshot")
        match = next((c for c in current["clients"] if identity(c) == identity(expected)), None)
        if not match or not match.get("mapped", True) or match.get("hidden", False):
            raise Stop("Target disappeared, changed identity, or is hidden")
        if geometry(match) != geometry(expected):
            raise Stop("Target moved or changed workspace; inspect a fresh screenshot")
        if require_active and identity(current["active"]) != identity(expected):
            raise Stop("Target lost focus; no further input sent")
        return match

    def before_input(self, target):
        """Host brokers override this to refresh authority after IPC queries."""

    def act(self, kind, token, address, *, reviewed=False, px=None, py=None, text=None, key=None):
        if not reviewed:
            raise Stop("Open the observation screenshot first, then pass --reviewed")
        if not re.fullmatch(r"0x[0-9a-fA-F]+", address):
            raise Stop("Invalid window address")
        receipt = self.load(token)
        target = next((c for c in receipt["clients"] if c.get("address") == address), None)
        if target is None:
            raise Stop("Target was not in this observation")
        if kind == "type":
            validate_text(text)
        if kind == "press_key" and (not isinstance(key, str) or key not in NAVIGATION_KEYS):
            raise Stop("Unsupported navigation key")
        self.check_target(target, receipt, require_active=kind != "focus")
        if kind == "click":
            if px is None or py is None or not (0 <= px < receipt["bounds"][2] and 0 <= py < receipt["bounds"][3]):
                raise Stop("Click must be inside the screenshot")
            x, y = px + receipt["bounds"][0], py + receipt["bounds"][1]
            tx, ty = target["at"]
            tw, th = target["size"]
            if not (tx <= x < tx + tw and ty <= y < ty + th):
                raise Stop("Click is outside the selected window")
        # A failed or interrupted input cannot be silently repeated with this receipt.
        with (self.state / token / "used").open("x") as f:
            f.write(kind)
        sent = 0
        key_pairs = 0
        try:
            if kind == "focus":
                self.before_input(target)
                self.dispatch(f'hl.dsp.focus({{window = "address:{address}"}})')
                if identity(self.hypr("activewindow")) != identity(target):
                    raise Stop("Focus command did not select the requested window")
            elif kind == "click":
                self.before_input(target)
                self.dispatch(f"hl.dsp.cursor.move({{x = {x}, y = {y}}})")
                self.check_target(target, receipt)
                cursor = self.hypr("cursorpos")
                if abs(cursor["x"] - x) > 1 or abs(cursor["y"] - y) > 1:
                    raise Stop("Pointer did not reach selected coordinates")
                try:
                    self.before_input(target)
                    self.dispatch('hl.dsp.send_key_state({mods = "", key = "mouse:272", state = "down"})')
                finally:
                    self.dispatch('hl.dsp.send_key_state({mods = "", key = "mouse:272", state = "up"})')
            elif kind == "type":
                from .writer import write_text
                def check_before_character():
                    self.check_target(target, receipt)
                    self.before_input(target)
                sent = write_text(text, self.env, check_before_character)
                self.check_target(target, receipt)
            elif kind == "press_key":
                from .writer import write_key
                def check_before_key():
                    self.check_target(target, receipt)
                    self.before_input(target)
                key_pairs = write_key(key, self.env, check_before_key)
                self.check_target(target, receipt)
            else:
                raise Stop("Unsupported action")
            result = self.observe()
            return {"ok": True, "action": kind, "characters_submitted": sent,
                    "key_pairs_submitted": key_pairs,
                    "visual_result_requires_review": True, "after": result}
        except Exception as e:
            if kind == "press_key":
                key_pairs = getattr(e, "completed", key_pairs)
            else:
                sent = getattr(e, "completed", sent)
            result = {"ok": False, "action": kind, "characters_submitted": sent,
                      "key_pairs_submitted": key_pairs,
                      "input_may_have_executed": True, "error": str(e), "do_not_retry": True}
            try:
                result["after"] = self.observe()
            except Exception as capture_error:
                result["verification_error"] = str(capture_error)
            return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check installed prerequisites only; no desktop access")
    sub.add_parser("observe", help="Capture desktop and fresh window identities")
    for command in ("focus", "click", "type"):
        action = sub.add_parser(command)
        action.add_argument("--observation", required=True)
        action.add_argument("--window", required=True)
        action.add_argument("--reviewed", action="store_true", help="Confirm screenshot was inspected")
        if command == "click":
            action.add_argument("--x", required=True, type=int, help="Screenshot pixel x")
            action.add_argument("--y", required=True, type=int, help="Screenshot pixel y")
        if command == "type":
            action.description = "Read bounded printable text from stdin; never sends Enter"
    args = p.parse_args()
    if args.command == "doctor":
        paths = ["/usr/bin/hyprctl", "/usr/bin/grim", "/usr/bin/wtype", "/usr/bin/systemctl"]
        available = {path: os.access(path, os.X_OK) for path in paths}
        print(json.dumps({"ok": all(available.values()), "executables": available,
                          "desktop_tested": False, "native_cua": False}, indent=2))
        return 0 if all(available.values()) else 1
    desktop = Desktop(session_environment())
    with desktop.locked():
        if args.command == "observe":
            result = {"ok": True, "observation": desktop.observe()}
        else:
            text = sys.stdin.read(MAX_TEXT + 1) if args.command == "type" else None
            result = desktop.act(args.command, args.observation, args.window,
                                 reviewed=args.reviewed, px=getattr(args, "x", None),
                                 py=getattr(args, "y", None), text=text)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1


if __name__ == "__main__":
    os.umask(0o077)
    try:
        sys.exit(main())
    except (Stop, OSError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error), "do_not_retry_input": True}))
        sys.exit(1)
