"""
facebook_client.py
==================
Tải video Facebook (video thường, Reel, fb.watch) và lấy danh sách video của
1 trang/kênh Facebook, dùng thư viện **yt-dlp**.

Giao diện cố tình GIỐNG `TikTokClient` / `DouyinClient` để GUI dùng chung 1 luồng:
  - fetch_video_info(url)            -> 1 item (video đơn lẻ)
  - fetch_all_user_posts(url, ...)   -> list item (cả trang/kênh, best-effort)
  - download_video(url, dest, ...)   -> tải về file mp4

Định dạng item: {id, desc, url, create_time, duration_s, platform="facebook"}

LƯU Ý: yt-dlp hỗ trợ rất tốt từng video/reel của Facebook, nhưng việc LIỆT KÊ
toàn bộ video của 1 trang phụ thuộc vào phiên bản yt-dlp và Facebook có cho
xem ẩn danh hay không. Nếu không liệt kê được, app báo rõ và gợi ý dán từng
link video (mỗi link 1 dòng). Video riêng tư/giới hạn cần Cookie Facebook
(mục Cài đặt). Lỗi lạ: cập nhật yt-dlp (`pip install -U yt-dlp`).
"""

from __future__ import annotations

import glob
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .douyin_client import DownloadCancelled
from .browser_sniffer import SnifferBlocked, SnifferUnavailable, sniff_profile_videos
from .fetch_filters import FetchFilters, ItemCollector, _to_int_or_none
from .tiktok_client import _CollectLogger, _import_ytdlp


class FacebookAPIError(Exception):
    pass


_COOKIE_EXPIRY = 4102444800  # 2100-01-01 (0 sẽ bị coi là đã hết hạn)
_GENERIC_TITLE_RE = re.compile(r"^(?:Video|Facebook|Reel)(?:\s+(?:video|reel))?(?:\s+#?\d+)?$", re.I)
_ID_IN_URL_RE = re.compile(r"(?:/videos?/|/reels?/|[?&]v=)(\d+)")


def _friendly_error(exc: Exception) -> str:
    raw = str(exc)
    low = raw.lower()
    if "unsupported url" in low:
        return (
            "yt-dlp chưa hỗ trợ liệt kê video từ link này. Hãy dán TỪNG link video/reel "
            "(mỗi link 1 dòng) hoặc cập nhật yt-dlp (pip install -U yt-dlp).\n\n" + raw
        )
    if (
        "cookies" in low or "login" in low or "log in" in low or "private" in low
        or "not available" in low or "you must be logged" in low
    ):
        return (
            "Facebook yêu cầu đăng nhập, hoặc video ở chế độ riêng tư/giới hạn. Hãy dán "
            "Cookie Facebook (đã đăng nhập) vào mục Cài đặt rồi thử lại.\n\n" + raw
        )
    if "403" in low or "unable to download" in low or "rate" in low:
        return (
            "Facebook từ chối truy vấn (chặn bot/khu vực/giới hạn tốc độ). Thử cập nhật "
            "yt-dlp, thêm Cookie Facebook trong Cài đặt, hoặc đợi một lúc rồi thử lại.\n\n" + raw
        )
    return raw


def _cookie_string_to_netscape(cookie: str) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        lines.append(f".facebook.com\tTRUE\t/\tTRUE\t{_COOKIE_EXPIRY}\t{name}\t{value.strip()}")
    return "\n".join(lines) + "\n"


def _entry_to_item(entry: dict) -> Optional[dict]:
    """Đổi 1 entry yt-dlp về item của GUI. None nếu thiếu id/url."""
    if not isinstance(entry, dict):
        return None
    url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or ""
    vid = str(entry.get("id") or "").strip()
    if not vid:
        m = _ID_IN_URL_RE.search(url)
        vid = m.group(1) if m else ""
    if not vid or not url.startswith("http"):
        return None

    desc = (entry.get("title") or entry.get("description") or "").strip()
    # Tiêu đề Facebook hay có dạng "12K views · 300 reactions | nội dung" -> giữ nguyên;
    # chỉ thay khi rỗng hoặc chung chung.
    if not desc or _GENERIC_TITLE_RE.match(desc):
        desc = (entry.get("description") or "").strip().splitlines()[0] if entry.get("description") else ""
    if not desc or _GENERIC_TITLE_RE.match(desc):
        desc = f"(không có mô tả) {vid}"

    try:
        create_time = int(entry.get("timestamp") or 0)
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
        "platform": "facebook",
    }


class FacebookClient:
    def __init__(self, cookie: str = "", use_playwright: bool = True):
        self.cookie = cookie.strip()
        # Playwright (trình duyệt ẩn + bắt JSON API) là cách lấy danh sách chính; yt-dlp là dự phòng
        self.use_playwright = use_playwright
        self.last_warning: str = ""

    # ------------------------------------------------------------ nội bộ --
    def _base_opts(self, logger, cookie_path: Optional[str]) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": logger,
            "socket_timeout": 20,
            "extractor_retries": 3,
        }
        if cookie_path:
            opts["cookiefile"] = cookie_path
        return opts

    def _make_cookie_file(self) -> Optional[str]:
        if not self.cookie:
            return None
        fd, path = tempfile.mkstemp(prefix="fb_cookie_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_cookie_string_to_netscape(self.cookie))
        return path

    @staticmethod
    def _rm(path: Optional[str]):
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------- 1 video đơn lẻ --
    def fetch_video_info(self, url: str) -> dict:
        """Lấy thông tin (tiêu đề, thời lượng, ngày đăng) của 1 video Facebook
        mà KHÔNG tải về. Trả về 1 item; raise FacebookAPIError nếu lỗi."""
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
                raise FacebookAPIError(_friendly_error(exc)) from exc
        finally:
            self._rm(cookie_path)

        # Link dạng playlist/post nhiều video: lấy video đầu tiên
        if isinstance(info, dict) and info.get("entries") and not info.get("formats"):
            first = next((e for e in info["entries"] if e), None)
            info = first or info
        item = _entry_to_item(info or {})
        if not item:
            raise FacebookAPIError(
                "Không đọc được thông tin video Facebook này (video riêng tư, đã xóa, "
                "hoặc cần Cookie đăng nhập)."
                + (f"\n\n{logger.warnings[-1][:300]}" if logger.warnings else "")
            )
        # Giữ link người dùng dán (link chuẩn mà yt-dlp tải được) nếu yt-dlp không trả
        if not item["url"].startswith("http"):
            item["url"] = url
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
        """Lấy video của kênh. Ưu tiên Playwright (bắt JSON API nội bộ khi cuộn
        trang - không phụ thuộc HTML); nếu Playwright không dùng được / không bắt
        được gì thì tự lùi về yt-dlp."""
        if collector is None:
            collector = ItemCollector(FetchFilters(max_items=max_items), stop_flag=stop_flag)
        self.last_warning = ""
        if self.use_playwright:
            try:
                sniff_profile_videos(
                    "facebook", profile_url, stop_flag, collector,
                    cookie=self.cookie, progress_cb=progress_cb,
                )
                return collector.result()
            except (SnifferUnavailable, SnifferBlocked) as exc:
                if collector.items or stop_flag():
                    return collector.result()
                note = f"Playwright không lấy được ({exc}) -> thử lại bằng yt-dlp."
                result = self._fetch_via_ytdlp(
                    profile_url, stop_flag, progress_cb, max_items, collector
                )
                self.last_warning = (note + " " + self.last_warning).strip()
                return result
        return self._fetch_via_ytdlp(profile_url, stop_flag, progress_cb, max_items, collector)

    def _fetch_via_ytdlp(
        self,
        profile_url: str,
        stop_flag: Callable[[], bool],
        progress_cb: Optional[Callable[[int], None]] = None,
        max_items: int = 0,
        collector: Optional[ItemCollector] = None,
    ) -> list[dict]:
        """Liệt kê video của 1 trang Facebook (best-effort, xem ghi chú đầu file)."""
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
                    info = ydl.extract_info(profile_url, download=False, process=False)
                    entries = (info or {}).get("entries")
                    if entries is None:
                        raise FacebookAPIError(
                            "Không liệt kê được video của trang này. Hãy dán TỪNG link "
                            "video/reel (mỗi link 1 dòng) để tải."
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
            except FacebookAPIError:
                raise
            except Exception as exc:
                if not collector.scanned:
                    raise FacebookAPIError(_friendly_error(exc)) from exc
                self.last_warning = f"Dừng sớm sau khi quét {collector.scanned} video do lỗi: {str(exc)[:200]}"
        finally:
            self._rm(cookie_path)

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
        ffmpeg_location: str = "",
    ):
        """Tải 1 video Facebook về `dest_path` (.mp4). Cùng quy ước với
        DouyinClient/TikTokClient: kiểm tra `stop_flag` liên tục; hủy giữa chừng
        thì xóa file dở và raise DownloadCancelled.

        `ffmpeg_location`: đường dẫn ffmpeg (rỗng = máy không có ffmpeg).
        Facebook thường tách riêng luồng hình và luồng tiếng ở chất lượng cao —
        có ffmpeg thì yt-dlp ghép lại (chất lượng tốt nhất); không có thì chỉ
        tải bản đã gộp sẵn (thường là SD)."""
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
                "outtmpl": {"default": str(dest_path).replace("%", "%%")},
                # Có ffmpeg -> lấy hình + tiếng chất lượng cao nhất rồi ghép. KHÔNG có
                # ffmpeg -> chỉ lấy bản đã gộp sẵn (yt-dlp sẽ KHÔNG tự lùi lại nếu cứ
                # yêu cầu ghép mà thiếu ffmpeg, mà để 2 file rời).
                "format": (
                    "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b"
                    if ffmpeg_location else "b[ext=mp4]/b"
                ),
                "merge_output_format": "mp4",
                "noplaylist": True,
                "overwrites": True,
                "retries": 3,
                "progress_hooks": [hook],
            }
        )
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        def cleanup():
            for suffix in (".part", ".ytdl"):
                dest_path.with_name(dest_path.name + suffix).unlink(missing_ok=True)
            # file tạm của luồng hình/tiếng riêng: "<tên>.f<format>.mp4(.part)"
            pattern = glob.escape(str(dest_path.with_suffix(""))) + ".f*"
            for tmp in glob.glob(pattern):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            cleanup()
            if isinstance(exc, DownloadCancelled) or (stop_flag and stop_flag()):
                raise DownloadCancelled("Đã dừng theo yêu cầu người dùng giữa chừng.") from None
            raise FacebookAPIError(_friendly_error(exc)) from exc
        finally:
            self._rm(cookie_path)

        if not dest_path.exists():
            raise FacebookAPIError(
                "yt-dlp báo xong nhưng không thấy file video đầu ra"
                + (f": {logger.warnings[-1][:200]}" if logger.warnings else ".")
            )
