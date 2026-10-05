"""LocalStorageAdapter boundary and integrity tests.

The adapter guards immutable source bytes: type allowlist, size limit,
content-addressed keys, atomic first write, and path-traversal refusal.
"""

from __future__ import annotations

import hashlib

import pytest

from app.core.config import get_settings
from app.services.storage import LocalStorageAdapter, StorageError, get_storage

JPEG_BYTES = b"\xff\xd8\xff\xe0synthetic-jpeg-body"


def test_put_bytes_round_trips_and_is_content_addressed(tmp_path) -> None:
    adapter = LocalStorageAdapter(root=tmp_path)
    stored = adapter.put_bytes(JPEG_BYTES, "visit.jpg", "image/jpeg")

    assert stored.sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
    assert stored.key == f"{stored.sha256[:2]}/{stored.sha256[2:4]}/{stored.sha256}.jpg"
    assert stored.byte_size == len(JPEG_BYTES)
    assert stored.mime_type == "image/jpeg"
    assert adapter.read_bytes(stored.key) == JPEG_BYTES
    assert adapter.path_for(stored.key).exists()

    again = adapter.put_bytes(JPEG_BYTES, "visit-copy.jpg", "image/jpeg")
    assert again.key == stored.key


def test_put_bytes_rejects_empty_oversized_and_disallowed_types(
    tmp_path, monkeypatch
) -> None:
    adapter = LocalStorageAdapter(root=tmp_path)

    with pytest.raises(StorageError, match="empty"):
        adapter.put_bytes(b"", "empty.jpg", "image/jpeg")

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 4)
    with pytest.raises(StorageError, match="size limit"):
        adapter.put_bytes(JPEG_BYTES, "big.jpg", "image/jpeg")
    monkeypatch.undo()

    with pytest.raises(StorageError, match="Unsupported document type"):
        adapter.put_bytes(JPEG_BYTES, "evil.exe", None)
    with pytest.raises(StorageError, match="Unsupported document type"):
        adapter.put_bytes(JPEG_BYTES, "notes.txt", "text/plain")


def test_missing_key_raises_file_not_found(tmp_path) -> None:
    adapter = LocalStorageAdapter(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        adapter.path_for("ab/cdef/deadbeef.jpg")


def test_path_traversal_is_refused(tmp_path) -> None:
    adapter = LocalStorageAdapter(root=tmp_path)
    with pytest.raises(StorageError, match="Invalid storage key"):
        adapter.read_bytes("../outside.jpg")
    with pytest.raises(StorageError, match="Invalid storage key"):
        adapter.path_for("..\\outside.jpg")


def test_get_storage_rejects_unsupported_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(get_settings(), "storage_backend", "s3")
    with pytest.raises(StorageError, match="Unsupported storage backend"):
        get_storage()


@pytest.mark.parametrize(
    "key", [r"C:\outside.jpg", "C:/outside.jpg", r"\\server\share\outside.jpg", r"ab\cd\source.jpg"]
)
def test_noncanonical_windows_keys_are_refused_on_every_platform(tmp_path, key) -> None:
    adapter = LocalStorageAdapter(root=tmp_path)
    with pytest.raises(StorageError, match="Invalid storage key"):
        adapter.path_for(key)
