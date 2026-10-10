"""
download_history.py
===================
Lịch sử video ĐÃ TẢI, lưu bằng SQLite (thư viện chuẩn của Python, không cần
cài thêm gì). Dùng để:

  * Đánh dấu video đã tải trước đó khi lấy lại danh sách của cùng 1 kênh.
  * Hỏi người dùng có muốn BỎ QUA video đã tải khi bấm "Tải video đã chọn".

Khóa của mỗi video là cặp (platform, video_id), với platform là "douyin" /
"tiktok" / "facebook" — tránh trùng ID giữa các nền tảng.

Module KHÔNG phụ thuộc GUI nên test được độc lập (xem tests/test_download_history.py).
Mọi lỗi của SQLite (file hỏng, đĩa đầy, không có quyền ghi...) đều được nuốt lại
và ghi vào `last_error` để lịch sử KHÔNG BAO GIỜ làm app bị sập hay chặn việc tải.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import HISTORY_DB_FILE

DEFAULT_PLATFORM = "douyin"  # item Douyin không có khóa "platform"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    platform       TEXT    NOT NULL,
    video_id       TEXT    NOT NULL,
    title          TEXT    NOT NULL DEFAULT '',
    original_title TEXT    NOT NULL DEFAULT '',
    url            TEXT    NOT NULL DEFAULT '',
    file_path      TEXT    NOT NULL DEFAULT '',
    downloaded_at  REAL    NOT NULL,
    download_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (platform, video_id)
);
"""

Key = tuple[str, str]  # (platform, video_id)


def normalize_platform(platform: Optional[str]) -> str:
    return (platform or DEFAULT_PLATFORM).strip().lower() or DEFAULT_PLATFORM


def make_key(platform: Optional[str], video_id) -> Key:
    return normalize_platform(platform), str(video_id)


@dataclass(frozen=True)
class HistoryRecord:
    platform: str
    video_id: str
    title: str
    original_title: str
    url: str
    file_path: str
    downloaded_at: float
    download_count: int

    @property
    def downloaded_at_text(self) -> str:
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(self.downloaded_at))

    @property
    def file_exists(self) -> bool:
        return bool(self.file_path) and Path(self.file_path).exists()


class DownloadHistory:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else HISTORY_DB_FILE
        self.last_error: str = ""
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------ nội bộ --
    def _connect(self) -> sqlite3.Connection:
        # Mỗi thao tác mở 1 kết nối riêng (rẻ với SQLite) -> an toàn khi nhiều
        # luồng tải gọi cùng lúc, không cần chia sẻ connection giữa các thread.
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        try:
            with self._lock, self._connect() as conn:
                conn.executescript(_SCHEMA)
            self.last_error = ""
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không khởi tạo được lịch sử tải: {exc}"

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> HistoryRecord:
        return HistoryRecord(
            platform=row["platform"],
            video_id=row["video_id"],
            title=row["title"],
            original_title=row["original_title"],
            url=row["url"],
            file_path=row["file_path"],
            downloaded_at=row["downloaded_at"],
            download_count=row["download_count"],
        )

    # --------------------------------------------------------------- ghi --
    def record(
        self,
        platform: Optional[str],
        video_id,
        title: str = "",
        original_title: str = "",
        url: str = "",
        file_path: str | Path = "",
    ) -> bool:
        """Ghi nhận 1 video đã tải xong. Tải lại cùng video thì cập nhật bản
        ghi cũ (đổi file_path/thời gian, tăng download_count). True nếu ghi được."""
        plat, vid = make_key(platform, video_id)
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO downloads
                        (platform, video_id, title, original_title, url, file_path, downloaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, video_id) DO UPDATE SET
                        title          = excluded.title,
                        original_title = excluded.original_title,
                        url            = excluded.url,
                        file_path      = excluded.file_path,
                        downloaded_at  = excluded.downloaded_at,
                        download_count = downloads.download_count + 1
                    """,
                    (plat, vid, title or "", original_title or "", url or "",
                     str(file_path or ""), time.time()),
                )
            self.last_error = ""
            return True
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không ghi được lịch sử tải: {exc}"
            return False

    def delete(self, platform: Optional[str], video_id) -> bool:
        plat, vid = make_key(platform, video_id)
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "DELETE FROM downloads WHERE platform = ? AND video_id = ?", (plat, vid)
                )
            return True
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không xóa được bản ghi lịch sử: {exc}"
            return False

    def clear(self) -> bool:
        try:
            with self._lock, self._connect() as conn:
                conn.execute("DELETE FROM downloads")
            return True
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không xóa được lịch sử: {exc}"
            return False

    # --------------------------------------------------------------- đọc --
    def get(self, platform: Optional[str], video_id) -> Optional[HistoryRecord]:
        return self.get_many([make_key(platform, video_id)]).get(make_key(platform, video_id))

    def contains(self, platform: Optional[str], video_id) -> bool:
        return self.get(platform, video_id) is not None

    def get_many(self, keys: Iterable[Key]) -> dict[Key, HistoryRecord]:
        """Tra nhiều video bằng 1 kết nối. Trả dict {(platform, id): bản ghi}
        chỉ gồm các video CÓ trong lịch sử."""
        wanted = [make_key(p, v) for p, v in keys]
        found: dict[Key, HistoryRecord] = {}
        if not wanted:
            return found
        try:
            with self._lock, self._connect() as conn:
                for plat, vid in wanted:
                    row = conn.execute(
                        "SELECT * FROM downloads WHERE platform = ? AND video_id = ?",
                        (plat, vid),
                    ).fetchone()
                    if row is not None:
                        found[(plat, vid)] = self._row_to_record(row)
            return found
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không đọc được lịch sử tải: {exc}"
            return {}

    def list_recent(self, limit: int = 300) -> list[HistoryRecord]:
        """Các video đã tải gần nhất (mới nhất trước), tối đa `limit` bản ghi."""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM downloads ORDER BY downloaded_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không đọc được lịch sử tải: {exc}"
            return []

    def count(self) -> int:
        try:
            with self._lock, self._connect() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0])
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"Không đọc được lịch sử tải: {exc}"
            return 0
