import base64
import json
import os
from pathlib import Path
import socket
import stat
import uuid

from .policy import Denied, read_private_json

MAX_RESPONSE = 36 * 1024 * 1024


def request(runtime, method, params, credentials=None, timeout=120):
    path = Path(runtime) / "control.sock"
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Denied("Service socket must be private and owned by this user")
    params = dict(params)
    if credentials:
        params["auth"] = read_private_json(credentials)
    payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
    encoded = json.dumps(payload, ensure_ascii=False).encode() + b"\n"
    if len(encoded) > 65536:
        raise Denied("Request exceeds size limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(encoded)
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_RESPONSE + 1)
    if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
        raise Denied("Invalid or oversized service response; input outcome may be unknown")
    reply = json.loads(line)
    if reply.get("id") != payload["id"]:
        raise Denied("Response ID mismatch; input outcome may be unknown")
    return reply


def save_images(value, directory):
    """Materialize received images in the caller's own writable workspace."""
    directory = Path(directory)
    if isinstance(value, list):
        return [save_images(v, directory) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: save_images(v, directory) for k, v in value.items() if k != "image"}
    if "image" in value:
        image = value["image"]
        if image.get("mime_type") != "image/png":
            raise Denied("Unsupported image type")
        data = base64.b64decode(image["base64"], validate=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise Denied("Invalid PNG response")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"desktop-{uuid.uuid4().hex}.png"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        result["screenshot"] = str(path.resolve())
    return result
