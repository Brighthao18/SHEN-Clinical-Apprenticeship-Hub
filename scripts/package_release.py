"""Package a clean public commit, not the surrounding working directory."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from check_public_tree import ROOT, check, tracked_paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "work/release")
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT):
        parser.error("Commit the reviewed public changes before packaging")
    paths = tracked_paths()
    result = check(paths)
    if result["problems"]:
        print(json.dumps({"problems": result["problems"]}, ensure_ascii=False))
        return 1
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = subprocess.check_output(["git", "archive", "--format=zip", "HEAD"], cwd=ROOT)
    archive = output / "shen-0.2.0-rc.1-source.zip"
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        inventory = [{"path": item.filename, "bytes": len(original.read(item)), "sha256": hashlib.sha256(original.read(item)).hexdigest()} for item in original.infolist() if not item.is_dir()]
        assert sorted(row["path"] for row in inventory) == paths
        manifest = {"version": "0.2.0-rc.1", "commit": commit, "files": inventory, "manifest_self_excluded": True}
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        for item in original.infolist():
            target.writestr(item, original.read(item))
        epoch = int(subprocess.check_output(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT))
        manifest_item = zipfile.ZipInfo("release-manifest.json", datetime.fromtimestamp(epoch, UTC).timetuple()[:6])
        manifest_item.compress_type = zipfile.ZIP_DEFLATED
        target.writestr(manifest_item, manifest_bytes)
    (output / "release-manifest.json").write_bytes(manifest_bytes)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"commit": commit, "files": len(paths), "archive": archive.name, "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
