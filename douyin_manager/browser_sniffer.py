"""
browser_sniffer.py
==================
Lấy danh sách video của 1 kênh TikTok / Facebook bằng **Playwright**: mở trình
duyệt ẩn (headless), tải trang như người dùng thật rồi CUỘN trang, đồng thời
"nghe lén" (Network Interception) các response JSON mà chính trang web gọi tới
API nội bộ của nền tảng. Nhờ đó lấy được id/link/ngày/view/tym từ JSON mà
KHÔNG phải bóc tách HTML (React/Vue đổi giao diện cũng không ảnh hưởng).

  - TikTok  : response của /api/post/item_list/ (và dữ liệu nhúng trong trang)
  - Facebook: response GraphQL (/api/graphql/) và dữ liệu JSON nhúng trong trang

Cách làm "walker" (duyệt đệ quy mọi dict trong JSON, nhận diện video theo bộ
khóa đặc trưng) cố tình không phụ thuộc đường dẫn cố định trong JSON, nên khi
nền tảng đổi cấu trúc lồng nhau vẫn thường chạy được.

Việc TẢI video vẫn dùng yt-dlp (xem tiktok_client / facebook_client).

Thứ tự thử trình duyệt: Chrome cài sẵn trên máy -> Edge -> Chromium của
Playwright -> (nếu chưa có) tự cài Chromium qua driver của Playwright.
Mọi lỗi "không dùng được Playwright" đều được ném dưới dạng `SnifferUnavailable`
để nơi gọi lùi về yt-dlp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Optional

from .fetch_filters import ItemCollector, _to_int_or_none

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Cuộn: dừng khi liên tiếp chừng này lần cuộn mà không có video mới nào
IDLE_SCROLLS_LIMIT = 6
SCROLL_PAUSE_MS = 1300
NAV_TIMEOUT_MS = 45000
# Trần an toàn (vòng cuộn) để không chạy vô hạn trên kênh khổng lồ
MAX_SCROLLS = 400


class SnifferUnavailable(Exception):
    """Playwright/trình duyệt không dùng được -> nơi gọi nên lùi về yt-dlp."""


class SnifferBlocked(Exception):
    """Trang yêu cầu đăng nhập / chặn bot, không lấy được video nào."""


# ======================================================================
#  PARSER (thuần JSON, không phụ thuộc Playwright -> test độc lập được)
# ======================================================================
def _walk(node) -> Iterator[dict]:
    """Duyệt đệ quy mọi dict trong cấu trúc JSON (không dùng đệ quy Python
    sâu: dùng stack để an toàn với JSON lồng rất sâu của Facebook)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _iso_or_epoch(v) -> int:
    n = _to_int_or_none(v) or 0
    return n if n > 0 else 0


def parse_tiktok_json(data) -> list[dict]:
    """Tìm video TikTok trong 1 JSON bất kỳ. Nhận diện: dict có `id` dạng số,
    `video` (dict) và `createTime`; tên tác giả lấy từ `author` (dict hoặc chuỗi)."""
    out: list[dict] = []
    for d in _walk(data):
        vid = d.get("id")
        video = d.get("video")
        if not (isinstance(video, dict) and "createTime" in d and vid and str(vid).isdigit()):
            continue
        author = d.get("author")
        uname = ""
        if isinstance(author, dict):
            uname = author.get("uniqueId") or ""
        elif isinstance(author, str):
            uname = author
        if not uname and isinstance(d.get("authorInfo"), dict):
            uname = d["authorInfo"].get("uniqueId") or ""
        # Thiếu tên tác giả vẫn có link hợp lệ dạng /@_/video/<id> (yt-dlp xử lý được)
        url = f"https://www.tiktok.com/@{uname or '_'}/video/{vid}"
        stats = d.get("stats") if isinstance(d.get("stats"), dict) else (
            d.get("statsV2") if isinstance(d.get("statsV2"), dict) else {}
        )
        desc = (d.get("desc") or "").strip() or f"(không có mô tả) {vid}"
        dur = video.get("duration") or 0
        try:
            dur = int(round(float(dur)))
        except (TypeError, ValueError):
            dur = 0
        out.append({
            "id": str(vid),
            "desc": desc,
            "url": url,
            "create_time": _iso_or_epoch(d.get("createTime")),
            "duration_s": dur,
            "view_count": _to_int_or_none(stats.get("playCount")),
            "like_count": _to_int_or_none(stats.get("diggCount")),
            "platform": "tiktok",
        })
    return out


_FB_ID_RE = re.compile(r"^\d{6,}$")


def _fb_text(v) -> str:
    """Facebook hay bọc chữ trong {"text": "..."}."""
    if isinstance(v, dict):
        v = v.get("text")
    return v.strip() if isinstance(v, str) else ""


def parse_facebook_json(data) -> list[dict]:
    """Tìm video Facebook trong 1 JSON (thường là GraphQL). Nhận diện: dict
    `__typename == "Video"` có `id` số kèm ít nhất 1 trường video đặc trưng
    (playable_url / permalink_url / length_in_second / creation_time...)."""
    out: list[dict] = []
    for d in _walk(data):
        if d.get("__typename") != "Video":
            continue
        vid = str(d.get("id") or "")
        if not _FB_ID_RE.match(vid):
            continue
        has_video_field = any(
            k in d for k in (
                "playable_url", "playable_url_quality_hd", "browser_native_hd_url",
                "browser_native_sd_url", "permalink_url", "length_in_second",
                "playable_duration_in_ms",
            )
        )
        if not has_video_field:
            continue
        permalink = d.get("permalink_url") or d.get("url") or ""
        if isinstance(permalink, str) and permalink.startswith("/"):
            permalink = "https://www.facebook.com" + permalink
        url = permalink if isinstance(permalink, str) and permalink.startswith("http") \
            else f"https://www.facebook.com/watch/?v={vid}"
        desc = (
            _fb_text(d.get("title")) or _fb_text(d.get("message"))
            or _fb_text(d.get("savable_description")) or _fb_text(d.get("description"))
        )
        desc = desc.splitlines()[0].strip() if desc else ""
        dur = d.get("length_in_second")
        if dur is None and d.get("playable_duration_in_ms") is not None:
            dur = (_to_int_or_none(d.get("playable_duration_in_ms")) or 0) / 1000
        try:
            dur = int(round(float(dur or 0)))
        except (TypeError, ValueError):
            dur = 0
        reactions = d.get("reaction_count")
        likes = _to_int_or_none(reactions.get("count")) if isinstance(reactions, dict) \
            else _to_int_or_none(reactions)
        out.append({
            "id": vid,
            "desc": desc or f"(không có mô tả) {vid}",
            "url": url,
            "create_time": _iso_or_epoch(d.get("creation_time") or d.get("publish_time")),
            "duration_s": dur,
            "view_count": _to_int_or_none(d.get("play_count") or d.get("video_view_count")),
            "like_count": likes,
            "platform": "facebook",
        })
    return out


_SCRIPT_JSON_RE = re.compile(
    r"<script[^>]*type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>", re.S | re.I
)
_SCRIPT_ID_JSON_RE = re.compile(
    r"<script[^>]*id=[\"']__UNIVERSAL_DATA_FOR_REHYDRATION__[\"'][^>]*>(.*?)</script>", re.S | re.I
)


def iter_json_chunks(text: str) -> Iterator[object]:
    """Tách 1 body response thành các giá trị JSON. Hỗ trợ: JSON thường, nhiều
    JSON nối nhau (mỗi dòng 1 JSON - kiểu GraphQL batch của Facebook, có thể có
    tiền tố `for (;;);`), và các thẻ <script type="application/json"> trong HTML."""
    text = (text or "").strip()
    if not text:
        return
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    if text[:1] in "{[":
        dec = json.JSONDecoder()
        pos, n = 0, len(text)
        while pos < n:
            while pos < n and text[pos] in " \r\n\t":
                pos += 1
            if pos >= n:
                break
            try:
                obj, end = dec.raw_decode(text, pos)
            except ValueError:
                break
            yield obj
            pos = end
        return
    if "<script" in text.lower():
        done_spans: set[int] = set()
        for m in list(_SCRIPT_ID_JSON_RE.finditer(text)) + list(_SCRIPT_JSON_RE.finditer(text)):
            if m.start(1) in done_spans:   # cùng 1 thẻ script khớp cả 2 regex
                continue
            done_spans.add(m.start(1))
            try:
                yield json.loads(m.group(1))
            except ValueError:
                continue


def parse_response_text(platform: str, text: str) -> list[dict]:
    parser = parse_tiktok_json if platform == "tiktok" else parse_facebook_json
    items: list[dict] = []
    for obj in iter_json_chunks(text):
        items.extend(parser(obj))
    return items


# URL của response đáng để đọc (lọc sớm cho nhẹ)
_TIKTOK_API_HINTS = ("/api/post/item_list", "/api/user/detail", "/api/creator/item_list",
                     "/api/repost/item_list", "item_list")
_FB_API_HINTS = ("/api/graphql", "graphql", "/ajax/bulk-route-definitions", "/ajax/")


def _wanted_response(platform: str, url: str, ctype: str, is_document: bool) -> bool:
    u = url.lower()
    if is_document:
        return True
    if "json" not in ctype and "javascript" not in ctype and "text/plain" not in ctype \
            and "text/html" not in ctype:
        return False
    hints = _TIKTOK_API_HINTS if platform == "tiktok" else _FB_API_HINTS
    return any(h in u for h in hints)


# ======================================================================
#  PLAYWRIGHT
# ======================================================================
def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright, Error as PWError  # noqa: WPS433
    except ImportError as exc:
        raise SnifferUnavailable(
            "Chưa cài Playwright. Chạy: pip install playwright"
        ) from exc
    return sync_playwright, PWError


def _install_chromium(log: Callable[[str], None]) -> bool:
    """Tải Chromium của Playwright bằng chính driver đi kèm thư viện (chạy được
    cả khi app đã đóng gói bằng PyInstaller, vì không cần `python -m playwright`)."""
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        driver = compute_driver_executable()
        cmd = list(driver) if isinstance(driver, (tuple, list)) else [str(driver)]
        cmd += ["install", "chromium"]
        log("Đang tải trình duyệt Chromium cho Playwright (chỉ lần đầu, ~150MB)...")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(
            cmd, env=get_driver_env(), capture_output=True, text=True, timeout=900, **kwargs
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _launch_browser(pw, log: Callable[[str], None]):
    """Mở trình duyệt headless: Chrome -> Edge -> Chromium của Playwright."""
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    last_exc: Optional[Exception] = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = {"headless": True, "args": args}
            if channel:
                kw["channel"] = channel
            return pw.chromium.launch(**kw)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    # Chưa có trình duyệt nào -> thử tự cài Chromium một lần
    msg = str(last_exc).lower() if last_exc else ""
    if "executable doesn't exist" in msg or "playwright install" in msg:
        if _install_chromium(log):
            try:
                return pw.chromium.launch(headless=True, args=args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
    raise SnifferUnavailable(
        "Không mở được trình duyệt cho Playwright (cần Chrome/Edge hoặc chạy "
        "`playwright install chromium`). " + (str(last_exc).splitlines()[0][:200] if last_exc else "")
    )


def _cookie_list(cookie: str, domain: str) -> list[dict]:
    out = []
    for part in (cookie or "").split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if sep and name:
            out.append({"name": name, "value": value.strip(), "domain": domain,
                        "path": "/", "secure": True})
    return out


_PLATFORM_DOMAIN = {"tiktok": ".tiktok.com", "facebook": ".facebook.com"}


def sniff_profile_videos(
    platform: str,
    profile_url: str,
    stop_flag: Callable[[], bool],
    collector: ItemCollector,
    cookie: str = "",
    progress_cb: Optional[Callable[[int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
    headless: bool = True,
) -> None:
    """Mở `profile_url`, cuộn trang và đẩy từng video bắt được vào `collector`
    (theo thứ tự xuất hiện = mới nhất trước). Dừng khi: collector báo đủ, người
    dùng bấm dừng, hoặc hết video mới.

    Raise SnifferUnavailable (không dùng được Playwright) hoặc SnifferBlocked
    (trang chặn/đòi đăng nhập và không lấy được gì)."""
    if platform not in _PLATFORM_DOMAIN:
        raise SnifferUnavailable(f"Chưa hỗ trợ nền tảng: {platform}")
    log = log or (lambda _m: None)
    sync_playwright, PWError = _import_playwright()

    pending: list = []          # response chờ xử lý (xử lý ở luồng chính, tránh gọi API trong handler)
    seen: set[str] = set()
    got = 0

    def on_response(resp):
        pending.append(resp)

    with sync_playwright() as pw:
        browser = _launch_browser(pw, log)
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT, locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            if cookie:
                ctx.add_cookies(_cookie_list(cookie, _PLATFORM_DOMAIN[platform]))

            # Không tải ảnh/video/font -> nhanh và nhẹ (JSON API vẫn đi qua bình thường)
            def route(r):
                if r.request.resource_type in ("image", "media", "font"):
                    r.abort()
                else:
                    r.continue_()
            ctx.route("**/*", route)

            page = ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            page.on("response", on_response)

            def drain() -> int:
                """Đọc các response đã gom, đẩy video mới vào collector.
                Trả về 1 nếu collector báo đã đủ."""
                nonlocal got
                done = 0
                while pending:
                    resp = pending.pop(0)
                    try:
                        req = resp.request
                        ctype = (resp.headers.get("content-type") or "").lower()
                        is_doc = req.resource_type == "document"
                        if not _wanted_response(platform, resp.url, ctype, is_doc):
                            continue
                        text = resp.text()
                    except Exception:  # noqa: BLE001 - response đã bị hủy/đóng, bỏ qua
                        continue
                    for item in parse_response_text(platform, text):
                        if item["id"] in seen:
                            continue
                        seen.add(item["id"])
                        got += 1
                        if collector.feed(item):
                            done = 1
                        if progress_cb:
                            progress_cb(len(collector.items))
                        if done or stop_flag():
                            return 1
                return done

            try:
                page.goto(profile_url, wait_until="domcontentloaded")
            except PWError as exc:
                raise SnifferUnavailable(f"Không mở được trang: {str(exc).splitlines()[0][:200]}") from exc

            page.wait_for_timeout(2500)
            if drain() or stop_flag():
                return

            idle = 0
            for _ in range(MAX_SCROLLS):
                if stop_flag():
                    break
                before = got
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(SCROLL_PAUSE_MS)
                except PWError:
                    break
                if drain() or stop_flag():
                    break
                idle = idle + 1 if got == before else 0
                if idle >= IDLE_SCROLLS_LIMIT:
                    break

            if got == 0:
                final_url = page.url.lower()
                if "login" in final_url or "checkpoint" in final_url:
                    raise SnifferBlocked(
                        "Trang yêu cầu đăng nhập. Hãy dán Cookie (đã đăng nhập) vào mục Cài đặt."
                    )
                raise SnifferBlocked(
                    "Trình duyệt ẩn không bắt được video nào (kênh trống/riêng tư hoặc nền tảng "
                    "đang chặn bot)."
                )
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
