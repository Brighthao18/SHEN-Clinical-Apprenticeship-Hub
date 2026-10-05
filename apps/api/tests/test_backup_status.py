"""Backup status surface tests.

备份成功与恢复验证成功必须分开报告；端点只读运维脚本产物，
未配置/目录不可访问时显式说明，绝不伪装成“正常”。
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _write_backup(root, backup_id: str, created_at: str, *, drilled: str | None = None):
    backup_dir = root / backup_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.1",
        "backup_id": backup_id,
        "created_at": created_at,
        "tier": "standard",
        "environment": "production",
        "contains_clinical_data": True,
        "document_file_count": 3,
        "files": [],
    }
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    if drilled is not None:
        (backup_dir / "restore_verification.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "verified_at": drilled,
                    "environment": "production",
                    "status": "production_isolated_restore_drill_passed",
                    "backup_id": backup_id,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def _write_sidecar(
    root,
    backup_id: str,
    name: str,
    *,
    status: str,
    verified_at: str,
    run_id: str | None = None,
    stage: str | None = None,
    error_type: str | None = None,
) -> None:
    record_dir = root / ".restore-verifications" / backup_id
    record_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        (root / backup_id / "backup_manifest.json").read_bytes()
    ).hexdigest()
    normalized = (
        f"production_isolated_restore_drill_{status}"
        if status in ("started", "passed", "failed")
        else status
    )
    record = {
        "schema_version": "1.2" if run_id else "1.1",
        "verified_at": verified_at,
        "environment": "production",
        "status": normalized,
        "backup_id": backup_id,
        "manifest_sha256": digest,
    }
    if run_id is not None:
        record["run_id"] = run_id
    if stage is not None:
        record["stage"] = stage
    if error_type is not None:
        record["error_type"] = error_type
    (record_dir / name).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _patch_root(monkeypatch, root):
    monkeypatch.setattr(
        "app.api.routes.backup_status.get_settings",
        lambda: SimpleNamespace(backup_manifest_dir=str(root)),
    )


def test_backup_status_unconfigured_reports_honestly(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.backup_status.get_settings",
        lambda: SimpleNamespace(backup_manifest_dir=""),
    )
    response = client.get("/api/v1/backup-status", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["latest_backup"] is None
    assert body["latest_restore_verification"] is None


def test_backup_status_separates_backup_and_restore_verification(
    client: TestClient, auth_headers, db_session, monkeypatch, tmp_path
) -> None:
    # 一次已演练恢复的备份 + 一次从未演练的备份。
    _write_backup(
        tmp_path,
        "bk-drilled",
        "2026-09-01T08:00:00+00:00",
        drilled="2026-09-02T09:00:00+00:00",
    )
    _write_backup(tmp_path, "bk-plain", "2026-09-05T08:00:00+00:00")
    _patch_root(monkeypatch, tmp_path)

    response = client.get("/api/v1/backup-status", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["directory_accessible"] is True
    assert len(body["backups"]) == 2

    # 最近备份是最新的 bk-plain（从未恢复验证）。
    assert body["latest_backup"]["backup_id"] == "bk-plain"
    # 最近恢复验证仍然指向更早但真正演练成功的 bk-drilled。
    assert body["latest_restore_verification"]["backup_id"] == "bk-drilled"

    # 未演练的备份必须显式显示 restore 状态为空，而不是复用备份成功。
    plain = next(item for item in body["backups"] if item["backup_id"] == "bk-plain")
    assert plain["restore_status"] is None
    assert plain["restore_run"] is None


def test_backup_status_missing_directory_is_explicit(
    client: TestClient, auth_headers, monkeypatch, tmp_path
) -> None:
    _patch_root(monkeypatch, tmp_path / "does-not-exist")
    response = client.get("/api/v1/backup-status", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["directory_accessible"] is False
    assert body["latest_backup"] is None


def test_backup_status_survives_corrupt_manifest(
    client: TestClient, auth_headers, monkeypatch, tmp_path
) -> None:
    broken = tmp_path / "bk-broken"
    broken.mkdir()
    (broken / "backup_manifest.json").write_text("{not json", encoding="utf-8")
    _write_backup(tmp_path, "bk-good", "2026-09-03T08:00:00+00:00")
    _patch_root(monkeypatch, tmp_path)

    response = client.get("/api/v1/backup-status", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["backup_id"] for item in body["backups"]] == ["bk-good"]


def test_backup_status_sorts_before_limiting_and_uses_restore_time(
    client, auth_headers, monkeypatch, tmp_path
):
    for day in range(1, 26):
        _write_backup(tmp_path, f"bk-{day:02}", f"2026-09-{day:02}T08:00:00Z")
    _write_backup(tmp_path, "bk-01", "2026-09-01T08:00:00Z", drilled="2026-09-28T00:00:00Z")
    _write_backup(tmp_path, "bk-25", "2026-09-25T08:00:00Z", drilled="2026-09-26T00:00:00Z")
    _patch_root(monkeypatch, tmp_path)
    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    assert len(body["backups"]) == 20
    assert body["latest_backup"]["backup_id"] == "bk-25"
    assert body["latest_restore_verification"]["backup_id"] == "bk-01"


def test_backup_status_partial_scan_does_not_claim_latest(
    client, auth_headers, monkeypatch, tmp_path
):
    _write_backup(tmp_path, "bk-one", "2026-09-01T08:00:00Z")
    _write_backup(tmp_path, "bk-two", "2026-09-02T08:00:00Z")
    monkeypatch.setattr("app.api.routes.backup_status._MAX_SCAN_ENTRIES", 1)
    _patch_root(monkeypatch, tmp_path)
    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    assert body["scan_complete"] is False
    assert body["latest_backup"] is None
    assert body["latest_restore_verification"] is None


def test_backup_sidecar_preserves_success_and_rejects_wrong_hash(
    client, auth_headers, monkeypatch, tmp_path
):
    _write_backup(tmp_path, "bk-test", "2026-09-01T08:00:00Z")
    records = tmp_path / ".restore-verifications" / "bk-test"
    records.mkdir(parents=True)
    digest = hashlib.sha256(
        (tmp_path / "bk-test" / "backup_manifest.json").read_bytes()
    ).hexdigest()
    for day, status, sha in ((2, "passed", digest), (3, "failed", digest), (4, "passed", "wrong")):
        (records / f"{day}.json").write_text(
            json.dumps(
                {
                    "backup_id": "bk-test",
                    "environment": "production",
                    "manifest_sha256": sha,
                    "status": f"production_isolated_restore_drill_{status}",
                    "verified_at": f"2026-09-0{day}T08:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    # JSON objects are untrusted: a list in a status field must not crash.
    (records / "invalid.json").write_text(
        json.dumps(
            {
                "backup_id": "bk-test",
                "environment": "production",
                "manifest_sha256": digest,
                "status": [],
                "verified_at": "2026-09-05T08:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    _patch_root(monkeypatch, tmp_path)
    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    # 最近成功是 9/2 那次（9/3 的失败不掩盖它）；时间统一为 ISO 形式。
    assert body["latest_restore_verification"]["verified_at"] == "2026-09-02T08:00:00+00:00"
    assert body["backups"][0]["restore_status"] == "failed"


def test_backup_status_groups_schema_v12_records_into_one_run(
    client: TestClient, auth_headers, monkeypatch, tmp_path
) -> None:
    """1.2 记录按 run_id 折叠成一次运行：开始/阶段/结束同属一次演练。"""

    _write_backup(tmp_path, "bk-run", "2026-09-01T08:00:00Z")
    _write_sidecar(
        tmp_path, "bk-run", "1.json",
        status="started", verified_at="2026-09-05T08:00:00Z", run_id="run-abc", stage="start",
    )
    _write_sidecar(
        tmp_path, "bk-run", "2.json",
        status="started", verified_at="2026-09-05T08:05:00Z", run_id="run-abc",
        stage="restore_database",
    )
    _write_sidecar(
        tmp_path, "bk-run", "3.json",
        status="passed", verified_at="2026-09-05T09:00:00Z", run_id="run-abc", stage="end",
    )
    _patch_root(monkeypatch, tmp_path)

    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    backup = body["backups"][0]
    assert backup["restore_status"] == "passed"
    run = backup["restore_run"]
    assert run["run_id"] == "run-abc"
    assert run["started_at"] == "2026-09-05T08:00:00+00:00"
    assert run["ended_at"] == "2026-09-05T09:00:00+00:00"
    attempt = body["latest_restore_attempt"]
    assert attempt["backup_id"] == "bk-run"
    assert attempt["run_id"] == "run-abc"
    assert attempt["status"] == "passed"
    assert body["latest_restore_verification"]["backup_id"] == "bk-run"


def test_backup_status_started_without_end_is_pending_never_judged(
    client: TestClient, auth_headers, monkeypatch, tmp_path
) -> None:
    """只有开始/阶段记录的运行是“结果待核对”，不判成败、不冒充成功。"""

    _write_backup(tmp_path, "bk-pending", "2026-09-01T08:00:00Z")
    _write_sidecar(
        tmp_path, "bk-pending", "1.json",
        status="started", verified_at="2026-09-06T08:00:00Z", run_id="run-pending", stage="start",
    )
    _write_sidecar(
        tmp_path, "bk-pending", "2.json",
        status="started", verified_at="2026-09-06T08:10:00Z", run_id="run-pending",
        stage="restore_database",
    )
    _patch_root(monkeypatch, tmp_path)

    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    backup = body["backups"][0]
    assert backup["restore_status"] == "pending"
    assert backup["restore_run"]["last_stage"] == "restore_database"
    assert backup["restore_run"]["ended_at"] is None
    attempt = body["latest_restore_attempt"]
    assert attempt["status"] == "pending"
    assert attempt["run_id"] == "run-pending"
    # 没有任何已通过的运行：最近成功必须如实为空。
    assert body["latest_restore_verification"] is None


def test_backup_status_latest_attempt_and_latest_success_are_independent(
    client: TestClient, auth_headers, monkeypatch, tmp_path
) -> None:
    """最近尝试（待核对）更新，也不能掩盖更早一次真正的成功。"""

    _write_backup(tmp_path, "bk-ok", "2026-09-01T08:00:00Z")
    _write_sidecar(
        tmp_path, "bk-ok", "1.json",
        status="started", verified_at="2026-09-02T08:00:00Z", run_id="run-ok", stage="start",
    )
    _write_sidecar(
        tmp_path, "bk-ok", "2.json",
        status="passed", verified_at="2026-09-02T09:00:00Z", run_id="run-ok", stage="end",
    )
    _write_backup(tmp_path, "bk-new", "2026-09-05T08:00:00Z")
    _write_sidecar(
        tmp_path, "bk-new", "1.json",
        status="started", verified_at="2026-09-06T08:00:00Z", run_id="run-hang", stage="start",
    )
    _write_sidecar(
        tmp_path, "bk-new", "2.json",
        status="started", verified_at="2026-09-06T08:20:00Z", run_id="run-hang",
        stage="container_checks",
    )
    _patch_root(monkeypatch, tmp_path)

    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    attempt = body["latest_restore_attempt"]
    assert attempt["backup_id"] == "bk-new"
    assert attempt["status"] == "pending"
    assert attempt["last_stage"] == "container_checks"
    # 最近成功仍然指向更早但真正通过的那次演练。
    assert body["latest_restore_verification"]["backup_id"] == "bk-ok"
    failed_or_pending = {
        item["backup_id"]: item["restore_status"] for item in body["backups"]
    }
    assert failed_or_pending["bk-new"] == "pending"
    assert failed_or_pending["bk-ok"] == "passed"


def test_backup_status_legacy_started_without_run_id_is_pending(
    client: TestClient, auth_headers, monkeypatch, tmp_path
) -> None:
    """旧格式（无 run_id）的 started 记录同样如实呈现为待核对。"""

    _write_backup(tmp_path, "bk-legacy", "2026-09-01T08:00:00Z")
    _write_sidecar(
        tmp_path, "bk-legacy", "1.json",
        status="started", verified_at="2026-09-03T08:00:00Z", stage="restore_database",
    )
    _patch_root(monkeypatch, tmp_path)

    body = client.get("/api/v1/backup-status", headers=auth_headers).json()
    backup = body["backups"][0]
    assert backup["restore_status"] == "pending"
    assert backup["restore_run"]["run_id"] is None
    assert backup["restore_run"]["started_at"] == "2026-09-03T08:00:00+00:00"
    assert body["latest_restore_attempt"]["status"] == "pending"
    assert body["latest_restore_verification"] is None
