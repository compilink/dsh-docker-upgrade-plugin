"""Verify the committed, versioned DSH tarball against the release source tree."""
import hashlib
import json
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "dsh-bundle-docker-upgrade"
FILES = {"package.json", "index.js", "client.js", "cordis.patch.yml", "README.md"}


def verify():
    metadata = json.loads((ROOT / "package.json").read_bytes())
    version = metadata["version"]
    if (metadata["name"] != PACKAGE or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or metadata.get("dependencies") or set(metadata.get("scripts", {})) - {"test"}):
        raise ValueError("Unexpected plugin manifest")
    archive = ROOT / "releases" / f"{PACKAGE}-{version}.tgz"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive.with_suffix(".tgz.sha256").read_text() != f"{digest}  {archive.name}\n":
        raise ValueError("Plugin archive checksum mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        expected = {"package/" + name for name in FILES}
        if len(members) != len(expected) or {m.name for m in members} != expected:
            raise ValueError("Unexpected plugin archive contents")
        for member in members:
            source = ROOT / Path(member.name).name
            if not member.isfile() or member.mode & 0o7000 or tar.extractfile(member).read() != source.read_bytes():
                raise ValueError(f"Source mismatch: {member.name}")
    print(f"Verified {archive.name} ({len(FILES)} files, SHA-256 {digest})")


if __name__ == "__main__":
    verify()
