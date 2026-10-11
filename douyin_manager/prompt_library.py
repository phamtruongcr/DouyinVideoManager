"""
prompt_library.py
=================
Thư viện PROMPT TÙY CHỈNH đã lưu cho tab "Kịch bản & Giọng đọc": đặt tên cho một prompt để chọn lại
bất cứ lúc nào (giống thư viện giọng mẫu `voice_library`, nhưng chỉ lưu văn bản nên nằm thẳng trong cấu hình).

Danh sách lưu trong cfg["review_saved_prompts"] = [{"name": ..., "text": ...}]. Các hàm chỉ sửa dict
`cfg` — người gọi tự `save_config`. Không phụ thuộc giao diện.
"""

from __future__ import annotations

import re
from typing import Optional

from .config import CUSTOM_PROMPT_MAX_LEN

CFG_KEY = "review_saved_prompts"
MAX_NAME_LEN = 60


class PromptLibraryError(ValueError):
    """Lỗi người dùng (thông báo tiếng Việt, hiển thị thẳng)."""


def load_prompts(cfg: dict) -> list[dict]:
    """Danh sách prompt đã lưu, bỏ mục hỏng/trùng tên (không phân biệt hoa thường)."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in (cfg.get(CFG_KEY) or []) if isinstance(cfg, dict) else []:
        if not isinstance(item, dict):
            continue
        name, text = str(item.get("name") or "").strip(), str(item.get("text") or "").strip()
        if name and text and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "text": text})
    return out


def prompt_names(cfg: dict) -> list[str]:
    return [p["name"] for p in load_prompts(cfg)]


def find_prompt(cfg: dict, name: str) -> Optional[dict]:
    key = (name or "").strip().lower()
    return next((p for p in load_prompts(cfg) if p["name"].lower() == key), None)


def find_prompt_by_text(cfg: dict, text: str) -> Optional[dict]:
    text = (text or "").strip()
    return next((p for p in load_prompts(cfg) if p["text"] == text), None) if text else None


def save_prompt(cfg: dict, name: str, text: str) -> dict:
    """Lưu `text` thành prompt tên `name` (trùng tên thì THAY prompt cũ). Trả về mục đã lưu."""
    name = re.sub(r"\s+", " ", name or "").strip()
    text = (text or "").strip()
    if not name:
        raise PromptLibraryError("Hãy đặt tên cho prompt.")
    if len(name) > MAX_NAME_LEN:
        raise PromptLibraryError(f"Tên prompt quá dài (tối đa {MAX_NAME_LEN} ký tự).")
    if not text:
        raise PromptLibraryError("Prompt đang trống, chưa có gì để lưu.")
    if len(text) > CUSTOM_PROMPT_MAX_LEN:
        raise PromptLibraryError(f"Prompt quá dài (tối đa {CUSTOM_PROMPT_MAX_LEN} ký tự).")
    entry = {"name": name, "text": text}
    prompts = [p for p in load_prompts(cfg) if p["name"].lower() != name.lower()]
    prompts.append(entry)
    prompts.sort(key=lambda p: p["name"].lower())
    cfg[CFG_KEY] = prompts
    return entry


def delete_prompt(cfg: dict, name: str) -> bool:
    """Xóa prompt khỏi danh sách. Trả về True nếu có prompt đó."""
    found = find_prompt(cfg, name)
    if not found:
        return False
    cfg[CFG_KEY] = [p for p in load_prompts(cfg) if p["name"].lower() != found["name"].lower()]
    return True
