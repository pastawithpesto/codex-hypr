#!/usr/bin/env python3
"""Build the private Wayland text driver; no system installation or pip needed."""
from pathlib import Path
import hashlib
import json
import shlex
import subprocess

root = Path(__file__).resolve().parent
build = root / "build/wtype"
build.mkdir(parents=True, exist_ok=True)
output = root / "bin/linux-cua-wtype"
output.parent.mkdir(exist_ok=True)
temporary = output.with_name(output.name + ".new")
temporary.unlink(missing_ok=True)
xml = root / "vendor/wtype/protocol/virtual-keyboard-unstable-v1.xml"
header = build / "virtual-keyboard-unstable-v1-client-protocol.h"
protocol = build / "virtual-keyboard-unstable-v1-protocol.c"
for mode, path in (("client-header", header), ("private-code", protocol)):
    subprocess.run(["wayland-scanner", mode, str(xml), str(path)], check=True)
flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "wayland-client", "xkbcommon"], text=True)
subprocess.run(["cc", "-O2", "-Wall", "-Wextra", '-DVERSION="linux-cua-stepped-0.1.0"',
                "-I" + str(build), str(root / "vendor/wtype/main.c"), str(protocol),
                *shlex.split(flags), "-lrt", "-o", str(temporary)], check=True)
temporary.chmod(0o755)
temporary.replace(output)
print(json.dumps({"driver": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, indent=2))
