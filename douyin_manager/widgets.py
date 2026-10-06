"""
widgets.py
==========
Các widget Tkinter tùy chỉnh dùng chung cho GUI.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme


class WrapFrame(ttk.Frame):
    """Frame chứa nhiều widget con (thường là các nút bấm), tự động xuống
    dòng khi bề ngang không đủ chỗ — giống hành vi flex-wrap trong CSS.
    Dùng cho các thanh nút để khi thu nhỏ cửa sổ, nút không bị tràn ra
    ngoài / bị che mất mà sẽ tự chia xuống dòng 2, dòng 3...

    Cách dùng: tạo WrapFrame như 1 Frame bình thường rồi gọi `.add(widget)`
    cho từng nút đã được `master=wrap_frame` (không tự pack/place nút đó
    nữa, WrapFrame sẽ tự định vị bằng .place())."""

    def __init__(self, master, hgap: int = 6, vgap: int = 6, valign: str = "top", **kwargs):
        super().__init__(master, **kwargs)
        self.hgap = hgap
        self.vgap = vgap
        # "top" (mặc định): các widget trong cùng 1 hàng căn theo mép trên.
        # "center": căn giữa theo chiều dọc — dùng khi trộn nút ttk với nút
        # màu (AccentButton) có chiều cao hơi khác nhau.
        self.valign = valign
        self._children: list[tk.Widget] = []
        self.bind("<Configure>", self._on_configure)

    def add(self, widget: tk.Widget):
        self._children.append(widget)
        self._reflow(self.winfo_width())

    def reflow(self):
        """Tính lại vị trí các widget con - gọi khi nội dung 1 widget con
        thay đổi kích thước (VD: đổi text của Label) mà không phải do
        resize cửa sổ, để WrapFrame cập nhật layout kịp thời."""
        self._reflow(self.winfo_width())

    def _on_configure(self, event):
        self._reflow(event.width)

    def _reflow(self, width: int):
        if width <= 1 or not self._children:
            return
        # Lượt 1: chia các widget vào từng hàng, tính chiều cao mỗi hàng
        rows: list[list[tuple[tk.Widget, int, int]]] = [[]]
        x = 0
        for child in self._children:
            child.update_idletasks()
            w = child.winfo_reqwidth()
            h = child.winfo_reqheight()
            # Nếu nút hiện tại không còn vừa hàng ngang -> xuống dòng mới
            if x > 0 and x + w > width:
                rows.append([])
                x = 0
            rows[-1].append((child, w, h))
            x += w + self.hgap
        # Lượt 2: đặt vị trí từng widget
        y = 0
        for row in rows:
            row_height = max(h for _, _, h in row)
            x = 0
            for child, w, h in row:
                dy = (row_height - h) // 2 if self.valign == "center" else 0
                child.place(x=x, y=y + dy, width=w, height=h)
                x += w + self.hgap
            y += row_height + self.vgap
        total_height = y - self.vgap
        # Cập nhật chiều cao của WrapFrame cho khớp số dòng hiện tại, để
        # các widget bên dưới (progress bar...) không bị đè lên
        if self.winfo_reqheight() != total_height:
            self.configure(height=total_height)


class AccentButton(tk.Label):
    """Nút bấm có MÀU NỀN riêng (xanh lá, xanh dương, đỏ...), trông giống
    nhau trên Windows/macOS/Linux. Dùng thay cho ttk.Button vì ttk.Button
    không đổi được màu nền ở nhiều theme hệ điều hành.

    Có API `state()` giống ttk (`state(["disabled"])`, `state(["!disabled"])`)
    nên thay thế trực tiếp được cho ttk.Button ở mọi chỗ đang gọi `.state()`."""

    def __init__(
        self, master, text: str, command=None, bg: str = "#1a73e8",
        hover_bg: str | None = None, fg: str = "#ffffff",
        disabled_bg: str = theme.DISABLED_BTN_BG, disabled_fg: str = theme.DISABLED_BTN_FG,
        padx: int = 14, pady: int = 5, font=("", 10, "bold"), **kwargs,
    ):
        super().__init__(
            master, text=text, bg=bg, fg=fg, padx=padx, pady=pady,
            font=font, cursor="hand2", **kwargs,
        )
        self._command = command
        self._bg = bg
        self._hover_bg = hover_bg or bg
        self._fg = fg
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._disabled = False
        self.bind("<Enter>", lambda e: self._paint(hover=True))
        self.bind("<Leave>", lambda e: self._paint(hover=False))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _paint(self, hover: bool = False):
        if self._disabled:
            self.configure(bg=self._disabled_bg, fg=self._disabled_fg, cursor="arrow")
        else:
            self.configure(
                bg=self._hover_bg if hover else self._bg, fg=self._fg, cursor="hand2",
            )

    def _on_release(self, event):
        if self._disabled or self._command is None:
            return
        # Chỉ kích hoạt nếu thả chuột vẫn còn trong nút (giống nút thật)
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()

    def state(self, flags=None):
        """Giống ttk: không truyền gì -> trả về trạng thái hiện tại."""
        if flags is None:
            return ("disabled",) if self._disabled else ()
        for flag in flags:
            if flag == "disabled":
                self._disabled = True
            elif flag == "!disabled":
                self._disabled = False
        self._paint()
        return ()

    def invoke(self):
        if not self._disabled and self._command is not None:
            self._command()


class ScrollableFrame(ttk.Frame):
    """Khung có thanh cuộn DỌC. Đặt widget con vào `self.body`. Nội dung
    tự giãn ngang theo bề rộng khung; khi cao hơn khung thì hiện thanh
    cuộn (và cuộn được bằng con lăn chuột khi trỏ chuột đang ở trong khung),
    nên dù cửa sổ nhỏ vẫn xem được toàn bộ các tùy chọn."""

    def __init__(
        self, master, canvas_width: int | None = None,
        body_style: str = "TFrame", canvas_bg: str | None = None, **kwargs,
    ):
        super().__init__(master, **kwargs)
        canvas_kw = {"width": canvas_width} if canvas_width else {}
        self._canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0,
            bg=canvas_bg or theme.CARD, **canvas_kw,
        )
        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self._canvas, style=body_style)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Chỉ bắt con lăn chuột khi trỏ chuột nằm trong khung này
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _on_body_configure(self, _event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._update_scrollbar_visibility()

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._window, width=event.width)
        self._update_scrollbar_visibility()

    def _update_scrollbar_visibility(self):
        need = self.body.winfo_reqheight() > self._canvas.winfo_height()
        if need and not self._vsb.winfo_ismapped():
            self._vsb.pack(side="right", fill="y", before=self._canvas)
        elif not need and self._vsb.winfo_ismapped():
            self._vsb.pack_forget()
            self._canvas.yview_moveto(0)

    def _bind_wheel(self, _event):
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", self._on_wheel)
        self.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self, event):
        # <Leave> cũng phát ra khi chuột đi vào widget con -> chỉ gỡ khi
        # chuột thực sự rời khỏi vùng của khung.
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            widget = None
        if widget is not None and str(widget).startswith(str(self)):
            return
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Button-4>")
        self.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        # Để Treeview / Text / Combobox tự cuộn nội dung của chính chúng
        if isinstance(event.widget, (ttk.Treeview, tk.Text, ttk.Combobox, tk.Listbox)):
            return
        if not self._vsb.winfo_ismapped():
            return
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        else:
            # Windows: bội số của 120; macOS: giá trị nhỏ (1, 2, ...)
            delta = -1 * (event.delta // 120 if abs(event.delta) >= 120 else event.delta)
        if delta:
            self._canvas.yview_scroll(int(delta), "units")


def bind_wraplength(label: tk.Widget, container: tk.Widget, margin: int = 30, min_width: int = 200):
    """Cho `label` tự xuống dòng theo bề rộng hiện tại của `container`, để
    các đoạn ghi chú dài không bị cắt mất khi cửa sổ thu nhỏ."""

    def _update(event):
        label.configure(wraplength=max(min_width, event.width - margin))

    container.bind("<Configure>", _update, add="+")


# ============================================================================
# Thành phần giao diện mới (thiết kế tối, dạng thẻ + tab viên thuốc)
# ============================================================================

def make_card(
    parent, title: str | None = None, padding=(12, 8), collapsible: bool = False,
    collapsed: bool = False, summary_var=None, on_toggle=None,
):
    """Tạo 1 "thẻ" (khung nền sáng hơn nền trang, có viền mảnh).

    Trả về (outer, inner): `outer` là khung ngoài — dùng để pack/grid thẻ vào
    trang; `inner` là khung bên trong — đặt widget con vào đây.

    collapsible=True: tiêu đề trở thành thanh bấm được (▾ mở / ▸ thu gọn); khi
    thu gọn chỉ còn thanh tiêu đề (kèm `summary_var` — 1 dòng tóm tắt — nếu có).
    `on_toggle(collapsed: bool)` được gọi mỗi khi người dùng đổi trạng thái."""
    outer = tk.Frame(
        parent, bg=theme.CARD, bd=0, highlightthickness=1,
        highlightbackground=theme.CARD_BORDER, highlightcolor=theme.CARD_BORDER,
    )
    box = ttk.Frame(outer, padding=padding)
    box.pack(fill="both", expand=True)
    if not collapsible:
        if title:
            ttk.Label(box, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
        return outer, box

    header = ttk.Frame(box)
    header.pack(fill="x")
    chevron = ttk.Label(header, text="▾", style="CardTitle.TLabel", cursor="hand2")
    chevron.pack(side="left", padx=(0, 6))
    title_lbl = ttk.Label(header, text=title or "", style="CardTitle.TLabel", cursor="hand2")
    title_lbl.pack(side="left")
    hint = ttk.Label(header, text="", style="Link.TLabel", cursor="hand2")
    hint.pack(side="right")
    summary_lbl = ttk.Label(header, textvariable=summary_var, style="Muted.TLabel", cursor="hand2") \
        if summary_var is not None else None
    content = ttk.Frame(box)
    state = {"collapsed": bool(collapsed)}

    def apply():
        if state["collapsed"]:
            content.pack_forget()
            chevron.configure(text="▸")
            hint.configure(text="Mở rộng")
            if summary_lbl is not None:
                summary_lbl.pack(side="left", padx=(12, 0), fill="x")
        else:
            if summary_lbl is not None:
                summary_lbl.pack_forget()
            content.pack(fill="both", expand=True, pady=(6, 0))
            chevron.configure(text="▾")
            hint.configure(text="Thu gọn")

    def toggle(_event=None):
        state["collapsed"] = not state["collapsed"]
        apply()
        if on_toggle is not None:
            on_toggle(state["collapsed"])

    for w in (header, chevron, title_lbl, hint) + ((summary_lbl,) if summary_lbl is not None else ()):
        w.bind("<Button-1>", toggle)
    apply()
    return outer, content


class SegmentedTabs(tk.Frame):
    """Thanh tab dạng "viên thuốc" (segmented control) nằm GIỮA, thay cho
    ttk.Notebook. API giống Notebook ở mức cần dùng: `add(frame, text=...)`,
    `select(index)`. Các frame con phải được tạo với master là chính widget này.

    variant="main": thanh tab chính trên cùng cửa sổ (tab chọn viền xanh dương).
    variant="sub" : nhóm tab con trong thẻ (tab chọn nền xám sáng)."""

    def __init__(self, master, variant: str = "sub", outer_bg: str | None = None, **kwargs):
        self.variant = variant
        self._outer_bg = outer_bg or (theme.BG if variant == "main" else theme.CARD)
        super().__init__(master, bg=self._outer_bg, **kwargs)

        holder = tk.Frame(self, bg=self._outer_bg)
        holder.pack(side="top", fill="x", pady=(6, 4) if variant == "main" else (0, 6))
        self._bar = tk.Frame(
            holder, bg=theme.SEG_BG, bd=0, highlightthickness=1,
            highlightbackground=theme.SEG_BORDER, highlightcolor=theme.SEG_BORDER,
        )
        self._bar.pack(anchor="center")   # luôn nằm giữa

        self.body = tk.Frame(self, bg=self._outer_bg)
        self.body.pack(side="top", fill="both", expand=True)

        self._tabs: list[tuple[tk.Widget, tk.Label]] = []
        self._current: int | None = None

    def add(self, frame: tk.Widget, text: str = ""):
        idx = len(self._tabs)
        main = self.variant == "main"
        label = tk.Label(
            self._bar, text=text.strip(), bg=theme.SEG_BG, fg=theme.MUTED,
            padx=16 if main else 12, pady=5 if main else 4, cursor="hand2",
            font=("", 10, "bold") if main else ("", 10), bd=0,
            highlightthickness=1, highlightbackground=theme.SEG_BG,
            highlightcolor=theme.SEG_BG,
        )
        label.pack(side="left", padx=2, pady=2)
        label.bind("<Button-1>", lambda e, i=idx: self.select(i))
        label.bind("<Enter>", lambda e, i=idx: self._on_hover(i, True))
        label.bind("<Leave>", lambda e, i=idx: self._on_hover(i, False))
        self._tabs.append((frame, label))
        if self._current is None:
            self.select(0)

    def _on_hover(self, idx: int, inside: bool):
        if idx == self._current:
            return
        self._tabs[idx][1].configure(fg=theme.FG if inside else theme.MUTED)

    def select(self, index: int):
        if not (0 <= index < len(self._tabs)):
            return
        for i, (frame, label) in enumerate(self._tabs):
            if i == index:
                if self.variant == "main":
                    label.configure(
                        bg=theme.MAIN_SEL_BG, fg="#ffffff",
                        highlightbackground=theme.MAIN_SEL_BORDER,
                        highlightcolor=theme.MAIN_SEL_BORDER,
                    )
                else:
                    label.configure(
                        bg=theme.SEG_SELECTED, fg="#ffffff",
                        highlightbackground=theme.SEG_SELECTED,
                        highlightcolor=theme.SEG_SELECTED,
                    )
                frame.pack(in_=self.body, fill="both", expand=True)
            else:
                frame.pack_forget()
                label.configure(
                    bg=theme.SEG_BG, fg=theme.MUTED,
                    highlightbackground=theme.SEG_BG, highlightcolor=theme.SEG_BG,
                )
        self._current = index
        self.event_generate("<<TabChanged>>")

    @property
    def current(self) -> int | None:
        return self._current


class CollapsibleNote(ttk.Frame):
    """Ghi chú dài được GẤP GỌN mặc định (chỉ hiện 1 dòng "ⓘ Ghi chú"), bấm
    vào để mở/đóng — giữ nguyên toàn bộ nội dung hướng dẫn mà giao diện vẫn
    gọn gàng như thiết kế."""

    def __init__(self, master, text: str, title: str = "ⓘ Ghi chú", **kwargs):
        super().__init__(master, **kwargs)
        self._title = title
        self._open = False
        self._toggle = ttk.Label(self, text=f"▸ {title}", style="Link.TLabel", cursor="hand2")
        self._toggle.pack(anchor="w")
        self._toggle.bind("<Button-1>", lambda e: self.toggle())
        self._body = ttk.Label(
            self, text=text, style="Muted.TLabel", justify="left", wraplength=760,
        )
        bind_wraplength(self._body, self, margin=8)

    def toggle(self):
        self._open = not self._open
        if self._open:
            self._body.pack(anchor="w", fill="x", pady=(4, 0))
            self._toggle.configure(text=f"▾ {self._title}")
        else:
            self._body.pack_forget()
            self._toggle.configure(text=f"▸ {self._title}")
