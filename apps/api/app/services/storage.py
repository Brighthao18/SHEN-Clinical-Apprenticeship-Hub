from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.core.config import get_settings

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/jp2",
    "image/png",
    "image/webp",
}
ALLOWED_SUFFIXES = {".pdf", ".bmp", ".gif", ".jpeg", ".jpg", ".jp2", ".png", ".webp"}


class StorageError(ValueError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    byte_size: int
    mime_type: str


class LocalStorageAdapter:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or get_settings().storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, filename: str, mime_type: str | None) -> StoredObject:
        settings = get_settings()
        if not data:
            raise StorageError("Uploaded file is empty")
        if len(data) > settings.max_upload_bytes:
            raise StorageError("Uploaded file exceeds the configured size limit")

        suffix = Path(filename).suffix.lower()
        detected_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if suffix not in ALLOWED_SUFFIXES or detected_mime not in ALLOWED_MIME_TYPES:
            raise StorageError("Unsupported document type")

        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:2]}/{digest[2:4]}/{digest}{suffix}"
        target = self._resolve_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return StoredObject(key=key, sha256=digest, byte_size=len(data), mime_type=detected_mime)

    def read_bytes(self, key: str) -> bytes:
        return self._resolve_key(key).read_bytes()

    def path_for(self, key: str) -> Path:
        path = self._resolve_key(key)
        if not path.exists():
            raise FileNotFoundError("Stored source document was not found")
        return path

    def _resolve_key(self, key: str) -> Path:
        # put_bytes emits forward-slash relative keys on every platform. Reject
        # Windows separators/drives before POSIX treats them as literal names.
        if "\\" in key or PureWindowsPath(key).drive:
            raise StorageError("Invalid storage key")
        candidate = (self.root / Path(key)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("Invalid storage key") from exc
        return candidate


def get_storage() -> LocalStorageAdapter:
    settings = get_settings()
    if settings.storage_backend != "local":
        raise StorageError(f"Unsupported storage backend: {settings.storage_backend}")
    return LocalStorageAdapter()
