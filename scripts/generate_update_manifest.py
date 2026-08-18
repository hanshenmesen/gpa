"""Generate a checksummed public update manifest from release artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from pathlib import Path

from gpa.update import release_key


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(tag: str, artifacts: list[Path], *, repository: str, architecture: str) -> dict:
    release = str(tag).removeprefix("v")
    release_key(release)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    assets = []
    for path in artifacts:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"artifact is not a regular file: {path}")
        assets.append({
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "download_url": f"https://github.com/{repository}/releases/download/{tag}/{path.name}",
        })
    return {
        "schema": "gpa.desktop-release/v1",
        "release": release,
        "tag": tag,
        "architecture": architecture,
        "prerelease": "-" in release,
        "release_page": f"https://github.com/{repository}/releases/tag/{tag}",
        "assets": assets,
        "installation": "manual_preview" if "-preview." in release else "signed_release",
        "desktop_authority_changed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--repository", default="hanshenmesen/gpa")
    parser.add_argument("--architecture", default=platform.machine() or "unknown")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(
        args.tag,
        args.artifacts,
        repository=args.repository,
        architecture=args.architecture,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
