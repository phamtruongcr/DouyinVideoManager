"""Icon vẽ bằng Pillow cho các nút hành động trong bảng video + tooltip.

Vẽ bằng code (không cần file ảnh) nên không phụ thuộc font emoji của từng hệ
điều hành và không cần đóng gói thêm tài nguyên. Ảnh được vẽ phóng to rồi thu
nhỏ (supersampling) để viền mịn, xuất PNG nền trong suốt rồi đưa cho Tk qua
`tk.PhotoImage(data=base64)` (không cần PIL.ImageTk).
"""
from __future__ import annotations

import base64
import io
import math
import tkinter as tk

from PIL import Image, ImageDraw

# Màu icon theo loại nút (bình thường), và màu khi bị vô hiệu hóa
ICON_COLORS = {
    "download": "#3b82f6",  # xanh dương
    "log": "#94a3b8",       # xám xanh
    "edit": "#eab308",      # vàng
    "delete": "#ef4444",    # đỏ
}
ICON_DISABLED = "#9ca3af"

_GRID = 24      # icon được thiết kế trên lưới 24x24
_SS = 8         # hệ số phóng to khi vẽ (supersampling)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _draw_glyph(kind: str, d: ImageDraw.ImageDraw, k: float, fill):
    """Vẽ hình của icon `kind` lên lưới 24x24 (đã nhân hệ số k)."""

    def P(x, y):
        return (x * k, y * k)

    def stroke(points, width, closed=False):
        pts = [P(*p) for p in points]
        if closed:
            pts = pts + [pts[0]]
        w = width * k
        d.line(pts, fill=fill, width=int(round(w)), joint="curve")
        r = w / 2  # đầu tròn ở mỗi điểm để nét bo tròn, liền mạch
        for x, y in pts:
            d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    if kind == "download":
        stroke([(12, 3.5), (12, 14.5)], 2.4)
        stroke([(7, 10), (12, 15), (17, 10)], 2.4)
        stroke([(4.5, 17), (4.5, 20.5), (19.5, 20.5), (19.5, 17)], 2.4)
    elif kind == "log":  # con mắt = "Xem"
        pts = []
        for i in range(0, 41):
            t = i / 40
            pts.append((2 + 20 * t, 12 - 7 * math.sin(math.pi * t)))
        for i in range(40, -1, -1):
            t = i / 40
            pts.append((2 + 20 * t, 12 + 7 * math.sin(math.pi * t)))
        stroke(pts, 2.2, closed=True)
        c, r = P(12, 12), 3.2 * k
        d.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), fill=fill)
    elif kind == "edit":  # bút chì
        s2 = math.sqrt(2)
        dx, dy = 1 / s2, -1 / s2    # hướng thân bút (lên phải)
        nx, ny = 1 / s2, 1 / s2     # pháp tuyến
        hw = 2.6
        p0, p1 = (8.0, 16.0), (18.0, 6.0)
        body = [
            (p0[0] - nx * hw, p0[1] - ny * hw), (p1[0] - nx * hw, p1[1] - ny * hw),
            (p1[0] + nx * hw, p1[1] + ny * hw), (p0[0] + nx * hw, p0[1] + ny * hw),
        ]
        d.polygon([P(*q) for q in body], fill=fill)
        tip = [
            (p0[0] - nx * hw, p0[1] - ny * hw), (p0[0] + nx * hw, p0[1] + ny * hw),
            (3.8, 20.2),
        ]
        d.polygon([P(*q) for q in tip], fill=fill)
        # đầu tẩy nhỏ tách bằng khe trong suốt
        cut = [
            (p1[0] - dx * 1.6 - nx * (hw + 1), p1[1] - dy * 1.6 - ny * (hw + 1)),
            (p1[0] - dx * 1.6 + nx * (hw + 1), p1[1] - dy * 1.6 + ny * (hw + 1)),
            (p1[0] - dx * 2.6 + nx * (hw + 1), p1[1] - dy * 2.6 + ny * (hw + 1)),
            (p1[0] - dx * 2.6 - nx * (hw + 1), p1[1] - dy * 2.6 - ny * (hw + 1)),
        ]
        d.polygon([P(*q) for q in cut], fill=(0, 0, 0, 0))
        stroke([(13, 21), (21, 21)], 2.0)
    elif kind == "delete":  # thùng rác
        stroke([(4, 6.5), (20, 6.5)], 2.4)
        stroke([(9, 6.5), (9, 4), (15, 4), (15, 6.5)], 2.2)
        stroke([(6, 9), (7.2, 20.5), (16.8, 20.5), (18, 9)], 2.2)
        stroke([(10.2, 11.5), (10.2, 17.5)], 1.8)
        stroke([(13.8, 11.5), (13.8, 17.5)], 1.8)


def render_icon(kind: str, color: str, size: int = 20, hover: bool = False) -> Image.Image:
    """Trả về ảnh RGBA `size`x`size`. `hover=True` thêm nền bo góc nhạt cùng
    tông màu để nút "sáng lên" khi rê chuột."""
    big = size * _SS
    k = big / _GRID
    rgb = _hex_rgb(color)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if hover:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.28), fill=rgb + (55,))
    # Vẽ glyph ở lớp riêng rồi dán đè, để khe trong suốt (đầu tẩy) vẫn trong suốt
    glyph = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    _draw_glyph(kind, ImageDraw.Draw(glyph), k * (0.78 if hover else 0.9), rgb + (255,))
    off = int(big * (0.11 if hover else 0.05))
    img.alpha_composite(glyph, (off, off))
    return img.resize((size, size), Image.LANCZOS)


def make_photo(kind: str, color: str, size: int = 20, hover: bool = False, master=None) -> tk.PhotoImage:
    buf = io.BytesIO()
    render_icon(kind, color, size, hover).save(buf, format="PNG")
    return tk.PhotoImage(master=master, data=base64.b64encode(buf.getvalue()))


class Tooltip:
    """Chú thích nhỏ hiện sau ~0,5 giây khi dừng chuột trên widget."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 500, wraplength: int = 0):
        self.widget, self.text, self.delay = widget, text, delay
        self.wraplength = wraplength  # >0: tự xuống dòng khi chú thích dài
        self._job = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._tip is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip, text=self.text, bg="#111827", fg="#f9fafb",
            padx=6, pady=2, font=("", 9), bd=0,
            wraplength=self.wraplength, justify="left",
        ).pack()
        self._tip = tip

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
