#!/usr/bin/env python3
"""Build a source archive from an explicit allowlist; never include credentials."""
import hashlib
import io
import json
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parent
files = [root / name for name in (".gitignore", "README.md", "VERIFICATION.md", "LICENSE", "pyproject.toml",
                                  "install-user-service.py", "build-source.py", "build-driver.py")]
files += sorted((root / "linux_cua").glob("*.py"))
files += sorted((root / "tests").glob("*.py"))
files += sorted((root / "systemd").glob("*.in"))
files += sorted(p for p in (root / "vendor").rglob("*") if p.is_file())
destination = root / "dist"
destination.mkdir(exist_ok=True)
archive = destination / "linux-computer-use-service-0.2.2.tar.gz"
manifest = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
with tarfile.open(archive, "w:gz") as bundle:
    for path in files:
        data = path.read_bytes()
        info = tarfile.TarInfo("linux-computer-use-service-0.2.2/" + str(path.relative_to(root)))
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        bundle.addfile(info, io.BytesIO(data))
(destination / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"archive": str(archive), "files": len(files),
                  "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))
