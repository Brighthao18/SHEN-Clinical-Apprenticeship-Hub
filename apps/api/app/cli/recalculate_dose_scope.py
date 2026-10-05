from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict

from app.db.session import SessionLocal
from app.services.recalculation import DoseScopeRecalculationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview, apply, or roll back the frozen JNU per-decoction dose-scope rule."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preview")

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--confirm-jnu-rule", action="store_true", required=True)

    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--correlation-id", type=uuid.UUID, required=True)
    rollback_parser.add_argument("--confirm-rollback", action="store_true", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with SessionLocal() as session:
        service = DoseScopeRecalculationService(session)
        if args.command == "preview":
            result = service.preview()
        elif args.command == "apply":
            result = service.apply()
        else:
            result = service.rollback(args.correlation_id)
    payload = asdict(result)
    if payload["correlation_id"] is not None:
        payload["correlation_id"] = str(payload["correlation_id"])
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
