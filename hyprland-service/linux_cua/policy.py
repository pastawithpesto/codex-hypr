"""Explicit local session grants; RPC clients cannot mint grants."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

ACTIONS = frozenset({"observe", "focus", "click", "type_text", "press_key"})


class Denied(ValueError):
    pass


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Denied(f"Directory must be owned by this user with mode 0700: {path}")
    return path


def read_private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Denied("Credential/policy file must be a private regular file owned by this user")
        if info.st_size > 16384:
            raise Denied("Policy file is too large")
        return json.load(stream)


def write_private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


class Grants:
    def __init__(self, runtime, clock=time.time):
        self.root = private_directory(Path(runtime) / "grants")
        self.clock = clock

    def create(self, actions, app_classes, ttl, credential_path, label=""):
        actions = set(actions)
        app_classes = sorted(set(app_classes))
        if not actions or not actions <= ACTIONS or "observe" not in actions:
            raise Denied("Every grant requires observe; supported actions: " + ", ".join(sorted(ACTIONS)))
        if not isinstance(ttl, int) or not 1 <= ttl <= 3600:
            raise Denied("Grant lifetime must be between 1 and 3600 seconds")
        if any(not isinstance(c, str) or not c or len(c) > 200 or not c.isprintable() or c == "*" for c in app_classes):
            raise Denied("Use exact printable app classes, without wildcard grants")
        if actions - {"observe"} and not app_classes:
            raise Denied("Input grants need at least one exact app class")
        if not isinstance(label, str) or len(label) > 200:
            raise Denied("Label must be at most 200 characters")
        session = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        grant = {"version": 1, "session": session, "created": self.clock(),
                 "expires": self.clock() + ttl, "actions": sorted(actions),
                 "app_classes": app_classes, "label": label,
                 "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        policy_path = self.root / f"{session}.json"
        write_private_json(policy_path, grant)
        try:
            write_private_json(credential_path, {"session": session, "token": token})
        except BaseException:
            policy_path.unlink()
            raise
        return {k: v for k, v in grant.items() if k != "token_sha256"}

    def authenticate(self, auth, action):
        if not isinstance(auth, dict):
            raise Denied("Session credentials required")
        session, token = auth.get("session"), auth.get("token")
        if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32}", session):
            raise Denied("Invalid session credentials")
        if not isinstance(token, str) or len(token) > 128:
            raise Denied("Invalid session credentials")
        try:
            grant = read_private_json(self.root / f"{session}.json")
        except (OSError, ValueError) as e:
            raise Denied("Session unavailable or revoked") from e
        if not hmac.compare_digest(grant["token_sha256"], hashlib.sha256(token.encode()).hexdigest()):
            raise Denied("Invalid session credentials")
        if grant["expires"] <= self.clock():
            raise Denied("Session expired")
        if action not in grant["actions"]:
            raise Denied("Action is not granted")
        return grant

    def revoke(self, session):
        if not re.fullmatch(r"[a-f0-9]{32}", session):
            raise Denied("Invalid session ID")
        (self.root / f"{session}.json").unlink(missing_ok=True)
