"""Release-only inventory from exact public dependency versions, never private files."""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import importlib.metadata as metadata
import io
import json
import re
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "work/license-cache"
DEST = ROOT / "licenses"
CACHE.mkdir(parents=True, exist_ok=True)
DEST.mkdir(exist_ok=True)
LICENSE_FILE = re.compile(r"(?i)(^|/)(licen[cs]e[^/]*|copying[^/]*|notice[^/]*|copyright[^/]*)$")


def fetch(url: str) -> bytes:
    key = hashlib.sha256(url.encode()).hexdigest()
    path = CACHE / key
    if path.exists():
        return path.read_bytes()
    for attempt in range(3):
        try:
            data = urllib.request.urlopen(url, timeout=35).read()
            path.write_bytes(data)
            return data
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    raise AssertionError


def normalized_license(value, info: dict) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    if isinstance(value, str) and value.strip() not in {"", "UNKNOWN"}:
        return value.strip() if len(value) < 180 else "LicenseRef-package-text"
    classifiers = info.get("classifiers", [])
    for suffix, expression in [
        ("MIT License", "MIT"), ("Apache Software License", "Apache-2.0"),
        ("BSD License", "BSD-3-Clause"), ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
        ("ISC License (ISCL)", "ISC"), ("Python Software Foundation License", "PSF-2.0"),
    ]:
        if any(v.endswith(" :: " + suffix) for v in classifiers):
            return expression
    return "UNKNOWN"


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", value)


def write_licenses(ecosystem: str, name: str, version: str, files: dict[str, bytes]):
    directory = DEST / ecosystem / (safe_name(name) + "-" + version)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (label, content) in enumerate(sorted(files.items())):
        filename = f"{index:02d}-" + safe_name(label)[-120:]
        path = directory / filename
        path.write_bytes(content)
        paths.append({"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(content).hexdigest()})
    return paths


def npm_package(item):
    name, version, resolution = item
    url = "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@") + "/" + version
    data = json.loads(fetch(url))
    dist = data["dist"]
    archive = fetch(dist["tarball"])
    if dist.get("integrity", "").startswith("sha512-"):
        assert base64.b64encode(hashlib.sha512(archive).digest()).decode() == dist["integrity"][7:]
    else:
        assert hashlib.sha1(archive).hexdigest() == dist["shasum"]
    if resolution.get("integrity"):
        kind, digest = resolution["integrity"].split("-", 1)
        assert base64.b64encode(hashlib.new(kind, archive).digest()).decode() == digest
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for entry in tar:
            if entry.isfile() and entry.size < 2_000_000 and LICENSE_FILE.search(entry.name):
                files[entry.name.removeprefix("package/")] = tar.extractfile(entry).read()
    return {"ecosystem": "npm", "name": name, "version": version,
            "license": normalized_license(data.get("license"), data), "metadata_source": url,
            "artifact_sha256": hashlib.sha256(archive).hexdigest(), "lock_integrity": resolution.get("integrity"),
            "license_files": write_licenses("npm", name, version, files), "purl": f"pkg:npm/{name}@{version}"}


def python_package(item):
    name, version = item
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    data = json.loads(fetch(url))
    info = data["info"]
    files = {}
    artifact_digest = None
    try:
        installed = metadata.distribution(name)
        assert installed.version == version
        for path in installed.files or []:
            if LICENSE_FILE.search(str(path)) and path.suffix not in {".py", ".pyc"}:
                real = installed.locate_file(path)
                if real.is_file() and real.stat().st_size < 2_000_000:
                    files[str(path)] = real.read_bytes()
    except (metadata.PackageNotFoundError, AssertionError):
        pass
    if not files:
        candidates = sorted(data["urls"], key=lambda a: (a["packagetype"] != "bdist_wheel", a["size"]))
        selected = candidates[0]
        archive = fetch(selected["url"])
        artifact_digest = hashlib.sha256(archive).hexdigest()
        assert artifact_digest == selected["digests"]["sha256"]
        if selected["filename"].endswith(".whl"):
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                for entry in z.infolist():
                    if not entry.is_dir() and entry.file_size < 2_000_000 and LICENSE_FILE.search(entry.filename):
                        files[entry.filename] = z.read(entry)
        else:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                for entry in tar:
                    if entry.isfile() and entry.size < 2_000_000 and LICENSE_FILE.search(entry.name):
                        files[entry.name] = tar.extractfile(entry).read()
    return {"ecosystem": "pypi", "name": name, "version": version,
            "license": normalized_license(info.get("license_expression") or info.get("license"), info),
            "metadata_source": url, "artifact_sha256": artifact_digest,
            "license_files": write_licenses("pypi", name, version, files), "purl": f"pkg:pypi/{name.lower()}@{version}"}


def main():
    previous_path = DEST / "dependency-inventory.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    prior = {(r["ecosystem"], r["name"], r["version"]): r for r in previous.get("components", [])}
    lock = yaml.safe_load((ROOT / "pnpm-lock.yaml").read_text())
    npm = [(name.rsplit("@", 1)[0], name.rsplit("@", 1)[1], value.get("resolution", {})) for name, value in lock["packages"].items()]
    python = sorted(set(re.findall(r"(?m)^([a-zA-Z0-9_.-]+)==([^ ;\\]+)", (ROOT / "apps/api/requirements-dev.lock").read_text())))
    rows, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        tasks = {pool.submit(npm_package, item): item[:2] for item in npm}
        tasks.update({pool.submit(python_package, item): item for item in python})
        for future in concurrent.futures.as_completed(tasks):
            try:
                rows.append(future.result())
            except Exception as exc:
                failures.append({"component": tasks[future], "error": type(exc).__name__})
    rows.sort(key=lambda row: (row["ecosystem"], row["name"], row["version"]))
    for row in rows:
        old = prior.get((row["ecosystem"], row["name"], row["version"]))
        if old and not row["license_files"]:
            retained = old.get("license_files", [])
            if retained and all((ROOT / f["path"]).is_file() and hashlib.sha256((ROOT / f["path"]).read_bytes()).hexdigest() == f["sha256"] for f in retained):
                row["license_files"] = retained
                for key in ("license_source", "license_origin"):
                    if key in old:
                        row[key] = old[key]
    document = {"version": "0.2.0-rc.1", "generated_date": "2026-10-05", "scope": "Python runtime/dev and all pnpm lock packages, including platform-optional packages", "locks": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ["pnpm-lock.yaml", "apps/api/requirements.lock", "apps/api/requirements-dev.lock"]}, "components": rows, "unresolved": failures}
    (DEST / "dependency-inventory.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    components = []
    for row in rows:
        component = {"type": "library", "name": row["name"], "version": row["version"], "bom-ref": row["purl"], "purl": row["purl"], "licenses": [{"license": {"name": row["license"]}}], "externalReferences": [{"type": "distribution", "url": row["metadata_source"]}]}
        if row["artifact_sha256"]:
            component["hashes"] = [{"alg": "SHA-256", "content": row["artifact_sha256"]}]
        components.append(component)
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "metadata": {"component": {"type": "application", "name": "shen", "version": "0.2.0-rc.1"}}, "components": components}
    (DEST / "sbom.cdx.json").write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    missing = [(row["name"], row["version"], row["license"]) for row in rows if row["license"] == "UNKNOWN" or not row["license_files"]]
    print(json.dumps({"components": len(rows), "failed": failures, "missing_license": missing}))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
