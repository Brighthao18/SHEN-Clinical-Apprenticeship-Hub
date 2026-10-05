"""Project-local launcher. Never loads a different checkout's secrets."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="SHEN demo and local production launcher")
    parser.add_argument("action", choices=["demo", "demo-stop", "doctor", "setup", "init", "start", "stop", "test-api"])
    action = parser.parse_args().action
    venv_python = ROOT / "work/venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if action == "doctor":
        print(f"Python {sys.version.split()[0]}; checkout: {ROOT}")
        return subprocess.call(["docker", "compose", "version"], cwd=ROOT)
    if action == "setup":
        if not venv_python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(ROOT / "work/venv")], check=True)
        subprocess.run([str(venv_python), "-m", "pip", "install", "--require-hashes", "-r", "apps/api/requirements-dev.lock"], cwd=ROOT, check=True)
        return subprocess.call([str(venv_python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "apps/api[dev]"], cwd=ROOT)
    if action in {"init", "test-api"}:
        if not venv_python.exists():
            parser.error("Run setup first to create this checkout's virtual environment")
        if action == "test-api":
            return subprocess.call([str(venv_python), "-m", "pytest", "tests"], cwd=ROOT / "apps/api")
        return subprocess.call([str(venv_python), "scripts/bootstrap_production.py", "init"], cwd=ROOT)
    if action.startswith("demo"):
        args = ["docker", "compose", "--env-file", "examples/demo/demo.env", "-f", "compose.demo.yaml"]
        args += ["up", "--build", "-d", "--wait", "--wait-timeout", "180"] if action == "demo" else ["stop"]
    else:
        if not venv_python.exists():
            parser.error("Run setup first to create this checkout's virtual environment")
        if not (ROOT / ".env.production.local").is_file():
            parser.error("Run init first; production secrets must be created on this machine")
        subprocess.run([str(venv_python), "scripts/bootstrap_production.py", "validate"], cwd=ROOT, check=True)
        args = ["docker", "compose", "--project-name", "shen-production", "--env-file", ".env.production.local", "-f", "compose.yaml", "-f", "compose.production.yaml"]
        args += ["up", "-d", "--build", "--wait", "--wait-timeout", "180"] if action == "start" else ["stop"]
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Operation failed ({type(exc).__name__}); see the preceding safe diagnostic.", file=sys.stderr)
        raise SystemExit(1) from None
