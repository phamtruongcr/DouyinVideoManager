"""
tiktok_client.py
================
Lấy danh sách video của 1 kênh TikTok và tải từng video, dùng thư viện
**yt-dlp** (có sẵn bộ giải mã chữ ký/anti-bot của TikTok và được cập nhật rất
thường xuyên) thay vì tự gọi API web của TikTok — endpoint web TikTok yêu cầu
tham số ký động (msToken/X-Bogus...) nên tự viết rất dễ hỏng.

Giao diện cố tình GIỐNG `DouyinClient` (fetch_all_user_posts / download_video,
cùng định dạng item) để GUI dùng chung 1 luồng cho cả 2 nền tảng.

Nếu TikTok đổi cách chặn bot khiến lấy danh sách lỗi: cập nhật yt-dlp
(`pip install -U yt-dlp`) thường là đủ.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .douyin_client import DownloadCancelled
from .fetch_filters import FetchFilters, ItemCollector, _to_int_or_none


class TikTokAPIError(Exception):
    pass


# Thời điểm hết hạn "xa" cho cookie khi ghi ra file Netscape (0 sẽ bị coi là
# đã hết hạn bởi http.cookiejar).
_COOKIE_EXPIRY = 4102444800  # 2100-01-01

_PLACEHOLDER_TITLE_RE = re.compile(r"^TikTok video #\d+$")
_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def _import_ytdlp():
    try:
        import yt_dlp  # noqa: WPS433 (import trễ có chủ đích)
    except ImportError as exc:
        raise TikTokAPIError(
            "Chưa cài thư viện yt-dlp (cần cho TikTok). Chạy: pip install -U yt-dlp"
        ) from exc
    return yt_dlp


def _friendly_error(exc: Exception) -> str:
    """Đổi lỗi kỹ thuật của yt-dlp thành gợi ý dễ hiểu."""
    raw = str(exc)
    low = raw.lower()
    if "private" in low or "login" in low or "log in" in low or "logged" in low:
        return (
            "TikTok yêu cầu đăng nhập hoặc kênh/video ở chế độ riêng tư. Hãy dán "
            "Cookie TikTok (đã đăng nhập) vào mục Cài đặt rồi thử lại.\n\n" + raw
        )
    if "impersonat" in low or "curl_cffi" in low or "curl-cffi" in low:
        return (
            "Thiếu thư viện curl-cffi để vượt chặn bot của TikTok. Chạy: "
            "pip install -U \"yt-dlp[default,curl-cffi]\"\n\n" + raw
        )
    if "unable to download" in low or "http error 403" in low or "403" in low:
        return (
            "TikTok từ chối truy vấn (có thể bị chặn bot/khu vực). Thử cập nhật "
            "yt-dlp (pip install -U yt-dlp), thêm Cookie TikTok trong Cài đặt, "
            "hoặc bật VPN rồi thử lại.\n\n" + raw
        )
    return raw


class _CollectLogger:
    """Logger im lặng của yt-dlp nhưng gom lại cảnh báo/lỗi để hiển thị khi cần."""

    def __init__(self):
        self.warnings: list[str] = []

    def debug(self, msg):  # noqa: D401
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.warnings.append(str(msg))


def _cookie_string_to_netscape(cookie: str) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        lines.append(
            f".tiktok.com\tTRUE\t/\tTRUE\t{_COOKIE_EXPIRY}\t{name}\t{value.strip()}"
        )
    return "\n".join(lines) + "\n"


def _entry_to_item(entry: dict) -> Optional[dict]:
    """Đổi 1 entry của yt-dlp về đúng định dạng item mà GUI dùng:
    {id, desc, url, create_time, duration_s, platform}. None nếu thiếu id/url."""
    if not isinstance(entry, dict):
        return None
    url = entry.get("webpage_url") or entry.get("url") or ""
    vid = str(entry.get("id") or "").strip()
    if not vid:
        m = _VIDEO_ID_RE.search(url)
        vid = m.group(1) if m else ""
    if not vid or not url.startswith("http"):
        return None

    desc = (entry.get("title") or entry.get("description") or "").strip()
    if not desc or _PLACEHOLDER_TITLE_RE.match(desc):
        desc = f"(không có mô tả) {vid}"

    create_time = entry.get("timestamp") or 0
    try:
        create_time = int(create_time)
    except (TypeError, ValueError):
        create_time = 0
    if not create_time:
        up = str(entry.get("upload_date") or "")
        if re.fullmatch(r"\d{8}", up):
            create_time = int(
                datetime(int(up[:4]), int(up[4:6]), int(up[6:]), tzinfo=timezone.utc).timestamp()
            )

    try:
        duration_s = int(round(float(entry.get("duration") or 0)))
    except (TypeError, ValueError):
        duration_s = 0

    return {
        "id": vid,
        "desc": desc,
        "url": url,
        "create_time": create_time,
        "duration_s": duration_s,
        "view_count": _to_int_or_none(entry.get("view_count")),
        "like_count": _to_int_or_none(entry.get("like_count")),
        "platform": "tiktok",
    }


class TikTokClient:
    def __init__(self, cookie: str = ""):
        self.cookie = cookie.strip()
        # Cảnh báo (không chí mạng) của lần lấy danh sách gần nhất, để GUI hiển thị
        self.last_warning: str = ""

    # ------------------------------------------------------------ nội bộ --
    def _base_opts(self, logger, cookie_path: Optional[str]) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": logger,
            "socket_timeout": 20,
            "extractor_retries": 5,
            "noplaylist": False,
        }
        if cookie_path:
            opts["cookiefile"] = cookie_path
        return opts

    def _make_cookie_file(self) -> Optional[str]:
        """Ghi cookie (dạng 'a=1; b=2') ra file Netscape tạm cho yt-dlp.
        Trả về đường dẫn, hoặc None nếu không có cookie."""
        if not self.cookie:
            return None
        fd, path = tempfile.mkstemp(prefix="tt_cookie_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_cookie_string_to_netscape(self.cookie))
        return path

    # ------------------------------------------------- 1 video đơn lẻ --
    def fetch_video_info(self, url: str) -> dict:
        """Lấy thông tin 1 video TikTok (link đầy đủ hoặc vm./vt.tiktok.com)
        mà KHÔNG tải về. Trả về 1 item; raise TikTokAPIError nếu lỗi."""
        yt_dlp = _import_ytdlp()
        logger = _CollectLogger()
        cookie_path = self._make_cookie_file()
        try:
            opts = self._base_opts(logger, cookie_path)
            opts["noplaylist"] = True
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:
                raise TikTokAPIError(_friendly_error(exc)) from exc
        finally:
            if cookie_path:
                try:
                    os.unlink(cookie_path)
                except OSError:
                    pass
        item = _entry_to_item(info or {})
        if not item:
            raise TikTokAPIError(
                "Không đọc được thông tin video TikTok này (video đã xóa, riêng tư "
                "hoặc TikTok đang chặn)."
                + (f"\n\n{logger.warnings[-1][:300]}" if logger.warnings else "")
            )
        return item

    # -------------------------------------------------- Danh sách video --
    def fetch_all_user_posts(
        self,
        profile_url: str,
        stop_flag: Callable[[], bool],
        progress_cb: Optional[Callable[[int], None]] = None,
        max_items: int = 0,
        collector: Optional[ItemCollector] = None,
    ) -> list[dict]:
        """Lấy video của kênh `profile_url` (dạng https://www.tiktok.com/@user),
        MỚI NHẤT trước. `max_items` = 0 -> lấy hết. Lấy dần từng video và kiểm
        tra `stop_flag` giữa chừng nên bấm dừng có tác dụng gần như ngay."""
        yt_dlp = _import_ytdlp()
        self.last_warning = ""
        logger = _CollectLogger()
        cookie_path = self._make_cookie_file()
        if collector is None:
            collector = ItemCollector(FetchFilters(max_items=max_items), stop_flag=stop_flag)
        items = collector.items   # dùng chung danh sách để biết đã lấy được bao nhiêu khi lỗi giữa chừng
        seen: set[str] = set()
        try:
            opts = self._base_opts(logger, cookie_path)
            opts.update({"extract_flat": "in_playlist", "lazy_playlist": True})
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    # process=False: nhận thẳng danh sách entry "lười" từ extractor
                    # (không xử lý/tải chi tiết từng video) -> nhanh và dừng được.
                    info = ydl.extract_info(profile_url, download=False, process=False)
                    entries = (info or {}).get("entries")
                    if entries is None:
                        raise TikTokAPIError(
                            "Không đọc được danh sách video của kênh này "
                            "(kênh không tồn tại, riêng tư hoặc TikTok đang chặn)."
                        )
                    for entry in entries:
                        if stop_flag():
                            break
                        item = _entry_to_item(entry)
                        if not item or item["id"] in seen:
                            continue
                        seen.add(item["id"])
                        done = collector.feed(item)
                        if progress_cb:
                            progress_cb(len(items))
                        if done:
                            break
            except TikTokAPIError:
                raise
            except Exception as exc:  # DownloadError/ExtractorError/lỗi mạng...
                if not collector.scanned:
                    raise TikTokAPIError(_friendly_error(exc)) from exc
                # Đã lấy được 1 phần -> trả phần đó, ghi chú để GUI báo
                self.last_warning = (
                    f"Dừng sớm sau khi quét {collector.scanned} video do lỗi: {str(exc)[:200]}"
                )
        finally:
            if cookie_path:
                try:
                    os.unlink(cookie_path)
                except OSError:
                    pass

        if not items and logger.warnings:
            self.last_warning = logger.warnings[-1][:300]
        return collector.result()

    # ----------------------------------------------------------- Tải về --
    def download_video(
        self,
        url: str,
        dest_path: Path,
        chunk_cb: Optional[Callable[[int, int], None]] = None,
        stop_flag: Optional[Callable[[], bool]] = None,
    ):
        """Tải 1 video TikTok (link trang, dạng .../@user/video/<id>) về
        `dest_path`. Cùng quy ước với DouyinClient.download_video: kiểm tra
        `stop_flag` liên tục, hủy giữa chừng thì xóa file dở và raise
        DownloadCancelled."""
        if stop_flag and stop_flag():
            raise DownloadCancelled("Đã dừng trước khi bắt đầu tải.")
        yt_dlp = _import_ytdlp()
        logger = _CollectLogger()
        cookie_path = self._make_cookie_file()

        def hook(d: dict):
            if stop_flag and stop_flag():
                raise DownloadCancelled("Đã dừng theo yêu cầu người dùng giữa chừng.")
            if chunk_cb and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                chunk_cb(int(d.get("downloaded_bytes") or 0), int(total))

        opts = self._base_opts(logger, cookie_path)
        opts.update(
            {
                # %% để dấu % trong đường dẫn không bị hiểu là mẫu đặt tên của yt-dlp
                "outtmpl": {"default": str(dest_path).replace("%", "%%")},
                "format": "b[ext=mp4]/b",
                "noplaylist": True,
                "overwrites": True,
                "retries": 3,
                "progress_hooks": [hook],
            }
        )

        def cleanup():
            for suffix in (".part", ".ytdl"):
                dest_path.with_name(dest_path.name + suffix).unlink(missing_ok=True)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            cleanup()
            if isinstance(exc, DownloadCancelled) or (stop_flag and stop_flag()):
                raise DownloadCancelled(
                    "Đã dừng theo yêu cầu người dùng giữa chừng."
                ) from None
            raise TikTokAPIError(_friendly_error(exc)) from exc
        finally:
            if cookie_path:
                try:
                    os.unlink(cookie_path)
                except OSError:
                    pass

        if not dest_path.exists():
            raise TikTokAPIError(
                "yt-dlp báo xong nhưng không thấy file video đầu ra"
                + (f": {logger.warnings[-1][:200]}" if logger.warnings else ".")
            )
