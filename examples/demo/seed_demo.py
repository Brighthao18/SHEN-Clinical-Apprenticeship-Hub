"""Idempotent demo setup through authenticated APIs; stdlib only."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("DEMO_API_URL", "http://api:8000").rstrip("/")
if urllib.parse.urlparse(BASE).hostname not in {"api", "127.0.0.1", "localhost"}:
    raise SystemExit("Refusing non-local demo API")
TOKEN = ""


def request(path: str, data=None, content_type="application/json"):
    headers = {"Content-Type": content_type}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def main() -> None:
    global TOKEN
    TOKEN = request("/api/v1/auth/token", urllib.parse.urlencode({"username": "admin", "password": "shen-demo-only"}).encode(), "application/x-www-form-urlencoded")["access_token"]
    encounters = []
    patient_id = None
    for case in json.loads((HERE / "cases.json").read_text(encoding="utf-8")):
        boundary = "shen-synthetic-demo-boundary"
        text = (HERE / case["ocr"]).read_text(encoding="utf-8")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"mock_ocr_text\"\r\n\r\n{text}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"label\"\r\n\r\n完全合成演示\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{case['file']}\"\r\nContent-Type: image/png\r\n\r\n").encode()
        body += (HERE / case["file"]).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        batch = request("/api/v1/imports", body, "multipart/form-data; boundary=" + boundary)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            batch = request("/api/v1/imports/" + batch["id"])
            item = batch["items"][0]
            if item["status"] in {"ACCEPTED", "UNRESOLVED", "FAILED"}:
                break
            time.sleep(0.5)
        if item["status"] != case["expected"]:
            raise RuntimeError("Unexpected demo terminal: " + item["status"])
        if item["status"] == "ACCEPTED":
            if patient_id is not None and patient_id != item["patient_id"]:
                raise RuntimeError("Synthetic follow-up failed deterministic patient matching")
            patient_id = item["patient_id"]
            encounters.append(item["encounter_id"])
        elif item.get("patient_id") or item.get("encounter_id"):
            raise RuntimeError("Unresolved demo committed clinical rows")
    # These authored mappings belong only to this fictional demonstration.
    # Use the ordinary confirmation API; never promote model suggestions or
    # create hospital dose specifications. Fuling stays deliberately pending.
    mappings = {"白术": ("白术", None), "炒白术": ("白术", "炒制"),
                "丹参": ("丹参", None), "黄芪": ("黄芪", None)}
    entries = request("/api/v1/drug-dictionary")
    for raw_name, (canonical, processing) in mappings.items():
        matching = [entry for entry in entries if entry["candidate_raw_name"] == raw_name]
        if len(matching) != 1:
            raise RuntimeError("Synthetic dictionary candidate is missing or ambiguous")
        entry = matching[0]
        if entry["status"] == "PENDING":
            request(f'/api/v1/drug-dictionary/{entry["id"]}/verify', {
                "canonical_name": canonical, "processing_method": processing,
            })
        elif (entry["status"], entry["canonical_name"], entry["processing_method"]) != (
            "VERIFIED", canonical, processing,
        ):
            raise RuntimeError("Synthetic dictionary mapping was changed; refusing overwrite")
    notes = request(f"/api/v1/patients/{patient_id}/notes?limit=200")
    rows = notes.get("items", []) if isinstance(notes, dict) else notes
    if not rows:
        request(f"/api/v1/patients/{patient_id}/notes", {
            "note_type": "case_record", "title": "合成演示：核对来源与版本",
            "content": "这是从零构造的合成演示作品。两次就诊用于学习核对来源、处方变化与版本保存，不代表诊疗建议。\n未记录的诊断与老师口述保持未记录。",
            "encounter_ids": encounters,
        })
    print(json.dumps({"status": "demo_ready", "accepted": 2, "unresolved": 1, "synthetic_only": True}))


if __name__ == "__main__":
    main()
