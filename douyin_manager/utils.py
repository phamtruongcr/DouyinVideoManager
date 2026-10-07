"""
utils.py
========
Các hàm tiện ích dùng chung, không phụ thuộc GUI:
  - extract_clean_link: trích xuất link Douyin / TikTok / Facebook sạch từ đoạn text lộn xộn
  - extract_all_links: trích xuất TẤT CẢ link (dùng khi dán nhiều link video đơn lẻ)
  - detect_platform: nhận diện link thuộc Douyin, TikTok hay Facebook
  - resolve_facebook_link: xác định link Facebook là trang/kênh hay video
  - resolve_tiktok_link: xác định link TikTok là kênh hay video
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

# Link TikTok (kể cả link rút gọn vm./vt.tiktok.com và tiktok.com/t/...). Cho
# phép ký tự '@' vì đường dẫn kênh có dạng /@username
TIKTOK_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.|vn\.)?tiktok\.com/[A-Za-z0-9\-_./?=&%@]+"
)

# Link Facebook (www/m/web/mbasic.facebook.com, fb.watch, fb.com). Cho phép thêm
# '+', '~', ':' vì link share/permalink của Facebook hay có các ký tự này.
FACEBOOK_LINK_RE = re.compile(
    r"https?://(?:(?:www\.|m\.|web\.|mbasic\.)?facebook\.com|fb\.watch|(?:www\.)?fb\.com)"
    r"/[A-Za-z0-9\-_./?=&%@+~:]+"
)

_ALL_LINK_RES = (DOUYIN_LINK_RE, TIKTOK_LINK_RE, FACEBOOK_LINK_RE)
_TRAILING_JUNK = ").,，。；;'\"!?！？ "

_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\n\r\t]')


def extract_all_links(raw_text: str) -> list[str]:
    """Trích TẤT CẢ link Douyin / TikTok / Facebook trong 1 đoạn text bất kỳ,
    theo thứ tự xuất hiện, đã bỏ dấu câu thừa ở cuối và đã loại trùng. Dùng khi
    người dùng dán nhiều link video đơn lẻ cùng lúc (mỗi link 1 dòng)."""
    if not raw_text:
        return []
    found: list[tuple[int, str]] = []
    for regex in _ALL_LINK_RES:
        for m in regex.finditer(raw_text):
            found.append((m.start(), m.group(0).rstrip(_TRAILING_JUNK)))
    found.sort(key=lambda t: t[0])
    links: list[str] = []
    for _, link in found:
        if link and link not in links:
            links.append(link)
    return links


def extract_clean_link(raw_text: str) -> str | None:
    """Từ 1 đoạn text bất kỳ (có thể lẫn chữ Hoa/Trung, emoji, khoảng
    trắng...) -> trả về link Douyin / TikTok / Facebook sạch xuất hiện đầu
    tiên, đã bỏ dấu câu/ký tự thừa bám ở cuối."""
    links = extract_all_links(raw_text)
    return links[0] if links else None


def detect_platform(link: str) -> str | None:
    """Trả về "tiktok" | "facebook" | "douyin" | None theo tên miền của link."""
    host = (urlparse(link).hostname or "").lower()
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host in ("fb.watch", "fb.com", "www.fb.com") or host == "facebook.com" \
            or host.endswith(".facebook.com"):
        return "facebook"
    if "douyin" in host:
        return "douyin"
    return None


_TIKTOK_PROFILE_PATH_RE = re.compile(r"^/@([\w.\-]+)/?$")


def resolve_tiktok_link(link: str):
    """Xác định link TikTok là kênh hay video.

    Trả về tuple: ("profile", profile_url) | ("video", video_id)
    Link rút gọn (vm./vt.tiktok.com, tiktok.com/t/...) được theo redirect để
    lấy URL thật; link đầy đủ thì phân tích luôn, không tốn request.
    Raise RuntimeError nếu không nhận diện được."""
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    is_short = host.split(".")[0] in ("vm", "vt") or parsed.path.startswith("/t/")
    if is_short:
        try:
            resp = requests.get(
                link, headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT, allow_redirects=True,
            )
            parsed = urlparse(resp.url)
        except requests.RequestException as exc:
            raise RuntimeError(f"Không thể truy cập link TikTok: {exc}") from exc

    path = parsed.path
    m = re.search(r"/video/(\d+)", path)
    if m:
        return "video", m.group(1)
    m = _TIKTOK_PROFILE_PATH_RE.match(path)
    if m:
        return "profile", f"https://www.tiktok.com/@{m.group(1)}"
    # Dạng /@user/xxx (photo, playlist...) -> coi như link chưa hỗ trợ
    raise RuntimeError(
        "Không nhận diện được đây là link kênh TikTok. Hãy dán link dạng "
        "tiktok.com/@tên_kênh."
    )


# Đường dẫn Facebook chắc chắn là 1 VIDEO / REEL đơn lẻ
_FB_VIDEO_PATH_RES = (
    re.compile(r"/videos?/"),            # /<page>/videos/<id>, /video/<id>
    re.compile(r"/video\.php"),          # /video.php?v=<id>
    re.compile(r"/reels?/\d"),           # /reel/<id>
    re.compile(r"/share/[vr]/"),         # link chia sẻ: /share/v/<code>, /share/r/<code>
    re.compile(r"/posts/"),              # bài đăng (thường kèm video)
    re.compile(r"/permalink\.php"),
    re.compile(r"/story\.php"),
)
# Đoạn đầu đường dẫn KHÔNG phải tên trang/kênh
_FB_RESERVED_FIRST_SEGMENTS = {
    "watch", "groups", "events", "marketplace", "gaming", "stories", "photo",
    "photos", "hashtag", "share", "login", "reel", "reels", "video", "videos",
    "story.php", "permalink.php", "video.php", "sharer", "dialog", "plugins",
}
_FB_PROFILE_TAB_SUFFIXES = {"videos", "reels", "photos", "posts", "about", "home"}


def resolve_facebook_link(link: str):
    """Xác định link Facebook là trang/kênh hay video đơn lẻ (KHÔNG gọi mạng).

    Trả về tuple: ("video", link_video) | ("profile", url_tab_video_của_trang)
    Raise RuntimeError nếu không nhận diện được (link nhóm, sự kiện...)."""
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    qs = parse_qs(parsed.query)

    if host == "fb.watch":
        return "video", link
    if path.rstrip("/") == "/watch" and "v" in qs:
        return "video", link
    if any(r.search(path) for r in _FB_VIDEO_PATH_RES):
        return "video", link

    segments = [s for s in path.split("/") if s]
    if segments and segments[0] == "profile.php" and "id" in qs:
        return "profile", f"https://www.facebook.com/profile.php?id={qs['id'][0]}&sk=videos"
    if segments and segments[0].lower() not in _FB_RESERVED_FIRST_SEGMENTS:
        name = segments[0]
        if len(segments) == 1 or segments[1].lower() in _FB_PROFILE_TAB_SUFFIXES:
            return "profile", f"https://www.facebook.com/{name}/videos"
    raise RuntimeError(
        "Không nhận diện được link Facebook này là video hay trang/kênh. Hãy dán "
        "link video/reel (…/videos/<số>, …/reel/<số>, fb.watch/…) hoặc link trang "
        "dạng facebook.com/tên_trang."
    )


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