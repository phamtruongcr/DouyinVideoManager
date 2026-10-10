"""
fetch_filters.py
================
Điều kiện lọc khi LẤY DANH SÁCH video của 1 kênh (Douyin / TikTok / Facebook):

    - Khoảng ngày đăng: từ ngày ... đến ngày ...
    - Thứ tự kết quả: Mới nhất -> Cũ nhất, hoặc Cũ nhất -> Mới nhất
    - Lượt xem tối thiểu, lượt tym (like) tối thiểu
    - Số video tối đa

Module này KHÔNG phụ thuộc GUI / mạng nên test được độc lập. Cách dùng:

    filters   = FetchFilters(date_from=..., min_views=1000, max_items=10)
    collector = ItemCollector(filters, stop_flag=..., enrich=client.fetch_video_info)
    for item in <nguồn video, MỚI NHẤT trước>:
        if collector.feed(item):      # True = đã đủ / không cần quét tiếp
            break
    items = collector.result()

Các nền tảng đều trả video theo thứ tự MỚI NHẤT -> CŨ NHẤT, nên:
  * Chế độ "Mới nhất -> Cũ nhất": dừng quét ngay khi đủ số video (rẻ nhất).
  * Chế độ "Cũ nhất -> Mới nhất": buộc phải quét tới video cũ nhất thỏa điều kiện
    (nếu có "từ ngày" thì chỉ quét tới ngày đó, không cần hết kênh) rồi đảo thứ tự.
  * Có "từ ngày": dừng quét khi gặp liên tiếp OLD_RUN_LIMIT video cũ hơn mốc đó
    (không dừng ngay ở video đầu tiên vì video GHIM của kênh nằm đầu danh sách
    nhưng có thể rất cũ).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Callable, Optional

# Số video cũ hơn mốc "từ ngày" gặp LIÊN TIẾP thì coi như đã quét qua vùng cần lấy.
OLD_RUN_LIMIT = 6
# Video ghim thường nằm đầu danh sách dù cũ -> khi lấy "mới nhất" có giới hạn số
# lượng, quét dư chừng này video rồi sắp xếp lại theo ngày để không bỏ sót.
PIN_MARGIN = 3

_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})\s*$")
_DATE_COMPACT_RE = re.compile(r"^\s*(\d{2})(\d{2})(\d{4})\s*$")
_DIGITS_RE = re.compile(r"[0-9]+")


# ------------------------------------------------------------ Đọc dữ liệu nhập --
def parse_date_text(text: str) -> Optional[date]:
    """'' -> None. Chấp nhận dd/mm/yyyy, d-m-yyyy, dd.mm.yyyy, ddmmyyyy.
    Sai định dạng / ngày không tồn tại -> ValueError (thông báo tiếng Việt)."""
    text = (text or "").strip()
    if not text:
        return None
    m = _DATE_RE.match(text) or _DATE_COMPACT_RE.match(text)
    if not m:
        raise ValueError("Ngày phải có dạng DD/MM/YYYY (ví dụ 25/12/2025).")
    day, month, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise ValueError(f"Ngày {day:02d}/{month:02d}/{year} không tồn tại.") from None


def parse_count_text(text: str) -> int:
    """'' -> 0. Bỏ dấu phân cách hàng nghìn (, . khoảng trắng). Sai -> ValueError."""
    t = (text or "").strip().replace(",", "").replace(".", "").replace(" ", "")
    if not t:
        return 0
    if not _DIGITS_RE.fullmatch(t):
        raise ValueError("Phải là số nguyên không âm.")
    return int(t)


def _day_timestamp(d: date, end_of_day: bool) -> int:
    """Epoch giây của đầu/cuối ngày theo GIỜ MÁY (khớp với cột 'Ngày đăng' đang
    hiển thị, vốn dùng datetime.fromtimestamp = giờ địa phương)."""
    dt = datetime.combine(d, dtime(23, 59, 59) if end_of_day else dtime.min)
    try:
        return int(dt.timestamp())
    except (OSError, OverflowError, ValueError):
        return 2 ** 62 if end_of_day else 0


def _to_int_or_none(value) -> Optional[int]:
    """Số liệu (view/like) từ API/yt-dlp -> int; thiếu/không hợp lệ -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ Điều kiện --
@dataclass
class FetchFilters:
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    newest_first: bool = True
    min_views: int = 0
    min_likes: int = 0
    max_items: int = 0          # 0 = không giới hạn
    # True = giữ nguyên thứ tự nguồn trả về (mới -> cũ), KHÔNG sắp xếp lại theo ngày.
    # Dùng cho Facebook: nền tảng không trả ngày/view/tym nên không thể sắp/lọc theo đó.
    keep_source_order: bool = False

    def __post_init__(self):
        self.ts_from: Optional[int] = (
            _day_timestamp(self.date_from, False) if self.date_from else None
        )
        self.ts_to: Optional[int] = (
            _day_timestamp(self.date_to, True) if self.date_to else None
        )

    @property
    def has_date(self) -> bool:
        return self.ts_from is not None or self.ts_to is not None

    @property
    def has_stats(self) -> bool:
        return self.min_views > 0 or self.min_likes > 0

    @property
    def is_active(self) -> bool:
        """Có điều kiện LỌC nào đang bật không (không tính thứ tự/số lượng)."""
        return self.has_date or self.has_stats

    def describe(self) -> str:
        parts = []
        if self.date_from or self.date_to:
            a = self.date_from.strftime("%d/%m/%Y") if self.date_from else "..."
            b = self.date_to.strftime("%d/%m/%Y") if self.date_to else "..."
            parts.append(f"ngày {a} → {b}")
        if self.min_views:
            parts.append(f"view ≥ {self.min_views:,}")
        if self.min_likes:
            parts.append(f"tym ≥ {self.min_likes:,}")
        parts.append("mới → cũ" if self.newest_first else "cũ → mới")
        if self.max_items:
            parts.append(f"tối đa {self.max_items}")
        return " · ".join(parts)


# -------------------------------------------------------------- Bộ thu thập --
class ItemCollector:
    """Nhận lần lượt từng video (theo thứ tự nguồn: mới nhất trước), lọc theo
    `FetchFilters`, và quyết định khi nào nên NGỪNG quét thêm.

    enrich(url) -> dict   (tùy chọn) hàm lấy chi tiết 1 video, dùng khi mục lấy
        từ danh sách THIẾU ngày/view/like mà điều kiện lại cần tới. Mỗi video
        chỉ gọi tối đa 1 lần; lỗi thì bỏ qua (video đó bị coi là thiếu dữ liệu).
    progress_cb(scanned, matched) gọi sau mỗi video để GUI cập nhật trạng thái.
    """

    def __init__(
        self,
        filters: FetchFilters,
        stop_flag: Optional[Callable[[], bool]] = None,
        enrich: Optional[Callable[[str], dict]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ):
        self.filters = filters
        self._stop_flag = stop_flag
        self._enrich = enrich
        self._progress_cb = progress_cb
        self.items: list[dict] = []
        self._seen: set[str] = set()
        self._old_run = 0
        # Thống kê để báo cho người dùng biết vì sao video bị loại
        self.scanned = 0
        self.out_of_range = 0     # ngoài khoảng ngày
        self.below_stats = 0      # đủ dữ liệu nhưng view/tym thấp hơn ngưỡng
        self.missing_data = 0     # thiếu ngày/view/tym -> không kiểm tra được
        self.enriched = 0
        self.enrich_failed = 0
        self.stopped_by_user = False

    # ------------------------------------------------------------ nội bộ --
    def _target_count(self) -> int:
        """Số video khớp cần thu thập trước khi dừng quét (0 = không dừng theo số)."""
        f = self.filters
        if f.keep_source_order:
            return f.max_items   # thứ tự nguồn: đủ số lượng là dừng, không cần dư cho video ghim
        if not f.max_items or not f.newest_first:
            return 0   # cũ -> mới: phải quét hết vùng cần mới biết video nào cũ nhất
        return f.max_items + PIN_MARGIN

    def _date_verdict(self, item: dict) -> str:
        """'ok' | 'new' (mới hơn 'đến ngày') | 'old' (cũ hơn 'từ ngày') | 'unknown'."""
        f = self.filters
        if not f.has_date:
            return "ok"
        ct = _to_int_or_none(item.get("create_time")) or 0
        if ct <= 0:
            return "unknown"
        if f.ts_to is not None and ct > f.ts_to:
            return "new"
        if f.ts_from is not None and ct < f.ts_from:
            return "old"
        return "ok"

    def _stats_missing(self, item: dict) -> bool:
        f = self.filters
        return (
            (f.min_views > 0 and item.get("view_count") is None)
            or (f.min_likes > 0 and item.get("like_count") is None)
        )

    def _try_enrich(self, item: dict) -> dict:
        if self._enrich is None or not item.get("url"):
            return item
        if self._stop_flag and self._stop_flag():
            return item
        self.enriched += 1
        try:
            full = self._enrich(item["url"])
        except Exception:  # noqa: BLE001 - mọi lỗi mạng/API: bỏ qua, video coi như thiếu dữ liệu
            self.enrich_failed += 1
            return item
        if not isinstance(full, dict):
            return item
        merged = dict(item)
        for key in ("create_time", "duration_s"):
            if not merged.get(key) and full.get(key):
                merged[key] = full[key]
        for key in ("view_count", "like_count"):
            if merged.get(key) is None and full.get(key) is not None:
                merged[key] = full[key]
        return merged

    def _report(self):
        if self._progress_cb:
            self._progress_cb(self.scanned, len(self.items))

    # ---------------------------------------------------------- giao diện --
    def feed(self, item: dict) -> bool:
        """Xử lý 1 video. Trả về True nếu KHÔNG cần quét thêm nữa."""
        if self._stop_flag and self._stop_flag():
            self.stopped_by_user = True
            return True
        vid = str(item.get("id", ""))
        if vid in self._seen:
            return False
        self._seen.add(vid)
        self.scanned += 1
        f = self.filters

        verdict = self._date_verdict(item)
        # Chỉ tốn công lấy chi tiết khi còn khả năng video này khớp mà thiếu dữ liệu
        if verdict in ("ok", "unknown") and (verdict == "unknown" or self._stats_missing(item)):
            item = self._try_enrich(item)
            verdict = self._date_verdict(item)

        stop = False
        accept = False
        if verdict == "old":
            self.out_of_range += 1
            self._old_run += 1
            if self._old_run >= OLD_RUN_LIMIT:
                stop = True   # nguồn đi từ mới -> cũ: phía sau chỉ càng cũ hơn
        elif verdict == "new":
            self.out_of_range += 1
        elif verdict == "unknown":
            self.missing_data += 1
        else:
            self._old_run = 0
            views = _to_int_or_none(item.get("view_count"))
            likes = _to_int_or_none(item.get("like_count"))
            if f.min_views > 0 and views is None:
                self.missing_data += 1
            elif f.min_likes > 0 and likes is None:
                self.missing_data += 1
            elif (f.min_views > 0 and views < f.min_views) or (
                f.min_likes > 0 and likes < f.min_likes
            ):
                self.below_stats += 1
            else:
                accept = True

        if accept:
            self.items.append(item)
            target = self._target_count()
            if target and len(self.items) >= target:
                stop = True
        self._report()
        return stop

    def result(self) -> list[dict]:
        """Danh sách cuối: sắp theo ngày đăng đúng thứ tự yêu cầu, cắt theo số tối đa."""
        f = self.filters
        if f.keep_source_order:
            out = list(self.items)
        else:
            out = sorted(
                self.items,
                key=lambda it: _to_int_or_none(it.get("create_time")) or 0,
                reverse=f.newest_first,
            )
        if f.max_items:
            out = out[: f.max_items]
        return out

    def summary(self) -> str:
        """1 dòng tóm tắt kết quả quét, hiển thị ở thanh trạng thái."""
        n = len(self.result())
        text = f"Đã quét {self.scanned} video → lấy {n} video khớp điều kiện"
        notes = []
        if self.out_of_range:
            notes.append(f"{self.out_of_range} ngoài khoảng ngày")
        if self.below_stats:
            notes.append(f"{self.below_stats} chưa đủ view/tym")
        if self.missing_data:
            notes.append(
                f"{self.missing_data} bị loại vì nền tảng không trả số liệu ngày/view/tym"
            )
        if notes:
            text += " (bỏ qua: " + "; ".join(notes) + ")"
        if self.stopped_by_user:
            text += " — đã dừng theo yêu cầu"
        return text + "."
