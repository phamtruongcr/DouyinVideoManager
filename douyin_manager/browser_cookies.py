"""
browser_cookies.py
==================
Tự đọc Cookie douyin.com / tiktok.com / facebook.com từ trình duyệt đang đăng nhập trên máy
(dùng bộ đọc cookie có sẵn của yt-dlp: giải mã DPAPI/Keychain/libsecret), rồi
đổi thành chuỗi "a=1; b=2" để dán vào ô Cookie của app.

Lợi ích so với dán tay / ghi cứng vào code: cookie luôn MỚI (không bị hết hạn),
không phải tự mở DevTools, và không để lộ cookie trong mã nguồn / Git.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# Thứ tự hiển thị trong combobox. Firefox đọc ổn định nhất trên mọi hệ điều hành.
BROWSERS = ["firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]

# Nền tảng -> các tên miền cần lấy cookie
PLATFORM_DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "tiktok": ("tiktok.com",),
    "facebook": ("facebook.com",),
}


# Thư mục dữ liệu của các trình duyệt nhân Chromium (chứa Default, Profile 1, ...)
def _chromium_user_data_dir(browser: str) -> Optional[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        rel = {
            "chrome": "Google/Chrome", "edge": "Microsoft Edge",
            "brave": "BraveSoftware/Brave-Browser", "chromium": "Chromium",
            "vivaldi": "Vivaldi",
        }
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        rel = {
            "chrome": "Google/Chrome/User Data", "edge": "Microsoft/Edge/User Data",
            "brave": "BraveSoftware/Brave-Browser/User Data",
            "chromium": "Chromium/User Data", "vivaldi": "Vivaldi/User Data",
        }
    else:
        base = home / ".config"
        rel = {
            "chrome": "google-chrome", "edge": "microsoft-edge",
            "brave": "BraveSoftware/Brave-Browser", "chromium": "chromium",
            "vivaldi": "vivaldi",
        }
    return (base / rel[browser]) if browser in rel else None


def list_profiles(browser: str) -> list[tuple[str, str]]:
    """Dò các profile của trình duyệt nhân Chromium trên máy. Trả về danh sách
    (tên_thư_mục, tên_hiển_thị), vd ("Profile 1", "Công việc"). Rỗng nếu trình
    duyệt không có profile kiểu này (Firefox/Safari...) hoặc không tìm thấy."""
    root = _chromium_user_data_dir((browser or "").lower())
    if not root or not root.is_dir():
        return []
    result: list[tuple[str, str]] = []
    for d in sorted(root.iterdir(), key=lambda p: (p.name != "Default", p.name)):
        if not d.is_dir() or not (d.name == "Default" or d.name.startswith("Profile ")):
            continue
        if not (d / "Cookies").exists() and not (d / "Network" / "Cookies").exists():
            continue
        name = d.name
        try:
            prefs = json.loads((d / "Preferences").read_text(encoding="utf-8"))
            name = (prefs.get("profile") or {}).get("name") or d.name
        except (OSError, ValueError):
            pass
        result.append((d.name, name))
    return result


class BrowserCookieError(Exception):
    pass


class _QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _hint_for(browser: str, raw: str) -> str:
    low = raw.lower()
    if browser in ("chrome", "edge", "brave", "opera", "vivaldi", "chromium"):
        return (
            f"Không đọc được Cookie từ {browser}. Thử: (1) ĐÓNG HẲN trình duyệt rồi "
            "bấm lại; (2) dùng Firefox (đọc ổn định nhất — Chrome/Edge bản mới trên "
            "Windows mã hóa cookie kiểu 'app-bound' nên app không giải mã được); "
            "(3) hoặc dán Cookie bằng tay.\n\nChi tiết: " + raw
        )
    if "permission" in low or "operation not permitted" in low:
        return (
            "Không có quyền đọc dữ liệu trình duyệt. Trên macOS hãy cấp quyền "
            "'Full Disk Access' cho Terminal/app rồi thử lại.\n\nChi tiết: " + raw
        )
    return f"Không đọc được Cookie từ {browser}: {raw}"


def _jar_to_strings(jar) -> dict[str, str]:
    result: dict[str, str] = {}
    for platform, domains in PLATFORM_DOMAINS.items():
        pairs: dict[str, str] = {}
        for c in jar:
            dom = (c.domain or "").lstrip(".").lower()
            if not any(dom == d or dom.endswith("." + d) for d in domains):
                continue
            try:
                if c.is_expired():
                    continue
            except Exception:
                pass
            if c.name and c.value is not None:
                pairs[c.name] = c.value  # trùng tên: giữ cái đọc sau
        if pairs:
            result[platform] = "; ".join(f"{k}={v}" for k, v in pairs.items())
    return result


def read_cookie_strings(browser: str, profile: Optional[str] = None) -> dict[str, str]:
    """Đọc cookie từ `browser`. Trả về {"douyin": "a=1; b=2", "tiktok": "..."}
    — chỉ có khóa của nền tảng nào TÌM THẤY cookie (dict rỗng = trình duyệt đó
    chưa từng đăng nhập/truy cập cả 2 trang). Raise BrowserCookieError khi
    không đọc được trình duyệt."""
    browser = (browser or "").strip().lower()
    if browser not in BROWSERS:
        raise BrowserCookieError(f"Trình duyệt không được hỗ trợ: {browser!r}")
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as exc:
        raise BrowserCookieError(
            "Chưa cài yt-dlp (cần để đọc cookie trình duyệt). Chạy: pip install -U yt-dlp"
        ) from exc
    try:
        jar = extract_cookies_from_browser(browser, profile, _QuietLogger())
    except Exception as exc:  # DownloadError, sqlite3.Error, PermissionError...
        raise BrowserCookieError(_hint_for(browser, str(exc))) from exc
    return _jar_to_strings(jar)
