import argparse
import json
import os
from pathlib import Path
import sys

from .client import request, save_images
from .desktop import NAVIGATION_KEYS
from .policy import ACTIONS, Denied, Grants, private_directory
from .server import serve


def main():
    parser = argparse.ArgumentParser(description="User-managed Hyprland computer-use service")
    default_runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "linux-computer-use"
    parser.add_argument("--runtime-dir", type=Path, default=default_runtime)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the Unix-socket service; grants required for desktop access")
    commands.add_parser("ping", help="Service health only; does not access the desktop")
    grant = commands.add_parser("authorize", help="Create a local expiring session grant")
    grant.add_argument("--actions", nargs="+", choices=sorted(ACTIONS), default=["observe"])
    grant.add_argument("--app-class", action="append", default=[])
    grant.add_argument("--ttl", type=int, default=300)
    grant.add_argument("--label", default="")
    grant.add_argument("--credentials-out", type=Path, required=True)
    revoke = commands.add_parser("revoke", help="Revoke a local session immediately")
    revoke.add_argument("session")
    for name in ("session.status", "observe", "focus", "click", "type_text", "press_key"):
        command = commands.add_parser(name)
        command.add_argument("--credentials", type=Path, required=True)
        if name not in ("session.status",):
            command.add_argument("--images-dir", type=Path, default=Path.cwd() / "cua-images")
        if name in ("focus", "click", "type_text", "press_key"):
            command.add_argument("--observation", required=True)
            command.add_argument("--window", required=True)
            command.add_argument("--reviewed", action="store_true", help="Confirm the fresh screenshot was inspected")
        if name == "click":
            command.add_argument("--x", type=int, required=True)
            command.add_argument("--y", type=int, required=True)
        if name == "type_text":
            command.description = "Read up to 200 printable characters from stdin; no Enter"
        if name == "press_key":
            command.add_argument("--key", choices=sorted(NAVIGATION_KEYS), required=True)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.runtime_dir)
        return 0
    if args.command in ("authorize", "revoke"):
        private_directory(args.runtime_dir)
        grants = Grants(args.runtime_dir)
        if args.command == "authorize":
            value = grants.create(args.actions, args.app_class, args.ttl, args.credentials_out, args.label)
            value["credential_file"] = str(args.credentials_out.resolve())
        else:
            grants.revoke(args.session)
            value = {"ok": True, "revoked": args.session}
        print(json.dumps(value, indent=2))
        return 0
    params = {}
    if args.command in ("focus", "click", "type_text", "press_key"):
        params.update(observation=args.observation, window=args.window, reviewed=args.reviewed)
    if args.command == "click":
        params.update(x=args.x, y=args.y)
    if args.command == "type_text":
        params["text"] = sys.stdin.read(201)
    if args.command == "press_key":
        params["key"] = args.key
    reply = request(args.runtime_dir, args.command, params, getattr(args, "credentials", None))
    if "result" in reply and hasattr(args, "images_dir"):
        reply["result"] = save_images(reply["result"], args.images_dir)
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 1 if "error" in reply or reply.get("result", {}).get("ok") is False else 0


def entry():
    os.umask(0o077)
    try:
        sys.exit(main())
    except (Denied, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error), "do_not_retry_input": True}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entry()
