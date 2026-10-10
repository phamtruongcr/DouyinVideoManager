"""
app_logger.py
=============
Ghi nhật ký (log) ra FILE để dễ kiểm tra khi có lỗi.

  * File: ~/.douyin_video_manager_logs/app.log  (tự xoay vòng: 1 MB x 5 file cũ)
  * Mức INFO (mặc định): các bước chính — lấy danh sách, tải, lỗi.
    Mức DEBUG: thêm chi tiết từng request/response (đổi trong Cài đặt → Nhật ký).
  * TỰ ĐỘNG CHE thông tin nhạy cảm (Cookie, msToken, a_bogus, API key...) nên
    có thể gửi file log cho người khác để nhờ xem lỗi.
  * Lỗi không bắt được (exception) ở luồng chính / luồng nền / callback Tkinter
    cũng được ghi lại kèm traceback.

Mọi lỗi của chính hệ thống log (không ghi được file...) bị nuốt để KHÔNG BAO GIỜ
làm app bị sập.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from . import config

LOGGER_NAME = "douyin_manager"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5

_setup_lock = threading.Lock()
_file_handler: Optional[logging.Handler] = None


# ----------------------------------------------------------- che bí mật --
_SECRET_NAMES = (
    "msToken", "a_bogus", "X-Bogus", "verifyFp", "s_v_web_id", "ttwid", "odin_tt",
    "sessionid", "sessionid_ss", "sid_tt", "sid_guard", "sid_ucp_v1", "ssid_ucp_v1",
    "uid_tt", "uid_tt_ss", "passport_csrf_token", "passport_csrf_token_default",
    "tt_chain_token", "tt_csrf_token", "ttwid", "c_user", "xs", "fr", "datr", "sb",
    "access_token", "token", "api_key", "apikey", "key",
)
_SECRET_NAME_RE = "|".join(re.escape(n) for n in sorted(set(_SECRET_NAMES), key=len, reverse=True))
_PATTERNS = [
    # Cookie: a=1; b=2   /  'cookie': '...'  -> che toàn bộ giá trị
    (re.compile(r"(?i)(\bcookie\b['\"]?\s*[:=]\s*['\"]?)([^\r\n'\"]+)"), r"\1<đã che>"),
    # name=value (query string / cookie) với tên nhạy cảm
    (re.compile(r"(?i)(?<![A-Za-z0-9_])(" + _SECRET_NAME_RE + r")=([^&;\s\"'<>]+)"), r"\1=<đã che>"),
    # Khóa Gemini / Google API
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "<đã che API key>"),
    # Authorization / Bearer
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)([^\r\n'\"]+)"), r"\1<đã che>"),
]


def redact(text: str) -> str:
    """Che Cookie, token, chữ ký, API key trong 1 chuỗi."""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return redact(super().format(record))


# ------------------------------------------------------------ thiết lập --
def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_dir() -> Path:
    return config.LOG_DIR


def log_file_path() -> Path:
    return config.LOG_DIR / config.LOG_FILE_NAME


def _normalize_level(level: Optional[str]) -> int:
    name = (level or config.DEFAULT_LOG_LEVEL).upper()
    if name not in config.LOG_LEVEL_OPTIONS:
        name = config.DEFAULT_LOG_LEVEL
    return getattr(logging, name)


def setup_logging(level: Optional[str] = None) -> Path:
    """Bật ghi log ra file (gọi 1 lần lúc khởi động; gọi lại chỉ đổi mức log).
    Trả về đường dẫn file log."""
    global _file_handler
    root = get_logger()
    lvl = _normalize_level(level)
    with _setup_lock:
        root.setLevel(lvl)
        root.propagate = False
        if _file_handler is None:
            fmt = RedactingFormatter(
                "%(asctime)s | %(levelname)-7s | %(threadName)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            try:
                config.LOG_DIR.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    log_file_path(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                    encoding="utf-8",
                )
                handler.setFormatter(fmt)
                root.addHandler(handler)
                _file_handler = handler
            except (OSError, ValueError) as exc:
                root.addHandler(logging.NullHandler())
                print(f"[log] Không ghi được file log: {exc}", file=sys.stderr)
            # Khi chạy từ terminal: in thêm cảnh báo/lỗi ra console cho tiện
            if sys.stderr is not None and not getattr(sys, "frozen", False):
                console = logging.StreamHandler(sys.stderr)
                console.setLevel(logging.WARNING)
                console.setFormatter(fmt)
                root.addHandler(console)
        if _file_handler is not None:
            _file_handler.setLevel(lvl)
    return log_file_path()


def set_level(level: str):
    setup_logging(level)
    get_logger().info("Đổi mức log sang %s", logging.getLevelName(_normalize_level(level)))


def current_level_name() -> str:
    return logging.getLevelName(get_logger().level)


# ------------------------------------------------- lỗi không bắt được --
def install_exception_hooks():
    """Ghi traceback của mọi exception không bắt được (luồng chính + luồng nền)."""
    log = get_logger("crash")

    def main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Lỗi không bắt được (luồng chính)", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical(
            "Lỗi không bắt được trong luồng %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = main_hook
    threading.excepthook = thread_hook


def log_startup_info(cfg: Optional[dict] = None):
    """Ghi thông tin môi trường 1 lần lúc mở app — rất hữu ích khi tìm lỗi."""
    log = get_logger("startup")
    log.info("=" * 60)
    log.info("Khởi động Douyin Video Manager")
    log.info("Python %s | %s %s | frozen=%s",
             sys.version.split()[0], platform.system(), platform.release(),
             bool(getattr(sys, "frozen", False)))
    for mod, label in (("yt_dlp", "yt-dlp"), ("playwright", "playwright"),
                       ("requests", "requests"), ("PIL", "Pillow")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", None)
            if ver is None and mod == "yt_dlp":
                from yt_dlp.version import __version__ as ver  # type: ignore
            if ver is None and mod == "playwright":
                from importlib.metadata import version as _v
                ver = _v("playwright")
            log.info("%s: %s", label, ver or "có (không rõ phiên bản)")
        except Exception:  # noqa: BLE001
            log.info("%s: KHÔNG có", label)
    if cfg is not None:
        # Chỉ ghi TÊN cookie (không ghi giá trị) để biết có thiếu cookie quan trọng không
        for key, label in (("cookie", "Douyin"), ("tiktok_cookie", "TikTok"),
                           ("facebook_cookie", "Facebook")):
            log.info("Cookie %s: %s", label, describe_cookie(cfg.get(key, "")))
        log.info("Gemini API key: %s", "đã nhập" if cfg.get("gemini_api_key") else "chưa nhập")


def describe_cookie(cookie: str) -> str:
    """Mô tả cookie mà KHÔNG lộ giá trị: số lượng + danh sách tên."""
    names = []
    for part in (cookie or "").split(";"):
        n, sep, _v = part.strip().partition("=")
        if sep and n.strip():
            names.append(n.strip())
    if not names:
        return "TRỐNG"
    shown = ", ".join(names[:25]) + (" ..." if len(names) > 25 else "")
    return f"{len(names)} mục ({len(cookie)} ký tự): {shown}"


# -------------------------------------------------- tiện ích cho giao diện --
def read_tail(max_lines: int = 200) -> str:
    """Đọc N dòng cuối của file log hiện tại (đã che bí mật khi ghi)."""
    try:
        with open(log_file_path(), "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    return "".join(lines[-max_lines:])


def clear_logs() -> bool:
    """Xóa file log (và các file xoay vòng cũ)."""
    ok = True
    flush_handler()
    try:
        for p in config.LOG_DIR.glob(config.LOG_FILE_NAME + "*"):
            try:
                if p == log_file_path() and _file_handler is not None:
                    # file đang mở: cắt về 0 byte thay vì xóa
                    open(p, "w", encoding="utf-8").close()
                else:
                    p.unlink()
            except OSError:
                ok = False
    except OSError:
        ok = False
    return ok


def flush_handler():
    if _file_handler is not None:
        try:
            _file_handler.flush()
        except Exception:  # noqa: BLE001
            pass


def open_path(path: Path) -> bool:
    """Mở file/thư mục bằng ứng dụng mặc định của hệ điều hành."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:  # noqa: BLE001
        return False
