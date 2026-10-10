"""
voice_library.py
================
Thư viện GIỌNG MẪU đã lưu cho tab "Kịch bản & Giọng đọc": đặt tên cho một file giọng
mẫu để chọn lại bất cứ lúc nào. File được SAO CHÉP vào thư mục riêng của app
(`VOICES_DIR`) nên không mất khi file gốc (VD trong Downloads) bị xóa/di chuyển.

Danh sách lưu trong cấu hình: cfg["tts_saved_voices"] = [{"name": ..., "path": ...}].
Các hàm chỉ sửa dict `cfg` — người gọi tự `save_config`. Không phụ thuộc giao diện.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import Optional

VOICES_DIR = Path.home() / ".douyin_video_manager_voices"
CFG_KEY = "tts_saved_voices"
MAX_NAME_LEN = 60


class VoiceLibraryError(ValueError):
    """Lỗi người dùng (thông báo tiếng Việt, hiển thị thẳng)."""


def load_voices(cfg: dict) -> list[dict]:
    """Danh sách giọng đã lưu, bỏ mục hỏng; KHÔNG kiểm tra file còn tồn tại."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in (cfg.get(CFG_KEY) or []) if isinstance(cfg, dict) else []:
        if not isinstance(item, dict):
            continue
        name, path = str(item.get("name") or "").strip(), str(item.get("path") or "").strip()
        if name and path and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "path": path})
    return out


def voice_names(cfg: dict) -> list[str]:
    return [v["name"] for v in load_voices(cfg)]


def find_voice(cfg: dict, name: str) -> Optional[dict]:
    key = (name or "").strip().lower()
    return next((v for v in load_voices(cfg) if v["name"].lower() == key), None)


def find_voice_by_path(cfg: dict, path: str) -> Optional[dict]:
    path = (path or "").strip()
    return next((v for v in load_voices(cfg) if v["path"] == path), None) if path else None


def _slug(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).replace("đ", "d").replace("Đ", "D")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "voice"


def save_voice(
    cfg: dict, name: str, source_path: str | Path, voices_dir: Optional[Path] = None,
) -> dict:
    """Lưu `source_path` thành giọng tên `name` (trùng tên thì THAY giọng cũ). Trả về mục đã lưu."""
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise VoiceLibraryError("Hãy đặt tên cho giọng.")
    if len(name) > MAX_NAME_LEN:
        raise VoiceLibraryError(f"Tên giọng quá dài (tối đa {MAX_NAME_LEN} ký tự).")
    src = Path(str(source_path or "").strip())
    if not src.is_file():
        raise VoiceLibraryError("Chưa chọn file giọng mẫu hợp lệ để lưu.")

    voices_dir = Path(voices_dir) if voices_dir else VOICES_DIR
    try:
        voices_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VoiceLibraryError(f"Không tạo được thư mục lưu giọng: {exc}") from exc

    old = find_voice(cfg, name)
    dest = voices_dir / f"{_slug(name)}{src.suffix.lower()}"
    try:
        if src.resolve() != dest.resolve():
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.copy2(src, tmp)
            tmp.replace(dest)
    except OSError as exc:
        raise VoiceLibraryError(f"Không sao chép được file giọng: {exc}") from exc
    # đổi file khi thay giọng cũ có đuôi khác -> dọn file cũ
    if old and old["path"] != str(dest):
        _remove_file_in(old["path"], voices_dir)

    entry = {"name": name, "path": str(dest)}
    voices = [v for v in load_voices(cfg) if v["name"].lower() != name.lower()]
    voices.append(entry)
    voices.sort(key=lambda v: v["name"].lower())
    cfg[CFG_KEY] = voices
    return entry


def delete_voice(cfg: dict, name: str, voices_dir: Optional[Path] = None) -> bool:
    """Xóa giọng khỏi danh sách (và xóa file bản sao nếu nằm trong thư mục giọng của app).
    Trả về True nếu có giọng đó."""
    voice = find_voice(cfg, name)
    if not voice:
        return False
    cfg[CFG_KEY] = [v for v in load_voices(cfg) if v["name"].lower() != voice["name"].lower()]
    _remove_file_in(voice["path"], Path(voices_dir) if voices_dir else VOICES_DIR)
    return True


def _remove_file_in(path: str, folder: Path) -> None:
    """Chỉ xóa file nằm TRONG `folder` (không bao giờ đụng file gốc của người dùng)."""
    try:
        p, root = Path(path).resolve(), Path(folder).resolve()
        if p.is_file() and root in p.parents:
            p.unlink()
    except OSError:
        pass
