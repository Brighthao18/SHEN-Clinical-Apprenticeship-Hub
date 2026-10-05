"""Check only explicit Git-tracked public files; never walk private directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"private_data", "private_tools", "Codex", "case1-5", "new cases", "node_modules", ".next", ".logs", ".locks", ".git"}
TOP = {".dockerignore", ".gitignore", ".gitattributes", ".editorconfig", ".env.example", "AGENTS.md", "LICENSE", "NOTICE", "README.md", "README.en.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "compose.yaml", "compose.production.yaml", "compose.demo.yaml", "release-allowlist.txt"}
DOCS = {"getting-started.md", "deployment.md", "privacy-boundaries.md", "architecture.md", "supported-scope.md", "provider-adapters.md", "development.md", "OPEN_DECISIONS.md", "release-checklist.md", "validation.md", "dependency-security.md"}
SECRETS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
]


def permitted(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if not parts or any(p in FORBIDDEN or p in {"..", "."} for p in parts):
        return False
    if name in TOP:
        return True
    if name in {"work/.gitkeep", "outputs/.gitkeep", "data/storage/.gitkeep"}:
        return True
    if parts[0] == "docs":
        return len(parts) == 2 and parts[1] in DOCS
    if name.endswith("package-lock.json") or (parts[-1].startswith(".env") and parts[-1] != ".env.example"):
        return False
    if any(p.endswith((".egg-info", ".db", ".sqlite3", ".dump", ".backup", ".pem", ".pfx")) for p in parts):
        return False
    return name.startswith(("apps/api/app/", "apps/api/tests/", "apps/api/alembic/", "apps/web/", "scripts/", "examples/", "licenses/", ".github/")) or name in {"apps/api/Dockerfile", "apps/api/.dockerignore", "apps/api/alembic.ini", "apps/api/pyproject.toml", "apps/api/requirements.lock", "apps/api/requirements-dev.lock", "apps/api/requirements-build.in", "apps/api/LICENSE", "apps/api/NOTICE"}


def tracked_paths() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return sorted(p.decode("utf-8") for p in output.split(b"\0") if p)


def check(paths: list[str], *, require_allowlist: bool = True) -> dict:
    problems = []
    if require_allowlist:
        expected = sorted((ROOT / "release-allowlist.txt").read_text(encoding="utf-8").splitlines())
        if paths != expected:
            problems.append({"path": "release-allowlist.txt", "reason": "does not match the tracked tree"})
    root = ROOT.resolve()
    inventory = []
    for name in paths:
        if not permitted(name):
            problems.append({"path": name, "reason": "outside public boundary"})
            continue
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            problems.append({"path": name, "reason": "missing, symlink or outside root"})
            continue
        data = path.read_bytes()
        inventory.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if path.suffix.lower() in {".png", ".ico"}:
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append({"path": name, "reason": "not UTF-8 text"})
            continue
        for pattern in SECRETS:
            if pattern.search(content):
                problems.append({"path": name, "reason": "private key/token pattern; value suppressed"})
        if re.search(r"[CD]:[/\\]Users[/\\]fuhao|D:[/\\]Coding", content):
            problems.append({"path": name, "reason": "personal workstation path"})
        if name == "licenses/dependency-inventory.json":
            document = json.loads(content)
            for relative, digest in document["locks"].items():
                if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
                    problems.append({"path": relative, "reason": "dependency inventory lock hash mismatch"})
            for component in document["components"]:
                for license_file in component["license_files"]:
                    relative = license_file["path"]
                    file = ROOT / relative
                    if not relative.startswith("licenses/") or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != license_file["sha256"]:
                        problems.append({"path": relative, "reason": "retained license hash mismatch"})
        if path.suffix == ".md" and not name.startswith("licenses/"):
            for target in re.findall(r"\]\(<?([^)>]+)>?\)", content):
                if ":" in target or target.startswith(("#", "/")):
                    continue
                local = target.split("#", 1)[0]
                if local and not (path.parent / local).exists():
                    problems.append({"path": name, "reason": "broken local document link"})
    return {"files": len(inventory), "problems": problems, "inventory": inventory,
            "scope": "tracked allowlist, known secret patterns and local links; not a proof that arbitrary sensitive text is absent"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=Path, help="explicit candidate path list; no directory scan")
    args = parser.parse_args()
    paths = sorted(args.paths.read_text(encoding="utf-8").splitlines()) if args.paths else tracked_paths()
    result = check(paths, require_allowlist=not args.paths)
    print(json.dumps({k: v for k, v in result.items() if k != "inventory"}, ensure_ascii=False))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
