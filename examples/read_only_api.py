"""Authenticated read-only example; no saved credentials or identity output."""
from __future__ import annotations

import argparse
import getpass
import json
import urllib.parse
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8100")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.scheme != "http":
        parser.error("Use your own localhost HTTP instance")
    password = getpass.getpass("Local administrator password: ")
    form = urllib.parse.urlencode({"username": "admin", "password": password}).encode()
    login = urllib.request.Request(base + "/api/v1/auth/token", data=form)
    with urllib.request.urlopen(login, timeout=15) as response:
        token = json.load(response)["access_token"]
    offset = 0
    while True:
        request = urllib.request.Request(
            base + f"/api/v1/patients?limit=50&offset={offset}",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            page = json.load(response)
        print(json.dumps({"patient_ids": [item["id"] for item in page["items"]]}))
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            break


if __name__ == "__main__":
    main()
