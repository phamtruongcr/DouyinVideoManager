"""
text_editor.py
==============
TRÌNH CHỈNH SỬA CHỮ kiểu CapCut cho tính năng "Chèn chữ lên video":

  * Khung xem trước là 1 khung hình THẬT của video (đã crop / viền đen / nền
    mờ đúng như khi xuất), chữ hiện trực tiếp trên đó.
  * Bấm vào chữ để chọn, KÉO để di chuyển (có đường gióng khi chạm giữa
    khung), kéo 4 góc để đổi cỡ, phím mũi tên để nhích, Delete để xóa.
  * Nhiều LỚP chữ; mỗi lớp có kiểu riêng (màu, đậm, viền, nền, font) và
    thời gian hiện riêng.
  * TIMELINE: mỗi lớp là 1 thanh — kéo thân thanh để dời, kéo mép trái/phải
    để chỉnh lúc bắt đầu / kết thúc; bấm hoặc kéo trên thước để tua.
  * Mẫu kiểu chữ dựng sẵn (bấm 1 cái là đổi cả kiểu).
  * Lớp BLUR (không chữ): làm mờ 1 vùng hình chữ nhật (che phụ đề/logo cũ…) —
    kéo để di chuyển, kéo 4 góc để đổi kích thước, chỉnh độ mờ + thời gian.
    Khi xuất, mọi lớp blur được áp TRƯỚC rồi mới vẽ chữ lên trên.

Các hàm thuần (không đụng Tk) nằm ở đầu file để dễ kiểm thử; class
`TextEditorWindow` chỉ lo giao diện. Vị trí chữ lưu dưới dạng TỈ LỆ 0..1 của
khung hình (cx, cy = tâm khối chữ) nên khớp mọi độ phân giải xuất ra.
"""

from __future__ import annotations

import copy
import math
import sys
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, colorchooser
from tkinter import font as tkfont
from typing import Optional

from .audio_merger import (
    BLUR_SIGMA_PER_STRENGTH,
    MergeError,
    extract_preview_frame,
    find_default_font,
    list_system_fonts,
    make_width_fn,
    probe_media_info,
    resolve_family_font,
    wrap_text_for_width,
)
from .config import (
    APP_TITLE,
    DEFAULT_TEXT_BOX_OPACITY,
    DEFAULT_TEXT_COLOR,
    DEFAULT_TEXT_SIZE_PERCENT,
    MAX_TEXT_SIZE_PERCENT,
    MIN_TEXT_SIZE_PERCENT,
)
from .widgets import ScrollableFrame, WrapFrame

try:  # Pillow chỉ dùng để xem trước hiệu ứng mờ THẬT; không có vẫn chạy được
    from PIL import Image, ImageFilter, ImageTk
except Exception:  # noqa: BLE001
    Image = ImageFilter = ImageTk = None

MIN_LAYER_SECONDS = 0.1
DEFAULT_BOX_PAD_PCT = 30     # lề nền chữ (ngang / dọc), tính theo % cỡ chữ
MAX_BOX_PAD_PCT = 150
DEFAULT_TOTAL_SECONDS = 10.0
DEFAULT_BLUR_STRENGTH = 60   # độ mờ mặc định của lớp blur (1..100)
MIN_BLUR_SIZE = 0.03         # cạnh nhỏ nhất của vùng blur (tỉ lệ khung hình)


# ============================================================================
# Hàm thuần (không phụ thuộc Tk) — có unit test
# ============================================================================

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def is_hex_color(value) -> bool:
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[1:])


def new_layer(**overrides) -> dict:
    """Một lớp chữ mới với giá trị mặc định (đã chuẩn hóa)."""
    base = dict(
        kind="text",               # "text" = lớp chữ, "blur" = vùng làm mờ không chữ
        text="Nhập chữ ở đây",
        cx=0.5, cy=0.5,
        size_pct=float(DEFAULT_TEXT_SIZE_PERCENT),
        color=DEFAULT_TEXT_COLOR,
        bold=True,
        outline=True, outline_color="#000000",
        box=False, box_color="#000000", box_opacity=int(DEFAULT_TEXT_BOX_OPACITY),
        box_pad_x=DEFAULT_BOX_PAD_PCT,   # lề nền theo chiều NGANG (% cỡ chữ)
        box_pad_y=DEFAULT_BOX_PAD_PCT,   # lề nền theo chiều DỌC (% cỡ chữ)
        start=0.0, duration=0.0,   # duration 0 = hiện đến hết video
        font="",                   # "" = font tự động; đường dẫn file = đã chọn file cụ thể
        font_family="",            # "" = font tự động; tên họ font = đã chọn từ danh sách font máy
        bw=0.8, bh=0.12,           # (chỉ lớp blur) rộng/cao vùng mờ, tỉ lệ 0..1 của khung hình
        blur=DEFAULT_BLUR_STRENGTH,  # (chỉ lớp blur) độ mờ 1..100
    )
    base.update(overrides)
    return normalize_layer(base)


def new_blur_layer(**overrides) -> dict:
    """Một lớp BLUR mới: vùng mờ nằm ở dải dưới khung hình (chỗ hay có phụ đề
    cũ). Kéo thả trong trình chỉnh sửa để đặt đúng chỗ cần che."""
    base = dict(kind="blur", text="", cx=0.5, cy=0.85, bw=0.8, bh=0.12)
    base.update(overrides)
    return new_layer(**base)


def is_blur(layer: dict) -> bool:
    return layer.get("kind") == "blur"


def fit_blur_center(layer: dict) -> None:
    """Kẹp tâm (cx, cy) của lớp blur để vùng mờ không tràn ra ngoài khung."""
    layer["cx"] = clamp(layer["cx"], layer["bw"] / 2, 1.0 - layer["bw"] / 2)
    layer["cy"] = clamp(layer["cy"], layer["bh"] / 2, 1.0 - layer["bh"] / 2)


def blur_rect(layer: dict, ox: float, oy: float, dw: float, dh: float) -> tuple[float, float, float, float]:
    """Hình chữ nhật (x0, y0, x1, y1) của vùng blur trên khung xem trước."""
    w, h = layer["bw"] * dw, layer["bh"] * dh
    x0 = ox + clamp(layer["cx"] * dw - w / 2, 0.0, dw - w)
    y0 = oy + clamp(layer["cy"] * dh - h / 2, 0.0, dh - h)
    return x0, y0, x0 + w, y0 + h


def normalize_layer(data: dict) -> dict:
    """Điền giá trị thiếu + ép kiểu + kẹp về khoảng hợp lệ. Dùng khi nạp cấu
    hình đã lưu (có thể cũ/hỏng) để editor và bộ xuất không bao giờ gặp giá
    trị lạ."""
    d = dict(data or {})

    def num(key, default):
        try:
            return float(d.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def color(key, default):
        return d[key].upper() if is_hex_color(d.get(key)) else default

    kind = "blur" if d.get("kind") == "blur" else "text"
    out = dict(
        kind=kind,
        text=str(d.get("text", "")),
        cx=clamp(num("cx", 0.5), 0.0, 1.0),
        cy=clamp(num("cy", 0.5), 0.0, 1.0),
        size_pct=clamp(num("size_pct", DEFAULT_TEXT_SIZE_PERCENT),
                       float(MIN_TEXT_SIZE_PERCENT), float(MAX_TEXT_SIZE_PERCENT)),
        color=color("color", DEFAULT_TEXT_COLOR),
        bold=bool(d.get("bold", True)),
        outline=bool(d.get("outline", True)),
        outline_color=color("outline_color", "#000000"),
        box=bool(d.get("box", False)),
        box_color=color("box_color", "#000000"),
        box_opacity=int(clamp(num("box_opacity", DEFAULT_TEXT_BOX_OPACITY), 0, 100)),
        box_pad_x=int(clamp(num("box_pad_x", DEFAULT_BOX_PAD_PCT), 0, MAX_BOX_PAD_PCT)),
        box_pad_y=int(clamp(num("box_pad_y", DEFAULT_BOX_PAD_PCT), 0, MAX_BOX_PAD_PCT)),
        start=max(0.0, num("start", 0.0)),
        duration=max(0.0, num("duration", 0.0)),
        font=str(d.get("font", "") or ""),
        font_family=str(d.get("font_family", "") or ""),
        bw=clamp(num("bw", 0.8), MIN_BLUR_SIZE, 1.0),
        bh=clamp(num("bh", 0.12), MIN_BLUR_SIZE, 1.0),
        blur=int(clamp(num("blur", DEFAULT_BLUR_STRENGTH), 1, 100)),
    )
    if kind == "blur":
        out["text"] = ""
        fit_blur_center(out)
    return out


def resolve_layer_font(layer: dict) -> Optional[str]:
    """File font (.ttf/.otf) THỰC SỰ dùng để vẽ lớp chữ này, theo đúng thứ
    tự ưu tiên dùng CHUNG cho cả khung xem trước và lúc xuất video:
      1. `font` — đường dẫn file cụ thể người dùng đã chọn thủ công
      2. `font_family` — tên họ font đã chọn từ danh sách font cài trên máy
         (tự đổi Regular <-> Bold theo `bold` khi bật/tắt "Đậm")
      3. font hệ thống mặc định (Arial/Segoe UI/DejaVu...), theo `bold`
    Trả về None nếu không tìm được font nào (máy không có font phù hợp)."""
    custom = str(layer.get("font", "") or "").strip()
    if custom:
        return custom
    family = str(layer.get("font_family", "") or "").strip()
    if family:
        found = resolve_family_font(family, bool(layer.get("bold")))
        if found:
            return found
    return find_default_font(bool(layer.get("bold")))


# Mẫu kiểu chữ dựng sẵn: chỉ đổi KIỂU (màu/viền/nền/đậm), giữ nguyên nội dung,
# vị trí, cỡ chữ và thời gian của lớp đang chọn.
STYLE_PRESETS: list[tuple[str, dict]] = [
    ("Trắng viền đen", dict(color="#FFFFFF", bold=True, outline=True, outline_color="#000000", box=False)),
    ("Vàng viền đen", dict(color="#FFD400", bold=True, outline=True, outline_color="#000000", box=False)),
    ("Nền đen mờ", dict(color="#FFFFFF", bold=True, outline=False, box=True, box_color="#000000", box_opacity=60)),
    ("Nền vàng", dict(color="#111111", bold=True, outline=False, box=True, box_color="#FFD400", box_opacity=95)),
    ("Nền đỏ", dict(color="#FFFFFF", bold=True, outline=False, box=True, box_color="#E11D48", box_opacity=90)),
    ("Neon xanh", dict(color="#00F5FF", bold=True, outline=True, outline_color="#004E64", box=False)),
    ("Chữ đen viền trắng", dict(color="#111111", bold=True, outline=True, outline_color="#FFFFFF", box=False)),
]


def layer_end(layer: dict, total: float) -> float:
    d = layer.get("duration", 0) or 0
    return total if d <= 0 else min(total, layer["start"] + d)


def layer_visible(layer: dict, t: float, total: float) -> bool:
    return layer["start"] - 1e-6 <= t <= layer_end(layer, total) + 1e-6


def set_layer_span(layer: dict, start: float, end: float, total: float) -> None:
    """Đặt khoảng thời gian [start, end] cho lớp. Nếu end chạm cuối video thì
    lưu duration = 0 (= hiện đến hết video, đúng cho cả video dài/ngắn khác)."""
    start = clamp(start, 0.0, max(0.0, total - MIN_LAYER_SECONDS))
    end = clamp(end, start + MIN_LAYER_SECONDS, max(total, start + MIN_LAYER_SECONDS))
    layer["start"] = round(start, 2)
    layer["duration"] = 0.0 if end >= total - 0.05 else round(end - start, 2)


def time_to_x(t: float, total: float, x0: float, x1: float) -> float:
    return x0 + (x1 - x0) * (t / total if total > 0 else 0.0)


def x_to_time(x: float, total: float, x0: float, x1: float) -> float:
    if x1 <= x0:
        return 0.0
    return clamp((x - x0) / (x1 - x0), 0.0, 1.0) * total


def snap_time(t: float, step: float = 0.1) -> float:
    return round(round(t / step) * step, 3)


def ruler_step(total: float, width_px: float, min_px: float = 64.0) -> float:
    """Khoảng cách (giây) giữa 2 vạch chia trên thước sao cho các vạch cách
    nhau tối thiểu `min_px` pixel."""
    pps = width_px / total if total > 0 else 1.0
    for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        if step * pps >= min_px:
            return float(step)
    return 600.0


def fmt_time(t: float, precise: bool = False) -> str:
    t = max(0.0, t)
    m = int(t // 60)
    s = t - 60 * m
    return f"{m}:{s:04.1f}" if precise else f"{m}:{int(s):02d}"


def stage_rect(avail_w: int, avail_h: int, aspect: float, margin: int = 12) -> tuple[int, int, int, int]:
    """Hình chữ nhật (ox, oy, dw, dh) của khung hình trong vùng `avail_w` x
    `avail_h`, giữ đúng tỉ lệ `aspect` = rộng/cao, canh giữa, kích thước chẵn."""
    aspect = aspect if aspect and aspect > 0 else 9 / 16
    aw = max(40, avail_w - 2 * margin)
    ah = max(40, avail_h - 2 * margin)
    if aw / ah > aspect:
        dh, dw = ah, ah * aspect
    else:
        dw, dh = aw, aw / aspect
    dw, dh = max(2, int(dw) // 2 * 2), max(2, int(dh) // 2 * 2)
    return (avail_w - dw) // 2, (avail_h - dh) // 2, dw, dh


def point_in_rect(x: float, y: float, rect: tuple[float, float, float, float]) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def block_position(cx: float, cy: float, dw: float, dh: float, bw: float, bh: float,
                   margin: float) -> tuple[float, float]:
    """Góc trên-trái (x, y) của khối chữ kích thước bw x bh trong khung dw x dh
    khi tâm ở tỉ lệ (cx, cy) — KẸP trong khung y hệt công thức của ffmpeg khi
    xuất (xem build_drawtext_filter) để khung xem trước khớp file xuất."""
    x = max(margin, min(dw - bw - margin, cx * dw - bw / 2))
    y = max(margin, min(dh - bh - margin, cy * dh - bh / 2))
    return x, y


def style_metrics(
    fontsize: int, outline: bool, box: bool,
    pad_x_pct: float = DEFAULT_BOX_PAD_PCT, pad_y_pct: float = DEFAULT_BOX_PAD_PCT,
) -> tuple[int, int, int, int]:
    """(pad_ngang, pad_dọc, độ_dày_viền, lề_an_toàn) — CÙNG công thức với bộ xuất."""
    def _pad(pct: float) -> int:
        if not box or pct <= 0:
            return 0
        return max(2, round(fontsize * pct / 100))

    pad_x, pad_y = _pad(pad_x_pct), _pad(pad_y_pct)
    border = max(1, round(fontsize * 0.06)) if outline else 0
    margin = max(pad_x, pad_y) + border + max(2, round(fontsize * 0.08))
    return pad_x, pad_y, border, margin


def wrap_width_limit(
    dw: float, fontsize: int, outline: bool, box: bool, pad_x_pct: float = DEFAULT_BOX_PAD_PCT
) -> float:
    """Bề rộng tối đa của 1 dòng chữ — CÙNG công thức với prepare_text_overlay."""
    pad = 2 * fontsize * pad_x_pct / 100 if box else 0
    border = 2 * max(1, round(fontsize * 0.06)) if outline else 0
    return max(fontsize * 2, dw * 0.94 - pad - border)


_FAMILY_HINTS = [
    ("arial", "Arial"), ("segoe", "Segoe UI"), ("tahoma", "Tahoma"), ("verdana", "Verdana"),
    ("dejavusans", "DejaVu Sans"), ("liberation", "Liberation Sans"), ("helvetica", "Helvetica"),
    ("yahei", "Microsoft YaHei"), ("noto", "Noto Sans"), ("roboto", "Roboto"),
]


_tk_family_cache: dict[str, str] = {}


def font_family_for(fontfile: str) -> str:
    """Tên họ font để Tk vẽ xem trước ĐÚNG font ffmpeg sẽ dùng.
    Đọc tên họ thật từ chính file font (Pillow); trên Windows còn đăng ký file
    font vào tiến trình (chỉ trong phiên chạy app, không cài vào máy) để Tk
    dùng được cả font người dùng chọn từ 'file font khác'. Không đọc được thì
    mới đoán theo tên file, cuối cùng là Arial."""
    if not fontfile:
        return "Arial"
    cached = _tk_family_cache.get(fontfile)
    if cached:
        return cached
    family = ""
    try:
        from PIL import ImageFont as _IF
        family = str(_IF.truetype(str(fontfile), 20).getname()[0] or "")
    except Exception:  # noqa: BLE001 - thiếu Pillow / font lỗi
        family = ""
    if family and sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.gdi32.AddFontResourceExW(str(fontfile), 0x10, 0)  # FR_PRIVATE
        except Exception:  # noqa: BLE001
            pass
    if not family:
        name = Path(fontfile).stem.lower().replace(" ", "").replace("-", "").replace("_", "")
        family = next((fam for hint, fam in _FAMILY_HINTS if hint in name), "Arial")
    _tk_family_cache[fontfile] = family
    return family


def layer_summary(index: int, layer: dict) -> str:
    if is_blur(layer):
        end = "hết" if (layer.get("duration") or 0) <= 0 else f"{layer['start'] + layer['duration']:.1f}s"
        return (f"{index + 1}. Blur {round(layer['bw'] * 100)}%×{round(layer['bh'] * 100)}%"
                f"   [{layer['start']:.1f}s → {end}]")
    first = (layer.get("text") or "").strip().splitlines()[0] if (layer.get("text") or "").strip() else "(trống)"
    if len(first) > 22:
        first = first[:21] + "…"
    end = "hết" if (layer.get("duration") or 0) <= 0 else f"{layer['start'] + layer['duration']:.1f}s"
    return f"{index + 1}. {first}   [{layer['start']:.1f}s → {end}]"


# ============================================================================
# Cửa sổ trình chỉnh sửa
# ============================================================================

HANDLE_SIZE = 9
RULER_H = 20
TL_MAX_ROWS_H = 132


class TextEditorWindow(tk.Toplevel):
    """Trình chỉnh sửa chữ (modal). Gọi `on_apply(layers)` khi bấm "Xong"."""

    def __init__(
        self,
        master,
        layers: list[dict],
        *,
        videos: list[Path],
        ffmpeg_path: str | None,
        ffprobe_path: str | None,
        aspect_ratio: tuple[int, int] | None,
        fit_mode: str,
        tmp_dir_fn,
        on_apply,
    ):
        super().__init__(master)
        self.title("Chỉnh sửa chữ & blur — kiểu CapCut")
        self.transient(master.winfo_toplevel())

        self.layers: list[dict] = [normalize_layer(layer) for layer in layers]
        self.sel = 0 if self.layers else -1
        self.videos = [Path(v) for v in videos]
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.aspect_ratio = aspect_ratio
        self.fit_mode = fit_mode
        self._tmp_dir_fn = tmp_dir_fn
        self._on_apply = on_apply

        self.ref_video: Path | None = None
        self.total = DEFAULT_TOTAL_SECONDS
        self.ref_dims: tuple[int, int] | None = None
        self.playhead = 0.0
        self._probe_cache: dict[Path, tuple[float, int, int]] = {}

        self._frame_photo = None
        self._frame_pil = None          # bản PIL của khung hình hiện tại (xem trước blur thật)
        self._blur_cache: dict = {}     # (vùng, sigma) -> PhotoImage đã làm mờ
        self._panel_kind: str | None = None
        self._frame_msg = "Đang tải khung hình..."
        self._frame_token = 0
        self._frame_after = None
        self._frame_files: list[Path] = []
        self._results: queue.Queue = queue.Queue()
        self._closed = False

        self._geom: dict[int, tuple[float, float, float, float]] = {}
        self._handles: dict[str, tuple[float, float, float, float]] = {}
        self._guides = (False, False)
        self._drag: dict | None = None
        self._tl_drag: dict | None = None
        self._tl_rows: list[tuple[float, float]] = []
        self._updating = False
        self._font_cache: dict[tuple, tkfont.Font] = {}

        self._build_ui()
        self._fit_window()
        self._refresh_layer_list()
        self._load_layer_to_panel()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())
        try:
            self.grab_set()
        except tk.TclError:
            pass

        self.after(120, self._poll_results)
        first = self.videos[0] if self.videos else None
        self._set_reference(first)
        self.after(80, self._redraw_all)

    # ------------------------------------------------------------------ UI --
    def _fit_window(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1260, sw - 60), min(800, sh - 100)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(min(980, sw), min(620, sh))

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # --- Thanh dưới cùng (pack/grid trước để luôn hiện đủ) ---
        bottom = ttk.Frame(self, padding=(10, 6))
        bottom.grid(row=1, column=0, sticky="ew")
        hint = ttk.Label(
            bottom, foreground="#555", justify="left",
            text=("Kéo chữ/vùng blur để di chuyển  •  kéo 4 góc để đổi cỡ  •  phím mũi tên để nhích  •  "
                  "Delete để xóa  •  kéo thanh dưới timeline để chỉnh thời gian"),
        )
        hint.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="✔ Xong", command=self._on_ok).pack(side="right")
        ttk.Button(bottom, text="Hủy", command=self._on_cancel).pack(side="right", padx=(0, 8))

        body = ttk.Frame(self)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)

        # --- Cột trái: video tham chiếu + khung xem trước + timeline ---
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 6), pady=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        ref_row = ttk.Frame(left)
        ref_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ref_row.columnconfigure(1, weight=1)
        ttk.Label(ref_row, text="Xem trước trên video:").grid(row=0, column=0, sticky="w")
        self.ref_var = tk.StringVar()
        self._ref_labels = [f"{i + 1}. {v.name}" for i, v in enumerate(self.videos)]
        self.ref_combo = ttk.Combobox(
            ref_row, textvariable=self.ref_var, values=self._ref_labels, state="readonly"
        )
        self.ref_combo.grid(row=0, column=1, sticky="ew", padx=6)
        self.ref_combo.bind("<<ComboboxSelected>>", self._on_ref_selected)
        ttk.Button(ref_row, text="Chọn file khác...", command=self._choose_other_video).grid(row=0, column=2)

        self.stage = tk.Canvas(left, background="#1e1e1e", highlightthickness=0, takefocus=True)
        self.stage.grid(row=1, column=0, sticky="nsew")
        self.stage.bind("<Configure>", lambda e: self._on_stage_resized())
        self.stage.bind("<Button-1>", self._on_stage_press)
        self.stage.bind("<B1-Motion>", self._on_stage_drag)
        self.stage.bind("<ButtonRelease-1>", self._on_stage_release)
        self.stage.bind("<Motion>", self._on_stage_motion)
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0), ("Up", 0, -1), ("Down", 0, 1)):
            self.stage.bind(f"<{key}>", lambda e, dx=dx, dy=dy: self._nudge(dx, dy, big=False))
            self.stage.bind(f"<Shift-{key}>", lambda e, dx=dx, dy=dy: self._nudge(dx, dy, big=True))
        self.stage.bind("<Delete>", lambda e: self._delete_layer())
        self.stage.bind("<BackSpace>", lambda e: self._delete_layer())

        play_row = ttk.Frame(left)
        play_row.grid(row=2, column=0, sticky="ew", pady=(6, 2))
        ttk.Button(play_row, text="⏮", width=3, command=lambda: self._set_playhead(0.0)).pack(side="left")
        ttk.Button(play_row, text="◀ 0.5s", command=lambda: self._set_playhead(self.playhead - 0.5)).pack(
            side="left", padx=(4, 0))
        ttk.Button(play_row, text="0.5s ▶", command=lambda: self._set_playhead(self.playhead + 0.5)).pack(
            side="left", padx=(4, 0))
        self.time_var = tk.StringVar()
        ttk.Label(play_row, textvariable=self.time_var).pack(side="left", padx=12)
        self.frame_msg_var = tk.StringVar()
        ttk.Label(play_row, textvariable=self.frame_msg_var, foreground="#a55").pack(side="left")

        self.timeline = tk.Canvas(left, background="#2a2a2e", highlightthickness=0, height=RULER_H + 24)
        self.timeline.grid(row=3, column=0, sticky="ew")
        self.timeline.bind("<Configure>", lambda e: self._draw_timeline())
        self.timeline.bind("<Button-1>", self._on_tl_press)
        self.timeline.bind("<B1-Motion>", self._on_tl_drag)
        self.timeline.bind("<ButtonRelease-1>", self._on_tl_release)
        self.timeline.bind("<Motion>", self._on_tl_motion)

        # --- Cột phải: bảng thuộc tính (cuộn được) ---
        scroller = ScrollableFrame(body, canvas_width=372)
        scroller.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=8)
        self._build_panel(scroller.body)

    def _build_panel(self, panel):
        panel.columnconfigure(0, weight=1)

        # ---- Danh sách lớp ----
        box = ttk.LabelFrame(panel, text="Các lớp (chữ / blur)", padding=6)
        box.pack(fill="x", pady=(0, 6))
        self.listbox = tk.Listbox(box, height=5, exportselection=False, activestyle="none")
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        btns = WrapFrame(box, hgap=4, vgap=4)
        btns.pack(fill="x", pady=(6, 0))
        btns.add(ttk.Button(btns, text="➕ Thêm chữ", command=self._add_layer))
        btns.add(ttk.Button(btns, text="🌫 Thêm blur", command=self._add_blur_layer))
        btns.add(ttk.Button(btns, text="⧉ Nhân đôi", command=self._duplicate_layer))
        btns.add(ttk.Button(btns, text="▲", width=3, command=lambda: self._move_layer(-1)))
        btns.add(ttk.Button(btns, text="▼", width=3, command=lambda: self._move_layer(1)))
        btns.add(ttk.Button(btns, text="🗑 Xóa", command=self._delete_layer))

        # ---- Vùng blur (chỉ hiện khi chọn lớp blur) ----
        blur_box = self._pnl_blur = ttk.LabelFrame(panel, text="Vùng làm mờ (blur)", padding=6)
        blur_box.columnconfigure(1, weight=1)
        ttk.Label(
            blur_box, foreground="#555", justify="left", wraplength=330,
            text="Kéo vùng trên video để di chuyển, kéo 4 góc để đổi kích thước. "
                 "Dùng để che phụ đề / logo cũ — chữ mới đặt lên trên vẫn rõ nét.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.blur_var = tk.DoubleVar(value=float(DEFAULT_BLUR_STRENGTH))
        self.blur_w_var = tk.DoubleVar(value=80.0)
        self.blur_h_var = tk.DoubleVar(value=12.0)
        self._blur_lbls = {}
        for row, (key, label, var, lo, hi) in enumerate((
            ("blur", "Độ mờ:", self.blur_var, 1, 100),
            ("bw", "Rộng:", self.blur_w_var, MIN_BLUR_SIZE * 100, 100),
            ("bh", "Cao:", self.blur_h_var, MIN_BLUR_SIZE * 100, 100),
        ), start=1):
            ttk.Label(blur_box, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Scale(blur_box, from_=lo, to=hi, variable=var,
                      command=lambda v: self._on_blur_changed()).grid(row=row, column=1, sticky="ew", padx=6)
            lbl = ttk.Label(blur_box, text="", width=6)
            lbl.grid(row=row, column=2, sticky="e")
            self._blur_lbls[key] = lbl

        # ---- Nội dung ----
        content = self._pnl_content = ttk.LabelFrame(panel, text="Nội dung (Enter để xuống dòng)", padding=6)
        content.pack(fill="x", pady=(0, 6))
        self.text_box = tk.Text(content, height=3, width=32, wrap="word", undo=True)
        self.text_box.pack(fill="x")
        self.text_box.bind("<KeyRelease>", lambda e: self._on_text_edited())

        # ---- Mẫu kiểu chữ ----
        presets = self._pnl_presets = ttk.LabelFrame(panel, text="Mẫu kiểu chữ (bấm để áp dụng)", padding=6)
        presets.pack(fill="x", pady=(0, 6))
        pw = WrapFrame(presets, hgap=4, vgap=4)
        pw.pack(fill="x")
        for name, style in STYLE_PRESETS:
            pw.add(ttk.Button(pw, text=name, command=lambda s=style: self._apply_preset(s)))

        # ---- Kiểu chữ ----
        style_box = self._pnl_style = ttk.LabelFrame(panel, text="Kiểu chữ", padding=6)
        style_box.pack(fill="x", pady=(0, 6))
        style_box.columnconfigure(1, weight=1)

        ttk.Label(style_box, text="Cỡ chữ:").grid(row=0, column=0, sticky="w", pady=2)
        self.size_var = tk.DoubleVar(value=float(DEFAULT_TEXT_SIZE_PERCENT))
        self.size_scale = ttk.Scale(
            style_box, from_=MIN_TEXT_SIZE_PERCENT, to=MAX_TEXT_SIZE_PERCENT,
            variable=self.size_var, command=lambda v: self._on_size_changed(),
        )
        self.size_scale.grid(row=0, column=1, sticky="ew", padx=6)
        self.size_lbl = ttk.Label(style_box, text="", width=6)
        self.size_lbl.grid(row=0, column=2, sticky="e")

        ttk.Label(style_box, text="Màu chữ:").grid(row=1, column=0, sticky="w", pady=2)
        self.color_sw = self._swatch(style_box, lambda: self._pick_color("color"))
        self.color_sw.grid(row=1, column=1, sticky="w", padx=6)
        self.bold_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(style_box, text="Đậm", variable=self.bold_var, command=self._on_style_toggled).grid(
            row=1, column=2, sticky="e")

        self.outline_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(style_box, text="Viền chữ", variable=self.outline_var,
                        command=self._on_style_toggled).grid(row=2, column=0, sticky="w", pady=2)
        self.outline_sw = self._swatch(style_box, lambda: self._pick_color("outline_color"))
        self.outline_sw.grid(row=2, column=1, sticky="w", padx=6)

        self.box_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(style_box, text="Nền chữ", variable=self.box_var,
                        command=self._on_style_toggled).grid(row=3, column=0, sticky="w", pady=2)
        self.box_sw = self._swatch(style_box, lambda: self._pick_color("box_color"))
        self.box_sw.grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(style_box, text="Độ đậm nền:").grid(row=4, column=0, sticky="w", pady=2)
        self.box_op_var = tk.DoubleVar(value=float(DEFAULT_TEXT_BOX_OPACITY))
        self.box_op_scale = ttk.Scale(style_box, from_=0, to=100, variable=self.box_op_var,
                                      command=lambda v: self._on_box_opacity_changed())
        self.box_op_scale.grid(row=4, column=1, sticky="ew", padx=6)
        self.box_op_lbl = ttk.Label(style_box, text="", width=6)
        self.box_op_lbl.grid(row=4, column=2, sticky="e")

        ttk.Label(style_box, text="Nền – ngang:").grid(row=5, column=0, sticky="w", pady=2)
        self.box_px_var = tk.DoubleVar(value=float(DEFAULT_BOX_PAD_PCT))
        self.box_px_scale = ttk.Scale(style_box, from_=0, to=MAX_BOX_PAD_PCT, variable=self.box_px_var,
                                      command=lambda v: self._on_box_pad_changed())
        self.box_px_scale.grid(row=5, column=1, sticky="ew", padx=6)
        self.box_px_lbl = ttk.Label(style_box, text="", width=6)
        self.box_px_lbl.grid(row=5, column=2, sticky="e")

        ttk.Label(style_box, text="Nền – dọc:").grid(row=6, column=0, sticky="w", pady=2)
        self.box_py_var = tk.DoubleVar(value=float(DEFAULT_BOX_PAD_PCT))
        self.box_py_scale = ttk.Scale(style_box, from_=0, to=MAX_BOX_PAD_PCT, variable=self.box_py_var,
                                      command=lambda v: self._on_box_pad_changed())
        self.box_py_scale.grid(row=6, column=1, sticky="ew", padx=6)
        self.box_py_lbl = ttk.Label(style_box, text="", width=6)
        self.box_py_lbl.grid(row=6, column=2, sticky="e")

        # ---- Thời gian ----
        time_box = self._pnl_time = ttk.LabelFrame(panel, text="Thời gian hiển thị (giây)", padding=6)
        time_box.pack(fill="x", pady=(0, 6))
        ttk.Label(time_box, text="Bắt đầu:").grid(row=0, column=0, sticky="w", pady=2)
        self.start_var = tk.DoubleVar(value=0.0)
        sp1 = ttk.Spinbox(time_box, from_=0, to=3600, increment=0.1, width=7, textvariable=self.start_var,
                          command=self._on_time_edited)
        sp1.grid(row=0, column=1, sticky="w", padx=6)
        sp1.bind("<Return>", lambda e: self._on_time_edited())
        sp1.bind("<FocusOut>", lambda e: self._on_time_edited())
        ttk.Label(time_box, text="Kéo dài:").grid(row=1, column=0, sticky="w", pady=2)
        self.dur_var = tk.DoubleVar(value=0.0)
        self.dur_spin = ttk.Spinbox(time_box, from_=0.1, to=3600, increment=0.1, width=7,
                                    textvariable=self.dur_var, command=self._on_time_edited)
        self.dur_spin.grid(row=1, column=1, sticky="w", padx=6)
        self.dur_spin.bind("<Return>", lambda e: self._on_time_edited())
        self.dur_spin.bind("<FocusOut>", lambda e: self._on_time_edited())
        self.to_end_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(time_box, text="Hiện đến hết video", variable=self.to_end_var,
                        command=self._on_time_edited).grid(row=2, column=0, columnspan=2, sticky="w")

        # ---- Font ----
        font_box = self._pnl_font = ttk.LabelFrame(panel, text="Font chữ", padding=6)
        font_box.pack(fill="x", pady=(0, 6))
        self.font_lbl = ttk.Label(font_box, text="", foreground="#555", justify="left", wraplength=330)
        self.font_lbl.pack(anchor="w")
        fb = WrapFrame(font_box, hgap=4, vgap=4)
        fb.pack(fill="x", pady=(4, 0))
        fb.add(ttk.Button(fb, text="🔤 Chọn font trong máy...", command=self._choose_system_font))
        fb.add(ttk.Button(fb, text="📁 Chọn file font khác...", command=self._choose_font))
        fb.add(ttk.Button(fb, text="↺ Font tự động", command=self._reset_font))

    def _swatch(self, parent, command) -> tk.Label:
        lbl = tk.Label(parent, text="   đổi màu   ", relief="solid", borderwidth=1, cursor="hand2")
        lbl.bind("<Button-1>", lambda e: command())
        return lbl

    # ------------------------------------------------------- Tiện ích lớp --
    @property
    def cur(self) -> dict | None:
        return self.layers[self.sel] if 0 <= self.sel < len(self.layers) else None

    def _refresh_layer_list(self):
        self.listbox.delete(0, "end")
        for i, layer in enumerate(self.layers):
            self.listbox.insert("end", layer_summary(i, layer))
        if 0 <= self.sel < len(self.layers):
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self.sel)
            self.listbox.see(self.sel)

    def _select(self, index: int, jump: bool = True):
        self.sel = index if 0 <= index < len(self.layers) else -1
        if jump and self.cur is not None and not layer_visible(self.cur, self.playhead, self.total):
            self.playhead = clamp(self.cur["start"], 0.0, self.total)
            self._request_frame()
        self._refresh_layer_list()
        self._load_layer_to_panel()
        self._redraw_all()

    def _on_list_select(self, _event=None):
        sel = self.listbox.curselection()
        if sel and sel[0] != self.sel:
            self._select(sel[0])

    def _add_layer(self):
        n = len(self.layers)
        layer = new_layer(cy=clamp(0.5 + 0.09 * (n % 5), 0.05, 0.95), start=round(self.playhead, 1))
        self.layers.append(layer)
        self._select(len(self.layers) - 1, jump=False)
        self.text_box.focus_set()
        self.text_box.tag_add("sel", "1.0", "end-1c")

    def _add_blur_layer(self):
        layer = new_blur_layer(start=round(self.playhead, 1))
        self.layers.append(layer)
        self._select(len(self.layers) - 1, jump=False)
        self.stage.focus_set()

    def _duplicate_layer(self):
        if self.cur is None:
            return
        clone = copy.deepcopy(self.cur)
        clone["cy"] = clamp(clone["cy"] + 0.08, 0.0, 1.0)
        if is_blur(clone):
            fit_blur_center(clone)
        self.layers.insert(self.sel + 1, clone)
        self._select(self.sel + 1, jump=False)

    def _delete_layer(self):
        if self.cur is None:
            return
        del self.layers[self.sel]
        self.sel = min(self.sel, len(self.layers) - 1)
        self._refresh_layer_list()
        self._load_layer_to_panel()
        self._redraw_all()

    def _move_layer(self, delta: int):
        j = self.sel + delta
        if self.cur is None or not (0 <= j < len(self.layers)):
            return
        self.layers[self.sel], self.layers[j] = self.layers[j], self.layers[self.sel]
        self.sel = j
        self._refresh_layer_list()
        self._redraw_all()

    # ---------------------------------------------- Bảng thuộc tính <-> lớp --
    def _load_layer_to_panel(self):
        self._updating = True
        try:
            layer = self.cur
            state = "normal" if layer else "disabled"
            self.text_box.configure(state=state)
            self.text_box.delete("1.0", "end")
            self._apply_kind_visibility()
            if layer is None:
                self.font_lbl.configure(text="Chưa có lớp nào — bấm \"➕ Thêm chữ\" hoặc \"🌫 Thêm blur\".")
                return
            if is_blur(layer):
                self._sync_blur_panel()
                self._load_time_to_panel()
                return
            self.text_box.insert("1.0", layer["text"])
            self.size_var.set(layer["size_pct"])
            self.size_lbl.configure(text=f"{layer['size_pct']:.1f}%")
            self.bold_var.set(layer["bold"])
            self.outline_var.set(layer["outline"])
            self.box_var.set(layer["box"])
            self.box_op_var.set(layer["box_opacity"])
            self.box_op_lbl.configure(text=f"{layer['box_opacity']}%")
            self.box_px_var.set(layer["box_pad_x"])
            self.box_px_lbl.configure(text=f"{layer['box_pad_x']}%")
            self.box_py_var.set(layer["box_pad_y"])
            self.box_py_lbl.configure(text=f"{layer['box_pad_y']}%")
            self._refresh_box_controls()
            self._paint_swatch(self.color_sw, layer["color"])
            self._paint_swatch(self.outline_sw, layer["outline_color"])
            self._paint_swatch(self.box_sw, layer["box_color"])
            self._load_time_to_panel()
            self._refresh_font_label()
        finally:
            self._updating = False

    def _apply_kind_visibility(self):
        """Bảng thuộc tính: lớp blur chỉ cần mục blur + thời gian; lớp chữ cần
        nội dung/kiểu/font. Chỉ sắp xếp lại khi loại lớp đổi (đỡ giật/mất vị trí cuộn)."""
        kind = "blur" if (self.cur is not None and is_blur(self.cur)) else "text"
        if kind == self._panel_kind:
            return
        self._panel_kind = kind
        frames = (self._pnl_blur, self._pnl_content, self._pnl_presets, self._pnl_style,
                  self._pnl_time, self._pnl_font)
        for f in frames:
            f.pack_forget()
        show = ((self._pnl_blur, self._pnl_time) if kind == "blur" else
                (self._pnl_content, self._pnl_presets, self._pnl_style, self._pnl_time, self._pnl_font))
        for f in show:
            f.pack(fill="x", pady=(0, 6))

    def _sync_blur_panel(self):
        """Đẩy giá trị của lớp blur đang chọn lên các thanh trượt (không kích hoạt sự kiện)."""
        layer = self.cur
        if layer is None or not is_blur(layer):
            return
        was = self._updating
        self._updating = True
        try:
            self.blur_var.set(layer["blur"])
            self.blur_w_var.set(round(layer["bw"] * 100, 1))
            self.blur_h_var.set(round(layer["bh"] * 100, 1))
            self._blur_lbls["blur"].configure(text=f"{layer['blur']}%")
            self._blur_lbls["bw"].configure(text=f"{layer['bw'] * 100:.0f}%")
            self._blur_lbls["bh"].configure(text=f"{layer['bh'] * 100:.0f}%")
        finally:
            self._updating = was

    def _on_blur_changed(self):
        layer = self.cur
        if self._updating or layer is None or not is_blur(layer):
            return
        layer["blur"] = int(clamp(round(float(self.blur_var.get())), 1, 100))
        layer["bw"] = round(clamp(float(self.blur_w_var.get()) / 100, MIN_BLUR_SIZE, 1.0), 3)
        layer["bh"] = round(clamp(float(self.blur_h_var.get()) / 100, MIN_BLUR_SIZE, 1.0), 3)
        fit_blur_center(layer)
        self._blur_lbls["blur"].configure(text=f"{layer['blur']}%")
        self._blur_lbls["bw"].configure(text=f"{layer['bw'] * 100:.0f}%")
        self._blur_lbls["bh"].configure(text=f"{layer['bh'] * 100:.0f}%")
        self._changed(list_too=True)

    def _load_time_to_panel(self):
        layer = self.cur
        if layer is None:
            return
        self.start_var.set(round(layer["start"], 2))
        to_end = (layer["duration"] or 0) <= 0
        self.to_end_var.set(to_end)
        self.dur_var.set(round(layer["duration"] if not to_end else max(self.total - layer["start"], 0.1), 2))
        self.dur_spin.configure(state="disabled" if to_end else "normal")

    @staticmethod
    def _paint_swatch(label: tk.Label, color: str):
        # chữ đen/trắng tùy độ sáng nền để luôn đọc được
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        fg = "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#ffffff"
        label.configure(background=color, foreground=fg)

    def _refresh_font_label(self):
        layer = self.cur
        if layer is None:
            return
        if layer["font"]:
            self.font_lbl.configure(text=f"Font (file riêng): {layer['font']}")
        elif layer["font_family"]:
            resolved = resolve_family_font(layer["font_family"], layer["bold"])
            kieu = "Đậm" if layer["bold"] else "Thường"
            if resolved:
                self.font_lbl.configure(text=f"Font: {layer['font_family']} ({kieu})")
            else:
                self.font_lbl.configure(
                    text=f"⚠ Không còn thấy font \"{layer['font_family']}\" trên máy — "
                    "đang dùng font tự động thay thế."
                )
        else:
            auto = find_default_font(layer["bold"])
            self.font_lbl.configure(
                text=f"Font tự động: {auto}" if auto else
                "⚠ Không tìm thấy font hệ thống — hãy chọn 1 file font (.ttf/.otf)."
            )

    def _changed(self, list_too: bool = False):
        if list_too:
            keep = self.sel
            self._refresh_layer_list()
            self.sel = keep
        self._redraw_all()

    def _on_text_edited(self):
        if self._updating or self.cur is None:
            return
        self.cur["text"] = self.text_box.get("1.0", "end-1c")
        self._changed(list_too=True)

    def _on_size_changed(self):
        if self._updating or self.cur is None:
            return
        self.cur["size_pct"] = round(clamp(float(self.size_var.get()), MIN_TEXT_SIZE_PERCENT,
                                           MAX_TEXT_SIZE_PERCENT), 1)
        self.size_lbl.configure(text=f"{self.cur['size_pct']:.1f}%")
        self._changed()

    def _on_box_opacity_changed(self):
        if self._updating or self.cur is None:
            return
        self.cur["box_opacity"] = int(clamp(float(self.box_op_var.get()), 0, 100))
        self.box_op_lbl.configure(text=f"{self.cur['box_opacity']}%")
        self._changed()

    def _on_box_pad_changed(self):
        if self._updating or self.cur is None:
            return
        self.cur["box_pad_x"] = int(clamp(float(self.box_px_var.get()), 0, MAX_BOX_PAD_PCT))
        self.cur["box_pad_y"] = int(clamp(float(self.box_py_var.get()), 0, MAX_BOX_PAD_PCT))
        self.box_px_lbl.configure(text=f"{self.cur['box_pad_x']}%")
        self.box_py_lbl.configure(text=f"{self.cur['box_pad_y']}%")
        self._changed()

    def _refresh_box_controls(self):
        """Chỉ cho chỉnh độ đậm / kích thước nền khi đang bật 'Nền chữ'."""
        on = bool(self.cur and self.cur["box"])
        for sc in (self.box_op_scale, self.box_px_scale, self.box_py_scale):
            sc.state(["!disabled"] if on else ["disabled"])

    def _on_style_toggled(self):
        if self._updating or self.cur is None:
            return
        self.cur["bold"] = bool(self.bold_var.get())
        self.cur["outline"] = bool(self.outline_var.get())
        self.cur["box"] = bool(self.box_var.get())
        self._refresh_box_controls()
        self._refresh_font_label()
        self._changed()

    def _pick_color(self, key: str):
        layer = self.cur
        if layer is None:
            return
        result = colorchooser.askcolor(color=layer[key], title="Chọn màu", parent=self)
        if result and result[1]:
            layer[key] = result[1].upper()
            # bật luôn viền/nền khi người dùng chủ động chọn màu cho chúng
            if key == "outline_color":
                layer["outline"] = True
            elif key == "box_color":
                layer["box"] = True
            self._load_layer_to_panel()
            self._changed()

    def _apply_preset(self, style: dict):
        layer = self.cur
        if layer is None:
            return
        layer.update(copy.deepcopy(style))
        self._load_layer_to_panel()
        self._changed()

    def _on_time_edited(self):
        if self._updating or self.cur is None:
            return
        layer = self.cur
        try:
            start = float(self.start_var.get())
            dur = float(self.dur_var.get())
        except (tk.TclError, ValueError):
            self._load_time_to_panel()
            return
        if self.to_end_var.get():
            set_layer_span(layer, start, self.total, self.total)
            layer["duration"] = 0.0
        else:
            set_layer_span(layer, start, start + max(dur, MIN_LAYER_SECONDS), self.total)
            if layer["duration"] == 0.0:      # chạm cuối video -> giữ ở chế độ "hết video"
                pass
        self._updating = True
        try:
            self._load_time_to_panel()
        finally:
            self._updating = False
        self._changed(list_too=True)
        self._draw_timeline()

    def _choose_system_font(self):
        layer = self.cur
        if layer is None:
            return
        dialog = FontPickerDialog(self, current_family=layer["font_family"], bold=layer["bold"])
        self.wait_window(dialog)
        if dialog.result == "browse_file":
            self._choose_font()
            return
        if dialog.result == "auto":
            layer["font"] = ""
            layer["font_family"] = ""
            self._refresh_font_label()
            self._changed()
        elif dialog.result:
            layer["font"] = ""
            layer["font_family"] = dialog.result
            self._refresh_font_label()
            self._changed()

    def _choose_font(self):
        layer = self.cur
        if layer is None:
            return
        chosen = filedialog.askopenfilename(
            title="Chọn file font", filetypes=[("Font", "*.ttf *.otf *.ttc"), ("Tất cả", "*.*")], parent=self)
        if chosen:
            layer["font"] = chosen
            layer["font_family"] = ""
            self._refresh_font_label()
            self._changed()

    def _reset_font(self):
        if self.cur is not None:
            self.cur["font"] = ""
            self.cur["font_family"] = ""
            self._refresh_font_label()
            self._changed()

    # ---------------------------------------------------- Video tham chiếu --
    def _on_ref_selected(self, _event=None):
        try:
            idx = self._ref_labels.index(self.ref_var.get())
        except ValueError:
            return
        self._set_reference(self.videos[idx])

    def _choose_other_video(self):
        chosen = filedialog.askopenfilename(
            title="Chọn video để xem trước",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv *.ts"), ("Tất cả", "*.*")],
            parent=self)
        if chosen:
            path = Path(chosen)
            if path not in self.videos:
                self.videos.insert(0, path)
                self._ref_labels = [f"{i + 1}. {v.name}" for i, v in enumerate(self.videos)]
                self.ref_combo.configure(values=self._ref_labels)
            self._set_reference(path)

    def _set_reference(self, path: Path | None):
        self.ref_video = path
        if path is None or not (self.ffmpeg_path and self.ffprobe_path):
            self.ref_dims = None
            self._frame_photo = None
            self._frame_msg = ("Chưa có video/ffmpeg để hiện khung hình — vẫn kéo thả chữ được."
                               if path is None or not self.ffmpeg_path else "Thiếu ffprobe.")
            self.frame_msg_var.set("")
            self._redraw_all()
            return
        try:
            self.ref_var.set(self._ref_labels[self.videos.index(path)])
        except ValueError:
            pass
        if path in self._probe_cache:
            self._apply_probe(path, self._probe_cache[path])
            return
        self._frame_msg = "Đang đọc video..."
        self._redraw_all()

        def work():
            try:
                info = probe_media_info(self.ffprobe_path, path)
                self._results.put(("probe", path, (info.duration, info.width, info.height)))
            except Exception as exc:  # noqa: BLE001 - báo lỗi lên giao diện, không làm treo luồng
                self._results.put(("error", path, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_probe(self, path: Path, info: tuple[float, int, int]):
        self._probe_cache[path] = info
        if path != self.ref_video:
            return
        self.total = max(info[0], MIN_LAYER_SECONDS * 2)
        self.ref_dims = (info[1], info[2])
        self.playhead = clamp(self.playhead, 0.0, self.total)
        self._load_time_to_panel()
        self._draw_timeline()
        self._request_frame()

    # -------------------------------------------- Khung hình xem trước -----
    def _aspect(self) -> float:
        if self.aspect_ratio:
            return self.aspect_ratio[0] / self.aspect_ratio[1]
        if self.ref_dims and self.ref_dims[1]:
            return self.ref_dims[0] / self.ref_dims[1]
        return 9 / 16

    def _stage_rect(self) -> tuple[int, int, int, int]:
        return stage_rect(max(self.stage.winfo_width(), 60), max(self.stage.winfo_height(), 60), self._aspect())

    def _on_stage_resized(self):
        self._redraw_all()
        self._request_frame()

    def _request_frame(self):
        if self._frame_after is not None:
            try:
                self.after_cancel(self._frame_after)
            except tk.TclError:
                pass
        self._frame_after = self.after(220, self._do_request_frame)

    def _do_request_frame(self):
        self._frame_after = None
        if self._closed or self.ref_video is None or self.ref_dims is None:
            return
        if not (self.ffmpeg_path and self.ffprobe_path):
            return
        _ox, _oy, dw, dh = self._stage_rect()
        self._frame_token += 1
        token, video, t = self._frame_token, self.ref_video, self.playhead
        try:
            out = Path(self._tmp_dir_fn()) / f"editor_frame_{token}.png"
        except OSError as exc:
            self._frame_msg = f"Không tạo được thư mục tạm: {exc}"
            self._redraw_all()
            return
        ffmpeg, aspect, fit = self.ffmpeg_path, self.aspect_ratio, self.fit_mode
        at = min(t, max(0.0, self.total - 0.1))

        def work():
            try:
                extract_preview_frame(ffmpeg, video, at, dw, dh, out, aspect_ratio=aspect, fit_mode=fit)
                self._results.put(("frame", token, (out, dw, dh)))
            except MergeError as exc:
                self._results.put(("frame_error", token, str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._results.put(("frame_error", token, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _poll_results(self):
        if self._closed:
            return
        try:
            while True:
                kind, key, payload = self._results.get_nowait()
                if kind == "probe":
                    self._apply_probe(key, payload)
                elif kind == "error":
                    if key == self.ref_video:
                        self._frame_msg = f"Không đọc được video: {payload}"
                        self.ref_dims = None
                        self._redraw_all()
                elif kind == "frame" and key == self._frame_token:
                    path, dw, dh = payload
                    try:
                        photo = tk.PhotoImage(file=str(path))
                    except tk.TclError as exc:
                        self._frame_msg = f"Không hiển thị được khung hình: {exc}"
                    else:
                        self._frame_photo = photo
                        self._frame_pil = None
                        self._blur_cache.clear()
                        if Image is not None:
                            try:
                                with Image.open(path) as im:
                                    self._frame_pil = im.convert("RGB")
                            except Exception:  # noqa: BLE001
                                self._frame_pil = None
                        self.frame_msg_var.set("")
                    self._frame_files.append(path)
                    self._cleanup_old_frames(keep=path)
                    self._redraw_all()
                elif kind == "frame_error" and key == self._frame_token:
                    self.frame_msg_var.set("Không trích được khung hình (xem thử vẫn dùng được).")
        except queue.Empty:
            pass
        self.after(120, self._poll_results)

    def _cleanup_old_frames(self, keep: Path):
        remaining = []
        for f in self._frame_files:
            if f == keep:
                remaining.append(f)
                continue
            try:
                f.unlink(missing_ok=True)
            except OSError:
                remaining.append(f)
        self._frame_files = remaining

    # -------------------------------------------------------------- Vẽ ------
    def _redraw_all(self):
        if self._closed:
            return
        self._redraw_stage()
        self._draw_timeline()
        self.time_var.set(f"{fmt_time(self.playhead, True)} / {fmt_time(self.total, True)}")

    def _tk_font(self, family: str, px: int, bold: bool) -> tkfont.Font:
        key = (family, px, bold)
        f = self._font_cache.get(key)
        if f is None:
            if len(self._font_cache) > 80:
                self._font_cache.clear()
            f = tkfont.Font(family=family, size=-px, weight="bold" if bold else "normal")
            self._font_cache[key] = f
        return f

    def _layer_lines(self, layer: dict, dw: int, fontsize: int) -> list[str]:
        fontfile = resolve_layer_font(layer)
        width_fn = make_width_fn(fontfile, fontsize, layer["bold"])
        limit = wrap_width_limit(dw, fontsize, layer["outline"], layer["box"], layer["box_pad_x"])
        wrapped = wrap_text_for_width(layer["text"].strip("\n") or " ", fontsize, limit, layer["bold"], width_fn)
        return wrapped.split("\n")

    def _redraw_stage(self):
        c = self.stage
        c.delete("all")
        W, H = max(c.winfo_width(), 60), max(c.winfo_height(), 60)
        ox, oy, dw, dh = self._stage_rect()
        if self._frame_photo is not None:
            c.create_image(ox, oy, image=self._frame_photo, anchor="nw")
        else:
            c.create_rectangle(ox, oy, ox + dw, oy + dh, fill="#3a3a3a", outline="")
            c.create_text(ox + dw / 2, oy + dh / 2, text=self._frame_msg, fill="#bbbbbb",
                          width=max(80, dw - 30), justify="center")
        c.create_rectangle(ox, oy, ox + dw, oy + dh, outline="#777777")

        self._geom.clear()
        shown = [(i, layer) for i, layer in enumerate(self.layers)
                 if layer_visible(layer, self.playhead, self.total)]
        # Giống bộ xuất: blur vẽ trước, chữ nằm đè lên trên
        for i, layer in shown:
            if is_blur(layer):
                self._draw_blur(i, layer, ox, oy, dw, dh)
        for i, layer in shown:
            if not is_blur(layer) and layer["text"].strip():
                self._draw_layer(i, layer, ox, oy, dw, dh)

        gv, gh = self._guides
        if gv:
            c.create_line(ox + dw / 2, oy, ox + dw / 2, oy + dh, fill="#ff4d94", dash=(4, 3))
        if gh:
            c.create_line(ox, oy + dh / 2, ox + dw, oy + dh / 2, fill="#ff4d94", dash=(4, 3))

        self._handles = {}
        if self.sel in self._geom:
            x0, y0, x1, y1 = self._geom[self.sel]
            c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline="#ffffff", dash=(4, 3))
            hs = HANDLE_SIZE / 2
            for name, (px, py) in {"tl": (x0 - 3, y0 - 3), "tr": (x1 + 3, y0 - 3),
                                   "bl": (x0 - 3, y1 + 3), "br": (x1 + 3, y1 + 3)}.items():
                c.create_rectangle(px - hs, py - hs, px + hs, py + hs, fill="#ffffff", outline="#3b82f6")
                self._handles[name] = (px - hs - 2, py - hs - 2, px + hs + 2, py + hs + 2)
        elif self.cur is not None and not layer_visible(self.cur, self.playhead, self.total):
            c.create_text(ox + dw / 2, oy + 14, fill="#ffcc66",
                          text="Lớp đang chọn chưa hiện ở thời điểm này — kéo thước thời gian", width=dw - 20)

    def _blur_preview(self, layer: dict, x0: float, y0: float, x1: float, y1: float, dw: int, dh: int):
        """PhotoImage vùng đã làm mờ THẬT (cần Pillow) — cùng công thức sigma với
        bộ xuất (theo tỉ lệ chiều cao khung). None nếu chưa có Pillow/khung hình."""
        pil = self._frame_pil
        if pil is None or ImageTk is None or pil.size != (dw, dh):
            return None
        ix0, iy0 = int(round(x0)), int(round(y0))
        ix1, iy1 = max(ix0 + 1, int(round(x1))), max(iy0 + 1, int(round(y1)))
        sigma = max(0.5, dh * layer["blur"] * BLUR_SIGMA_PER_STRENGTH)
        key = (ix0, iy0, ix1, iy1, round(sigma, 1))
        photo = self._blur_cache.get(key)
        if photo is None:
            try:
                region = pil.crop((ix0, iy0, ix1, iy1)).filter(ImageFilter.GaussianBlur(sigma))
                photo = ImageTk.PhotoImage(region)
            except Exception:  # noqa: BLE001
                return None
            if len(self._blur_cache) > 24:
                self._blur_cache.clear()
            self._blur_cache[key] = photo
        return photo

    def _draw_blur(self, i: int, layer: dict, ox: int, oy: int, dw: int, dh: int):
        c = self.stage
        x0, y0, x1, y1 = blur_rect(layer, ox, oy, dw, dh)
        photo = self._blur_preview(layer, x0 - ox, y0 - oy, x1 - ox, y1 - oy, dw, dh)
        if photo is not None:
            c.create_image(round(x0), round(y0), image=photo, anchor="nw")
        else:   # chưa có Pillow / khung hình: tô mờ tạm để vẫn thấy vùng
            c.create_rectangle(x0, y0, x1, y1, fill="#cfcfcf", outline="", stipple="gray50")
        c.create_rectangle(x0, y0, x1, y1, outline="#c084fc", dash=(3, 3))
        c.create_text(x0 + 4, y0 + 3, text="BLUR", anchor="nw", fill="#e9d5ff", font=("", 8, "bold"))
        self._geom[i] = (x0, y0, x1, y1)

    def _draw_layer(self, i: int, layer: dict, ox: int, oy: int, dw: int, dh: int):
        c = self.stage
        fontsize = max(6, round(dh * layer["size_pct"] / 100))
        fontfile = resolve_layer_font(layer)
        custom = bool(layer["font"].strip())
        picked = layer["font_family"].strip()
        if picked and not custom and resolve_family_font(picked, layer["bold"]):
            family = picked                      # đúng họ font đã chọn trong danh sách
        else:
            family = font_family_for(fontfile or "")
        # Bộ xuất không tự in đậm khi dùng file font riêng -> xem trước cũng vậy
        font = self._tk_font(family, fontsize, bool(layer["bold"]) and not custom)
        lines = self._layer_lines(layer, dw, fontsize)
        line_h = font.metrics("linespace")
        bw = max(font.measure(ln) for ln in lines)
        bh = line_h * len(lines)
        pad_x, pad_y, border, margin = style_metrics(
            fontsize, layer["outline"], layer["box"], layer["box_pad_x"], layer["box_pad_y"])
        x, y = block_position(layer["cx"], layer["cy"], dw, dh, bw, bh, margin)
        tx0, ty0 = ox + x, oy + y

        if layer["box"]:
            stipple = "" if layer["box_opacity"] >= 85 else ("gray75" if layer["box_opacity"] >= 60 else
                                                              ("gray50" if layer["box_opacity"] >= 35 else "gray25"))
            opts = {"stipple": stipple} if stipple else {}
            c.create_rectangle(tx0 - pad_x, ty0 - pad_y, tx0 + bw + pad_x, ty0 + bh + pad_y,
                               fill=layer["box_color"], outline="", **opts)
        text = "\n".join(lines)
        cxp, cyp = tx0 + bw / 2, ty0 + bh / 2
        if layer["outline"]:
            for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
                c.create_text(cxp + dx * border, cyp + dy * border, text=text, font=font,
                              fill=layer["outline_color"], justify="center", anchor="center")
        c.create_text(cxp, cyp, text=text, font=font, fill=layer["color"], justify="center", anchor="center")
        ex = pad_x + (border if layer["outline"] else 0)
        ey = pad_y + (border if layer["outline"] else 0)
        self._geom[i] = (tx0 - ex, ty0 - ey, tx0 + bw + ex, ty0 + bh + ey)

    # ------------------------------------------------ Tương tác khung hình --
    def _handle_at(self, x: float, y: float) -> str | None:
        for name, rect in self._handles.items():
            if point_in_rect(x, y, rect):
                return name
        return None

    def _layer_at(self, x: float, y: float) -> int:
        # Chữ ưu tiên hơn blur (chữ nằm đè trên blur), rồi tới lớp cuối danh sách
        for i in sorted(self._geom, key=lambda k: (not is_blur(self.layers[k]), k), reverse=True):
            if point_in_rect(x, y, self._geom[i]):
                return i
        return -1

    def _on_stage_motion(self, event):
        if self._drag:
            return
        if self._handle_at(event.x, event.y):
            cursor = "sizing"
        elif self._layer_at(event.x, event.y) >= 0:
            cursor = "fleur"
        else:
            cursor = "arrow"
        try:
            self.stage.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_stage_press(self, event):
        self.stage.focus_set()
        ox, oy, dw, dh = self._stage_rect()
        handle = self._handle_at(event.x, event.y) if self.cur is not None else None
        if handle and self.sel in self._geom and is_blur(self.cur):
            x0, y0, x1, y1 = self._geom[self.sel]
            fx, fy = {"tl": (x1, y1), "tr": (x0, y1), "bl": (x1, y0), "br": (x0, y0)}[handle]
            self._drag = dict(mode="blur_resize", fx=fx, fy=fy)
            return
        if handle and self.sel in self._geom:
            x0, y0, x1, y1 = self._geom[self.sel]
            ccx, ccy = (x0 + x1) / 2, (y0 + y1) / 2
            self._drag = dict(mode="resize", cx=ccx, cy=ccy, size0=self.cur["size_pct"],
                              dist0=max(4.0, math.hypot(event.x - ccx, event.y - ccy)))
            return
        hit = self._layer_at(event.x, event.y)
        if hit >= 0:
            if hit != self.sel:
                self._select(hit, jump=False)
            layer = self.cur
            self._drag = dict(mode="move", x0=event.x, y0=event.y, cx0=layer["cx"], cy0=layer["cy"])
        else:
            self._drag = None
            if self.sel != -1:
                self.sel = -1
                self._refresh_layer_list()
                self._load_layer_to_panel()
                self._redraw_all()

    def _on_stage_drag(self, event):
        layer = self.cur
        if not self._drag or layer is None:
            return
        _ox, _oy, dw, dh = self._stage_rect()
        d = self._drag
        if d["mode"] == "blur_resize":
            ox, oy, _dw, _dh = self._stage_rect()
            mx, my = clamp(event.x, ox, ox + dw), clamp(event.y, oy, oy + dh)
            fx, fy = d["fx"], d["fy"]
            min_w, min_h = MIN_BLUR_SIZE * dw, MIN_BLUR_SIZE * dh
            if abs(mx - fx) < min_w:       # không cho thu nhỏ hơn cạnh tối thiểu
                mx = clamp(fx + (min_w if mx >= fx else -min_w), ox, ox + dw)
            if abs(my - fy) < min_h:
                my = clamp(fy + (min_h if my >= fy else -min_h), oy, oy + dh)
            x0, x1 = sorted((fx, mx))
            y0, y1 = sorted((fy, my))
            layer["bw"] = round(clamp((x1 - x0) / dw, MIN_BLUR_SIZE, 1.0), 4)
            layer["bh"] = round(clamp((y1 - y0) / dh, MIN_BLUR_SIZE, 1.0), 4)
            layer["cx"] = clamp(((x0 + x1) / 2 - ox) / dw, 0.0, 1.0)
            layer["cy"] = clamp(((y0 + y1) / 2 - oy) / dh, 0.0, 1.0)
            fit_blur_center(layer)
            self._sync_blur_panel()
        elif d["mode"] == "move":
            cx = clamp(d["cx0"] + (event.x - d["x0"]) / dw, 0.0, 1.0)
            cy = clamp(d["cy0"] + (event.y - d["y0"]) / dh, 0.0, 1.0)
            snap_v = abs(cx * dw - dw / 2) < 6
            snap_h = abs(cy * dh - dh / 2) < 6
            layer["cx"] = 0.5 if snap_v else cx
            layer["cy"] = 0.5 if snap_h else cy
            if is_blur(layer):
                fit_blur_center(layer)
            self._guides = (snap_v, snap_h)
        else:
            dist = max(4.0, math.hypot(event.x - d["cx"], event.y - d["cy"]))
            layer["size_pct"] = round(clamp(d["size0"] * dist / d["dist0"], MIN_TEXT_SIZE_PERCENT,
                                            MAX_TEXT_SIZE_PERCENT), 1)
            self._updating = True
            try:
                self.size_var.set(layer["size_pct"])
                self.size_lbl.configure(text=f"{layer['size_pct']:.1f}%")
            finally:
                self._updating = False
        self._redraw_stage()

    def _on_stage_release(self, _event):
        was_blur_resize = bool(self._drag and self._drag.get("mode") == "blur_resize")
        self._drag = None
        if was_blur_resize:
            self._refresh_layer_list()
        self._guides = (False, False)
        self._redraw_stage()

    def _nudge(self, dx: int, dy: int, big: bool):
        layer = self.cur
        if layer is None:
            return "break"
        step = 0.02 if big else 0.004
        layer["cx"] = clamp(layer["cx"] + dx * step, 0.0, 1.0)
        layer["cy"] = clamp(layer["cy"] + dy * step, 0.0, 1.0)
        if is_blur(layer):
            fit_blur_center(layer)
        self._redraw_stage()
        return "break"

    # ---------------------------------------------------------- Timeline ---
    def _tl_geometry(self) -> tuple[float, float]:
        w = max(self.timeline.winfo_width(), 200)
        return 10.0, w - 10.0

    def _tl_row_height(self) -> int:
        n = max(1, len(self.layers))
        return int(clamp((TL_MAX_ROWS_H) // n, 12, 24))

    def _draw_timeline(self):
        if self._closed:
            return
        c = self.timeline
        c.delete("all")
        x0, x1 = self._tl_geometry()
        row_h = self._tl_row_height()
        rows = max(1, len(self.layers))
        height = RULER_H + rows * row_h + 6
        if int(float(c.cget("height"))) != height:
            c.configure(height=height)
        W = x1 + 10

        # thước
        step = ruler_step(self.total, x1 - x0)
        t = 0.0
        while t <= self.total + 1e-6:
            x = time_to_x(t, self.total, x0, x1)
            c.create_line(x, RULER_H - 7, x, RULER_H, fill="#888888")
            c.create_text(x + 3, RULER_H - 11, text=fmt_time(t), fill="#aaaaaa", anchor="w", font=("", 8))
            t += step
        c.create_line(x0, RULER_H, x1, RULER_H, fill="#555555")

        # các thanh
        self._tl_rows = []
        for i, layer in enumerate(self.layers):
            y0 = RULER_H + 3 + i * row_h
            y1 = y0 + row_h - 3
            self._tl_rows.append((y0, y1))
            bx0 = time_to_x(layer["start"], self.total, x0, x1)
            bx1 = time_to_x(layer_end(layer, self.total), self.total, x0, x1)
            bx1 = max(bx1, bx0 + 6)
            selected = i == self.sel
            blur_bar = is_blur(layer)
            if blur_bar:
                fill = "#a855f7" if selected else "#6b5b8a"
            else:
                fill = "#3b82f6" if selected else "#64748b"
            c.create_rectangle(bx0, y0, bx1, y1, fill=fill,
                               outline="#ffffff" if selected else "#94a3b8")
            c.create_rectangle(bx0, y0, bx0 + 4, y1, fill="#ffffff", outline="")
            c.create_rectangle(bx1 - 4, y0, bx1, y1, fill="#ffffff", outline="")
            label = "Blur" if blur_bar else (layer["text"].strip().splitlines() or [""])[0][:28]
            if bx1 - bx0 > 40 and row_h >= 14:
                c.create_text(bx0 + 8, (y0 + y1) / 2, text=label, fill="#ffffff", anchor="w", font=("", 8))

        # đầu phát
        px = time_to_x(self.playhead, self.total, x0, x1)
        c.create_line(px, 0, px, height, fill="#ff3b30", width=2)
        c.create_polygon(px - 6, 0, px + 6, 0, px, 9, fill="#ff3b30", outline="")

    def _set_playhead(self, t: float):
        t = clamp(t, 0.0, self.total)
        if abs(t - self.playhead) < 1e-6:
            return
        self.playhead = round(t, 3)
        self._redraw_all()
        self._request_frame()

    def _tl_hit(self, x: float, y: float):
        """(chỉ_số_lớp, vùng) với vùng in {"l","r","move"}; (-1, None) nếu trượt."""
        x0, x1 = self._tl_geometry()
        for i, (ry0, ry1) in enumerate(self._tl_rows):
            if ry0 <= y <= ry1:
                layer = self.layers[i]
                bx0 = time_to_x(layer["start"], self.total, x0, x1)
                bx1 = max(time_to_x(layer_end(layer, self.total), self.total, x0, x1), bx0 + 6)
                if bx0 - 4 <= x <= bx1 + 4:
                    if x <= bx0 + 6:
                        return i, "l"
                    if x >= bx1 - 6:
                        return i, "r"
                    return i, "move"
        return -1, None

    def _on_tl_motion(self, event):
        if self._tl_drag:
            return
        _i, zone = self._tl_hit(event.x, event.y)
        cursor = {"l": "sb_h_double_arrow", "r": "sb_h_double_arrow", "move": "fleur"}.get(zone, "arrow")
        try:
            self.timeline.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_tl_press(self, event):
        x0, x1 = self._tl_geometry()
        if event.y <= RULER_H:
            self._tl_drag = dict(mode="scrub")
            self._set_playhead(snap_time(x_to_time(event.x, self.total, x0, x1)))
            return
        idx, zone = self._tl_hit(event.x, event.y)
        if idx < 0:
            self._tl_drag = dict(mode="scrub")
            self._set_playhead(snap_time(x_to_time(event.x, self.total, x0, x1)))
            return
        if idx != self.sel:
            self._select(idx, jump=False)
        layer = self.layers[idx]
        self._tl_drag = dict(mode=zone, idx=idx, s0=layer["start"], e0=layer_end(layer, self.total),
                             t0=x_to_time(event.x, self.total, x0, x1))

    def _on_tl_drag(self, event):
        d = self._tl_drag
        if not d:
            return
        x0, x1 = self._tl_geometry()
        t = x_to_time(event.x, self.total, x0, x1)
        if d["mode"] == "scrub":
            self._set_playhead(snap_time(t))
            return
        layer = self.layers[d["idx"]]
        dt = t - d["t0"]
        s, e = d["s0"], d["e0"]
        if d["mode"] == "move":
            length = e - s
            s = clamp(s + dt, 0.0, max(0.0, self.total - length))
            e = s + length
        elif d["mode"] == "l":
            s = clamp(s + dt, 0.0, e - MIN_LAYER_SECONDS)
        else:  # "r"
            e = clamp(e + dt, s + MIN_LAYER_SECONDS, self.total)
        set_layer_span(layer, snap_time(s), snap_time(e), self.total)
        self._updating = True
        try:
            self._load_time_to_panel()
        finally:
            self._updating = False
        self._redraw_all()

    def _on_tl_release(self, _event):
        was_edit = self._tl_drag and self._tl_drag.get("mode") in ("move", "l", "r")
        self._tl_drag = None
        if was_edit:
            self._refresh_layer_list()
            self._redraw_all()

    # ------------------------------------------------------------ Đóng -----
    def _cleanup(self):
        self._closed = True
        if self._frame_after is not None:
            try:
                self.after_cancel(self._frame_after)
            except tk.TclError:
                pass
        for f in self._frame_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        self._frame_files.clear()
        self._frame_photo = None

    def _on_ok(self):
        result = copy.deepcopy(self.layers)
        self._cleanup()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self._on_apply(result)

    def _on_cancel(self):
        self._cleanup()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# ============================================================================
# Hộp thoại chọn font — tìm kiếm trong danh sách font cài trên máy
# ============================================================================

class FontPickerDialog(tk.Toplevel):
    """Modal cho phép gõ để lọc TÊN font cài trên máy thay vì phải tự mò
    file .ttf/.otf. `result` sau khi đóng cửa sổ là:
      None            — người dùng hủy (Esc / nút Hủy / đóng cửa sổ)
      "auto"          — chọn "Tự động (theo hệ thống)"
      "browse_file"   — chọn "Chọn file khác..." (nơi gọi tự mở file dialog)
      "<tên họ font>" — đã chọn 1 font cụ thể trong danh sách
    """

    AUTO_LABEL = "— Tự động (theo hệ thống) —"
    BROWSE_LABEL = "— Chọn file khác (.ttf/.otf)... —"

    def __init__(self, master, current_family: str = "", bold: bool = False):
        super().__init__(master)
        self.title("Chọn font")
        self.transient(master.winfo_toplevel() if hasattr(master, "winfo_toplevel") else master)
        self.result: Optional[str] = None
        self._families: list[str] = []
        self._filtered: list[str] = []
        self._current_family = current_family
        self._bold = bold
        self._results: queue.Queue = queue.Queue()

        self._build_ui()
        self._fit()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.search_entry.focus_set()

        self._set_status("Đang quét font cài trên máy...")
        threading.Thread(target=self._scan_worker, daemon=True).start()
        self.after(80, self._poll_results)

    def _fit(self):
        self.geometry("420x460")
        self.minsize(320, 320)

    def _build_ui(self):
        pad = ttk.Frame(self, padding=10)
        pad.pack(fill="both", expand=True)
        pad.rowconfigure(2, weight=1)
        pad.columnconfigure(0, weight=1)

        ttk.Label(pad, text="Gõ để tìm font:").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(pad, textvariable=self.search_var)
        self.search_entry.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.search_entry.bind("<KeyRelease>", self._on_search_changed)
        self.search_entry.bind("<Down>", lambda e: (self.listbox.focus_set(), self._move_selection(0)))
        self.search_entry.bind("<Return>", lambda e: self._confirm())

        list_frame = ttk.Frame(pad)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_frame, exportselection=False, activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self.listbox.bind("<Double-Button-1>", lambda e: self._confirm())
        self.listbox.bind("<Return>", lambda e: self._confirm())

        self.status_var = tk.StringVar()
        ttk.Label(pad, textvariable=self.status_var, foreground="#555", wraplength=380, justify="left").grid(
            row=3, column=0, sticky="ew", pady=(6, 0))

        btn_row = ttk.Frame(pad)
        btn_row.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btn_row, text="Hủy", command=self._on_cancel).pack(side="right")
        self.ok_btn = ttk.Button(btn_row, text="Chọn", command=self._confirm)
        self.ok_btn.pack(side="right", padx=(0, 6))

    def _set_status(self, text: str):
        self.status_var.set(text)

    # ---------------------------------------------------------- Quét font --
    def _scan_worker(self):
        try:
            fonts = list_system_fonts()
            self._results.put(("ok", fonts))
        except Exception as exc:  # noqa: BLE001 - không để lỗi quét làm treo dialog
            self._results.put(("error", str(exc)))

    def _poll_results(self):
        try:
            kind, payload = self._results.get_nowait()
        except queue.Empty:
            try:
                self.after(80, self._poll_results)
            except tk.TclError:
                pass
            return
        if kind == "ok":
            self._on_fonts_loaded(payload)
        else:
            self._set_status(f"Không quét được danh sách font ({payload}). Vẫn có thể dùng \"Chọn file khác\".")
            self._families = []
            self._apply_filter()

    def _on_fonts_loaded(self, fonts: dict[str, dict[str, str]]):
        self._fonts = fonts
        self._families = sorted(fonts.keys(), key=str.casefold)
        count = len(self._families)
        self._set_status(f"Tìm thấy {count} font trên máy. Gõ để lọc theo tên." if count else
                         "Không tìm thấy font nào trên máy — vẫn có thể dùng \"Chọn file khác\".")
        self._apply_filter(select=self._current_family)

    # -------------------------------------------------------------- Lọc ----
    def _on_search_changed(self, _event=None):
        self._apply_filter()

    def _apply_filter(self, select: str = ""):
        query = self.search_var.get().strip().lower()
        if query:
            filtered = [f for f in self._families if query in f.lower()]
        else:
            filtered = list(self._families)
        self._filtered = filtered
        self.listbox.delete(0, "end")
        self.listbox.insert("end", self.AUTO_LABEL)
        for f in filtered:
            self.listbox.insert("end", f)
        self.listbox.insert("end", self.BROWSE_LABEL)

        target = select or (self.search_var.get().strip() if False else "")
        idx = 0
        if select and select in filtered:
            idx = 1 + filtered.index(select)
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self._on_list_select()

    def _move_selection(self, delta: int):
        size = self.listbox.size()
        if size == 0:
            return
        cur = (self.listbox.curselection() or (0,))[0]
        nxt = max(0, min(size - 1, cur + delta))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(nxt)
        self.listbox.see(nxt)
        self._on_list_select()

    def _on_list_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        label = self.listbox.get(sel[0])
        if label in (self.AUTO_LABEL, self.BROWSE_LABEL):
            return
        entry = getattr(self, "_fonts", {}).get(label, {})
        has_regular = "regular" in entry
        has_bold = "bold" in entry
        if has_regular and has_bold:
            styles = "có Regular và Bold"
        elif has_bold:
            styles = "chỉ có bản Bold"
        elif has_regular:
            styles = "chỉ có bản Regular"
        else:
            styles = "1 kiểu duy nhất"
        self._set_status(f"{label} ({styles})")

    # ------------------------------------------------------------- Chọn ----
    def _confirm(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        label = self.listbox.get(sel[0])
        if label == self.AUTO_LABEL:
            self.result = "auto"
        elif label == self.BROWSE_LABEL:
            self.result = "browse_file"
        else:
            self.result = label
        self._close()

    def _on_cancel(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
