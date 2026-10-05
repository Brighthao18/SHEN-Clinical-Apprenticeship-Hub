"""Host-side wrapper for explicit unresolved-ingestion reprocessing.

Phase 5.2 rule: unresolved historical cases are replayed ONLY through this
explicit command after a parser/policy upgrade — never automatically on
application startup. The wrapper always runs inside the targeted Compose
project's api container because production PostgreSQL stays Docker-internal.

Usage (dry run first, then apply):

    python scripts/reprocess_unresolved.py list --template JNU_LEGACY_PRESCRIPTION_V1
    python scripts/reprocess_unresolved.py reprocess --template JNU_LEGACY_PRESCRIPTION_V1
    python scripts/reprocess_unresolved.py reprocess --template JNU_LEGACY_PRESCRIPTION_V1 --apply

Output contains stable IDs and counts only — no PII, no OCR text.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.production.local"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explicit unresolved-ingestion replay")
    parser.add_argument("command", choices=["list", "reprocess"])
    parser.add_argument("--template", default=None, help="Template filter, e.g. JNU_LEGACY_PRESCRIPTION_V1")
    parser.add_argument("--parser-version", default=None, help="Recorded parser-version filter")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Replay for real; without this flag the command is a dry run",
    )
    parser.add_argument("--project", default="shen-production", help="Compose project name")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.env_file.is_file():
        raise SystemExit(f"Environment file is missing: {args.env_file.name}")
    command = [
        "docker",
        "compose",
        "--project-name",
        args.project,
        "--env-file",
        str(args.env_file),
        "-f",
        str(PROJECT_ROOT / "compose.yaml"),
        "-f",
        str(PROJECT_ROOT / "compose.production.yaml"),
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "app.services.reprocess",
        args.command,
    ]
    if args.template:
        command += ["--template", args.template]
    if args.parser_version:
        command += ["--parser-version", args.parser_version]
    if args.apply:
        command += ["--apply"]
    result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=PROJECT_ROOT)
    if result.stdout.strip():
        # Re-emit container JSON so callers can parse it reliably.
        try:
            payload = json.loads(result.stdout)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        except json.JSONDecodeError:
            print(result.stdout, end="")
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
