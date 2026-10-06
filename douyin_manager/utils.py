"""
utils.py
========
Các hàm tiện ích dùng chung, không phụ thuộc GUI:
  - extract_clean_link: trích xuất link Douyin sạch từ đoạn text lộn xộn
  - resolve_link: theo redirect để xác định loại link (profile / video)
  - format_post_time: đổi epoch giây -> chuỗi ngày dd/mm/yyyy
  - safe_filename: làm sạch tên file trước khi lưu ra đĩa
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from .config import USER_AGENT, REQUEST_TIMEOUT

# Regex bắt mọi link Douyin (ngắn hoặc đầy đủ) nằm lẫn trong đoạn text rác
DOUYIN_LINK_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|m\.douyin\.com|iesdouyin\.com)"
    r"/[A-Za-z0-9\-_./?=&%]+"
)

_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\n\r\t]')


def extract_clean_link(raw_text: str) -> str | None:
    """Từ 1 đoạn text bất kỳ (có thể lẫn chữ Hoa/Trung, emoji, khoảng
    trắng...) -> trả về link Douyin sạch đầu tiên tìm thấy, đã bỏ dấu
    câu/ký tự thừa bám ở cuối."""
    if not raw_text:
        return None
    match = DOUYIN_LINK_RE.search(raw_text)
    if not match:
        return None
    link = match.group(0)
    # Cắt bỏ các ký tự rác hay bị dính ở cuối link khi copy từ caption
    link = link.rstrip(").,，。；;'\"!?！？ ")
    return link


def resolve_link(link: str):
    """Theo link (có thể là link rút gọn v.douyin.com) để tìm ra loại
    (profile / video) và id tương ứng (sec_uid hoặc aweme_id).

    Trả về tuple: ("profile", sec_uid) | ("video", aweme_id)
    Raise RuntimeError nếu không nhận diện được.
    """
    headers = {"User-Agent": USER_AGENT}
    final_url = link
    # Link rút gọn / bất kỳ link nào cần theo redirect để lấy URL thật
    try:
        resp = requests.get(
            link, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        final_url = resp.url
    except requests.RequestException as exc:
        raise RuntimeError(f"Không thể truy cập link: {exc}") from exc

    parsed = urlparse(final_url)
    path = parsed.path

    if "/user/" in path:
        sec_uid = path.split("/user/", 1)[1].split("/")[0]
        sec_uid = sec_uid.split("?")[0]
        if sec_uid:
            return "profile", sec_uid

    if "/video/" in path:
        aweme_id = path.split("/video/", 1)[1].split("/")[0].split("?")[0]
        if aweme_id:
            return "video", aweme_id

    # Một số link rút gọn share redirect qua modal_id hoặc share/video
    qs = parse_qs(parsed.query)
    if "modal_id" in qs:
        return "video", qs["modal_id"][0]

    raise RuntimeError(
        "Không nhận diện được đây là link kênh (channel) hay link video Douyin."
    )


def format_post_time(create_time: int) -> str:
    """create_time là epoch giây do Douyin trả về -> chuỗi ngày (dd/mm/yyyy)."""
    if not create_time:
        return "-"
    try:
        return datetime.fromtimestamp(create_time).strftime("%d/%m/%Y")
    except (OSError, OverflowError, ValueError):
        return "-"


def safe_filename(name: str, max_len: int = 60) -> str:
    name = _INVALID_CHARS_RE.sub("_", name).strip()
    if not name:
        name = "video"
    if len(name) > max_len:
        name = name[:max_len]
    return name


def unique_filename(directory: Path, stem: str, ext: str, claimed: set[str]) -> str:
    """Trả về 1 TÊN FILE sạch (chỉ dựa trên tiêu đề, không có mã/ID nào kèm
    theo) mà chưa tồn tại trong `directory` và chưa nằm trong `claimed`.

    `claimed`: tập hợp các tên đã được "giữ chỗ" bởi video khác CÙNG đợt tải
    (nhiều luồng tải song song) — cần vì lúc 2 luồng cùng kiểm tra đĩa gần
    như đồng thời, file của luồng kia có thể CHƯA kịp ghi ra đĩa. Tên trả
    về được tự động thêm vào `claimed` trước khi trả về.

    Nếu `stem.ext` đã bị chiếm (trùng tiêu đề với video khác, hoặc đã có
    sẵn từ lần tải trước) -> thử `stem (2).ext`, `stem (3).ext`, ... giống
    cách trình duyệt tự tránh đè file, thay vì gắn thêm mã ID vào tên.

    An toàn khi gọi đồng thời từ nhiều luồng CHỈ KHI nơi gọi tự khóa bằng
    1 Lock dùng chung bao quanh lệnh gọi hàm này (hàm không tự khóa)."""
    candidate = f"{stem}.{ext}"
    n = 2
    while candidate in claimed or (directory / candidate).exists():
        candidate = f"{stem} ({n}).{ext}"
        n += 1
    claimed.add(candidate)
    return candidate