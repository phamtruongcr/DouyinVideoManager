"""
douyin_client.py
=================
Client gọi trực tiếp endpoint mà douyin.com dùng khi duyệt trang cá nhân
tác giả (aweme/v1/web/aweme/post/) để lấy danh sách video, và tải video
về máy.

LƯU Ý KỸ THUẬT: Douyin không có API công khai chính thức. Nếu Douyin
thay đổi cơ chế chống bot (a_bogus/msToken) khiến endpoint này ngừng
hoạt động, xem phần "Phương án dự phòng" trong README.md (tự host
Douyin_TikTok_Download_API của Evil0ctal) và chỉ cần đổi URL / hàm
fetch_user_posts_page bên dưới để trỏ sang backend đó.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from .config import USER_AGENT, REQUEST_TIMEOUT, PAGE_COUNT, REQUEST_DELAY


class DouyinAPIError(Exception):
    pass


class DownloadCancelled(Exception):
    """Người dùng bấm '⏹ Dừng tải' trong lúc video này đang tải dở."""
    pass


def _parse_aweme_list_page(data: dict):
    """Parse 1 trang dữ liệu JSON THÔ có dạng {"aweme_list": [...],
    "has_more": ..., "max_cursor": ...} — ĐÚNG NGUYÊN VẸN cấu trúc mà
    endpoint `aweme/v1/web/aweme/post/` trả về (xem fetch_user_posts_page
    bên dưới).

    Trả về (items, has_more, next_cursor).
    Raise ValueError nếu response không có 'aweme_list' (thường do thiếu
    cookie/cookie hết hạn, hoặc bị chặn bot) — nơi gọi tự quyết định
    thông báo lỗi phù hợp.
    """
    aweme_list = data.get("aweme_list")
    if aweme_list is None:
        raise ValueError("response không có 'aweme_list'")

    items = []
    for item in aweme_list:
        try:
            aweme_id = item["aweme_id"]
            desc = (item.get("desc") or "").strip() or f"(không có mô tả) {aweme_id}"
            video = item.get("video", {})
            play_addr = video.get("play_addr", {}) or {}
            url_list = play_addr.get("url_list") or []
            if not url_list:
                continue
            raw_play_url = url_list[0]
            # Mẹo phổ biến: đổi 'playwm' (play with watermark) -> 'play'
            # để lấy luồng không watermark mà chính Douyin đã publish.
            no_wm_url = raw_play_url.replace("playwm", "play")
            create_time = item.get("create_time", 0)
            duration_ms = video.get("duration", 0)
            items.append(
                {
                    "id": str(aweme_id),
                    "desc": desc,
                    "url": no_wm_url,
                    "create_time": create_time,
                    "duration_s": round(duration_ms / 1000) if duration_ms else 0,
                }
            )
        except (KeyError, IndexError, TypeError):
            continue

    has_more = bool(data.get("has_more", 0))
    next_cursor = data.get("max_cursor", 0)
    return items, has_more, next_cursor


class DouyinClient:
    def __init__(self, cookie: str = ""):
        self.cookie = cookie.strip()

    def _headers(self, referer: str) -> dict:
        h = {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def fetch_user_posts_page(self, sec_uid: str, max_cursor: int = 0):
        """Gọi 1 trang danh sách video của tác giả. Trả về (items, has_more, next_cursor)."""
        url = (
            "https://www.douyin.com/aweme/v1/web/aweme/post/"
            f"?device_platform=webapp&aid=6383&channel=channel_pc_web"
            f"&sec_user_id={sec_uid}&max_cursor={max_cursor}&count={PAGE_COUNT}"
            f"&publish_video_strategy_type=0&source=channel_pc_web"
        )
        referer = f"https://www.douyin.com/user/{sec_uid}"
        try:
            resp = requests.get(
                url, headers=self._headers(referer), timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise DouyinAPIError(f"Lỗi kết nối: {exc}") from exc

        if resp.status_code != 200:
            raise DouyinAPIError(f"Douyin trả về mã lỗi HTTP {resp.status_code}.")

        try:
            data = resp.json()
        except ValueError as exc:
            raise DouyinAPIError(
                "Không đọc được dữ liệu JSON trả về (có thể bị chặn bot)."
            ) from exc

        try:
            return _parse_aweme_list_page(data)
        except ValueError as exc:
            # Douyin thường trả status_code khác 0 khi cookie thiếu/hết hạn
            raise DouyinAPIError(
                "Không lấy được danh sách video. Thường do thiếu Cookie hợp lệ "
                "hoặc Douyin đang chặn truy vấn tự động. Hãy cập nhật Cookie "
                "trong mục Cài đặt (xem README.md)."
            ) from exc

    def fetch_all_user_posts(self, sec_uid: str, stop_flag, progress_cb=None, max_items=0):
        """Lặp lấy hết các trang. `stop_flag` là callable trả True nếu cần dừng.
        `progress_cb(count)` được gọi sau mỗi trang để cập nhật UI."""
        all_items = []
        cursor = 0
        while True:
            if stop_flag():
                break
            items, has_more, cursor = self.fetch_user_posts_page(sec_uid, cursor)
            all_items.extend(items)
            if progress_cb:
                progress_cb(len(all_items))
            if max_items and len(all_items) >= max_items:
                all_items = all_items[:max_items]
                break
            if not has_more or not items:
                break
            time.sleep(REQUEST_DELAY)
        return all_items

    def download_video(self, url: str, dest_path: Path, chunk_cb=None, stop_flag=None):
        """Tải video về `dest_path`.

        `stop_flag`: callable không tham số, trả True nếu người dùng bấm
        "⏹ Dừng tải" và muốn hủy NGAY LẬP TỨC, kể cả khi video này đang
        tải dở. Được kiểm tra trước khi mở kết nối và SAU MỖI CHUNK ghi
        (256KB) — nghĩa là việc dừng có hiệu lực gần như tức thời, không
        cần đợi tải xong video hiện tại rồi mới dừng ở video kế tiếp.
        Khi bị hủy giữa chừng, file tạm (.part) đang ghi dở sẽ bị xóa
        luôn, tránh để lại file rác/hỏng trên đĩa, và raise
        `DownloadCancelled` để nơi gọi biết đây là hủy chủ động (không
        phải lỗi mạng/lỗi đĩa)."""
        if stop_flag and stop_flag():
            raise DownloadCancelled("Đã dừng trước khi bắt đầu tải.")

        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        with requests.get(
            url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT
        ) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            written = 0
            try:
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if stop_flag and stop_flag():
                            raise DownloadCancelled(
                                "Đã dừng theo yêu cầu người dùng giữa chừng."
                            )
                        if not chunk:
                            continue
                        f.write(chunk)
                        written += len(chunk)
                        if chunk_cb:
                            chunk_cb(written, total)
            except DownloadCancelled:
                # Xóa ngay file tạm đang dở dang, không để lại rác trên đĩa
                tmp_path.unlink(missing_ok=True)
                raise
        tmp_path.replace(dest_path)