"""
theme.py
========
Giao diện TỐI dùng chung cho toàn bộ app (bảng màu + cấu hình ttk.Style).

Cách dùng: gọi `apply_dark_theme(root)` MỘT LẦN ngay sau khi tạo cửa sổ chính
(trước khi dựng các widget). Mọi widget ttk (Frame, Label, Entry, Combobox,
Spinbox, Checkbutton, Button, Treeview, Scrollbar, Progressbar...) sẽ tự có
màu tối; các style phụ dành riêng cho thiết kế mới:

    Page.TFrame / Page.TLabel / PageMuted.TLabel : nền "trang" (tối nhất)
    CardTitle.TLabel                              : tiêu đề thẻ (đậm)
    Muted.TLabel                                  : chữ phụ, màu xám
    Link.TLabel                                   : chữ dạng liên kết (xanh)
    Empty.TLabel                                  : chữ trạng thái "bảng trống"

Quy ước: widget mặc định có nền = CARD (màu của thẻ), vì phần lớn widget nằm
bên trong thẻ; riêng khung nền toàn trang dùng các style "Page.*".
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# ------------------------------------------------------------------ Bảng màu --
BG = "#14161b"            # nền cửa sổ / trang
CARD = "#1c1f26"          # nền thẻ
CARD_BORDER = "#2b303a"   # viền thẻ
FIELD = "#12141a"         # nền ô nhập / bảng
FIELD_BORDER = "#323843"  # viền ô nhập
FG = "#e6e8ec"            # chữ chính
MUTED = "#8a91a0"         # chữ phụ
DISABLED_FG = "#5b6270"

BTN = "#262a33"           # nút thường
BTN_HOVER = "#313642"
BTN_PRESS = "#2a2e38"
BTN_DISABLED = "#1f222a"

ACCENT = "#2563eb"        # xanh dương (nút chính)
ACCENT_HOVER = "#1d4ed8"
LINK = "#6ea8fe"
GREEN = "#1f9d57"         # nút Bắt đầu
GREEN_HOVER = "#17804a"
RED = "#c0444c"           # nút Dừng
RED_HOVER = "#a3363e"

DISABLED_BTN_BG = "#3a3f4a"   # nền nút màu khi bị khóa
DISABLED_BTN_FG = "#8a91a0"
DISABLED_RED_BG = "#5a2f35"   # nút Dừng khi chưa dùng được (đỏ trầm như bản thiết kế)
DISABLED_RED_FG = "#d9a3a8"

SEG_BG = "#22262e"        # thanh tab dạng viên thuốc
SEG_BORDER = "#313642"
SEG_SELECTED = "#383d49"  # tab con đang chọn
MAIN_SEL_BG = "#1b3157"   # tab chính đang chọn
MAIN_SEL_BORDER = "#2f6fdc"

AI_BG = "#4f3fc4"          # nút hành động AI (Tạo kịch bản): tím
AI_HOVER = "#6152dc"
AI_BORDER = "#8b7cff"
DIM = "#6b7280"            # chữ rất nhẹ (dòng xem trước)

TICK_GREEN = "#22c55e"      # màu dấu tích xanh
TICK_GREEN_DIM = "#2f6f4a"

SELECT_BG = "#2f5d9e"     # dòng đang chọn trong bảng
SELECT_FG = "#ffffff"


_CHECK_IMAGES: list = []   # giữ tham chiếu ảnh (nếu không Tk sẽ xóa mất ảnh)


def _make_check_image(root, size: int, fill: str, border: str, tick: str | None):
    """Vẽ 1 ô tích bo góc nhẹ bằng PhotoImage (không cần Pillow)."""
    img = tk.PhotoImage(master=root, width=size, height=size)
    img.put(CARD, to=(0, 0, size, size))                       # nền ngoài trùng nền thẻ
    img.put(border, to=(1, 0, size - 1, size))                 # viền (cắt 4 góc cho bo nhẹ)
    img.put(border, to=(0, 1, size, size - 1))
    img.put(fill, to=(2, 1, size - 2, size - 1))
    img.put(fill, to=(1, 2, size - 1, size - 2))
    if tick:
        def dot(x, y):
            img.put(tick, to=(x, y, x + 2, y + 2))             # nét dày 2px
        s = size / 18.0
        pts = []
        for (x0, y0, x1, y1) in ((4.5, 9.5, 7.5, 12.5), (7.5, 12.5, 13.5, 5.5)):
            steps = 8
            for i in range(steps + 1):
                pts.append((round((x0 + (x1 - x0) * i / steps) * s), round((y0 + (y1 - y0) * i / steps) * s)))
        for (x, y) in pts:
            dot(x, y)
    return img


def _install_green_check(root, style: ttk.Style):
    """Thay ô tích mặc định (dấu X xanh dương) bằng ô có dấu ✓ XANH LÁ."""
    size = 18
    off = _make_check_image(root, size, FIELD, "#4a5262", None)
    on = _make_check_image(root, size, TICK_GREEN, TICK_GREEN, "#ffffff")
    dis = _make_check_image(root, size, BTN_DISABLED, "#3a3f4a", None)
    dis_on = _make_check_image(root, size, TICK_GREEN_DIM, TICK_GREEN_DIM, "#b9c9bf")
    _CHECK_IMAGES.extend([off, on, dis, dis_on])
    try:
        style.element_create(
            "Green.Check.indicator", "image", off,
            ("disabled", "selected", dis_on), ("disabled", dis), ("selected", on),
            width=size + 6, sticky="w",
        )
    except tk.TclError:
        return   # đã tạo trước đó
    style.layout("TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Green.Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])


def apply_dark_theme(root: tk.Misc) -> ttk.Style:
    """Áp dụng giao diện tối cho toàn app. Trả về ttk.Style đã cấu hình."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")   # theme duy nhất cho phép đổi màu đầy đủ trên cả Win/Mac/Linux
    except tk.TclError:
        pass

    try:
        root.configure(bg=BG)
    except tk.TclError:
        pass

    # Cỡ chữ mặc định dễ đọc hơn trên Windows/Linux (macOS giữ nguyên cỡ hệ thống)
    if sys.platform != "darwin":
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=10)
            except tk.TclError:
                pass

    # Widget Tk thuần (Text, Listbox, Toplevel, popup của Combobox) không đi qua ttk.Style
    opts = {
        "*Toplevel.background": BG,
        "*Text.background": FIELD,
        "*Text.foreground": FG,
        "*Text.insertBackground": FG,
        "*Text.selectBackground": SELECT_BG,
        "*Text.selectForeground": SELECT_FG,
        "*Text.relief": "flat",
        "*Text.highlightThickness": 1,
        "*Text.highlightBackground": FIELD_BORDER,
        "*Text.highlightColor": ACCENT,
        "*Listbox.background": FIELD,
        "*Listbox.foreground": FG,
        "*Listbox.selectBackground": SELECT_BG,
        "*Listbox.selectForeground": SELECT_FG,
        "*Listbox.highlightThickness": 0,
        "*TCombobox*Listbox.background": FIELD,
        "*TCombobox*Listbox.foreground": FG,
        "*TCombobox*Listbox.selectBackground": SELECT_BG,
        "*TCombobox*Listbox.selectForeground": SELECT_FG,
    }
    for pattern, value in opts.items():
        root.option_add(pattern, value)

    # ---------------------------------------------------------- Mặc định chung --
    style.configure(
        ".", background=CARD, foreground=FG, fieldbackground=FIELD,
        bordercolor=CARD_BORDER, darkcolor=CARD, lightcolor=CARD, troughcolor=FIELD,
        focuscolor=CARD, selectbackground=SELECT_BG, selectforeground=SELECT_FG,
        insertcolor=FG, relief="flat",
    )
    style.map(".", foreground=[("disabled", DISABLED_FG)])

    # ------------------------------------------------------------- Frame / Label --
    style.configure("TFrame", background=CARD)
    style.configure("Page.TFrame", background=BG)
    style.configure("TLabel", background=CARD, foreground=FG)
    style.configure("Page.TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
    style.configure("PageMuted.TLabel", background=BG, foreground=MUTED)
    style.configure("CardTitle.TLabel", background=CARD, foreground=FG, font=("", 11, "bold"))
    style.configure("Link.TLabel", background=CARD, foreground=LINK)
    style.configure("Empty.TLabel", background=FIELD, foreground=MUTED)

    style.configure(
        "TLabelframe", background=CARD, bordercolor=CARD_BORDER,
        darkcolor=CARD_BORDER, lightcolor=CARD_BORDER, relief="solid",
    )
    style.configure("TLabelframe.Label", background=CARD, foreground=FG, font=("", 10, "bold"))

    # ------------------------------------------------------------------- Button --
    style.configure(
        "TButton", background=BTN, foreground=FG, bordercolor=FIELD_BORDER,
        darkcolor=BTN, lightcolor=BTN, focuscolor=BTN, padding=(12, 6), relief="flat",
    )
    style.map(
        "TButton",
        background=[("disabled", BTN_DISABLED), ("pressed", BTN_PRESS), ("active", BTN_HOVER)],
        darkcolor=[("disabled", BTN_DISABLED), ("pressed", BTN_PRESS), ("active", BTN_HOVER)],
        lightcolor=[("disabled", BTN_DISABLED), ("pressed", BTN_PRESS), ("active", BTN_HOVER)],
        foreground=[("disabled", DISABLED_FG)],
        bordercolor=[("active", "#434a58")],
    )

    # ------------------------------------------------- Entry / Combobox / Spinbox --
    field_common = dict(
        fieldbackground=FIELD, foreground=FG, bordercolor=FIELD_BORDER,
        darkcolor=FIELD_BORDER, lightcolor=FIELD_BORDER, insertcolor=FG, padding=(6, 3),
    )
    focus_ring = dict(
        bordercolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
    )
    style.configure("TEntry", **field_common)
    style.map(
        "TEntry",
        fieldbackground=[("disabled", BTN_DISABLED), ("readonly", FIELD)],
        foreground=[("disabled", DISABLED_FG), ("readonly", FG)],
        **focus_ring,
    )

    style.configure(
        "TCombobox", background=BTN, arrowcolor=FG, selectbackground=FIELD,
        selectforeground=FG, arrowsize=14, **field_common,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("disabled", BTN_DISABLED), ("readonly", FIELD)],
        foreground=[("disabled", DISABLED_FG), ("readonly", FG)],
        selectbackground=[("readonly", FIELD)],
        selectforeground=[("readonly", FG)],
        background=[("active", BTN_HOVER), ("!active", BTN)],
        arrowcolor=[("disabled", DISABLED_FG)],
        **focus_ring,
    )

    style.configure("TSpinbox", background=BTN, arrowcolor=FG, arrowsize=12, **field_common)
    style.map(
        "TSpinbox",
        fieldbackground=[("disabled", BTN_DISABLED), ("readonly", FIELD)],
        foreground=[("disabled", DISABLED_FG)],
        background=[("active", BTN_HOVER), ("!active", BTN)],
        arrowcolor=[("disabled", DISABLED_FG)],
        **focus_ring,
    )

    # --------------------------------------------------- Checkbutton / Radiobutton --
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            name, background=CARD, foreground=FG, indicatorbackground=FIELD,
            indicatorforeground="#ffffff", bordercolor=FIELD_BORDER,
            darkcolor=FIELD_BORDER, lightcolor=FIELD_BORDER, focuscolor=CARD,
            indicatormargin=(0, 1, 6, 1), padding=2,
        )
        style.map(
            name,
            background=[("active", CARD)],
            foreground=[("disabled", DISABLED_FG)],
            indicatorbackground=[
                ("disabled", BTN_DISABLED), ("selected", ACCENT), ("!selected", FIELD),
            ],
            indicatorforeground=[("disabled", DISABLED_FG), ("selected", "#ffffff")],
            bordercolor=[("selected", ACCENT), ("active", "#4a5262")],
        )

    try:
        _install_green_check(root, style)
    except tk.TclError:
        pass   # nếu không vẽ được ảnh thì dùng ô tích mặc định

    # ----------------------------------------------------------------- Notebook --
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=BTN, foreground=MUTED, padding=(14, 6), borderwidth=0)
    style.map(
        "TNotebook.Tab",
        background=[("selected", CARD), ("active", BTN_HOVER)],
        foreground=[("selected", FG)],
    )

    # ---------------------------------------------- Scrollbar / Progressbar / Misc --
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(
            name, background="#2f343e", troughcolor=FIELD, bordercolor=FIELD,
            darkcolor="#2f343e", lightcolor="#2f343e", arrowcolor=MUTED,
            arrowsize=13, gripcount=0, relief="flat",
        )
        style.map(
            name,
            background=[("pressed", "#4a5262"), ("active", "#3b4150")],
            darkcolor=[("pressed", "#4a5262"), ("active", "#3b4150")],
            lightcolor=[("pressed", "#4a5262"), ("active", "#3b4150")],
        )

    style.configure(
        "Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
        bordercolor=FIELD, darkcolor=ACCENT, lightcolor=ACCENT, thickness=8,
    )
    style.configure("TSeparator", background=CARD_BORDER)
    style.configure(
        "Horizontal.TScale", background=CARD, troughcolor=FIELD, bordercolor=FIELD_BORDER,
        darkcolor=CARD, lightcolor=CARD,
    )

    # ----------------------------------------------------------------- Treeview --
    style.configure(
        "Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG,
        bordercolor=FIELD_BORDER, darkcolor=FIELD_BORDER, lightcolor=FIELD_BORDER,
        rowheight=26, borderwidth=0,
    )
    style.map(
        "Treeview",
        background=[("selected", SELECT_BG)],
        foreground=[("selected", SELECT_FG)],
    )
    style.configure(
        "Treeview.Heading", background="#242832", foreground=FG, relief="flat",
        padding=(8, 4), bordercolor=FIELD_BORDER, darkcolor="#242832", lightcolor="#242832",
        font=("", 10, "bold"),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", "#2d323d")],
        darkcolor=[("active", "#2d323d")],
        lightcolor=[("active", "#2d323d")],
    )
    try:   # bỏ viền ngoài của Treeview cho phẳng
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    except tk.TclError:
        pass

    # ---------------------------------------- Kiểu riêng cho tab Kịch bản & Giọng đọc --
    style.configure(
        "Secondary.TButton", background=BG, foreground="#b8bdc9", bordercolor="#3a4050",
        darkcolor=BG, lightcolor=BG, focuscolor=BG, padding=(16, 9), relief="flat",
    )
    style.map(
        "Secondary.TButton",
        background=[("disabled", BG), ("pressed", BTN_PRESS), ("active", BTN)],
        darkcolor=[("disabled", BG), ("pressed", BTN_PRESS), ("active", BTN)],
        lightcolor=[("disabled", BG), ("pressed", BTN_PRESS), ("active", BTN)],
        foreground=[("disabled", DISABLED_FG), ("active", FG)],
        bordercolor=[("active", "#5a6274")],
    )
    style.configure("Icon.TButton", padding=(4, 3), width=3)
    style.configure("Dim.TLabel", background=CARD, foreground=DIM, font=("", 9))
    style.configure(
        "Thin.Horizontal.TScale", background=CARD, troughcolor=FIELD_BORDER,
        bordercolor=CARD, darkcolor=ACCENT, lightcolor=ACCENT, sliderlength=14,
    )

    return style
