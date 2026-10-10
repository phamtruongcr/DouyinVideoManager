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

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .douyin_client import DownloadCancelled
from .app_logger import get_logger
from .browser_sniffer import SnifferBlocked, SnifferUnavailable, sniff_profile_videos

logger = get_logger("tiktok")
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
    def __init__(self, cookie: str = "", use_playwright: bool = True):
        self.cookie = cookie.strip()
        # Playwright (trình duyệt ẩn + bắt JSON API) là cách lấy danh sách chính; yt-dlp là dự phòng
        self.use_playwright = use_playwright
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
        """Lấy video của kênh. Ưu tiên Playwright (bắt JSON API nội bộ khi cuộn
        trang - không phụ thuộc HTML); nếu Playwright không dùng được / không bắt
        được gì thì tự lùi về yt-dlp."""
        if collector is None:
            collector = ItemCollector(FetchFilters(max_items=max_items), stop_flag=stop_flag)
        self.last_warning = ""
        if self.use_playwright:
            try:
                sniff_profile_videos(
                    "tiktok", profile_url, stop_flag, collector,
                    cookie=self.cookie, progress_cb=progress_cb,
                )
                return collector.result()
            except (SnifferUnavailable, SnifferBlocked) as exc:
                if collector.items or stop_flag():
                    return collector.result()
                note = f"Playwright không lấy được ({exc}) -> thử lại bằng yt-dlp."
                logger.warning(note)
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
    @staticmethod
    def _is_video_fmt(f: dict) -> bool:
        """Định dạng có phần HÌNH (không phải chỉ-tiếng)."""
        vc = (f.get("vcodec") or "").lower()
        if vc == "none":
            return False
        return bool(vc) or bool(f.get("width") or f.get("height"))

    @staticmethod
    def _codec_rank(f: dict) -> int:
        vc = (f.get("vcodec") or "").lower()
        if vc.startswith(("h264", "avc")):
            return 0           # phát được mọi nơi
        if vc.startswith(("h265", "hevc", "bytevc1")):
            return 2           # nhiều máy chỉ nghe tiếng, mất hình
        return 1

    @staticmethod
    def _probe_video(ffprobe: Optional[str], path: Path) -> Optional[dict]:
        """Đọc luồng hình đầu tiên của file. Trả {} nếu file KHÔNG có hình,
        None nếu không kiểm tra được (thiếu ffprobe / lỗi)."""
        if not ffprobe:
            return None
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=60,
            ).stdout
            streams = (json.loads(out or "{}").get("streams")) or []
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if not streams or not streams[0].get("width"):
            return {}
        return streams[0]

    @staticmethod
    def _transcode_h264(ffmpeg: str, src: Path, dst: Path) -> bool:
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", str(src),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                 "-movflags", "+faststart", str(dst)],
                capture_output=True, timeout=900,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0

    def download_video(
        self,
        url: str,
        dest_path: Path,
        chunk_cb: Optional[Callable[[int, int], None]] = None,
        stop_flag: Optional[Callable[[], bool]] = None,
        ffmpeg_location: str = "",
    ):
        """Tải 1 video TikTok (link trang, dạng .../@user/video/<id>) về
        `dest_path`. Cùng quy ước với DouyinClient.download_video: kiểm tra
        `stop_flag` liên tục, hủy giữa chừng thì xóa file dở và raise
        DownloadCancelled.

        Video gắn giỏ hàng (TikTok Shop) hay chỉ trả bản HEVC hoặc định dạng lạ
        -> tải ra file chỉ có tiếng. Vì vậy: liệt kê định dạng, thử lần lượt từng
        bản CÓ HÌNH (H.264 trước), kiểm tra file bằng ffprobe; nếu chỉ còn HEVC
        thì chuyển sang H.264 bằng ffmpeg (nếu có)."""
        if stop_flag and stop_flag():
            raise DownloadCancelled("Đã dừng trước khi bắt đầu tải.")
        yt_dlp = _import_ytdlp()
        logger = _CollectLogger()
        cookie_path = self._make_cookie_file()
        ffmpeg = ffmpeg_location or shutil.which("ffmpeg") or ""
        if ffmpeg and Path(ffmpeg).is_dir():
            ffmpeg = next(
                (str(Path(ffmpeg) / n) for n in ("ffmpeg.exe", "ffmpeg")
                 if (Path(ffmpeg) / n).is_file()), "",
            )
        ffprobe = None
        if ffmpeg:
            from .audio_merger import find_ffprobe
            ffprobe = find_ffprobe(ffmpeg)

        def hook(d: dict):
            if stop_flag and stop_flag():
                raise DownloadCancelled("Đã dừng theo yêu cầu người dùng giữa chừng.")
            if chunk_cb and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                chunk_cb(int(d.get("downloaded_bytes") or 0), int(total))

        def cleanup():
            for suffix in (".part", ".ytdl"):
                dest_path.with_name(dest_path.name + suffix).unlink(missing_ok=True)

        def run(fmt: str, out: Path, info_only: bool = False):
            opts = self._base_opts(logger, cookie_path)
            opts.update({
                # %% để dấu % trong đường dẫn không bị hiểu là mẫu đặt tên của yt-dlp
                "outtmpl": {"default": str(out).replace("%", "%%")},
                "format": fmt,
                "format_sort": ["vcodec:h264", "res", "br"],
                "noplaylist": True,
                "overwrites": True,
                "retries": 3,
                "progress_hooks": [hook],
            })
            if ffmpeg:
                opts["ffmpeg_location"] = ffmpeg
            with yt_dlp.YoutubeDL(opts) as ydl:
                if info_only:
                    return ydl.extract_info(url, download=False)
                ydl.download([url])
                return None

        tried: list[str] = []
        try:
            try:
                info = run("all", dest_path, info_only=True) or {}
            except Exception as exc:
                if isinstance(exc, DownloadCancelled) or (stop_flag and stop_flag()):
                    raise
                info = {}
            formats = [f for f in (info.get("formats") or []) if f.get("format_id")]
            video_fmts = sorted(
                (f for f in formats if self._is_video_fmt(f)),
                key=lambda f: (self._codec_rank(f), -(f.get("height") or 0),
                               -(f.get("tbr") or 0)),
            )
            # Không liệt kê được định dạng -> dùng bộ chọn tổng quát (như trước)
            candidates = [f["format_id"] for f in video_fmts] or [
                "b[vcodec!=none][ext=mp4]/b[vcodec!=none]/b"
            ]
            hevc_file: Optional[Path] = None
            ok = False
            for fmt in candidates[:6]:
                if stop_flag and stop_flag():
                    raise DownloadCancelled("Đã dừng theo yêu cầu người dùng giữa chừng.")
                cleanup()
                dest_path.unlink(missing_ok=True)
                try:
                    run(fmt, dest_path)
                except Exception as exc:
                    if isinstance(exc, DownloadCancelled) or (stop_flag and stop_flag()):
                        raise
                    tried.append(f"{fmt}: lỗi tải")
                    continue
                if not dest_path.exists():
                    tried.append(f"{fmt}: không có file")
                    continue
                probe = self._probe_video(ffprobe, dest_path)
                if probe is None:            # không kiểm tra được -> tin file đã tải
                    ok = True
                    break
                if not probe:
                    tried.append(f"{fmt}: file không có hình")
                    continue
                codec = (probe.get("codec_name") or "").lower()
                if codec in ("hevc", "h265") and ffmpeg:
                    # Giữ lại, thử bản khác trước; hết bản khác mới chuyển mã
                    if hevc_file is None:
                        hevc_file = dest_path.with_name(dest_path.name + ".hevc")
                        hevc_file.unlink(missing_ok=True)
                        dest_path.replace(hevc_file)
                    tried.append(f"{fmt}: HEVC")
                    continue
                ok = True
                break

            if not ok and hevc_file is not None and hevc_file.exists():
                if self._transcode_h264(ffmpeg, hevc_file, dest_path):
                    ok = True
            if not ok:
                if hevc_file is not None and hevc_file.exists():
                    hevc_file.replace(dest_path)   # còn hơn không: giữ bản HEVC
                    ok = True
                else:
                    dest_path.unlink(missing_ok=True)
                    kinds = ", ".join(
                        f"{f.get('format_id')}({f.get('vcodec') or '?'}/{f.get('acodec') or '?'})"
                        for f in formats[:8]
                    ) or "không liệt kê được"
                    raise TikTokAPIError(
                        "TikTok không trả bản video CÓ HÌNH cho clip này (thường gặp ở "
                        "video gắn giỏ hàng) — đã bỏ file chỉ có tiếng.\n"
                        f"Định dạng nhận được: {kinds}\n"
                        + ("Đã thử: " + "; ".join(tried) + "\n" if tried else "")
                        + "Thử: dán Cookie TikTok (đã đăng nhập) ở Cài đặt, hoặc mở lại app để tự cập nhật yt-dlp."
                    )
        except Exception as exc:
            cleanup()
            if isinstance(exc, (TikTokAPIError, DownloadCancelled)):
                if isinstance(exc, DownloadCancelled):
                    dest_path.unlink(missing_ok=True)
                raise
            if stop_flag and stop_flag():
                raise DownloadCancelled(
                    "Đã dừng theo yêu cầu người dùng giữa chừng."
                ) from None
            raise TikTokAPIError(_friendly_error(exc)) from exc
        finally:
            cleanup()
            if dest_path.with_name(dest_path.name + ".hevc").exists():
                dest_path.with_name(dest_path.name + ".hevc").unlink(missing_ok=True)
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
