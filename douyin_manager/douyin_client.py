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

import json
import time
from pathlib import Path

import requests

from .config import USER_AGENT, MOBILE_USER_AGENT, REQUEST_TIMEOUT, PAGE_COUNT, REQUEST_DELAY
from .fetch_filters import FetchFilters, ItemCollector, _to_int_or_none


class DouyinAPIError(Exception):
    pass


class DownloadCancelled(Exception):
    """Người dùng bấm '⏹ Dừng tải' trong lúc video này đang tải dở."""
    pass


def _aweme_to_item(item: dict):
    """Đổi 1 object 'aweme' (video) của Douyin về item {id, desc, url,
    create_time, duration_s, view_count, like_count}. None nếu thiếu dữ liệu/không
    có link phát. view_count/like_count = None nếu Douyin không trả số liệu."""
    try:
        aweme_id = item["aweme_id"]
        desc = (item.get("desc") or "").strip() or f"(không có mô tả) {aweme_id}"
        video = item.get("video", {})
        play_addr = video.get("play_addr", {}) or {}
        url_list = play_addr.get("url_list") or []
        if not url_list:
            return None
        raw_play_url = url_list[0]
        # Mẹo phổ biến: đổi 'playwm' (play with watermark) -> 'play'
        # để lấy luồng không watermark mà chính Douyin đã publish.
        no_wm_url = raw_play_url.replace("playwm", "play")
        create_time = item.get("create_time", 0)
        duration_ms = video.get("duration", 0)
        stats = item.get("statistics") or {}
        # Douyin hay ẩn lượt xem (play_count = 0) -> coi là "không có số liệu"
        # (None) thay vì 0 thật, để bộ lọc không loại nhầm và báo rõ cho người dùng.
        view_count = _to_int_or_none(stats.get("play_count")) or None
        like_count = _to_int_or_none(stats.get("digg_count"))
        return {
            "id": str(aweme_id),
            "desc": desc,
            "url": no_wm_url,
            "create_time": create_time,
            "duration_s": round(duration_ms / 1000) if duration_ms else 0,
            "view_count": view_count,
            "like_count": like_count,
        }
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _parse_share_page_html(html: str):
    """Tìm item video trong HTML trang chia sẻ iesdouyin.com/share/video/<id>
    (dữ liệu nằm trong biến `window._ROUTER_DATA = {...}`). Trả về dict item
    thô của Douyin (có 'aweme_id', 'video'...) hoặc None nếu không thấy."""
    marker = "window._ROUTER_DATA"
    pos = html.find(marker)
    if pos < 0:
        return None
    brace = html.find("{", pos)
    if brace < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[brace:])
    except ValueError:
        return None
    loader = (data or {}).get("loaderData") or {}
    for page in loader.values():
        if not isinstance(page, dict):
            continue
        info = page.get("videoInfoRes") or {}
        item_list = info.get("item_list") or []
        if item_list and isinstance(item_list[0], dict):
            return item_list[0]
    return None


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
        parsed = _aweme_to_item(item)
        if parsed:
            items.append(parsed)

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

    # ------------------------------------------------- 1 video đơn lẻ --
    def fetch_video_by_id(self, aweme_id: str) -> dict:
        """Lấy thông tin + link phát của 1 video Douyin theo `aweme_id`.

        Cách 1 (chính): đọc trang chia sẻ di động iesdouyin.com/share/video/<id>
        — không cần chữ ký a_bogus nên ổn định hơn. Cách 2 (dự phòng): endpoint
        aweme/detail của web (cần Cookie hợp lệ). Raise DouyinAPIError nếu cả
        hai đều thất bại."""
        errors = []
        try:
            resp = requests.get(
                f"https://www.iesdouyin.com/share/video/{aweme_id}/",
                headers={
                    "User-Agent": MOBILE_USER_AGENT,
                    "Referer": "https://www.douyin.com/",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                raw = _parse_share_page_html(resp.text)
                item = _aweme_to_item(raw) if raw else None
                if item:
                    return item
                errors.append("trang chia sẻ không có dữ liệu video (có thể là bài ảnh/đã xóa)")
            else:
                errors.append(f"trang chia sẻ trả HTTP {resp.status_code}")
        except requests.RequestException as exc:
            errors.append(f"lỗi kết nối trang chia sẻ: {exc}")

        try:
            resp = requests.get(
                "https://www.douyin.com/aweme/v1/web/aweme/detail/"
                f"?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={aweme_id}",
                headers=self._headers(f"https://www.douyin.com/video/{aweme_id}"),
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                detail = (resp.json() or {}).get("aweme_detail")
                item = _aweme_to_item(detail) if detail else None
                if item:
                    return item
                errors.append("endpoint detail không trả dữ liệu (thiếu/hết hạn Cookie?)")
            else:
                errors.append(f"endpoint detail trả HTTP {resp.status_code}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"endpoint detail lỗi: {exc}")

        raise DouyinAPIError(
            "Không lấy được video Douyin này. Có thể video đã bị xóa/riêng tư, là bài "
            "ảnh (không phải video), hoặc cần Cookie Douyin mới trong Cài đặt.\n"
            "Chi tiết: " + "; ".join(errors)
        )

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

    def fetch_all_user_posts(
        self, sec_uid: str, stop_flag, progress_cb=None, max_items=0, collector=None,
    ):
        """Lặp lấy các trang (video MỚI NHẤT trước) cho tới khi đủ/hết.
        `stop_flag` là callable trả True nếu cần dừng.
        `progress_cb(count)` được gọi sau mỗi trang để cập nhật UI.
        `collector` (ItemCollector) áp điều kiện ngày/view/tym/thứ tự/số lượng;
        không truyền -> chỉ giới hạn bằng `max_items` như trước đây."""
        if collector is None:
            collector = ItemCollector(FetchFilters(max_items=max_items), stop_flag=stop_flag)
        cursor = 0
        while True:
            if stop_flag():
                break
            items, has_more, cursor = self.fetch_user_posts_page(sec_uid, cursor)
            done = False
            for item in items:
                if collector.feed(item):
                    done = True
                    break
            if progress_cb:
                progress_cb(len(collector.items))
            if done or not has_more or not items:
                break
            time.sleep(REQUEST_DELAY)
        return collector.result()

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