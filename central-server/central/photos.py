"""Photo metadata and immutable-file storage for the central v1 API.

The database stores only metadata/tombstones; photo bytes always live below the
single central data directory.  This module deliberately does no image decode
or transformation.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import canonical_json, utc_timestamp

MAX_PHOTO_BYTES = 20 * 1024 * 1024
ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
 source_device_id TEXT NOT NULL, photo_id TEXT NOT NULL, captured_at TEXT NOT NULL,
 time_source TEXT NOT NULL CHECK(time_source IN ('captured','added')),
 mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 byte_size INTEGER NOT NULL, sha256 TEXT NOT NULL, business_date TEXT NOT NULL,
 file_name TEXT, status TEXT NOT NULL CHECK(status IN ('active','deleted')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
 PRIMARY KEY(source_device_id, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(business_date DESC, captured_at DESC, source_device_id, photo_id);
CREATE TABLE IF NOT EXISTS photo_syncs (
 sync_id TEXT PRIMARY KEY, source_device_id TEXT NOT NULL, completed_at TEXT,
 response_json TEXT, timeline_event_id TEXT
);
CREATE TABLE IF NOT EXISTS photo_sync_changes (
 sync_id TEXT NOT NULL, source_device_id TEXT NOT NULL, photo_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('added','removed')), business_date TEXT NOT NULL,
 PRIMARY KEY(sync_id, source_device_id, photo_id, action)
);
"""

def _signature_matches(mime: str, data: bytes) -> bool:
    if mime == "image/jpeg": return data.startswith(b"\xff\xd8\xff")
    if mime == "image/png": return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/gif": return data.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp": return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    # ISO BMFF: major brand heic/heix/hevc/hevx/mif1; enough to reject renamed arbitrary files.
    return len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1"}

class PhotoStore:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.root = Path(store.database_path).parent / "media" / "photos"
        # File replacement and its matching SQLite transition form one logical
        # operation.  Serialize them so two retries cannot clean up each
        # other's newly committed file after a database failure.
        self._lock = threading.RLock()
        with store._connection() as connection:
            connection.executescript(SCHEMA)

    def _business_date(self, captured_at: str) -> str:
        value = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("captured_at must be UTC ISO-8601")
        settings = self.store.get_shared_settings()
        return self.store._business_date(value, int(settings["day_start_hour"]), settings["timezone"]).isoformat()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if key != "file_name"}

    def list(self, *, source_device_id: str | None, cursor: str | None, limit: int, include_deleted: bool = True, business_date: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        clauses, params = [], []
        if source_device_id: clauses.append("source_device_id=?"); params.append(source_device_id)
        if business_date: clauses.append("business_date=?"); params.append(business_date)
        if not include_deleted: clauses.append("status='active'")
        if cursor:
            try: marker = json.loads(cursor)
            except json.JSONDecodeError as error: raise ValueError("invalid cursor") from error
            if not isinstance(marker, list) or len(marker) != 3: raise ValueError("invalid cursor")
            clauses.append("(captured_at, source_device_id, photo_id) < (?, ?, ?)"); params.extend(marker)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.store._connection() as c:
            rows = c.execute("SELECT * FROM photos" + where + " ORDER BY captured_at DESC, source_device_id DESC, photo_id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        more = len(rows) > limit; rows = rows[:limit]
        next_cursor = None
        if more and rows:
            last = rows[-1]; next_cursor = json.dumps([last["captured_at"], last["source_device_id"], last["photo_id"]], separators=(",", ":"))
        return {"photos": [self._row(row) for row in rows], "next_cursor": next_cursor}

    def upload(self, device_id: str, photo_id: str, metadata: dict[str, Any], data: bytes, sync_id: str) -> tuple[dict[str, Any], bool]:
        if not _uuid(photo_id) or not _uuid(sync_id): raise ValueError("photo_id and X-Photo-Sync-Id must be UUIDs")
        required = {"captured_at", "time_source", "mime_type", "width", "height", "byte_size", "sha256"}
        if set(metadata) != required: raise ValueError("missing or invalid photo metadata")
        if metadata["mime_type"] not in ALLOWED_MIMES or metadata["time_source"] not in {"captured", "added"}: raise ValueError("unsupported image metadata")
        if not all(isinstance(metadata[k], int) and metadata[k] > 0 for k in ("width", "height", "byte_size")): raise ValueError("dimensions and byte_size must be positive integers")
        if metadata["byte_size"] != len(data) or len(data) > MAX_PHOTO_BYTES: raise ValueError("photo exceeds declared or maximum byte size")
        digest = hashlib.sha256(data).hexdigest()
        if metadata["sha256"] != digest or len(digest) != 64: raise ValueError("SHA-256 mismatch")
        if not _signature_matches(metadata["mime_type"], data): raise ValueError("image signature does not match MIME type")
        business_date = self._business_date(metadata["captured_at"])
        now = utc_timestamp()
        device_key = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:16]
        name = f"{device_key}-{photo_id}-{digest}.bin"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / name
        with self._lock, self.store._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            target_preexisted = target.exists()
            try:
                old = c.execute("SELECT * FROM photos WHERE source_device_id=? AND photo_id=?", (device_id, photo_id)).fetchone()
                if old is not None and old["status"] == "active":
                    same = old["sha256"] == digest and old["captured_at"] == metadata["captured_at"] and old["mime_type"] == metadata["mime_type"]
                    if not same: raise ValueError("photo_id conflicts with different immutable content")
                    if not target.exists():
                        self._write_atomic(target, data)
                    result = self._row(old); c.commit(); return result, False
                # Publish the immutable bytes before exposing an active metadata
                # row.  A failed database write may leave an unreferenced file,
                # but can never leave a readable active row without content.
                self._write_atomic(target, data)
                c.execute("""INSERT INTO photos(source_device_id,photo_id,captured_at,time_source,mime_type,width,height,byte_size,sha256,business_date,file_name,status,created_at,updated_at,deleted_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                  ON CONFLICT(source_device_id,photo_id) DO UPDATE SET captured_at=excluded.captured_at,time_source=excluded.time_source,mime_type=excluded.mime_type,width=excluded.width,height=excluded.height,byte_size=excluded.byte_size,sha256=excluded.sha256,business_date=excluded.business_date,file_name=excluded.file_name,status='active',updated_at=excluded.updated_at,deleted_at=NULL""",
                  (device_id,photo_id,metadata["captured_at"],metadata["time_source"],metadata["mime_type"],metadata["width"],metadata["height"],len(data),digest,business_date,name,"active",now,now))
                c.execute("INSERT OR IGNORE INTO photo_sync_changes VALUES(?,?,?,?,?)", (sync_id,device_id,photo_id,"added",business_date))
                row = c.execute("SELECT * FROM photos WHERE source_device_id=? AND photo_id=?", (device_id,photo_id)).fetchone(); c.commit()
            except Exception:
                c.rollback()
                if not target_preexisted:
                    target.unlink(missing_ok=True)
                raise
        return self._row(row), True

    def _write_atomic(self, target: Path, data: bytes) -> None:
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data); out.flush(); os.fsync(out.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def delete(self, device_id: str, photo_id: str, sync_id: str, *, admin: bool = False) -> tuple[dict[str, Any] | None, bool]:
        if not _uuid(photo_id) or not _uuid(sync_id): raise ValueError("photo_id and sync_id must be UUIDs")
        with self._lock, self.store._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT * FROM photos WHERE source_device_id=? AND photo_id=?", (device_id, photo_id)).fetchone()
                if row is None: c.commit(); return None, False
                if row["status"] == "deleted": c.commit(); return self._row(row), False
                deleted_at = utc_timestamp()
                c.execute("UPDATE photos SET status='deleted', deleted_at=?, updated_at=? WHERE source_device_id=? AND photo_id=?", (deleted_at,deleted_at,row["source_device_id"],photo_id))
                # Management deletion invalidates the source device's central
                # state, but it is not a device-confirmed sync operation and
                # therefore must not leave an uncompletable sync-change row.
                if not admin:
                    c.execute("INSERT OR IGNORE INTO photo_sync_changes VALUES(?,?,?,?,?)", (sync_id,row["source_device_id"],photo_id,"removed",row["business_date"]))
                updated = c.execute("SELECT * FROM photos WHERE source_device_id=? AND photo_id=?", (device_id, photo_id)).fetchone()
                c.commit()
            except Exception: c.rollback(); raise
        path = self.root / str(row["file_name"])
        try: path.unlink(missing_ok=True)
        except OSError: pass
        return self._row(updated), True

    def content(self, source_device_id: str, photo_id: str) -> tuple[bytes, str] | None:
        with self.store._connection() as c: row = c.execute("SELECT mime_type,file_name,status FROM photos WHERE source_device_id=? AND photo_id=?", (source_device_id, photo_id)).fetchone()
        if row is None or row["status"] != "active" or not row["file_name"]: return None
        try: return (self.root / str(row["file_name"])).read_bytes(), str(row["mime_type"])
        except OSError: return None

    def complete(self, device_id: str, sync_id: str) -> dict[str, Any]:
        if not _uuid(sync_id): raise ValueError("sync_id must be UUID")
        with self.store._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                sync = c.execute("SELECT * FROM photo_syncs WHERE sync_id=?", (sync_id,)).fetchone()
                if sync is not None:
                    if sync["source_device_id"] != device_id: raise ValueError("sync_id belongs to another device")
                    c.commit(); return json.loads(sync["response_json"])
                changes = c.execute("SELECT action,business_date FROM photo_sync_changes WHERE sync_id=? AND source_device_id=?", (sync_id,device_id)).fetchall()
                added = sum(row["action"] == "added" for row in changes); removed = sum(row["action"] == "removed" for row in changes)
                settings = self.store.get_shared_settings(); today = self.store._business_date(datetime.now(timezone.utc), int(settings["day_start_hour"]), settings["timezone"]).isoformat()
                today_added = sum(row["action"] == "added" and row["business_date"] == today for row in changes)
                event_id = str(uuid.uuid4()); now = utc_timestamp()
                # A sync completion is the only photo fact that reaches AI; never include a URL/hash/file name.
                detail = f"照片同步确认：新增 {added} 张（当前业务日 {today_added} 张），移除 {removed} 张。业务日：{today}。"
                c.execute("""INSERT INTO timeline_events(timeline_event_id,occurred_at,created_at,event_key,category,importance,title,detail,source_kind,source_device_id,wish_id,trigger_id,subject_json,evidence_json,statistics_window_json,delivery_json,dedupe_key)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id,now,now,"photo.sync_confirmed","device","normal","照片同步",detail,"device",device_id,None,None,canonical_json({"business_date":today}),canonical_json({"added_count":added,"current_business_date_added_count":today_added,"removed_count":removed,"business_date":today}),None,None,f"photo-sync:{device_id}:{sync_id}"))
                response = {"sync_id":sync_id,"added_count":added,"current_business_date_added_count":today_added,"removed_count":removed,"business_date":today,"timeline_event_id":event_id}
                c.execute("INSERT INTO photo_syncs VALUES(?,?,?,?,?)", (sync_id,device_id,now,canonical_json(response),event_id)); c.commit(); return response
            except Exception: c.rollback(); raise

def _uuid(value: object) -> bool:
    try: return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError): return False
