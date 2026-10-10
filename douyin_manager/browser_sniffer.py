"""
browser_sniffer.py
==================
Lấy danh sách video của 1 kênh TikTok / Facebook bằng **Playwright**: mở trình
duyệt ẩn (headless), tải trang như người dùng thật rồi CUỘN trang, đồng thời
"nghe lén" (Network Interception) các response JSON mà chính trang web gọi tới
API nội bộ của nền tảng. Nhờ đó lấy được id/link/ngày/view/tym từ JSON mà
KHÔNG phải bóc tách HTML (React/Vue đổi giao diện cũng không ảnh hưởng).

  - Douyin  : response của /aweme/v1/web/aweme/post/ (trình duyệt tự tạo chữ ký a_bogus/msToken)
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

from .app_logger import get_logger
from .fetch_filters import ItemCollector, _to_int_or_none

logger = get_logger("sniffer")

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
        # Đẩy theo thứ tự ĐẢO để pop() ra đúng thứ tự gốc của JSON (video mới
        # nhất vẫn đứng trước) — bộ lọc/ItemCollector dựa vào thứ tự này.
        if isinstance(cur, dict):
            yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


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


def parse_douyin_json(data) -> list[dict]:
    """Tìm video Douyin trong 1 JSON bất kỳ: lấy mọi danh sách `aweme_list`
    (đúng cấu trúc endpoint aweme/v1/web/aweme/post/ trả về) theo ĐÚNG thứ tự
    gốc (mới nhất trước) và đổi từng phần tử bằng cùng hàm mà đường gọi API
    trực tiếp dùng (`douyin_client._aweme_to_item`), nên item giống hệt nhau."""
    from .douyin_client import _aweme_to_item  # import muộn: tránh import vòng

    out: list[dict] = []
    for d in _walk(data):
        lst = d.get("aweme_list")
        if not isinstance(lst, list):
            continue
        for raw in lst:
            if isinstance(raw, dict):
                item = _aweme_to_item(raw)
                if item:
                    out.append(item)
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
    parser = {
        "douyin": parse_douyin_json,
        "tiktok": parse_tiktok_json,
    }.get(platform, parse_facebook_json)
    items: list[dict] = []
    for obj in iter_json_chunks(text):
        items.extend(parser(obj))
    return items


# URL của response đáng để đọc (lọc sớm cho nhẹ)
_TIKTOK_API_HINTS = ("/api/post/item_list", "/api/user/detail", "/api/creator/item_list",
                     "/api/repost/item_list", "item_list")
_DOUYIN_API_HINTS = ("/aweme/v1/web/aweme/post", "/aweme/post")
_FB_API_HINTS = ("/api/graphql", "graphql", "/ajax/bulk-route-definitions", "/ajax/")


def _wanted_response(platform: str, url: str, ctype: str, is_document: bool) -> bool:
    u = url.lower()
    if is_document:
        return True
    if "json" not in ctype and "javascript" not in ctype and "text/plain" not in ctype \
            and "text/html" not in ctype:
        return False
    hints = {
        "douyin": _DOUYIN_API_HINTS,
        "tiktok": _TIKTOK_API_HINTS,
    }.get(platform, _FB_API_HINTS)
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


def _launch_browser(pw, log: Callable[[str], None], headless: bool = True):
    """Mở trình duyệt headless: Chrome -> Edge -> Chromium của Playwright."""
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    last_exc: Optional[Exception] = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = {"headless": headless, "args": args}
            if channel:
                kw["channel"] = channel
            return pw.chromium.launch(**kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không mở được trình duyệt channel=%s: %s", channel or "chromium",
                           str(exc).splitlines()[0][:200] if str(exc) else exc)
            last_exc = exc
    # Chưa có trình duyệt nào -> thử tự cài Chromium một lần
    msg = str(last_exc).lower() if last_exc else ""
    if "executable doesn't exist" in msg or "playwright install" in msg:
        if _install_chromium(log):
            try:
                return pw.chromium.launch(headless=headless, args=args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
    raise SnifferUnavailable(
        "Không mở được trình duyệt cho Playwright (cần Chrome/Edge hoặc chạy "
        "`playwright install chromium`). " + (str(last_exc).splitlines()[0][:200] if last_exc else "")
    )


def _safe_title(page) -> str:
    try:
        return page.title()
    except Exception:  # noqa: BLE001
        return ""


def _cookie_list(cookie: str, domain: str) -> list[dict]:
    out = []
    for part in (cookie or "").split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if sep and name:
            out.append({"name": name, "value": value.strip(), "domain": domain,
                        "path": "/", "secure": True})
    return out


def _build_user_agent(version: str) -> str:
    """UA khớp với phiên bản Chrome THẬT đang chạy. Trước đây UA cố định Chrome/126
    trong khi trình duyệt là 154 -> lệch dấu vân tay, rất dễ bị Douyin đẩy sang captcha."""
    major = (version or "").split(".")[0]
    if not major.isdigit():
        return USER_AGENT
    if sys.platform == "darwin":
        plat = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("linux"):
        plat = "X11; Linux x86_64"
    else:
        plat = "Windows NT 10.0; Win64; x64"
    return (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


# Chờ người dùng tự giải captcha (khi mở cửa sổ trình duyệt thật)
CAPTCHA_WAIT_S = 180


def _captcha_now(page) -> bool:
    url = (page.url or "").lower()
    title = _safe_title(page)
    return ("captcha" in url or "verify" in url
            or any(h in title for h in _CAPTCHA_TEXT_HINTS))


_PLATFORM_DOMAIN = {
    "douyin": ".douyin.com", "tiktok": ".tiktok.com", "facebook": ".facebook.com",
}

# Dấu hiệu trang đang đòi xác minh (captcha/kéo thanh trượt) — trình duyệt ẩn không tự giải được
_CAPTCHA_URL_HINTS = ("captcha", "verify", "login")
_CAPTCHA_TEXT_HINTS = ("验证码中间页", "captcha", "请完成下列验证", "拖动滑块", "Verify to continue")

# Trang kênh hiện "服务异常，重新刷新拉取数据" (lỗi tải danh sách bài đăng phía Douyin): bấm "刷新" trên
# trang thường gọi lại được API; thử vài lần trước khi bỏ cuộc.
_SERVICE_ERROR_HINTS = ("服务异常", "重新刷新", "拉取数据")
SERVICE_RETRIES = 3


def _service_error_now(page) -> bool:
    """Trang đang hiện thông báo lỗi dịch vụ (服务异常 ...) thay cho danh sách video?"""
    try:
        text = page.inner_text("body")[:4000]
    except Exception:  # noqa: BLE001
        return False
    return any(h in text for h in _SERVICE_ERROR_HINTS)


def _click_refresh(page) -> bool:
    """Bấm chữ "刷新" (làm mới) trên trang; không bấm được thì trả False để nơi gọi tải lại trang."""
    try:
        page.get_by_text("刷新", exact=True).first.click(timeout=2500)
        return True
    except Exception:  # noqa: BLE001
        return False


def sniff_profile_videos(
    platform: str,
    profile_url: str,
    stop_flag: Callable[[], bool],
    collector: ItemCollector,
    cookie: str = "",
    progress_cb: Optional[Callable[[int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
    headless: bool = True,
    interactive: bool = False,
    state_path: Optional[str] = None,
) -> None:
    """Mở `profile_url`, cuộn trang và đẩy từng video bắt được vào `collector`
    (theo thứ tự xuất hiện = mới nhất trước). Dừng khi: collector báo đủ, người
    dùng bấm dừng, hoặc hết video mới.

    Raise SnifferUnavailable (không dùng được Playwright) hoặc SnifferBlocked
    (trang chặn/đòi đăng nhập và không lấy được gì).

    interactive=True (nên đi kèm headless=False): nếu gặp captcha thì ĐỢI tối đa
    CAPTCHA_WAIT_S giây để người dùng tự kéo thanh trượt trong cửa sổ trình duyệt.
    state_path: file lưu/nạp cookie + localStorage của phiên Playwright, để lần sau
    không phải giải captcha lại."""
    if platform not in _PLATFORM_DOMAIN:
        raise SnifferUnavailable(f"Chưa hỗ trợ nền tảng: {platform}")
    user_log = log or (lambda _m: None)

    def log(msg: str):   # noqa: F811 - ghi cả ra file log lẫn callback (nếu có)
        logger.info(msg)
        user_log(msg)

    logger.info("Sniff bắt đầu | nền tảng=%s | headless=%s | url=%s | cookie=%s",
                platform, headless, profile_url, "có" if cookie else "KHÔNG")
    sync_playwright, PWError = _import_playwright()

    pending: list = []          # response chờ xử lý (xử lý ở luồng chính, tránh gọi API trong handler)
    seen: set[str] = set()
    got = 0

    seen_urls: list[str] = []    # URL aweme đã thấy (chẩn đoán khi không bắt được video)

    def on_response(resp):
        pending.append(resp)
        try:
            u = resp.url
            if "aweme" in u and len(seen_urls) < 40:
                seen_urls.append(f"{resp.status} {u[:140]}")
        except Exception:  # noqa: BLE001
            pass

    ctx = None
    solved_captcha = False
    with sync_playwright() as pw:
        browser = _launch_browser(pw, log, headless=headless)
        try:
            logger.info("Đã mở trình duyệt, phiên bản: %s", getattr(browser, "version", "?"))
            ua = _build_user_agent(getattr(browser, "version", ""))
            ctx_kw = dict(user_agent=ua, locale="zh-CN",
                          viewport={"width": 1366, "height": 900})
            if state_path and os.path.exists(state_path):
                ctx_kw["storage_state"] = state_path
                logger.info("Nạp phiên Playwright đã lưu: %s", state_path)
            ctx = browser.new_context(**ctx_kw)
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            if cookie:
                ctx.add_cookies(_cookie_list(cookie, _PLATFORM_DOMAIN[platform]))

            # Chế độ ẩn: bỏ ảnh/video/font cho nhẹ. Chế độ tương tác (người dùng giải captcha):
            # KHÔNG chặn gì cả — ảnh ghép hình của captcha cũng là "image", chặn sẽ gây lỗi [5202].
            if not interactive:
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
                        status = resp.status
                        text = resp.text()
                    except Exception:  # noqa: BLE001 - response đã bị hủy/đóng, bỏ qua
                        continue
                    parsed = parse_response_text(platform, text)
                    api_hit = not is_doc
                    if api_hit or parsed:
                        logger.info("Bắt response | HTTP %s | %d byte | %d video | %s",
                                    status, len(text), len(parsed), resp.url[:160])
                    if api_hit and not parsed:
                        logger.debug("Nội dung response (đầu): %s", text[:300].replace("\n", " "))
                    for item in parsed:
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

            solved_captcha = False
            if interactive and got == 0 and _captcha_now(page):
                log("Douyin đang hiện captcha — hãy kéo thanh trượt trong cửa sổ trình duyệt "
                    f"vừa mở (chờ tối đa {CAPTCHA_WAIT_S}s). ĐỪNG đóng cửa sổ đó.")
                deadline = time.time() + CAPTCHA_WAIT_S
                while time.time() < deadline and not stop_flag():
                    page.wait_for_timeout(1500)
                    if drain():
                        return
                    if not _captcha_now(page):
                        solved_captcha = True
                        break
                if solved_captcha:
                    log("Đã qua captcha — đang chờ trang kênh tải danh sách video...")
                    # Đợi trang tự tải (mạng sang Trung Quốc chậm); nếu vẫn chưa có thì tải lại 1 lần
                    for attempt in range(2):
                        for _ in range(20):
                            if stop_flag():
                                return
                            page.wait_for_timeout(1000)
                            if drain():
                                return
                            if got:
                                break
                        if got:
                            break
                        if attempt == 0:
                            logger.info("Chưa thấy video sau captcha -> tải lại trang")
                            try:
                                page.reload(wait_until="domcontentloaded")
                            except PWError:
                                pass

            # Trang báo "服务异常，重新刷新拉取数据": tự bấm "刷新" (hoặc tải lại trang) vài lần
            if got == 0 and not stop_flag() and not (interactive and _captcha_now(page)):
                for attempt in range(1, SERVICE_RETRIES + 1):
                    if stop_flag() or not _service_error_now(page):
                        break
                    log(f"Douyin báo 服务异常 (lỗi tải danh sách) — thử làm mới lần {attempt}/{SERVICE_RETRIES}...")
                    if not _click_refresh(page):
                        try:
                            page.reload(wait_until="domcontentloaded")
                        except PWError:
                            pass
                    for _ in range(6):
                        page.wait_for_timeout(1000)
                        if drain() or got:
                            break
                    if got:
                        break
                    page.wait_for_timeout(1500 * attempt)

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
                if idle >= (15 if interactive else IDLE_SCROLLS_LIMIT):
                    break

            logger.info("Sniff kết thúc | bắt được %d video | url cuối=%s", got, page.url[:160])
            if got == 0:
                logger.warning("Các URL 'aweme' đã thấy (%d): %s", len(seen_urls),
                               " || ".join(seen_urls) if seen_urls else "(không có)")
                try:
                    txt = page.inner_text("body")[:300].replace("\n", " | ")
                    logger.warning("Chữ hiển thị trên trang: %s", txt)
                except Exception:  # noqa: BLE001
                    pass
                if interactive:
                    try:
                        from .config import LOG_DIR
                        LOG_DIR.mkdir(parents=True, exist_ok=True)
                        shot = LOG_DIR / "last_sniff.png"
                        page.screenshot(path=str(shot))
                        logger.warning("Đã lưu ảnh chụp trang: %s", shot)
                    except Exception:  # noqa: BLE001
                        pass
                final_url = page.url.lower()
                service_err = _service_error_now(page)
                try:
                    body = (page.content() or "")[:6000]
                except Exception:  # noqa: BLE001
                    body = ""
                logger.warning("Không bắt được video nào | tiêu đề trang=%r | đầu trang: %s",
                               _safe_title(page), body[:200].replace("\n", " "))
                if platform == "douyin" and service_err:
                    raise SnifferBlocked(
                        "Chính trang Douyin báo \"服务异常，重新刷新拉取数据\" (lỗi phía Douyin / kiểm soát "
                        "rủi ro), đã tự làm mới nhưng vẫn không có danh sách video — mở kênh đó bằng trình "
                        "duyệt thường cũng sẽ thấy lỗi này. Thử: đợi vài phút rồi lấy lại; đổi mạng/IP "
                        "(tắt hoặc đổi VPN); đăng nhập lại douyin.com rồi cập nhật Cookie trong Cài đặt; "
                        "hoặc tải từng video bằng link."
                    )
                if platform == "douyin" and (
                    any(h in final_url for h in _CAPTCHA_URL_HINTS)
                    or any(h in body for h in _CAPTCHA_TEXT_HINTS)
                ):
                    raise SnifferBlocked(
                        "Douyin đang yêu cầu xác minh (captcha/kéo thanh trượt) hoặc đăng nhập — "
                        "trình duyệt ẩn không tự giải được. Hãy mở douyin.com bằng trình duyệt "
                        "thường, vượt xác minh, rồi lấy lại Cookie (Cài đặt) và thử lại."
                    )
                if "login" in final_url or "checkpoint" in final_url:
                    raise SnifferBlocked(
                        "Trang yêu cầu đăng nhập. Hãy dán Cookie (đã đăng nhập) vào mục Cài đặt."
                    )
                raise SnifferBlocked(
                    "Trình duyệt ẩn không bắt được video nào (kênh trống/riêng tư hoặc nền tảng "
                    "đang chặn bot)."
                )
        except PWError as exc:
            msg = str(exc).splitlines()[0][:200] if str(exc) else "lỗi Playwright"
            if "closed" in msg.lower():
                raise SnifferBlocked(
                    "Cửa sổ trình duyệt đã bị đóng trước khi xong (đừng đóng cửa sổ khi đang "
                    "chờ giải captcha / tải danh sách)."
                ) from exc
            raise SnifferUnavailable(f"Lỗi Playwright: {msg}") from exc
        finally:
            if state_path and (got > 0 or solved_captcha):
                try:
                    ctx.storage_state(path=state_path)
                except Exception:  # noqa: BLE001
                    pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
