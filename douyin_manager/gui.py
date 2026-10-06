"""
gui.py
======
Cửa sổ chính của ứng dụng (class DouyinApp), gồm 2 TAB:
  1. "Tải video Douyin": nạp danh sách video, chọn/bỏ chọn, dịch tiêu đề,
     xóa khỏi danh sách, xuất TXT/Excel, tải video (đơn lẻ / hàng loạt).
     Bố cục từ trên xuống: Nguồn dữ liệu -> Cài đặt -> Hành động + Bảng -> Tiến độ.
  2. "Ghép Audio vào Video": xem audio_merge_gui.AudioMergeTab.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .config import (
    APP_TITLE,
    load_config,
    save_config,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_BATCH_WORKERS,
    MIN_GEMINI_BATCH_WORKERS,
    MAX_GEMINI_BATCH_WORKERS,
    GEMINI_MODEL_SUGGESTIONS,
    DEFAULT_DOWNLOAD_WORKERS,
    MIN_DOWNLOAD_WORKERS,
    MAX_DOWNLOAD_WORKERS,
    DEFAULT_FILENAME_MAX_LEN,
    MIN_FILENAME_MAX_LEN,
    MAX_FILENAME_MAX_LEN,
    DEFAULT_TRANSLATE_STYLE,
    TRANSLATE_STYLE_OPTIONS,
    TRANSLATE_STYLE_CUSTOM,
    DEFAULT_CUSTOM_TITLE_PROMPT,
)
from .audio_merge_gui import AudioMergeTab
from .douyin_client import DouyinAPIError, DouyinClient, DownloadCancelled
from .gemini_translator import translate_batch_with_gemini, list_available_models
from .utils import extract_clean_link, resolve_link, format_post_time, safe_filename, unique_filename
from .widgets import AccentButton, WrapFrame, SegmentedTabs
from . import theme

CHECK_ON = "\u2611"   # ☑
CHECK_OFF = "\u2610"  # ☐


def _resource_path(rel: str) -> Path:
    """Đường dẫn tới file tài nguyên, chạy được cả khi chạy từ source lẫn
    khi đã đóng gói bằng PyInstaller (--onefile giải nén vào sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent.parent / rel


class DouyinApp(tk.Tk):
    def __init__(self):
        super().__init__()
        theme.apply_dark_theme(self)
        self.title(APP_TITLE)
        self._set_window_icon()
        self._apply_initial_geometry()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.cfg = load_config()
        self.client = DouyinClient(cookie=self.cfg.get("cookie", ""))
        self.gemini_api_key = self.cfg.get("gemini_api_key", "")
        self.gemini_model = self.cfg.get("gemini_model", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL
        self.gemini_fallback_model = self.cfg.get(
            "gemini_fallback_model", DEFAULT_GEMINI_FALLBACK_MODEL
        )
        try:
            self.gemini_batch_workers = int(
                self.cfg.get("gemini_batch_workers", DEFAULT_GEMINI_BATCH_WORKERS)
            )
        except (TypeError, ValueError):
            self.gemini_batch_workers = DEFAULT_GEMINI_BATCH_WORKERS
        self.translate_style = self.cfg.get("translate_style", DEFAULT_TRANSLATE_STYLE)
        if self.translate_style not in TRANSLATE_STYLE_OPTIONS.values():
            self.translate_style = DEFAULT_TRANSLATE_STYLE
        # Prompt do người dùng tự điền (dùng khi kiểu dịch = "Tự điền prompt riêng").
        self.custom_title_prompt = self.cfg.get("custom_title_prompt", DEFAULT_CUSTOM_TITLE_PROMPT)
        if not isinstance(self.custom_title_prompt, str):
            self.custom_title_prompt = DEFAULT_CUSTOM_TITLE_PROMPT
        self.gemini_batch_workers = max(
            MIN_GEMINI_BATCH_WORKERS, min(MAX_GEMINI_BATCH_WORKERS, self.gemini_batch_workers)
        )
        self.download_dir = Path(
            self.cfg.get("download_dir", str(Path.home() / "Downloads" / "Douyin"))
        )
        try:
            self.download_workers = int(
                self.cfg.get("download_workers", DEFAULT_DOWNLOAD_WORKERS)
            )
        except (TypeError, ValueError):
            self.download_workers = DEFAULT_DOWNLOAD_WORKERS
        self.download_workers = max(
            MIN_DOWNLOAD_WORKERS, min(MAX_DOWNLOAD_WORKERS, self.download_workers)
        )
        try:
            self.filename_max_len = int(
                self.cfg.get("filename_max_len", DEFAULT_FILENAME_MAX_LEN)
            )
        except (TypeError, ValueError):
            self.filename_max_len = DEFAULT_FILENAME_MAX_LEN
        self.filename_max_len = max(
            MIN_FILENAME_MAX_LEN, min(MAX_FILENAME_MAX_LEN, self.filename_max_len)
        )

        # dữ liệu video: id -> dict(desc,url,...); giữ thứ tự riêng
        self.videos: dict[str, dict] = {}
        self.order: list[str] = []
        self.checked: dict[str, bool] = {}
        self.statuses: dict[str, str] = {}   # vid -> "Chưa tải" / "Đang tải" / "Đã tải" / "Lỗi"
        self.logs: dict[str, str] = {}       # vid -> nội dung log/lỗi chi tiết
        self.titles: dict[str, str] = {}     # vid -> tiêu đề (có thể sửa tay)
        self.downloading_ids: set[str] = set()
        self._downloading_lock = threading.Lock()

        # vid -> {"download": Label, "log": Label, "edit": Label, "delete": Label}
        # Các label dạng "link" (chữ màu, gạch chân) được đặt đè lên (place)
        # 4 cột hành động của mỗi hàng trong Treeview, để nhìn giống link có
        # thể bấm thay vì chữ thường, dễ phân biệt hơn.
        self.row_widgets: dict[str, dict[str, tk.Label]] = {}

        self.task_queue = queue.Queue()
        self.stop_loading_flag = False
        self.is_busy = False
        self.is_translating = False

        # Cờ dừng tải dùng CHUNG cho mọi phiên tải (tải hàng loạt lẫn tải
        # từng video lẻ) — set() là báo hiệu dừng NGAY, kể cả video đang
        # tải dở (kiểm tra trong douyin_client.download_video sau mỗi
        # chunk 256KB). active_download_sessions đếm số phiên tải đang
        # chạy để biết khi nào bật/tắt nút "⏹ Dừng tải" và khi nào an
        # toàn để clear() cờ dừng cho phiên tải MỚI (không đụng tới phiên
        # đang dừng dở của lần trước).
        self.download_stop_event = threading.Event()
        self.active_download_sessions = 0

        self._build_ui()
        self.after(100, self._poll_queue)
        self.after(150, self._reposition_action_buttons)

    # ------------------------------------------------- Cửa sổ / đóng app --
    def _apply_initial_geometry(self):
        """Chọn kích thước cửa sổ ban đầu theo kích thước màn hình (đủ lớn để
        thấy hết mọi nút, nhưng không vượt quá màn hình), đặt giữa màn hình."""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1280, max(900, sw - 100))
        h = min(860, max(600, sh - 140))
        w, h = min(w, sw), min(h, sh)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(min(900, sw), min(600, sh))

    def _on_close(self):
        """Đóng app: dừng ghép đang chạy (nếu có) và xóa các bản xem thử tạm."""
        merge_tab = getattr(self, "merge_tab", None)
        was_merging = False
        if merge_tab is not None:
            try:
                was_merging = merge_tab.shutdown()
            except Exception:  # noqa: BLE001 - đóng app không được phép kẹt vì lỗi dọn dẹp
                pass
        if was_merging:
            # Chờ ffmpeg kịp nhận lệnh dừng rồi mới đóng hẳn
            self.after(700, self.destroy)
        else:
            self.destroy()

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        self.notebook = SegmentedTabs(self, variant="main")
        self.notebook.pack(fill="both", expand=True)

        self.tab_download = ttk.Frame(self.notebook)
        self.merge_tab = AudioMergeTab(self.notebook, self.cfg)
        self.notebook.add(self.tab_download, text="⬇  Tải video Douyin")
        self.notebook.add(self.merge_tab, text="♫  Ghép Audio vào Video")

        self._build_download_tab(self.tab_download)

    def _build_download_tab(self, root):
        """Bố cục theo luồng thao tác tuyến tính, từ trên xuống dưới:
            1. Nguồn dữ liệu  ->  2. Cài đặt (Tải về | Xử lý/Dịch)
            ->  3. Hành động + Bảng video  ->  4. Tiến độ / trạng thái (đáy)."""
        PAD = 10

        # --- ĐÁY: pack TRƯỚC (side=bottom) để thanh tiến độ + trạng thái luôn
        # hiện đủ, không bị đẩy ra khỏi cửa sổ khi phần phía trên dài ra ---
        bottom = ttk.Frame(root, padding=(PAD, 4, PAD, PAD))
        bottom.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x")
        self.progress_label_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.progress_label_var, style="Muted.TLabel").pack(
            anchor="w"
        )
        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(bottom, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")

        top = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        top.pack(side="top", fill="x")

        # =================================================================
        # 1. NGUỒN DỮ LIỆU: link + số lượng + (Lấy danh sách | Cài đặt)
        # =================================================================
        source = ttk.LabelFrame(top, text=" 1 · Nguồn dữ liệu ", padding=8)
        source.pack(fill="x")
        source.columnconfigure(0, weight=1)

        ttk.Label(
            source, text="Link kênh Douyin (có thể dán cả đoạn text lộn xộn)"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(source, text="Số lượng video muốn lấy").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        # Ô nhập link trải dài; dấu ✕ (xóa link) nằm BÊN TRONG ô, góc phải.
        # Chừa chỗ bên phải cho ✕ để chữ không chạy đè lên nó.
        style = ttk.Style(self)
        try:
            style.configure("Clear.TEntry", padding=(4, 2, 26, 2))
            link_style = "Clear.TEntry"
        except tk.TclError:
            link_style = "TEntry"
        self.link_var = tk.StringVar()
        self.link_entry = ttk.Entry(source, textvariable=self.link_var, style=link_style)
        self.link_entry.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.link_entry.bind("<Return>", lambda e: self.on_load_click())

        entry_bg = style.lookup("TEntry", "fieldbackground") or "white"
        self._clear_btn = tk.Label(
            self.link_entry, text="✕", fg="#8a8a8a", bg=entry_bg,
            cursor="hand2", font=("", 10),
        )
        self._clear_btn.bind("<Button-1>", lambda e: self.on_clear_link())
        self._clear_btn.bind("<Enter>", lambda e: self._clear_btn.config(fg="#d93025"))
        self._clear_btn.bind("<Leave>", lambda e: self._clear_btn.config(fg="#8a8a8a"))
        self.link_var.trace_add("write", lambda *_: self._update_clear_button())

        self.max_items_var = tk.StringVar(value="")
        ttk.Entry(source, textvariable=self.max_items_var, width=10).grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=(2, 0)
        )
        ttk.Label(
            source,
            text="Số lượng: để trống = lấy tất cả, tính từ video MỚI NHẤT trở về.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        source_btns = ttk.Frame(source)
        source_btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.load_btn = AccentButton(
            source_btns, text="🔍 Lấy danh sách video", command=self.on_load_click,
            bg="#1a73e8", hover_bg="#1557b0", padx=18, pady=6,
        )
        self.load_btn.pack(side="left")
        ttk.Button(
            source_btns, text="⚙ Cài đặt (Cookie)", command=self.open_settings
        ).pack(side="left", padx=(8, 0))

        # =================================================================
        # 2. CÀI ĐẶT: tách riêng "Tải về" và "Xử lý / Dịch thuật"
        # =================================================================
        settings = ttk.Frame(top)
        settings.pack(fill="x", pady=(8, 0))
        settings.columnconfigure(0, weight=3, uniform="settings")
        settings.columnconfigure(1, weight=2, uniform="settings")

        def spin(parent, var, lo, hi, on_change, width=5):
            sp = ttk.Spinbox(
                parent, from_=lo, to=hi, textvariable=var, width=width, command=on_change,
            )
            sp.bind("<FocusOut>", lambda e: on_change())
            sp.bind("<Return>", lambda e: on_change())
            return sp

        # ---- 2a. Cài đặt TẢI VỀ ----
        dl = ttk.LabelFrame(settings, text=" 2a · Cài đặt tải về ", padding=8)
        dl.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        dl.columnconfigure(1, weight=1)

        ttk.Label(dl, text="Thư mục lưu").grid(row=0, column=0, sticky="w", pady=2)
        self.folder_var = tk.StringVar(value=str(self.download_dir))
        ttk.Entry(dl, textvariable=self.folder_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=(8, 6), pady=2
        )
        ttk.Button(dl, text="Chọn thư mục...", command=self.on_choose_folder).grid(
            row=0, column=2, sticky="e", pady=2
        )

        ttk.Label(dl, text="Số video tải song song").grid(row=1, column=0, sticky="w", pady=2)
        self.download_workers_var = tk.IntVar(value=self.download_workers)
        spin(
            dl, self.download_workers_var, MIN_DOWNLOAD_WORKERS, MAX_DOWNLOAD_WORKERS,
            self._on_download_workers_changed,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(dl, text="Độ dài tên file (số ký tự lấy từ tiêu đề)").grid(
            row=2, column=0, sticky="w", pady=2
        )
        self.filename_max_len_var = tk.IntVar(value=self.filename_max_len)
        spin(
            dl, self.filename_max_len_var, MIN_FILENAME_MAX_LEN, MAX_FILENAME_MAX_LEN,
            self._on_filename_max_len_changed,
        ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)

        # ---- 2b. Cài đặt XỬ LÝ / DỊCH THUẬT ----
        tr = ttk.LabelFrame(settings, text=" 2b · Xử lý / Dịch thuật ", padding=8)
        tr.grid(row=0, column=1, sticky="nsew")
        tr.columnconfigure(1, weight=1)

        self.auto_translate_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_translate_titles", False))
        )
        ttk.Checkbutton(
            tr, text="Dịch tiêu đề → Tiếng Việt (tự động sau khi lấy danh sách)",
            variable=self.auto_translate_var, command=self._on_auto_translate_toggled,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Label(tr, text="Kiểu dịch").grid(row=1, column=0, sticky="w", pady=2)
        self._translate_style_labels = list(TRANSLATE_STYLE_OPTIONS.keys())
        current_label = next(
            (lbl for lbl, val in TRANSLATE_STYLE_OPTIONS.items() if val == self.translate_style),
            self._translate_style_labels[0],
        )
        self.translate_style_var = tk.StringVar(value=current_label)
        translate_style_combo = ttk.Combobox(
            tr, textvariable=self.translate_style_var,
            values=self._translate_style_labels, state="readonly", width=30,
        )
        translate_style_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
        translate_style_combo.bind("<<ComboboxSelected>>", self._on_translate_style_changed)

        ttk.Label(tr, text="Prompt dịch tiêu đề").grid(row=2, column=0, sticky="w", pady=2)
        prompt_row = ttk.Frame(tr)
        prompt_row.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2)
        prompt_row.columnconfigure(1, weight=1)
        ttk.Button(
            prompt_row, text="✎ Soạn prompt...", command=self.open_title_prompt_editor
        ).grid(row=0, column=0, sticky="w")
        self.prompt_state_var = tk.StringVar()
        ttk.Label(
            prompt_row, textvariable=self.prompt_state_var, style="Muted.TLabel"
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self._refresh_prompt_state_label()

        ttk.Label(tr, text="Số lô dịch song song").grid(row=3, column=0, sticky="w", pady=2)
        self.gemini_batch_workers_var = tk.IntVar(value=self.gemini_batch_workers)
        spin(
            tr, self.gemini_batch_workers_var, MIN_GEMINI_BATCH_WORKERS,
            MAX_GEMINI_BATCH_WORKERS, self._on_gemini_batch_workers_changed,
        ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=2)

        # =================================================================
        # 3. HÀNH ĐỘNG CHÍNH + BẢNG VIDEO
        # =================================================================
        mid = ttk.Frame(root, padding=(PAD, 8, PAD, 0))
        mid.pack(side="top", fill="both", expand=True)

        title_row = ttk.Frame(mid)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="Video", font=("", 14, "bold")).pack(side="left")

        # Xuất file: gộp Excel + TXT vào 1 nút dropdown, góc trên bên phải bảng
        export_btn = ttk.Menubutton(title_row, text="Xuất file...  ▾")
        export_menu = tk.Menu(export_btn, tearoff=False)
        export_menu.add_command(label="Xuất ra Excel (.xlsx)", command=self.export_excel)
        export_menu.add_command(label="Xuất ra TXT (.txt)", command=self.export_txt)
        export_btn["menu"] = export_menu
        export_btn.pack(side="right")

        # Toàn bộ nút hành động nằm NGAY TRÊN BÊN TRÁI bảng, trong 1 khung tự
        # xuống dòng: cửa sổ hẹp thì chia thành nhiều hàng, không nút nào bị che.
        toolbar = WrapFrame(mid, hgap=6, vgap=6, valign="center")
        toolbar.pack(fill="x", pady=(6, 6))
        self.actions_bar = toolbar
        self.tools_bar = toolbar

        # Nút cốt lõi nhất của ứng dụng: xanh lá, nổi bật nhất
        self.download_btn = AccentButton(
            toolbar, text="⬇ Tải video đã chọn", command=self.on_download_selected,
            bg="#1e8e3e", hover_bg="#166a2d", padx=18, pady=6,
        )
        toolbar.add(self.download_btn)

        self.stop_download_btn = AccentButton(
            toolbar, text="⏹ Dừng tải", command=self.on_stop_download,
            bg="#d93025", hover_bg="#a52714",
        )
        toolbar.add(self.stop_download_btn)
        self.stop_download_btn.state(["disabled"])

        toolbar.add(tk.Frame(toolbar, width=1, height=24, bg=theme.CARD_BORDER))  # vạch ngăn

        toolbar.add(ttk.Button(
            toolbar, text="Chọn tất cả", command=lambda: self.set_all_checked(True)
        ))
        toolbar.add(ttk.Button(
            toolbar, text="Bỏ chọn tất cả", command=lambda: self.set_all_checked(False)
        ))
        toolbar.add(ttk.Button(
            toolbar, text="Xóa mục đã chọn khỏi danh sách", command=self.on_delete_selected
        ))

        toolbar.add(tk.Frame(toolbar, width=1, height=24, bg=theme.CARD_BORDER))  # vạch ngăn

        # Dịch thủ công (video đã tick, hoặc tất cả nếu chưa tick) — dùng khi
        # tắt dịch tự động ở 2b, hoặc muốn dịch lại sau khi đổi kiểu dịch.
        toolbar.add(ttk.Button(
            toolbar, text="🌐 Dịch tiêu đề → Tiếng Việt", command=self.on_translate_titles
        ))

        table_row = ttk.Frame(mid)
        table_row.pack(fill="both", expand=True)

        cols = (
            "chk",
            "title",
            "url",
            "duration",
            "post_time",
            "status",
            "download",
            "log",
            "edit",
            "delete",
            "stt",  # số thứ tự: nằm cuối ở dữ liệu nhưng hiển thị ngay sau checkbox
        )
        self.tree = ttk.Treeview(
            table_row, columns=cols, show="headings", selectmode="extended",
            displaycolumns=(
                "chk", "stt", "title", "url", "duration", "post_time",
                "status", "download", "log", "edit", "delete",
            ),
        )
        self.tree.heading("chk", text=CHECK_OFF, command=self.toggle_select_all)
        self.tree.heading("stt", text="STT")
        self.tree.heading("title", text="Tiêu đề")
        self.tree.heading("url", text="Url")
        self.tree.heading("duration", text="Thời lượng")
        self.tree.heading("post_time", text="Ngày đăng")
        self.tree.heading("status", text="Trạng thái")
        self.tree.heading("download", text="Tải về")
        self.tree.heading("log", text="Log")
        self.tree.heading("edit", text="Sửa")
        self.tree.heading("delete", text="Xóa")

        self.tree.column("chk", width=32, anchor="center", stretch=False)
        self.tree.column("stt", width=46, minwidth=40, anchor="center", stretch=False)
        self.tree.column("title", width=260, minwidth=110, anchor="w")
        self.tree.column("url", width=220, minwidth=90, anchor="w")
        self.tree.column("duration", width=80, anchor="center", stretch=False)
        self.tree.column("post_time", width=120, anchor="center", stretch=False)
        self.tree.column("status", width=110, anchor="center", stretch=False)
        self.tree.column("download", width=64, anchor="center", stretch=False)
        self.tree.column("log", width=54, anchor="center", stretch=False)
        self.tree.column("edit", width=54, anchor="center", stretch=False)
        self.tree.column("delete", width=54, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(table_row, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_row, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        # Thanh cuộn NGANG: quan trọng vì tổng độ rộng các cột (đặc biệt
        # khi người dùng kéo giãn cột "Tiêu đề"/"Url", hoặc thu nhỏ cửa
        # sổ) có thể vượt quá bề ngang khung nhìn — nếu không có thanh
        # cuộn ngang, các cột bên phải như "Sửa"/"Xóa" sẽ bị đẩy ra ngoài
        # mà KHÔNG CÓ CÁCH NÀO xem lại được. Đặt hsb ở dưới cùng (pack
        # trước) để nó luôn chiếm trọn bề ngang, phần còn lại phía trên
        # mới chia cho tree + vsb.
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.bind("<Button-1>", self.on_tree_click)

        # Lấy đúng màu nền/chữ hiện tại của Treeview (tự thích ứng theo theme
        # sáng/tối của hệ điều hành) để làm nền cho các label kiểu "link" ở
        # cột hành động. Hàng đang được CHỌN (bấm chuột) được tô 1 màu xanh
        # riêng, rõ ràng, để người dùng luôn biết mình đang chọn dòng nào —
        # áp dụng cho cả bảng ở tab Ghép Audio. Các label link đè lên bảng
        # được đổi nền theo trạng thái chọn của hàng (xem
        # _sync_link_backgrounds) nên không bị lộ khối màu lệch tông.
        style = ttk.Style(self)
        row_bg = style.lookup("Treeview", "fieldbackground") or style.lookup(
            "Treeview", "background"
        )
        row_fg = style.lookup("Treeview", "foreground") or "black"
        self._row_bg = row_bg or "SystemWindowBackgroundColor"
        try:
            r, g, b_ = (c // 256 for c in self.winfo_rgb(self._row_bg))
            is_dark = (0.299 * r + 0.587 * g + 0.114 * b_) < 128
        except tk.TclError:
            is_dark = False
        if is_dark:
            self._select_bg, self._select_fg = "#2f5d9e", "#ffffff"
        else:
            self._select_bg, self._select_fg = "#bcd8f8", "#000000"
        style.configure("Treeview.Heading", font=("", 9, "bold"))
        style.map(
            "Treeview",
            background=[("selected", self._select_bg)],
            foreground=[("selected", self._select_fg)],
        )
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sync_link_backgrounds())
        self._link_font = ("", 9, "underline")

    def _set_window_icon(self):
        """Đặt icon cho cửa sổ + thanh taskbar. Không có file icon thì bỏ qua,
        không làm app lỗi."""
        try:
            ico = _resource_path("assets/app_icon.ico")
            png = _resource_path("assets/app_icon.png")
            if sys.platform.startswith("win") and ico.exists():
                self.iconbitmap(default=str(ico))
            elif png.exists():
                self._icon_img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    # ------------------------------------------------------------ Cài đặt --
    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Cài đặt")
        # Chiều cao tự co theo màn hình để nút "Lưu" luôn nằm trong tầm nhìn.
        win.geometry(f"560x{min(760, max(420, self.winfo_screenheight() - 120))}")
        win.minsize(480, 380)
        win.transient(self)

        ttk.Label(
            win,
            text=(
                "Dán Cookie của trang douyin.com (lấy từ trình duyệt đã mở "
                "douyin.com — xem README.md để biết cách lấy).\n"
                "Cookie giúp app lấy được danh sách video ổn định hơn."
            ),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 4))

        text = tk.Text(win, height=6, wrap="word")
        text.insert("1.0", self.client.cookie)
        text.pack(fill="both", expand=True, padx=10, pady=4)

        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=10, pady=8)

        ttk.Label(
            win,
            text=(
                "Gemini API Key (dùng cho chức năng \"Dịch tiêu đề → Tiếng Việt\").\n"
                "Lấy miễn phí tại: https://aistudio.google.com/apikey"
            ),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 4))

        gemini_var = tk.StringVar(value=self.gemini_api_key)
        gemini_entry = ttk.Entry(win, textvariable=gemini_var, show="•")
        gemini_entry.pack(fill="x", padx=10, pady=(0, 4))

        model_status_var = tk.StringVar(value="")
        fetch_models_btn = ttk.Button(
            win, text="⟳ Tải danh sách model khả dụng từ Gemini"
        )
        fetch_models_btn.pack(anchor="w", padx=10, pady=(0, 2))
        ttk.Label(
            win,
            textvariable=model_status_var,
            style="Muted.TLabel",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        ttk.Label(
            win,
            text=(
                "Model chính (bấm nút trên để lấy TẤT CẢ model đang khả dụng cho "
                "đúng API Key này, hoặc tự gõ tên model khác):"
            ),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 2))
        model_var = tk.StringVar(value=self.gemini_model)
        model_combo = ttk.Combobox(
            win, textvariable=model_var, values=GEMINI_MODEL_SUGGESTIONS
        )
        model_combo.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(
            win,
            text=(
                "Model dự phòng (tự động chuyển sang khi model chính lỗi/quá tải/"
                "hết quota phút; để TRỐNG để tắt fallback):"
            ),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 2))
        fallback_model_var = tk.StringVar(value=self.gemini_fallback_model)
        fallback_model_combo = ttk.Combobox(
            win, textvariable=fallback_model_var, values=[""] + GEMINI_MODEL_SUGGESTIONS
        )
        fallback_model_combo.pack(fill="x", padx=10, pady=(0, 8))

        def on_models_success(models: list[str]):
            fetch_models_btn.state(["!disabled"])
            if not models:
                model_status_var.set(
                    "Gemini không trả về model nào hỗ trợ dịch text cho API Key này."
                )
                return
            model_combo["values"] = models
            fallback_model_combo["values"] = [""] + models
            model_status_var.set(
                f"Đã tải {len(models)} model khả dụng cho API Key này — chọn trong danh sách bên dưới."
            )

        def on_models_error(msg: str):
            fetch_models_btn.state(["!disabled"])
            model_status_var.set(f"Không tải được danh sách model: {msg}")

        def fetch_models():
            api_key = gemini_var.get().strip()
            if not api_key:
                messagebox.showwarning(
                    APP_TITLE, "Nhập Gemini API Key trước khi tải danh sách model."
                )
                return
            model_status_var.set("Đang tải danh sách model từ Gemini...")
            fetch_models_btn.state(["disabled"])

            def worker():
                try:
                    models = list_available_models(api_key)
                except RuntimeError as exc:
                    win.after(0, on_models_error, str(exc))
                    return
                win.after(0, on_models_success, models)

            threading.Thread(target=worker, daemon=True).start()

        fetch_models_btn.configure(command=fetch_models)

        # Đã có sẵn API Key từ trước -> tự tải luôn danh sách model, khỏi
        # cần người dùng bấm nút mới thấy danh sách đầy đủ.
        if self.gemini_api_key:
            win.after(200, fetch_models)


        # Hàng nút "Lưu" được ghim ở ĐÁY (pack trước mọi widget khác) nên luôn
        # hiện đủ dù cửa sổ bị thu nhỏ; ô Cookie ở trên tự co giãn theo.
        btn_row = ttk.Frame(win)
        first_widget = win.pack_slaves()[0]
        btn_row.pack(side="bottom", fill="x", padx=10, pady=8, before=first_widget)

        def save_and_close():
            self.client.cookie = text.get("1.0", "end").strip()
            self.cfg["cookie"] = self.client.cookie

            self.gemini_api_key = gemini_var.get().strip()
            self.cfg["gemini_api_key"] = self.gemini_api_key

            self.gemini_model = model_var.get().strip() or DEFAULT_GEMINI_MODEL
            self.cfg["gemini_model"] = self.gemini_model

            self.gemini_fallback_model = fallback_model_var.get().strip()
            self.cfg["gemini_fallback_model"] = self.gemini_fallback_model

            # Số lô dịch song song giờ nằm ở toolbar chính (cạnh nút "Dịch
            # tiêu đề") và tự lưu ngay khi đổi, không cần Cài đặt nữa.

            save_config(self.cfg)
            win.destroy()

        ttk.Button(btn_row, text="Lưu", command=save_and_close).pack(side="right")
        ttk.Button(
            btn_row,
            text="Mở douyin.com để lấy Cookie",
            command=lambda: webbrowser.open("https://www.douyin.com"),
        ).pack(side="right", padx=(0, 6))
        ttk.Button(
            btn_row,
            text="Lấy Gemini API Key",
            command=lambda: webbrowser.open("https://aistudio.google.com/apikey"),
        ).pack(side="right", padx=(0, 6))

    def on_choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=str(self.download_dir))
        if chosen:
            self.download_dir = Path(chosen)
            self.folder_var.set(str(self.download_dir))
            self.cfg["download_dir"] = str(self.download_dir)
            save_config(self.cfg)
            self.actions_bar.reflow()

    def _on_download_workers_changed(self):
        """Đồng bộ + lưu ngay số luồng tải song song mỗi khi người dùng đổi
        giá trị trên thanh công cụ chính (không cần vào Cài đặt)."""
        try:
            val = int(self.download_workers_var.get())
        except (tk.TclError, ValueError):
            val = DEFAULT_DOWNLOAD_WORKERS
        val = max(MIN_DOWNLOAD_WORKERS, min(MAX_DOWNLOAD_WORKERS, val))
        self.download_workers = val
        self.download_workers_var.set(val)
        self.cfg["download_workers"] = val
        save_config(self.cfg)

    def _on_gemini_batch_workers_changed(self):
        """Đồng bộ + lưu ngay số lô dịch song song mỗi khi người dùng đổi
        giá trị trên thanh công cụ chính (không cần vào Cài đặt nữa)."""
        try:
            val = int(self.gemini_batch_workers_var.get())
        except (tk.TclError, ValueError):
            val = DEFAULT_GEMINI_BATCH_WORKERS
        val = max(MIN_GEMINI_BATCH_WORKERS, min(MAX_GEMINI_BATCH_WORKERS, val))
        self.gemini_batch_workers = val
        self.gemini_batch_workers_var.set(val)
        self.cfg["gemini_batch_workers"] = val
        save_config(self.cfg)

    def _on_translate_style_changed(self, _event=None):
        self.translate_style = TRANSLATE_STYLE_OPTIONS.get(
            self.translate_style_var.get(), DEFAULT_TRANSLATE_STYLE
        )
        self.cfg["translate_style"] = self.translate_style
        save_config(self.cfg)
        self._refresh_prompt_state_label()
        # Vừa chọn "Tự điền prompt riêng" mà chưa có prompt nào -> mở khung soạn luôn.
        if self.translate_style == TRANSLATE_STYLE_CUSTOM and not self.custom_title_prompt.strip():
            self.open_title_prompt_editor()

    def _refresh_prompt_state_label(self):
        """Hiện trạng thái prompt tùy chỉnh cạnh nút "Soạn prompt"."""
        if self.translate_style != TRANSLATE_STYLE_CUSTOM:
            self.prompt_state_var.set("(chỉ dùng khi chọn kiểu \"Tự điền prompt riêng\")")
            return
        prompt = " ".join(self.custom_title_prompt.split())
        if not prompt:
            self.prompt_state_var.set("⚠ Chưa có prompt")
        else:
            self.prompt_state_var.set("✔ " + (prompt[:38] + "…" if len(prompt) > 38 else prompt))

    def open_title_prompt_editor(self):
        """Cửa sổ soạn prompt dịch tiêu đề. Lưu vào config; tự chuyển kiểu
        dịch sang "Tự điền prompt riêng" khi bấm Lưu với prompt không rỗng."""
        win = tk.Toplevel(self)
        win.title("Prompt dịch tiêu đề")
        win.geometry("620x440")
        win.minsize(480, 340)
        win.transient(self)
        win.grab_set()

        ttk.Label(
            win,
            text=(
                "Nhập hướng dẫn để Gemini dịch / viết lại TIÊU ĐỀ video theo ý bạn "
                "(giọng điệu, độ dài, phong cách, từ cấm...).\n"
                "• Dùng {lang} để chèn ngôn ngữ đích (hiện là \"Tiếng Việt\").\n"
                "• Không cần dặn định dạng trả về — app tự thêm yêu cầu đánh số "
                "dòng để ghép kết quả đúng tiêu đề."
            ),
            wraplength=580, justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 4))

        box = tk.Text(win, wrap="word", undo=True, height=12)
        box.insert("1.0", self.custom_title_prompt)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        box.focus_set()

        def reset_default():
            box.delete("1.0", "end")
            box.insert("1.0", DEFAULT_CUSTOM_TITLE_PROMPT)

        def save():
            prompt = box.get("1.0", "end").strip()
            self.custom_title_prompt = prompt
            self.cfg["custom_title_prompt"] = prompt
            if prompt:
                # Có prompt -> tự chọn luôn kiểu "Tự điền prompt riêng".
                self.translate_style = TRANSLATE_STYLE_CUSTOM
                self.cfg["translate_style"] = TRANSLATE_STYLE_CUSTOM
                self.translate_style_var.set(
                    next(l for l, v in TRANSLATE_STYLE_OPTIONS.items() if v == TRANSLATE_STYLE_CUSTOM)
                )
            save_config(self.cfg)
            self._refresh_prompt_state_label()
            win.destroy()

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_row, text="Lưu", command=save).pack(side="right")
        ttk.Button(btn_row, text="Hủy", command=win.destroy).pack(side="right", padx=(0, 6))
        ttk.Button(btn_row, text="Khôi phục prompt mẫu", command=reset_default).pack(side="left")

    def _on_filename_max_len_changed(self):
        """Đồng bộ + lưu ngay độ dài tên file tối đa mỗi khi người dùng
        đổi giá trị trên thanh công cụ chính. Áp dụng cho các lượt tải
        TIẾP THEO (không đổi tên các file đã tải trước đó)."""
        try:
            val = int(self.filename_max_len_var.get())
        except (tk.TclError, ValueError):
            val = DEFAULT_FILENAME_MAX_LEN
        val = max(MIN_FILENAME_MAX_LEN, min(MAX_FILENAME_MAX_LEN, val))
        self.filename_max_len = val
        self.filename_max_len_var.set(val)
        self.cfg["filename_max_len"] = val
        save_config(self.cfg)

    # ------------------------------------------------------------- Link --
    def on_clear_link(self):
        """Xóa trống ô nhập link, không đụng tới danh sách video đã tải."""
        self.link_var.set("")
        self.link_entry.focus_set()

    def _update_clear_button(self):
        """Hiện dấu ✕ (xóa link) trong ô nhập chỉ khi ô đang có nội dung."""
        if self.link_var.get():
            self._clear_btn.place(relx=1.0, x=-6, rely=0.5, anchor="e")
        else:
            self._clear_btn.place_forget()

    def _on_auto_translate_toggled(self):
        self.cfg["auto_translate_titles"] = bool(self.auto_translate_var.get())
        save_config(self.cfg)

    # ------------------------------------------------------ Nạp danh sách --
    def on_load_click(self):
        if self.is_busy:
            return
        raw = self.link_var.get().strip()
        clean = extract_clean_link(raw)
        if not clean:
            messagebox.showwarning(
                APP_TITLE, "Không tìm thấy link Douyin hợp lệ trong nội dung đã nhập."
            )
            return
        # cập nhật lại ô nhập bằng link đã làm sạch, đúng yêu cầu đề bài
        self.link_var.set(clean)

        qty_raw = self.max_items_var.get().strip()
        max_items = 0
        if qty_raw:
            if not qty_raw.isdigit() or int(qty_raw) <= 0:
                messagebox.showwarning(
                    APP_TITLE, "Số lượng video phải là một số nguyên dương (hoặc để trống)."
                )
                return
            max_items = int(qty_raw)

        for vid in list(self.row_widgets.keys()):
            self._destroy_row_widgets(vid)
        self.videos.clear()
        self.order.clear()
        self.checked.clear()
        self.statuses.clear()
        self.logs.clear()
        self.titles.clear()
        self.tree.delete(*self.tree.get_children())

        self.is_busy = True
        self.stop_loading_flag = False
        self.status_var.set(f"Đang phân giải link: {clean}")
        self.download_btn.state(["disabled"])

        threading.Thread(
            target=self._load_worker, args=(clean, max_items), daemon=True
        ).start()

    def _load_worker(self, link: str, max_items: int = 0):
        try:
            kind, ident = resolve_link(link)
        except RuntimeError as exc:
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        if kind == "video":
            self.task_queue.put(
                (
                    "error",
                    "Link này là 1 video đơn lẻ, không phải link kênh. "
                    "Hãy dán link trang cá nhân (dạng douyin.com/user/...).",
                )
            )
            self.task_queue.put(("load_done", None))
            return

        sec_uid = ident
        self.task_queue.put(("status", f"Đang tải danh sách video (sec_uid: {sec_uid[:12]}...)"))

        def progress_cb(count):
            self.task_queue.put(("status", f"Đã tải {count} video..."))

        try:
            items = self.client.fetch_all_user_posts(
                sec_uid,
                stop_flag=lambda: self.stop_loading_flag,
                progress_cb=progress_cb,
                max_items=max_items,
            )
        except DouyinAPIError as exc:
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        self.task_queue.put(("videos_loaded", items))
        self.task_queue.put(("load_done", None))

    # --------------------------------------------------------- Poll queue --
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.task_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    messagebox.showerror(APP_TITLE, payload)
                elif kind == "videos_loaded":
                    self._populate_tree(payload)
                elif kind == "load_done":
                    self.is_busy = False
                    self.download_btn.state(["!disabled"])
                    # Công tắc "Dịch tiêu đề → Tiếng Việt" bật: tự dịch luôn
                    # toàn bộ danh sách vừa lấy (nếu lấy được video nào)
                    if self.auto_translate_var.get() and self.order:
                        self.on_translate_titles(auto=True)
                elif kind == "dl_progress":
                    done, total, label = payload
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.progress_label_var.set(label)
                elif kind == "dl_done":
                    self.is_busy = False
                    self.download_btn.state(["!disabled"])
                    self.progress_label_var.set(payload)
                    self._end_download_session()
                elif kind == "video_status":
                    vid, text = payload
                    self.update_row_status(vid, text)
                elif kind == "title_translated":
                    vid, translated = payload
                    if self.tree.exists(vid):
                        vals = list(self.tree.item(vid, "values"))
                        vals[1] = translated[:80]
                        self.tree.item(vid, values=vals)
                elif kind == "translate_done":
                    self.is_translating = False
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _populate_tree(self, items):
        if not items:
            self.status_var.set(
                "Không lấy được video nào. Kiểm tra lại link hoặc cập nhật Cookie ở "
                "mục Cài đặt."
            )
            return
        for item in items:
            vid = item["id"]
            self.videos[vid] = item
            self.order.append(vid)
            self.checked[vid] = False
            self.titles[vid] = item["desc"]
            self.statuses[vid] = "Chưa tải"
            mins, secs = divmod(item["duration_s"], 60)
            duration_str = f"{mins}:{secs:02d}" if item["duration_s"] else "-"
            post_time_str = format_post_time(item.get("create_time", 0))
            self.tree.insert(
                "",
                "end",
                iid=vid,
                values=(
                    CHECK_OFF,
                    item["desc"][:80],
                    item["url"],
                    duration_str,
                    post_time_str,
                    "Chưa tải",
                    "",  # cột Tải về: để trống, nút thật sẽ đè lên (xem _create_row_widgets)
                    "",  # cột Log
                    "",  # cột Sửa
                    "",  # cột Xóa
                    len(self.order),  # cột STT (đánh số từ 1)
                ),
            )
            self._create_row_widgets(vid)
        self.status_var.set(f"Đã tải xong {len(items)} video.")

    # ----------------------------------------------------------- Checkbox --
    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        # Cột Tải về/Log/Sửa/Xóa (#7-#10) giờ có nút bấm thật đè lên (xem
        # _create_row_widgets), nút tự gọi command riêng nên không cần xử lý
        # ở đây nữa. Chỉ còn cột checkbox (#1) là glyph văn bản thường.
        if col == "#1":
            self.toggle_checked(row)

    # -------------------------------------------------- Nút hành động --
    # Màu chữ cho từng loại link (bình thường / khi bị vô hiệu hóa)
    _LINK_COLORS = {
        "download": ("#3b82f6", "#9ca3af"),  # xanh dương / xám khi đang tải
        "log": ("#9ca3af", "#9ca3af"),        # xám
        "edit": ("#eab308", "#9ca3af"),       # vàng
        "delete": ("#ef4444", "#9ca3af"),     # đỏ
    }

    def _create_row_widgets(self, vid: str):
        """Tạo 4 label dạng LINK (chữ màu, gạch chân, con trỏ tay) đè lên
        hàng `vid` trong Treeview, thay cho nút bấm thật. Dùng Label thay vì
        Button vì Button gốc trên macOS (Aqua) không tô được màu nền/chữ
        tùy ý, còn Label thì hiển thị đúng màu trên mọi hệ điều hành."""

        def make_link(key, text, command):
            color, _ = self._LINK_COLORS[key]
            lbl = tk.Label(
                self.tree, text=text, fg=color,
                bg=self._select_bg if vid in self.tree.selection() else self._row_bg,
                font=self._link_font, cursor="hand2",
            )
            lbl.bind("<Button-1>", lambda e, c=command: c())
            return lbl

        dl_lbl = make_link("download", "Tải", lambda v=vid: self.start_single_download(v))
        log_lbl = make_link("log", "Xem", lambda v=vid: self.show_log_popup(v))
        edit_lbl = make_link("edit", "Sửa", lambda v=vid: self.edit_title(v))
        del_lbl = make_link("delete", "Xóa", lambda v=vid: self.delete_single(v))

        self.row_widgets[vid] = {
            "download": dl_lbl, "log": log_lbl, "edit": edit_lbl, "delete": del_lbl,
        }

    def _set_download_link_enabled(self, vid: str, enabled: bool):
        """Bật/tắt link Tải của 1 hàng (tắt khi video đó đang tải, tránh
        bấm trùng)."""
        widgets = self.row_widgets.get(vid)
        if not widgets:
            return
        lbl = widgets["download"]
        normal_color, disabled_color = self._LINK_COLORS["download"]
        if enabled:
            lbl.config(fg=normal_color, cursor="hand2")
            lbl.bind("<Button-1>", lambda e, v=vid: self.start_single_download(v))
        else:
            lbl.config(fg=disabled_color, cursor="arrow")
            lbl.unbind("<Button-1>")

    def _destroy_row_widgets(self, vid: str):
        widgets = self.row_widgets.pop(vid, None)
        if widgets:
            for w in widgets.values():
                w.destroy()

    def _sync_link_backgrounds(self):
        """Đổi nền các label link theo hàng đang chọn / không chọn cho khớp
        với màu tô của Treeview."""
        selected = set(self.tree.selection())
        for vid, widgets in self.row_widgets.items():
            bg = self._select_bg if vid in selected else self._row_bg
            for lbl in widgets.values():
                if lbl.cget("bg") != bg:
                    lbl.config(bg=bg)

    def _reposition_action_buttons(self):
        """Đặt lại vị trí 4 label hành động của mỗi hàng theo đúng ô (cell)
        hiện tại của Treeview (bbox thay đổi khi cuộn / đổi kích thước cửa
        sổ). Ẩn label nếu hàng đang cuộn ra ngoài vùng nhìn thấy. Gọi lặp
        lại định kỳ để tự cập nhật theo mọi thao tác cuộn/resize."""
        pad = 2
        for vid, widgets in list(self.row_widgets.items()):
            if not self.tree.exists(vid):
                self._destroy_row_widgets(vid)
                continue
            for col, key in (
                ("download", "download"),
                ("log", "log"),
                ("edit", "edit"),
                ("delete", "delete"),
            ):
                lbl = widgets[key]
                bbox = self.tree.bbox(vid, col)
                if not bbox:
                    lbl.place_forget()
                    continue
                x, y, w, h = bbox
                lbl.place(
                    x=x + pad, y=y + (h // 2), anchor="w",
                    width=max(w - 2 * pad, 10),
                )
        self._sync_link_backgrounds()
        self.after(150, self._reposition_action_buttons)

    def toggle_checked(self, vid: str):
        self.checked[vid] = not self.checked.get(vid, False)
        mark = CHECK_ON if self.checked[vid] else CHECK_OFF
        vals = list(self.tree.item(vid, "values"))
        vals[0] = mark
        self.tree.item(vid, values=vals)

    def toggle_select_all(self):
        any_unchecked = any(not self.checked.get(v, False) for v in self.order)
        self.set_all_checked(any_unchecked)

    def set_all_checked(self, state: bool):
        mark = CHECK_ON if state else CHECK_OFF
        for vid in self.order:
            self.checked[vid] = state
            vals = list(self.tree.item(vid, "values"))
            vals[0] = mark
            self.tree.item(vid, values=vals)
        self.tree.heading("chk", text=CHECK_ON if state else CHECK_OFF)

    def get_checked_ids(self) -> list[str]:
        return [v for v in self.order if self.checked.get(v)]

    def update_row_status(self, vid: str, status_text: str):
        self.statuses[vid] = status_text
        if self.tree.exists(vid):
            vals = list(self.tree.item(vid, "values"))
            vals[5] = status_text
            self.tree.item(vid, values=vals)
        is_downloading = status_text.startswith("Đang tải")
        self._set_download_link_enabled(vid, enabled=not is_downloading)

    # ---------------------------------------------------- Phiên tải video --
    def _begin_download_session(self, ids: list[str]):
        """Gọi TRƯỚC khi khởi động 1 thread tải (hàng loạt hoặc lẻ từng
        video). Chỉ clear() cờ dừng nếu đây là phiên tải ĐẦU TIÊN đang
        hoạt động — nếu đã có phiên khác đang tải (hoặc đang dừng dở) thì
        giữ nguyên trạng thái cờ, tránh vô tình "hồi sinh" các tải đang
        được yêu cầu dừng."""
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if self.active_download_sessions == 0:
            self.download_stop_event.clear()
        self.active_download_sessions += 1
        self.stop_download_btn.state(["!disabled"])
        threading.Thread(target=self._download_worker, args=(ids,), daemon=True).start()

    def _end_download_session(self):
        self.active_download_sessions = max(0, self.active_download_sessions - 1)
        if self.active_download_sessions == 0:
            self.stop_download_btn.state(["disabled"])

    def on_stop_download(self):
        """Dừng NGAY mọi video đang tải (kể cả đang tải dở), không đợi
        video hiện tại tải xong rồi mới dừng ở video kế tiếp. Cờ này được
        douyin_client.download_video kiểm tra sau MỖI chunk (256KB) nên
        có hiệu lực gần như tức thời."""
        if self.active_download_sessions == 0:
            return
        self.download_stop_event.set()
        self.status_var.set("Đang dừng tải... (dừng ngay cả video đang tải dở)")
        self.stop_download_btn.state(["disabled"])

    # -------------------------------------------------------- Tải 1 video --
    def start_single_download(self, vid: str):
        if vid in self.downloading_ids:
            return
        if vid not in self.videos:
            return
        self._begin_download_session([vid])

    # ------------------------------------------------------------- Log --
    def show_log_popup(self, vid: str):
        item = self.videos.get(vid)
        if not item:
            return
        log_text = self.logs.get(vid) or "Chưa có log (video chưa được tải lần nào)."
        win = tk.Toplevel(self)
        win.title("Log video")
        win.geometry("520x240")
        win.transient(self)
        ttk.Label(
            win,
            text=self.titles.get(vid, item["desc"]),
            wraplength=480,
            font=("", 10, "bold"),
            justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(win, wrap="word")
        txt.insert("1.0", log_text)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=10, pady=4)
        ttk.Button(win, text="Đóng", command=win.destroy).pack(pady=(0, 8))

    # --------------------------------------------------------- Sửa tiêu đề --
    def edit_title(self, vid: str):
        item = self.videos.get(vid)
        if not item:
            return
        current = self.titles.get(vid, item["desc"])
        win = tk.Toplevel(self)
        win.title("Sửa tiêu đề")
        win.geometry("480x180")
        win.transient(self)
        ttk.Label(
            win, text="Tiêu đề video (dùng để đặt tên file khi tải / khi xuất Excel, TXT):"
        ).pack(anchor="w", padx=10, pady=(10, 4))
        text = tk.Text(win, height=4, wrap="word")
        text.insert("1.0", current)
        text.pack(fill="both", expand=True, padx=10, pady=4)

        def save():
            new_title = text.get("1.0", "end").strip() or current
            self.titles[vid] = new_title
            if self.tree.exists(vid):
                vals = list(self.tree.item(vid, "values"))
                vals[1] = new_title[:80]
                self.tree.item(vid, values=vals)
            win.destroy()

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_row, text="Lưu", command=save).pack(side="right")
        ttk.Button(btn_row, text="Hủy", command=win.destroy).pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------- Dịch --
    def on_translate_titles(self, auto: bool = False):
        """`auto=True`: được gọi tự động sau khi lấy danh sách (công tắc dịch
        đang bật) — dịch TẤT CẢ, không hỏi lại, và nếu thiếu API Key thì chỉ
        báo ở thanh trạng thái chứ không bật hộp thoại làm gián đoạn."""
        if self.is_translating:
            return
        if not self.gemini_api_key and auto:
            self.status_var.set(
                "Chưa có Gemini API Key nên bỏ qua dịch tự động — nhập key ở "
                "\"Cài đặt (Cookie)\"."
            )
            return
        if not self.gemini_api_key:
            messagebox.showwarning(
                APP_TITLE,
                "Chưa có Gemini API Key. Hãy vào mục \"Cài đặt (Cookie)\" để nhập "
                "API Key (lấy miễn phí tại https://aistudio.google.com/apikey).",
            )
            self.open_settings()
            return
        if self.translate_style == TRANSLATE_STYLE_CUSTOM and not self.custom_title_prompt.strip():
            if auto:
                self.status_var.set(
                    "Kiểu dịch \"Tự điền prompt riêng\" nhưng chưa có prompt nên bỏ qua dịch tự động."
                )
            else:
                messagebox.showwarning(
                    APP_TITLE,
                    "Bạn đang chọn kiểu dịch \"Tự điền prompt riêng\" nhưng chưa nhập prompt. "
                    "Hãy soạn prompt trước.",
                )
                self.open_title_prompt_editor()
            return
        ids = list(self.order) if auto else self.get_checked_ids()
        if not ids:
            if not self.order:
                messagebox.showinfo(APP_TITLE, "Danh sách video đang trống.")
                return
            if not messagebox.askyesno(
                APP_TITLE,
                "Chưa tick chọn video nào. Dịch tiêu đề cho TẤT CẢ video trong danh sách?",
            ):
                return
            ids = list(self.order)

        self.is_translating = True
        self.status_var.set(
            f"Đang dịch {len(ids)} tiêu đề sang Tiếng Việt bằng Gemini "
            f"(model: {self.gemini_model}, {self.gemini_batch_workers} luồng song song)..."
        )
        threading.Thread(target=self._translate_worker, args=(ids,), daemon=True).start()

    def _translate_worker(self, ids: list[str]):
        total = len(ids)
        valid_ids: list[str] = []
        original_titles: list[str] = []
        for vid in ids:
            item = self.videos.get(vid)
            if not item:
                continue
            valid_ids.append(vid)
            original_titles.append(self.titles.get(vid, item["desc"]))

        if not valid_ids:
            self.task_queue.put(("status", "Không có tiêu đề nào để dịch."))
            self.task_queue.put(("translate_done", None))
            return

        counters = {"done": 0, "errors": 0}

        def on_item(idx: int, translated: str, err: str | None):
            vid = valid_ids[idx]
            if err:
                self.logs[vid] = f"Lỗi dịch tiêu đề (Gemini): {err}"
                counters["errors"] += 1
            else:
                self.titles[vid] = translated
                self.task_queue.put(("title_translated", (vid, translated)))
                counters["done"] += 1
            self.task_queue.put(
                (
                    "status",
                    f"Đang dịch bằng Gemini ({self.gemini_batch_workers} luồng song song)... "
                    f"{counters['done'] + counters['errors']}/{total}",
                )
            )

        try:
            _, _, fatal_error = translate_batch_with_gemini(
                original_titles,
                self.gemini_api_key,
                target_lang="Tiếng Việt",
                model=self.gemini_model,
                fallback_model=self.gemini_fallback_model,
                max_workers=self.gemini_batch_workers,
                stop_flag=lambda: not self.is_translating,
                on_item=on_item,
                style=self.translate_style,
                custom_prompt=self.custom_title_prompt,
            )
        except RuntimeError as exc:
            # Lỗi xảy ra ngay trước khi kịp chạy lô nào (VD thiếu API Key) ->
            # áp dụng cho toàn bộ danh sách.
            for vid in valid_ids:
                self.logs.setdefault(vid, f"Lỗi dịch tiêu đề (Gemini): {exc}")
            self.task_queue.put(("status", f"Dịch thất bại: {exc}"))
            self.task_queue.put(("translate_done", None))
            return

        summary = (
            f"Đã dịch xong {counters['done']}/{total} tiêu đề bằng Gemini "
            f"(model: {self.gemini_model}, {self.gemini_batch_workers} luồng song song)."
        )
        if counters["errors"]:
            summary += f" ({counters['errors']} lỗi, xem cột Log của video tương ứng)."
        if fatal_error:
            summary += (
                f" DỪNG SỚM do lỗi nghiêm trọng: {fatal_error} — các lô chưa kịp "
                "dịch đã bị bỏ qua, phần đã dịch xong trước đó vẫn được giữ lại."
            )
        self.task_queue.put(("status", summary))
        self.task_queue.put(("translate_done", None))

    # --------------------------------------------------------------- Xóa --
    def _renumber_rows(self):
        """Đánh lại STT (1, 2, 3...) theo thứ tự hiện tại trong danh sách,
        gọi sau khi xóa video để số thứ tự luôn liên tục."""
        for i, vid in enumerate(self.order, start=1):
            if self.tree.exists(vid):
                vals = list(self.tree.item(vid, "values"))
                vals[10] = i
                self.tree.item(vid, values=vals)

    def _remove_ids(self, ids: list[str]):
        for vid in ids:
            self._destroy_row_widgets(vid)
            if self.tree.exists(vid):
                self.tree.delete(vid)
            self.videos.pop(vid, None)
            self.checked.pop(vid, None)
            self.statuses.pop(vid, None)
            self.logs.pop(vid, None)
            self.titles.pop(vid, None)
            if vid in self.order:
                self.order.remove(vid)
        self._renumber_rows()

    def delete_single(self, vid: str):
        if vid not in self.videos:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "Xóa video này khỏi danh sách? "
            "(Không ảnh hưởng video gốc trên Douyin)",
        ):
            return
        self._remove_ids([vid])
        self.status_var.set("Đã xóa 1 video khỏi danh sách.")

    def on_delete_selected(self):
        ids = self.get_checked_ids()
        if not ids:
            messagebox.showinfo(APP_TITLE, "Chưa tick chọn video nào để xóa khỏi danh sách.")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Xóa {len(ids)} video khỏi danh sách hiện tại? "
            "(Chỉ xóa khỏi danh sách trong app, không ảnh hưởng video gốc trên Douyin)",
        ):
            return
        self._remove_ids(ids)
        self.status_var.set(f"Đã xóa {len(ids)} video khỏi danh sách.")

    # ------------------------------------------------------------ Xuất file --
    def export_txt(self):
        if not self.order:
            messagebox.showinfo(APP_TITLE, "Danh sách video đang trống.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt")],
            initialfile="douyin_videos.txt",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for vid in self.order:
                    item = self.videos[vid]
                    title = self.titles.get(vid, item["desc"])
                    f.write(f"{title}\t{item['url']}\n")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không ghi được file: {exc}")
            return
        messagebox.showinfo(APP_TITLE, f"Đã xuất {len(self.order)} video ra:\n{path}")

    def export_excel(self):
        if not self.order:
            messagebox.showinfo(APP_TITLE, "Danh sách video đang trống.")
            return
        try:
            import openpyxl
        except ImportError:
            messagebox.showerror(
                APP_TITLE,
                "Thiếu thư viện 'openpyxl'. Hãy chạy: pip install openpyxl "
                "(hoặc pip3 install openpyxl) rồi thử lại.",
            )
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx")],
            initialfile="douyin_videos.xlsx",
        )
        if not path:
            return
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Video"
        ws.append(["Tiêu đề", "Url", "Thời lượng (giây)", "Trạng thái"])
        for vid in self.order:
            item = self.videos[vid]
            title = self.titles.get(vid, item["desc"])
            ws.append(
                [title, item["url"], item["duration_s"], self.statuses.get(vid, "Chưa tải")]
            )
        try:
            wb.save(path)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không lưu được file: {exc}")
            return
        messagebox.showinfo(APP_TITLE, f"Đã xuất {len(self.order)} video ra:\n{path}")

    # ---------------------------------------------------------- Download --
    def on_download_selected(self):
        if self.is_busy:
            return
        ids = self.get_checked_ids()
        if not ids:
            messagebox.showinfo(APP_TITLE, "Chưa tick chọn video nào để tải.")
            return

        self.is_busy = True
        self.download_btn.state(["disabled"])
        self._begin_download_session(ids)

    def _download_worker(self, ids: list[str]):
        total = len(ids)
        max_workers = max(1, min(self.download_workers, total))
        counters = {"done": 0, "errors": 0, "cancelled": 0}
        lock = threading.Lock()
        # Tên file CHỈ dựa trên tiêu đề (không gắn mã/ID) — tập hợp này
        # "giữ chỗ" các tên đã dùng TRONG ĐỢT TẢI NÀY để nhiều luồng tải
        # song song không bao giờ chọn trùng tên nhau (xem unique_filename).
        claimed_filenames: set[str] = set()

        def download_one(vid: str):
            with self._downloading_lock:
                if vid in self.downloading_ids:
                    self.task_queue.put(
                        (
                            "status",
                            f"Bỏ qua: video {vid} đang được 1 tác vụ tải khác xử lý.",
                        )
                    )
                    return
                self.downloading_ids.add(vid)

            # Nếu đã bấm "⏹ Dừng tải" TRƯỚC KHI video này kịp bắt đầu (còn
            # nằm trong hàng đợi của ThreadPoolExecutor) -> bỏ qua luôn,
            # không tốn thêm 1 kết nối mạng nào nữa.
            if self.download_stop_event.is_set():
                self.task_queue.put(("video_status", (vid, "Đã dừng")))
                self.downloading_ids.discard(vid)
                return
            item = self.videos.get(vid)
            if not item:
                self.downloading_ids.discard(vid)
                return
            title = self.titles.get(vid, item["desc"])
            # Tên file CHỈ lấy từ tiêu đề đã cắt gọn, KHÔNG gắn thêm mã/ID
            # nào. Để 2 video khác nhau nhưng trùng ~60 ký tự đầu tiêu đề
            # (ví dụ cùng series, chỉ khác số tập) không ghi đè lẫn nhau,
            # unique_filename tự thêm " (2)", " (3)"... giống cách trình
            # duyệt tránh đè file — toàn bộ thao tác này khóa bằng `lock`
            # để nhiều luồng tải song song không bao giờ chọn trùng tên.
            with lock:
                fname = unique_filename(
                    self.download_dir, safe_filename(title, max_len=self.filename_max_len),
                    "mp4", claimed_filenames,
                )
            dest = self.download_dir / fname
            self.task_queue.put(("video_status", (vid, "Đang tải...")))

            def chunk_cb(done, tot, f=fname, v=vid):
                kb_done = done // 1024
                kb_total = f"/{tot // 1024} KB" if tot else ""
                self.task_queue.put(("video_status", (v, f"Đang tải {kb_done}KB{kb_total}")))

            try:
                self.client.download_video(
                    item["url"], dest, chunk_cb=chunk_cb,
                    stop_flag=self.download_stop_event.is_set,
                )
            except DownloadCancelled:
                # Dừng NGAY GIỮA CHỪNG (không phải đợi tải xong): file
                # .part dang dở đã được douyin_client tự xóa.
                self.logs[vid] = (
                    f"Đã dừng tải giữa chừng lúc {time.strftime('%H:%M:%S')} "
                    "theo yêu cầu người dùng (phần tải dở đã được xóa)."
                )
                self.task_queue.put(("video_status", (vid, "Đã dừng")))
                with lock:
                    counters["cancelled"] += 1
                    done_count = counters["done"] + counters["errors"] + counters["cancelled"]
                self.task_queue.put(
                    (
                        "dl_progress",
                        (done_count, total, f"[{done_count}/{total}] Đã dừng: {fname}"),
                    )
                )
                self.downloading_ids.discard(vid)
                return
            except (requests.RequestException, OSError) as exc:
                self.logs[vid] = f"Lỗi khi tải lúc {time.strftime('%H:%M:%S')}: {exc}"
                self.task_queue.put(("video_status", (vid, "Lỗi")))
                with lock:
                    counters["errors"] += 1
                    done_count = counters["done"] + counters["errors"] + counters["cancelled"]
                self.task_queue.put(
                    (
                        "dl_progress",
                        (done_count, total, f"[{done_count}/{total}] Lỗi khi tải {fname}: {exc}"),
                    )
                )
                self.downloading_ids.discard(vid)
                return

            self.logs[vid] = f"Đã tải thành công lúc {time.strftime('%H:%M:%S')}: {dest}"
            self.task_queue.put(("video_status", (vid, "Đã tải")))
            with lock:
                counters["done"] += 1
                done_count = counters["done"] + counters["errors"] + counters["cancelled"]
            self.task_queue.put(
                (
                    "dl_progress",
                    (done_count, total, f"[{done_count}/{total}] Đã tải xong: {fname}"),
                )
            )
            self.downloading_ids.discard(vid)

        # Tải nhiều video CÙNG LÚC (tối đa `max_workers` tiến trình song
        # song, cấu hình được trên thanh công cụ chính), thay vì tuần tự
        # từng cái một như trước — mỗi hàng trong bảng vẫn hiện tiến độ
        # (KB đã tải) RIÊNG của video đó, cập nhật độc lập theo thời gian
        # thực dù đang tải nhiều video cùng lúc.
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_one, vid) for vid in ids]
            for future in as_completed(futures):
                future.result()  # để lộ exception nếu có lỗi lập trình ngoài dự kiến

        if self.download_stop_event.is_set():
            summary = (
                f"Đã DỪNG tải theo yêu cầu — hoàn tất {counters['done']}/{total} video "
                f"trước khi dừng ({counters['cancelled']} video bị hủy giữa chừng"
            )
            if counters["errors"]:
                summary += f", {counters['errors']} lỗi"
            summary += ")."
        else:
            summary = f"Hoàn tất: đã tải {counters['done']}/{total} video vào {self.download_dir}"
            if counters["errors"]:
                summary += f" ({counters['errors']} lỗi, xem cột Log của video tương ứng)."
        self.task_queue.put(("dl_done", summary))