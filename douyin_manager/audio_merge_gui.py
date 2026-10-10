"""
audio_merge_gui.py
====================
TAB "Ghép Audio vào Video" (nằm cạnh tab tải video trong cửa sổ chính):
chọn thư mục, chế độ ghép cặp (khớp tên / trộn ngẫu nhiên), tùy chọn kích
thước/tỉ lệ khung hình/chất lượng/hiệu ứng âm thanh/chèn chữ & blur, xem thử
TỪNG dòng (file xem thử chỉ nằm trong thư mục tạm, tự xóa), xóa dòng không
ưng ý, tick chọn dòng cần ghép, chạy ffmpeg (song song, dừng được giữa
chừng), xuất log CSV, tự tránh lặp cặp với các lần chạy trước.

Toàn bộ logic ffmpeg/thuật toán nằm ở `audio_merger.py` (không phụ thuộc
GUI); file này chỉ lo giao diện + điều phối luồng.
"""

from __future__ import annotations

import atexit
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from .audio_merger import (
    MediaPair,
    MergeCancelled,
    MergeError,
    build_ffmpeg_command,
    build_preview_command,
    build_random_mix_pairs,
    cleanup_stale_preview_dirs,
    create_preview_dir,
    ffmpeg_filter_has_option,
    ffmpeg_filter_option_type,
    ffmpeg_has_filter,
    find_ffmpeg,
    find_ffprobe,
    is_blur_layer,
    list_media_files,
    load_pair_history,
    match_video_audio_pairs,
    probe_media_info,
    run_ffmpeg_merge,
    save_pair_history,
    assign_unique_output_paths,
    write_merge_log_csv,
)
from .config import (
    APP_TITLE,
    ASPECT_RATIO_OPTIONS,
    AUDIO_EXTENSIONS,
    DEFAULT_FADE_SECONDS,
    DEFAULT_MERGE_WORKERS,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_BG_MUSIC_VOLUME_PERCENT,
    DEFAULT_PREVIEW_SECONDS,
    FIT_MODE_OPTIONS,
    MAX_MERGE_WORKERS,
    MAX_PREVIEW_SECONDS,
    MERGE_HISTORY_MAX,
    MIN_MERGE_WORKERS,
    MIN_PREVIEW_SECONDS,
    OUTPUT_FORMAT_OPTIONS,
    PAIRING_MODE_FILENAME,
    PAIRING_MODE_OPTIONS,
    PAIRING_MODE_RANDOM,
    QUALITY_OPTIONS,
    RESOLUTION_OPTIONS,
    VIDEO_EXTENSIONS,
    save_config,
)
from . import theme
from .text_editor import TextEditorWindow, layer_summary, new_layer, normalize_layer, resolve_layer_font
from .widgets import (
    AccentButton, ScrollableFrame, SegmentedTabs, WrapFrame,
    bind_wraplength, make_card,
)

def resolve_resolution_label(saved) -> str:
    """Đổi nhãn độ phân giải đã lưu (có thể là nhãn CŨ kiểu \"1080p - Full HD
    (chiều cao 1080px)\") sang nhãn mới tương ứng; không nhận ra thì về mặc định."""
    if saved in RESOLUTION_OPTIONS:
        return saved
    m = re.match(r"\s*(\d{3,4})p", str(saved or ""))
    if m:
        for label, value in RESOLUTION_OPTIONS.items():
            if value == int(m.group(1)):
                return label
    return next(iter(RESOLUTION_OPTIONS))


_OLD_TEXT_POSITIONS = {   # nhãn vị trí của phiên bản cũ -> (cx, cy)
    "Trên - Trái": (0.12, 0.08), "Trên - Giữa": (0.5, 0.08), "Trên - Phải": (0.88, 0.08),
    "Giữa - Trái": (0.12, 0.5), "Chính giữa": (0.5, 0.5), "Giữa - Phải": (0.88, 0.5),
    "Dưới - Trái": (0.12, 0.92), "Dưới - Giữa": (0.5, 0.9), "Dưới - Phải": (0.88, 0.92),
}


def load_text_layers(cfg: dict) -> list[dict]:
    """Nạp danh sách lớp chữ đã lưu. Nếu người dùng đang có cấu hình chèn chữ
    KIỂU CŨ (1 khối chữ duy nhất) thì tự đổi thành 1 lớp chữ để không mất."""
    saved = cfg.get("merge_text_layers")
    if isinstance(saved, list):
        return [normalize_layer(x) for x in saved if isinstance(x, dict)]
    old_text = str(cfg.get("merge_text_content", "") or "").strip()
    if not old_text:
        return []
    cx, cy = _OLD_TEXT_POSITIONS.get(cfg.get("merge_text_position"), (0.5, 0.9))
    return [normalize_layer(dict(
        text=old_text, cx=cx, cy=cy, size_pct=cfg.get("merge_text_size", 5),
        color=cfg.get("merge_text_color", "#FFFFFF"), bold=cfg.get("merge_text_bold", True),
        outline=cfg.get("merge_text_outline", True), box=cfg.get("merge_text_box", False),
        box_opacity=cfg.get("merge_text_box_opacity", 50), start=cfg.get("merge_text_start", 0),
        duration=cfg.get("merge_text_duration", 0), font=cfg.get("merge_text_font", ""),
    ))]


class OverlayConfigError(ValueError):
    """Cấu hình chèn chữ / nhạc nền chưa hợp lệ (thông báo lỗi hiển thị thẳng
    cho người dùng)."""


CHECK_ON = "\u2611"   # ☑
CHECK_OFF = "\u2610"  # ☐


class AudioMergeTab(ttk.Frame):
    """Tab "Ghép Audio vào Video". Được gắn vào ttk.Notebook của cửa sổ chính."""

    def __init__(self, master, cfg: dict):
        super().__init__(master, style="Page.TFrame")
        self.cfg = cfg

        self.video_dir: Path | None = (
            Path(cfg["merge_video_dir"]) if cfg.get("merge_video_dir") else None
        )
        self.audio_dir: Path | None = (
            Path(cfg["merge_audio_dir"]) if cfg.get("merge_audio_dir") else None
        )
        self.output_dir: Path | None = (
            Path(cfg["merge_output_dir"]) if cfg.get("merge_output_dir") else None
        )

        try:
            self.merge_workers = int(cfg.get("merge_workers", DEFAULT_MERGE_WORKERS))
        except (TypeError, ValueError):
            self.merge_workers = DEFAULT_MERGE_WORKERS
        self.merge_workers = max(MIN_MERGE_WORKERS, min(MAX_MERGE_WORKERS, self.merge_workers))

        try:
            self.preview_seconds = int(cfg.get("merge_preview_seconds", DEFAULT_PREVIEW_SECONDS))
        except (TypeError, ValueError):
            self.preview_seconds = DEFAULT_PREVIEW_SECONDS
        self.preview_seconds = max(
            MIN_PREVIEW_SECONDS, min(MAX_PREVIEW_SECONDS, self.preview_seconds)
        )

        self.pairs: list[MediaPair] = []
        self.row_by_stem: dict[str, str] = {}      # key nội bộ của cặp -> iid trong Treeview
        self.pair_by_iid: dict[str, MediaPair] = {}  # iid -> cặp
        self.checked: dict[str, bool] = {}         # key nội bộ -> có tick ghép không
        self.text_checked: dict[str, bool] = {}    # key nội bộ -> có tick chèn chữ cho dòng này không
        self.bg_checked: dict[str, bool] = {}      # key nội bộ -> có tick nhạc nền cho dòng này không
        self.task_queue: queue.Queue = queue.Queue()
        self.is_running = False
        self.is_previewing = False
        self.stop_event = threading.Event()
        self.ffmpeg_path: str | None = None
        self.ffprobe_path: str | None = None
        self._merge_opts: dict = {}
        self._merge_text_overlay = None
        self._merge_bg_music_path = None
        self.text_layers: list[dict] = load_text_layers(cfg)

        # Bản xem thử chỉ nằm trong thư mục TẠM của hệ điều hành (không phải
        # thư mục xuất), tạo lười khi xem thử lần đầu, xóa khi thoát app.
        self.preview_dir: Path | None = None
        self.preview_files: list[Path] = []
        self._preview_counter = 0
        cleanup_stale_preview_dirs()
        atexit.register(self.cleanup_previews)

        self._probe_token = 0          # đánh dấu đợt đọc thời lượng (ffprobe) hiện tại
        self._build_ui()
        self._detect_ffmpeg()
        self.after(100, self._poll_queue)

    # ============================================================== UI ==
    def _build_ui(self):
        PAD = 10

        # ---- PHẦN CỐ ĐỊNH Ở ĐÁY (pack trước để luôn hiện đủ, không bao giờ
        # bị đẩy ra khỏi cửa sổ khi màn hình/cửa sổ nhỏ) ----
        bottom = ttk.Frame(self, style="Page.TFrame", padding=(PAD, 2, PAD, 6))
        bottom.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="Chưa quét thư mục nào.")
        status_lbl = ttk.Label(
            bottom, textvariable=self.status_var, style="PageMuted.TLabel", justify="left"
        )
        status_lbl.pack(anchor="w", pady=(4, 0))
        bind_wraplength(status_lbl, bottom)

        actions_outer = ttk.Frame(self, style="Page.TFrame", padding=(PAD, 4, PAD, 2))
        actions_outer.pack(side="bottom", fill="x")
        actions_outer.columnconfigure(0, weight=1)

        # Bên trái: các nút thao tác danh sách (tự xuống dòng khi cửa sổ hẹp)
        bar = WrapFrame(actions_outer, hgap=6, vgap=6, valign="center", style="Page.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        self.actions_bar = bar
        bar.add(ttk.Button(bar, text="☑  Chọn tất cả", command=lambda: self._set_all_checked(True)))
        bar.add(ttk.Button(bar, text="☐  Bỏ chọn tất cả", command=lambda: self._set_all_checked(False)))
        self.delete_btn = ttk.Button(bar, text="🗑  Xóa các dòng đã chọn", command=self.on_delete_rows)
        bar.add(self.delete_btn)
        self.preview_btn = ttk.Button(
            bar, text=f"▶  Xem thử ({self.preview_seconds} giây)", command=self.on_preview
        )
        bar.add(self.preview_btn)

        # Bên phải: BẮT ĐẦU GHÉP (xanh lá) + DỪNG (đỏ)
        run_box = ttk.Frame(actions_outer, style="Page.TFrame")
        run_box.grid(row=0, column=1, sticky="ne", padx=(12, 0))
        self.start_btn = AccentButton(
            run_box, text="▶  BẮT ĐẦU GHÉP", command=self.on_start,
            bg=theme.GREEN, hover_bg=theme.GREEN_HOVER, padx=20, pady=6,
        )
        self.start_btn.pack(side="left")
        self.stop_btn = AccentButton(
            run_box, text="■  DỪNG", command=self.on_stop,
            bg=theme.RED, hover_bg=theme.RED_HOVER,
            disabled_bg=theme.DISABLED_RED_BG, disabled_fg=theme.DISABLED_RED_FG,
            padx=20, pady=6,
        )
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.stop_btn.state(["disabled"])

        # ---- PHẦN CUỘN ĐƯỢC: các thẻ tùy chọn + danh sách ----
        scroller = ScrollableFrame(self, body_style="Page.TFrame", canvas_bg=theme.BG)
        scroller.pack(side="top", fill="both", expand=True)
        body = scroller.body

        # ============ THẺ 1: Thư mục nguồn & đích + cấu hình ghép cặp ============
        self.video_dir_var = tk.StringVar(value=str(self.video_dir) if self.video_dir else "")
        self.audio_dir_var = tk.StringVar(value=str(self.audio_dir) if self.audio_dir else "")
        self.output_dir_var = tk.StringVar(value=str(self.output_dir) if self.output_dir else "")

        # Khi thu gọn, thanh tiêu đề hiện tóm tắt 3 thư mục (chỉ tên thư mục cuối)
        self.folders_summary_var = tk.StringVar(value="")
        for var in (self.video_dir_var, self.audio_dir_var, self.output_dir_var):
            var.trace_add("write", lambda *_: self._refresh_folders_summary())
        self._refresh_folders_summary()

        folders_outer, folders = make_card(
            body, "Thư mục nguồn & Đích", collapsible=True,
            collapsed=bool(self.cfg.get("merge_card_folders_collapsed", False)),
            summary_var=self.folders_summary_var,
            on_toggle=lambda c: self._remember_card_state("merge_card_folders_collapsed", c),
        )
        folders_outer.pack(fill="x", padx=PAD, pady=(PAD, 5))

        rows = ttk.Frame(folders)
        rows.pack(fill="x")
        rows.columnconfigure(1, weight=1)
        self._folder_row(rows, "Thư mục Video", self.video_dir_var, self._choose_video_dir, 0)
        self._folder_row(rows, "Thư mục Audio", self.audio_dir_var, self._choose_audio_dir, 1)
        self._folder_row(rows, "Thư mục xuất", self.output_dir_var, self._choose_output_dir, 2)

        ttk.Label(folders, text="Cấu hình ghép cặp", style="CardTitle.TLabel").pack(
            anchor="w", pady=(8, 4)
        )
        cfg_area = ttk.Frame(folders)
        cfg_area.pack(fill="x")
        cfg_area.columnconfigure(0, weight=1)
        left = ttk.Frame(cfg_area)
        left.grid(row=0, column=0, sticky="nsew")

        mode_row = ttk.Frame(left)
        mode_row.pack(fill="x", pady=(0, 4))
        self.mode_row = mode_row
        ttk.Label(mode_row, text="Chế độ ghép cặp:").pack(side="left")
        self.pairing_mode_var = tk.StringVar(
            value=self.cfg.get("merge_pairing_mode_label", next(iter(PAIRING_MODE_OPTIONS)))
        )
        pairing_combo = ttk.Combobox(
            mode_row, textvariable=self.pairing_mode_var,
            values=list(PAIRING_MODE_OPTIONS.keys()), state="readonly", width=30,
        )
        pairing_combo.pack(side="left", padx=(10, 0))
        pairing_combo.bind("<<ComboboxSelected>>", lambda e: self._on_pairing_mode_changed())

        # Các tùy chọn của chế độ Random Mix nằm ở HÀNG RIÊNG (tự xuống dòng
        # khi hẹp) — chỉ hiện khi chọn chế độ Random Mix.
        self.random_row = WrapFrame(left, hgap=10, vgap=4, valign="center")
        self.random_count_label = ttk.Label(self.random_row, text="Số video muốn xuất:")
        self.random_count_var = tk.StringVar(value=self.cfg.get("merge_random_count", ""))
        self.random_count_entry = ttk.Entry(self.random_row, textvariable=self.random_count_var, width=10)
        self.shuffle_btn = ttk.Button(self.random_row, text="🔄  Xoay vòng/Xáo lại", command=self.on_scan)
        self.avoid_history_var = tk.BooleanVar(
            value=bool(self.cfg.get("merge_avoid_history", True))
        )
        self.avoid_history_chk = ttk.Checkbutton(
            self.random_row, text="Tránh lặp cặp cũ (kể cả lần chạy trước)",
            variable=self.avoid_history_var,
        )
        for w in (self.random_count_label, self.random_count_entry, self.avoid_history_chk, self.shuffle_btn):
            self.random_row.add(w)

        self._on_pairing_mode_changed(initial=True)

        # Nút chính: nằm góc phải-dưới của khu cấu hình
        self.scan_btn = AccentButton(
            cfg_area, text="⚡  Quét & Ghép cặp Video ↔ Audio", command=self.on_scan,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, padx=18, pady=7,
        )
        self.scan_btn.grid(row=0, column=1, sticky="se", padx=(16, 0))


        # ============ THẺ 2: Các nhóm tùy chọn (tab viên thuốc) ============
        opts_outer, opts = make_card(
            body, "Tùy chọn ghép", collapsible=True,
            collapsed=bool(self.cfg.get("merge_card_options_collapsed", False)),
            on_toggle=lambda c: self._remember_card_state("merge_card_options_collapsed", c),
        )
        opts_outer.pack(fill="x", padx=PAD, pady=5)
        self.option_tabs = SegmentedTabs(opts, variant="sub")
        self.option_tabs.pack(fill="x")

        tab_size = ttk.Frame(self.option_tabs, padding=(6, 0))
        tab_audio = ttk.Frame(self.option_tabs, padding=(6, 0))
        tab_bg_music = ttk.Frame(self.option_tabs, padding=(6, 0))
        tab_text = ttk.Frame(self.option_tabs, padding=(6, 0))
        tab_advanced = ttk.Frame(self.option_tabs, padding=(6, 0))
        self.option_tabs.add(tab_size, "Kích thước & Tỉ lệ")
        self.option_tabs.add(tab_audio, "Âm thanh nâng cao")
        self.option_tabs.add(tab_bg_music, "Nhạc nền")
        self.option_tabs.add(tab_text, "Chèn chữ & Blur")
        self.option_tabs.add(tab_advanced, "Nâng cao")

        self._build_tab_size(tab_size)
        self._build_tab_audio(tab_audio)
        self._build_tab_bg_music(tab_bg_music)
        self._build_tab_text(tab_text)
        self._build_tab_advanced(tab_advanced)

        # ============ THẺ 3: Danh sách ghép ============
        list_outer, list_card = make_card(body)
        list_outer.pack(fill="both", expand=True, padx=PAD, pady=(5, PAD))

        head = ttk.Frame(list_card)
        head.pack(fill="x", pady=(0, 2))
        ttk.Label(head, text="Danh sách ghép", style="CardTitle.TLabel").pack(side="left")
        self.list_count_var = tk.StringVar(value="(0)")
        ttk.Label(head, textvariable=self.list_count_var, style="Muted.TLabel").pack(
            side="left", padx=(6, 0)
        )


        tree_box = ttk.Frame(list_card)
        tree_box.pack(fill="both", expand=True)

        columns = ("chk", "text_chk", "video", "audio", "status", "stt", "bg_chk", "duration")
        # Thứ tự cột DỮ LIỆU giữ nguyên như bản cũ (chỉ thêm "duration" ở cuối) để
        # các hàm đọc/ghi values[i] không bị lệch; thứ tự HIỂN THỊ theo thiết kế mới.
        self._display_cols = ("chk", "stt", "video", "audio", "duration", "status", "text_chk", "bg_chk")
        self.tree = ttk.Treeview(
            tree_box, columns=columns, show="headings", height=9, selectmode="extended",
            displaycolumns=self._display_cols,
        )
        self.tree.heading("chk", text=CHECK_OFF, command=self._toggle_all_checked)
        self.tree.heading("stt", text="STT")
        self.tree.heading("video", text="Tên Video", anchor="w")
        self.tree.heading("audio", text="Tên Audio", anchor="w")
        self.tree.heading("duration", text="Thời lượng")
        self.tree.heading("status", text="Trạng thái")
        self.tree.heading("text_chk", text=f"{CHECK_OFF} Chữ", command=self._toggle_all_text_checked)
        self.tree.heading("bg_chk", text=f"{CHECK_OFF} Nhạc", command=self._toggle_all_bg_checked)
        self.tree.column("chk", width=40, minwidth=40, anchor="center", stretch=False)
        self.tree.column("stt", width=56, minwidth=44, anchor="center", stretch=False)
        self.tree.column("video", width=280, minwidth=120, anchor="w")
        self.tree.column("audio", width=280, minwidth=120, anchor="w")
        self.tree.column("duration", width=96, minwidth=80, anchor="center", stretch=False)
        self.tree.column("status", width=160, minwidth=110, anchor="center", stretch=False)
        self.tree.column("text_chk", width=66, minwidth=60, anchor="center", stretch=False)
        self.tree.column("bg_chk", width=70, minwidth=64, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        # Chữ gợi ý hiện ở giữa bảng khi danh sách còn trống
        self.empty_lbl = ttk.Label(
            tree_box,
            text="✦\nChưa có cặp nào. Chọn thư mục rồi bấm \"Quét & Ghép cặp\".",
            style="Empty.TLabel", justify="center", font=("", 11),
        )

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Delete>", lambda e: self.on_delete_rows())
        self._refresh_list_state()

    def _build_tab_size(self, parent):
        # Bố cục dạng lưới 2 cặp (nhãn + ô chọn) mỗi hàng: gọn, không tràn
        # ngang ở cửa sổ nhỏ, ô chọn tự giãn theo bề rộng.
        parent.columnconfigure(1, weight=1, uniform="opt")
        parent.columnconfigure(3, weight=1, uniform="opt")

        def lbl(text, row, col):
            ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)

        lbl("Chất lượng video xuất:", 0, 0)
        self.resolution_var = tk.StringVar(
            value=resolve_resolution_label(self.cfg.get("merge_resolution"))
        )
        ttk.Combobox(
            parent, textvariable=self.resolution_var, values=list(RESOLUTION_OPTIONS.keys()),
            state="readonly", width=24,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=2)

        lbl("Mức nén (dung lượng file):", 0, 2)
        self.quality_var = tk.StringVar(
            value=self.cfg.get("merge_quality", next(iter(QUALITY_OPTIONS)))
        )
        ttk.Combobox(
            parent, textvariable=self.quality_var, values=list(QUALITY_OPTIONS.keys()),
            state="readonly", width=24,
        ).grid(row=0, column=3, sticky="ew", pady=2)

        lbl("Loại file xuất:", 1, 0)
        self.format_var = tk.StringVar(value=self.cfg.get("merge_format", DEFAULT_OUTPUT_FORMAT))
        ttk.Combobox(
            parent, textvariable=self.format_var, values=OUTPUT_FORMAT_OPTIONS,
            state="readonly", width=8,
        ).grid(row=1, column=1, sticky="w", padx=(0, 16), pady=2)

        lbl("Số tiến trình ghép song song:", 1, 2)
        self.workers_var = tk.IntVar(value=self.merge_workers)
        workers_spin = ttk.Spinbox(
            parent, from_=MIN_MERGE_WORKERS, to=MAX_MERGE_WORKERS,
            textvariable=self.workers_var, width=4, command=self._on_workers_changed,
        )
        workers_spin.grid(row=1, column=3, sticky="w", pady=2)
        workers_spin.bind("<FocusOut>", lambda e: self._on_workers_changed())
        workers_spin.bind("<Return>", lambda e: self._on_workers_changed())

        lbl("Tỉ lệ khung hình xuất:", 2, 0)
        self.aspect_var = tk.StringVar(
            value=self.cfg.get("merge_aspect", next(iter(ASPECT_RATIO_OPTIONS)))
        )
        ttk.Combobox(
            parent, textvariable=self.aspect_var, values=list(ASPECT_RATIO_OPTIONS.keys()),
            state="readonly", width=22,
        ).grid(row=2, column=1, sticky="ew", padx=(0, 16), pady=2)

        lbl("Tùy chỉnh (W : H):", 2, 2)
        custom = ttk.Frame(parent)
        custom.grid(row=2, column=3, sticky="w", pady=2)
        self.custom_w_var = tk.IntVar(value=int(self.cfg.get("merge_custom_w", 9)))
        ttk.Spinbox(custom, from_=1, to=100, textvariable=self.custom_w_var, width=4).pack(side="left")
        ttk.Label(custom, text=" : ").pack(side="left")
        self.custom_h_var = tk.IntVar(value=int(self.cfg.get("merge_custom_h", 16)))
        ttk.Spinbox(custom, from_=1, to=100, textvariable=self.custom_h_var, width=4).pack(side="left")
        ttk.Label(custom, text='(chỉ dùng khi chọn "Tùy chỉnh...")', style="Muted.TLabel").pack(
            side="left", padx=(8, 0)
        )

        lbl("Cách xử lý phần dư (khi tỉ lệ khác gốc):", 3, 0)
        self.fit_mode_var = tk.StringVar(
            value=self.cfg.get("merge_fit_mode", next(iter(FIT_MODE_OPTIONS)))
        )
        ttk.Combobox(
            parent, textvariable=self.fit_mode_var, values=list(FIT_MODE_OPTIONS.keys()),
            state="readonly", width=48,
        ).grid(row=3, column=1, columnspan=3, sticky="ew", pady=2)

        self.no_upscale_var = tk.BooleanVar(value=bool(self.cfg.get("merge_no_upscale", False)))
        ttk.Checkbutton(
            parent, text="Không phóng to quá độ phân giải của video gốc (tránh file nặng mà không nét hơn)",
            variable=self.no_upscale_var,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 0))

        ttk.Separator(parent, orient="horizontal").grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=(6, 4)
        )

        lbl("✂ Cắt bớt đầu video (giây):", 6, 0)
        self.trim_start_var = tk.DoubleVar(value=float(self.cfg.get("merge_trim_start", 0)))
        trim_start_spin = ttk.Spinbox(
            parent, from_=0, to=3600, increment=0.5, textvariable=self.trim_start_var, width=8,
        )
        trim_start_spin.grid(row=6, column=1, sticky="w", padx=(0, 16), pady=2)

        lbl("✂ Cắt bớt cuối video (giây):", 6, 2)
        self.trim_end_var = tk.DoubleVar(value=float(self.cfg.get("merge_trim_end", 0)))
        trim_end_spin = ttk.Spinbox(
            parent, from_=0, to=3600, increment=0.5, textvariable=self.trim_end_var, width=8,
        )
        trim_end_spin.grid(row=6, column=3, sticky="w", pady=2)



    def _build_tab_audio(self, parent):
        row1 = ttk.Frame(parent)
        row1.pack(fill="x")
        ttk.Label(row1, text="Giữ % audio gốc của video (trộn cùng audio mới):").pack(side="left")
        self.mix_percent_var = tk.IntVar(value=int(self.cfg.get("merge_audio_mix_percent", 0)))
        ttk.Spinbox(
            row1, from_=0, to=100, textvariable=self.mix_percent_var, width=5
        ).pack(side="left", padx=(6, 4))
        ttk.Label(row1, text="% (0 = xóa hẳn audio gốc, mặc định)").pack(side="left")

        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(6, 0))
        self.loop_audio_var = tk.BooleanVar(value=bool(self.cfg.get("merge_loop_audio", False)))
        ttk.Checkbutton(
            row2, text="Lặp lại audio nếu ngắn hơn video (thay vì để im lặng phần thiếu)",
            variable=self.loop_audio_var,
        ).pack(anchor="w")

        row3 = ttk.Frame(parent)
        row3.pack(fill="x", pady=(6, 0))
        self.normalize_var = tk.BooleanVar(value=bool(self.cfg.get("merge_normalize", False)))
        ttk.Checkbutton(
            row3,
            text="Chuẩn hóa âm lượng (loudness normalize) — audio các nguồn khác nhau sẽ đồng đều hơn",
            variable=self.normalize_var,
        ).pack(anchor="w")

        row4 = ttk.Frame(parent)
        row4.pack(fill="x", pady=(6, 0))
        self.fade_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("merge_fade_enabled", False)))
        ttk.Checkbutton(
            row4, text="Fade in/out audio ở đầu/cuối, mỗi bên:", variable=self.fade_enabled_var,
        ).pack(side="left")
        self.fade_seconds_var = tk.DoubleVar(
            value=float(self.cfg.get("merge_fade_seconds", DEFAULT_FADE_SECONDS))
        )
        ttk.Spinbox(
            row4, from_=0.5, to=10, increment=0.5, textvariable=self.fade_seconds_var, width=5
        ).pack(side="left", padx=(6, 4))
        ttk.Label(row4, text="giây").pack(side="left")

    def _build_tab_bg_music(self, parent):
        self.bg_music_enabled_var = tk.BooleanVar(
            value=bool(self.cfg.get("merge_bg_music_enabled", False))
        )
        ttk.Checkbutton(
            parent, text="Thêm nhạc nền (trộn thêm, không thay thế audio chính)",
            variable=self.bg_music_enabled_var, command=self._on_bg_music_toggle,
        ).pack(anchor="w")

        file_row = ttk.Frame(parent)
        file_row.pack(fill="x", pady=(8, 0))
        ttk.Label(file_row, text="File nhạc nền:").pack(side="left")
        self.bg_music_path_var = tk.StringVar(value=self.cfg.get("merge_bg_music_path", ""))
        ttk.Entry(file_row, textvariable=self.bg_music_path_var, width=40, state="readonly").pack(
            side="left", padx=(10, 6)
        )
        ttk.Button(file_row, text="Chọn file nhạc...", command=self._choose_bg_music).pack(side="left")

        grid = ttk.Frame(parent)
        grid.pack(fill="x", pady=(6, 0))
        ttk.Label(grid, text="Âm lượng nhạc nền:").grid(row=0, column=0, sticky="w", pady=4)
        self.bg_music_volume_var = tk.IntVar(
            value=int(self.cfg.get("merge_bg_music_volume", DEFAULT_BG_MUSIC_VOLUME_PERCENT))
        )
        ttk.Spinbox(
            grid, from_=0, to=100, textvariable=self.bg_music_volume_var, width=5,
        ).grid(row=0, column=1, sticky="w", padx=(6, 4))
        ttk.Label(grid, text="%").grid(row=0, column=2, sticky="w")

        opts_row = WrapFrame(parent, hgap=14, vgap=4)
        opts_row.pack(fill="x", pady=(8, 0))
        self.bg_music_loop_var = tk.BooleanVar(
            value=bool(self.cfg.get("merge_bg_music_loop", True))
        )
        opts_row.add(ttk.Checkbutton(
            opts_row, text="Lặp lại nhạc nền nếu ngắn hơn video", variable=self.bg_music_loop_var,
        ))
        self.bg_music_ducking_var = tk.BooleanVar(
            value=bool(self.cfg.get("merge_bg_music_ducking", True))
        )
        opts_row.add(ttk.Checkbutton(
            opts_row, text="Tự động giảm nhạc nền khi audio chính đang có tiếng (ducking)",
            variable=self.bg_music_ducking_var,
        ))


    def _on_bg_music_toggle(self):
        if self.bg_music_enabled_var.get() and not self.bg_music_path_var.get().strip():
            self._choose_bg_music()
            if not self.bg_music_path_var.get().strip():
                self.bg_music_enabled_var.set(False)

    def _choose_bg_music(self):
        chosen = filedialog.askopenfilename(
            title="Chọn file nhạc nền",
            filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("Tất cả", "*.*")],
            parent=self,
        )
        if chosen:
            self.bg_music_path_var.set(chosen)
            self.cfg["merge_bg_music_path"] = chosen
            save_config(self.cfg)

    def _build_tab_text(self, parent):
        self.text_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("merge_text_enabled", False)))
        ttk.Checkbutton(
            parent, text="Chèn chữ / blur lên video (áp dụng cho mọi video được ghép & cả bản xem thử)",
            variable=self.text_enabled_var, command=self._on_text_toggle,
        ).pack(anchor="w")

        bar = WrapFrame(parent, hgap=8, vgap=6)
        bar.pack(fill="x", pady=(8, 4))
        bar.add(ttk.Button(bar, text="🎨 Mở trình chỉnh sửa chữ & blur (kéo thả như CapCut)",
                           command=self._open_text_editor))
        bar.add(ttk.Button(bar, text="🗑 Xóa hết chữ & blur", command=self._clear_text_layers))

        self.text_summary_var = tk.StringVar()
        summary = ttk.Label(parent, textvariable=self.text_summary_var, justify="left")
        summary.pack(anchor="w", fill="x", pady=(2, 6))
        bind_wraplength(summary, parent, margin=16)
        self._refresh_text_summary()


    def _refresh_text_summary(self):
        if not self.text_layers:
            self.text_summary_var.set("Chưa có chữ/blur nào. Bấm \"Mở trình chỉnh sửa chữ & blur\" để thêm.")
            return
        lines = [f"Có {len(self.text_layers)} lớp (chữ / blur):"]
        lines += ["   " + layer_summary(i, layer) for i, layer in enumerate(self.text_layers[:8])]
        if len(self.text_layers) > 8:
            lines.append(f"   ... và {len(self.text_layers) - 8} lớp nữa")
        self.text_summary_var.set("\n".join(lines))

    def _on_text_toggle(self):
        if self.text_enabled_var.get() and not self.text_layers:
            self._open_text_editor()
            if not self.text_layers:
                self.text_enabled_var.set(False)

    def _clear_text_layers(self):
        if not self.text_layers:
            return
        if messagebox.askyesno(APP_TITLE, "Xóa toàn bộ các lớp chữ & blur đã tạo?", parent=self):
            self.text_layers = []
            self.text_enabled_var.set(False)
            self._refresh_text_summary()
            self._save_text_options_to_cfg()
            save_config(self.cfg)

    def _editor_reference_videos(self) -> list[Path]:
        """Danh sách video làm nền xem trước trong trình chỉnh sửa: các dòng
        đang chọn trước, rồi các dòng còn lại của danh sách ghép."""
        seen: set[Path] = set()
        videos: list[Path] = []

        def add(p):
            if p is not None and p not in seen and len(videos) < 40:
                seen.add(p)
                videos.append(p)

        for iid in self.tree.selection():
            pair = self.pair_by_iid.get(iid)
            if pair:
                add(pair.video)
        for pair in self.pairs:
            add(pair.video)
        if not videos and self.video_dir and self.video_dir.is_dir():
            for p in list_media_files(self.video_dir, VIDEO_EXTENSIONS)[:40]:
                add(p)
        return videos

    def _open_text_editor(self):
        if self.is_running:
            messagebox.showinfo(APP_TITLE, "Đang ghép — hãy đợi xong hoặc bấm Dừng rồi mới chỉnh chữ.")
            return
        self._detect_ffmpeg()
        aspect_value = ASPECT_RATIO_OPTIONS.get(self.aspect_var.get())
        if aspect_value == "custom":
            try:
                aspect_value = (max(1, int(self.custom_w_var.get())), max(1, int(self.custom_h_var.get())))
            except (tk.TclError, ValueError):
                aspect_value = None
        try:
            TextEditorWindow(
                self,
                self.text_layers,
                videos=self._editor_reference_videos(),
                ffmpeg_path=self.ffmpeg_path,
                ffprobe_path=self.ffprobe_path,
                aspect_ratio=aspect_value,
                fit_mode=FIT_MODE_OPTIONS.get(self.fit_mode_var.get(), "crop"),
                tmp_dir_fn=self._ensure_temp_dir,
                on_apply=self._on_text_layers_applied,
            )
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không mở được trình chỉnh sửa chữ: {exc}")

    def _on_text_layers_applied(self, layers: list[dict]):
        self.text_layers = [normalize_layer(x) for x in layers]
        if self.text_layers:
            self.text_enabled_var.set(True)
        self._refresh_text_summary()
        self._save_text_options_to_cfg()
        save_config(self.cfg)

    def _build_tab_advanced(self, parent):
        ffmpeg_row = ttk.Frame(parent)
        ffmpeg_row.pack(fill="x")
        ttk.Label(ffmpeg_row, text="ffmpeg:").pack(side="left")
        ttk.Button(ffmpeg_row, text="Chọn ffmpeg thủ công...", command=self._choose_ffmpeg).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            ffmpeg_row, text="Tải ffmpeg (mở trang tải)",
            command=lambda: webbrowser.open("https://www.gyan.dev/ffmpeg/builds/"),
        ).pack(side="left", padx=(8, 0))
        # Dòng trạng thái ffmpeg (có thể rất dài) nằm RIÊNG, tự xuống dòng
        # -> các nút bên trên không bao giờ bị đẩy ra ngoài cửa sổ.
        self.ffmpeg_status_var = tk.StringVar(value="Đang kiểm tra...")
        ffmpeg_status = ttk.Label(
            parent, textvariable=self.ffmpeg_status_var, style="Muted.TLabel",
            wraplength=800, justify="left",
        )
        ffmpeg_status.pack(anchor="w", fill="x", pady=(4, 0))
        bind_wraplength(ffmpeg_status, parent, margin=16)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=10)

        log_row = ttk.Frame(parent)
        log_row.pack(fill="x")
        self.export_csv_var = tk.BooleanVar(value=bool(self.cfg.get("merge_export_csv", True)))
        ttk.Checkbutton(
            log_row, text="Xuất file log CSV sau mỗi lần ghép (video, audio, file ra, trạng thái)",
            variable=self.export_csv_var,
        ).pack(anchor="w")

        history_row = ttk.Frame(parent)
        history_row.pack(fill="x", pady=(6, 0))
        ttk.Button(history_row, text="🗑 Xóa lịch sử cặp đã dùng", command=self._clear_history).pack(
            side="left"
        )
        n_hist = len(load_pair_history(self.cfg))
        self.history_count_var = tk.StringVar(value=f"Hiện đang lưu {n_hist} cặp trong lịch sử.")
        ttk.Label(history_row, textvariable=self.history_count_var, style="Muted.TLabel").pack(
            side="left", padx=(10, 0)
        )

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=10)

        preview_row = ttk.Frame(parent)
        preview_row.pack(fill="x")
        ttk.Label(preview_row, text="Độ dài bản xem thử:").pack(side="left")
        self.preview_seconds_var = tk.IntVar(value=self.preview_seconds)
        sec_spin = ttk.Spinbox(
            preview_row, from_=MIN_PREVIEW_SECONDS, to=MAX_PREVIEW_SECONDS,
            textvariable=self.preview_seconds_var, width=5,
            command=self._on_preview_seconds_changed,
        )
        sec_spin.pack(side="left", padx=(8, 4))
        sec_spin.bind("<FocusOut>", lambda e: self._on_preview_seconds_changed())
        sec_spin.bind("<Return>", lambda e: self._on_preview_seconds_changed())
        ttk.Label(preview_row, text="giây (dùng cho nút \"Xem thử\" ở thanh dưới cùng)", style="Muted.TLabel").pack(
            side="left", padx=(2, 0)
        )

    def _remember_card_state(self, key: str, collapsed: bool):
        self.cfg[key] = bool(collapsed)
        save_config(self.cfg)

    def _refresh_folders_summary(self):
        def short(v):
            val = v.get().strip()
            return (Path(val).name or val) if val else "—"
        self.folders_summary_var.set(
            f"Video: {short(self.video_dir_var)}  ·  Audio: {short(self.audio_dir_var)}"
            f"  ·  Xuất: {short(self.output_dir_var)}"
        )

    def _folder_row(self, parent, label, var, command, row):
        """1 hàng chọn thư mục: biểu tượng + nhãn | ô đường dẫn (chỉ đọc) | nút Chọn."""
        ttk.Label(parent, text=f"📁  {label}").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
        ttk.Entry(parent, textvariable=var, state="readonly").grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Button(parent, text="📂  Chọn...", command=command).grid(row=row, column=2, padx=(8, 0), pady=2)

    # ------------------------------------------------------------ Sự kiện UI --
    def _on_pairing_mode_changed(self, initial: bool = False):
        is_random = PAIRING_MODE_OPTIONS.get(self.pairing_mode_var.get()) == PAIRING_MODE_RANDOM
        if is_random:
            if not self.random_row.winfo_manager():
                self.random_row.pack(fill="x", pady=(2, 2), after=self.mode_row)
        else:
            self.random_row.pack_forget()
        if not initial:
            self.cfg["merge_pairing_mode_label"] = self.pairing_mode_var.get()
            save_config(self.cfg)

    def _on_preview_seconds_changed(self):
        try:
            val = int(self.preview_seconds_var.get())
        except (tk.TclError, ValueError):
            val = DEFAULT_PREVIEW_SECONDS
        val = max(MIN_PREVIEW_SECONDS, min(MAX_PREVIEW_SECONDS, val))
        self.preview_seconds = val
        self.preview_seconds_var.set(val)
        self.cfg["merge_preview_seconds"] = val
        save_config(self.cfg)
        try:   # nhãn nút "Xem thử (N giây)" luôn khớp số giây đang đặt
            self.preview_btn.configure(text=f"▶  Xem thử ({val} giây)")
            self.actions_bar.reflow()
        except (tk.TclError, AttributeError):
            pass

    def _on_workers_changed(self):
        try:
            val = int(self.workers_var.get())
        except (tk.TclError, ValueError):
            val = DEFAULT_MERGE_WORKERS
        val = max(MIN_MERGE_WORKERS, min(MAX_MERGE_WORKERS, val))
        self.merge_workers = val
        self.workers_var.set(val)
        self.cfg["merge_workers"] = val
        save_config(self.cfg)

    def _choose_video_dir(self):
        chosen = filedialog.askdirectory(
            initialdir=str(self.video_dir) if self.video_dir else str(Path.home()), parent=self,
        )
        if chosen:
            self.video_dir = Path(chosen)
            self.video_dir_var.set(chosen)
            self.cfg["merge_video_dir"] = chosen
            save_config(self.cfg)

    def _choose_audio_dir(self):
        chosen = filedialog.askdirectory(
            initialdir=str(self.audio_dir) if self.audio_dir else str(Path.home()), parent=self,
        )
        if chosen:
            self.set_audio_dir(Path(chosen))

    def set_audio_dir(self, path: Path):
        """Đặt thư mục Audio (cũng được tab "Kịch bản & Giọng đọc" gọi để dùng chung)."""
        self.audio_dir = Path(path)
        self.audio_dir_var.set(str(path))
        self.cfg["merge_audio_dir"] = str(path)
        save_config(self.cfg)

    def _choose_output_dir(self):
        chosen = filedialog.askdirectory(
            initialdir=str(self.output_dir) if self.output_dir else str(Path.home()), parent=self,
        )
        if chosen:
            self.output_dir = Path(chosen)
            self.output_dir_var.set(chosen)
            self.cfg["merge_output_dir"] = chosen
            save_config(self.cfg)

    def _choose_ffmpeg(self):
        chosen = filedialog.askopenfilename(title="Chọn file ffmpeg.exe (hoặc ffmpeg)", parent=self)
        if chosen:
            self.cfg["merge_ffmpeg_path"] = chosen
            save_config(self.cfg)
            self._detect_ffmpeg()

    def _detect_ffmpeg(self):
        self.ffmpeg_path = find_ffmpeg(self.cfg.get("merge_ffmpeg_path", ""))
        self.ffprobe_path = find_ffprobe(self.ffmpeg_path, self.cfg.get("merge_ffprobe_path", ""))
        if self.ffmpeg_path and self.ffprobe_path:
            self.ffmpeg_status_var.set(f"✓ Đã tìm thấy: {self.ffmpeg_path}")
        elif self.ffmpeg_path and not self.ffprobe_path:
            self.ffmpeg_status_var.set(
                "⚠ Tìm thấy ffmpeg nhưng THIẾU ffprobe (phải đi kèm cùng thư mục) — "
                "tải lại bản đầy đủ (\"full\") của ffmpeg."
            )
        else:
            self.ffmpeg_status_var.set(
                "⚠ Chưa tìm thấy ffmpeg — bấm \"Tải ffmpeg\" để cài, hoặc \"Chọn "
                "ffmpeg thủ công\" nếu đã tải sẵn."
            )

    def _clear_history(self):
        self.cfg["merge_used_pairs_history"] = []
        save_config(self.cfg)
        self.history_count_var.set("Hiện đang lưu 0 cặp trong lịch sử.")

    # ------------------------------------------------------------- Quét --
    def on_scan(self):
        if self.is_running:
            return
        if not self.video_dir or not self.audio_dir:
            messagebox.showwarning(APP_TITLE, "Hãy chọn Thư mục Video và Thư mục Audio trước.")
            return

        videos = list_media_files(self.video_dir, VIDEO_EXTENSIONS)
        audios = list_media_files(self.audio_dir, AUDIO_EXTENSIONS)

        mode = PAIRING_MODE_OPTIONS.get(self.pairing_mode_var.get(), PAIRING_MODE_FILENAME)
        if mode == PAIRING_MODE_RANDOM:
            count_str = self.random_count_var.get().strip()
            try:
                count = int(count_str) if count_str else None
            except ValueError:
                messagebox.showwarning(APP_TITLE, "Số lượng video muốn xuất phải là số nguyên.")
                return
            history = load_pair_history(self.cfg) if self.avoid_history_var.get() else set()
            self.pairs = build_random_mix_pairs(videos, audios, target_count=count, used_history=history)
            self.cfg["merge_random_count"] = count_str
            self.cfg["merge_avoid_history"] = self.avoid_history_var.get()
            save_config(self.cfg)
        else:
            self.pairs = match_video_audio_pairs(videos, audios)

        self.tree.delete(*self.tree.get_children())
        self.row_by_stem.clear()
        self.pair_by_iid.clear()
        self.checked.clear()
        self.text_checked.clear()
        self.bg_checked.clear()
        for i, pair in enumerate(self.pairs):
            # Random Mix có thể có nhiều dòng trùng stem (cùng video ghép
            # nhiều audio khác nhau qua nhiều vòng) -> dùng key riêng theo
            # thứ tự để cập nhật status không bị đè nhau.
            key = f"{pair.stem}#{i}"
            pair.stem = key  # dùng key duy nhất làm định danh nội bộ từ đây
            # Mặc định TICK sẵn mọi dòng đã đủ video + audio — cả ghép lẫn
            # chèn chữ lẫn nhạc nền (chèn chữ/nhạc nền chỉ thực sự áp dụng
            # khi bật ở tab tương ứng).
            self.checked[key] = pair.is_ready
            self.text_checked[key] = True
            self.bg_checked[key] = True
            item_id = self.tree.insert(
                "", "end",
                values=(
                    CHECK_ON if pair.is_ready else "",
                    CHECK_ON if pair.is_ready else "",
                    pair.video.name if pair.video else "—",
                    pair.audio.name if pair.audio else "—",
                    pair.status,
                    i + 1,  # STT
                    CHECK_ON if pair.is_ready else "",  # bg_chk
                    "…" if pair.video else "—",  # duration (điền sau bằng ffprobe)
                ),
            )
            self.row_by_stem[key] = item_id
            self.pair_by_iid[item_id] = pair

        self._refresh_all_check_heading()
        self._refresh_all_text_check_heading()
        self._refresh_all_bg_check_heading()
        self._refresh_list_state()
        self._start_duration_probe()
        if not self.pairs:
            self.status_var.set("Không tìm thấy video/audio nào trong 2 thư mục đã chọn.")
        else:
            self._update_list_summary(prefix="Đã quét xong. ")

    def _renumber_rows(self):
        """Đánh lại STT (1, 2, 3...) theo thứ tự dòng hiện tại sau khi xóa."""
        for n, iid in enumerate(self.tree.get_children(), start=1):
            values = list(self.tree.item(iid, "values"))
            values[5] = n
            self.tree.item(iid, values=values)

    def _update_row_status(self, key: str, status: str):
        item_id = self.row_by_stem.get(key)
        if not item_id or not self.tree.exists(item_id):
            return
        values = list(self.tree.item(item_id, "values"))
        values[4] = status
        self.tree.item(item_id, values=values)

    # ------------------------------------------- Danh sách: tick / xóa dòng --
    def _refresh_list_state(self):
        """Cập nhật số dòng ở tiêu đề "Danh sách ghép (N)" và chữ gợi ý khi bảng trống."""
        if not hasattr(self, "empty_lbl"):
            return
        n = len(self.pairs)
        self.list_count_var.set(f"({n})")
        if n:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(in_=self.tree, relx=0.5, rely=0.5, anchor="center")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = int(round(max(0.0, seconds)))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

    def _start_duration_probe(self):
        """Đọc thời lượng từng VIDEO bằng ffprobe ở luồng nền rồi điền vào cột
        "Thời lượng" (không làm đơ giao diện; quét lại thì đợt cũ tự hủy)."""
        self._probe_token += 1
        token = self._probe_token
        if not self.ffprobe_path:
            for iid in self.tree.get_children():
                self._set_row_duration(iid, "—")
            return
        items = [(p.stem, p.video) for p in self.pairs if p.video]
        ffprobe = self.ffprobe_path

        def worker():
            cache: dict = {}   # Random Mix có thể lặp lại cùng 1 video ở nhiều dòng
            for key, video in items:
                if token != self._probe_token:
                    return
                if video not in cache:
                    try:
                        cache[video] = self._format_duration(probe_media_info(ffprobe, video).duration)
                    except Exception:  # noqa: BLE001 - file hỏng thì chỉ hiện "—"
                        cache[video] = "—"
                self.task_queue.put(("duration", (token, key, cache[video])))

        threading.Thread(target=worker, daemon=True).start()

    def _set_row_duration(self, iid: str, text: str):
        if not self.tree.exists(iid):
            return
        values = list(self.tree.item(iid, "values"))
        if len(values) > 7:
            values[7] = text
            self.tree.item(iid, values=values)

    def _update_list_summary(self, prefix: str = ""):
        self._refresh_list_state()
        total = len(self.pairs)
        ready = sum(1 for p in self.pairs if p.is_ready)
        ticked = sum(1 for p in self.pairs if p.is_ready and self.checked.get(p.stem))
        msg = f"{prefix}Danh sách có {total} dòng, {ready} cặp sẵn sàng, {ticked} dòng đã tick để ghép."
        if self.text_enabled_var.get():
            texted = sum(
                1 for p in self.pairs
                if p.is_ready and self.checked.get(p.stem) and self.text_checked.get(p.stem)
            )
            msg += f" {texted} dòng trong số đó sẽ được chèn chữ."
        if self.bg_music_enabled_var.get():
            bg_ticked = sum(
                1 for p in self.pairs
                if p.is_ready and self.checked.get(p.stem) and self.bg_checked.get(p.stem, True)
            )
            msg += f" {bg_ticked} dòng trong số đó sẽ có nhạc nền."
        self.status_var.set(msg)

    def _refresh_check_cell(self, iid: str):
        pair = self.pair_by_iid.get(iid)
        if not pair or not self.tree.exists(iid):
            return
        values = list(self.tree.item(iid, "values"))
        if pair.is_ready:
            values[0] = CHECK_ON if self.checked.get(pair.stem) else CHECK_OFF
            values[1] = CHECK_ON if self.text_checked.get(pair.stem, True) else CHECK_OFF
            values[6] = CHECK_ON if self.bg_checked.get(pair.stem, True) else CHECK_OFF
        else:
            values[0] = ""
            values[1] = ""
            values[6] = ""
        self.tree.item(iid, values=values)

    def _refresh_all_check_heading(self):
        ready = [p for p in self.pairs if p.is_ready]
        all_on = bool(ready) and all(self.checked.get(p.stem) for p in ready)
        self.tree.heading("chk", text=CHECK_ON if all_on else CHECK_OFF)

    def _refresh_all_text_check_heading(self):
        ready = [p for p in self.pairs if p.is_ready]
        all_on = bool(ready) and all(self.text_checked.get(p.stem, True) for p in ready)
        self.tree.heading("text_chk", text=f"{CHECK_ON if all_on else CHECK_OFF} Chữ")

    def _refresh_all_bg_check_heading(self):
        ready = [p for p in self.pairs if p.is_ready]
        all_on = bool(ready) and all(self.bg_checked.get(p.stem, True) for p in ready)
        self.tree.heading("bg_chk", text=f"{CHECK_ON if all_on else CHECK_OFF} Nhạc")

    def _toggle_checked(self, iid: str):
        pair = self.pair_by_iid.get(iid)
        if not pair or not pair.is_ready or self.is_running:
            return
        self.checked[pair.stem] = not self.checked.get(pair.stem, False)
        self._refresh_check_cell(iid)
        self._refresh_all_check_heading()
        self._update_list_summary()

    def _toggle_text_checked(self, iid: str):
        pair = self.pair_by_iid.get(iid)
        if not pair or not pair.is_ready or self.is_running:
            return
        self.text_checked[pair.stem] = not self.text_checked.get(pair.stem, True)
        self._refresh_check_cell(iid)
        self._refresh_all_text_check_heading()
        self._update_list_summary()

    def _toggle_bg_checked(self, iid: str):
        pair = self.pair_by_iid.get(iid)
        if not pair or not pair.is_ready or self.is_running:
            return
        self.bg_checked[pair.stem] = not self.bg_checked.get(pair.stem, True)
        self._refresh_check_cell(iid)
        self._refresh_all_bg_check_heading()
        self._update_list_summary()

    def _set_all_checked(self, state: bool):
        if self.is_running:
            return
        for iid, pair in self.pair_by_iid.items():
            if pair.is_ready:
                self.checked[pair.stem] = state
                self._refresh_check_cell(iid)
        self._refresh_all_check_heading()
        if self.pairs:
            self._update_list_summary()

    def _set_all_text_checked(self, state: bool):
        if self.is_running:
            return
        for iid, pair in self.pair_by_iid.items():
            if pair.is_ready:
                self.text_checked[pair.stem] = state
                self._refresh_check_cell(iid)
        self._refresh_all_text_check_heading()
        if self.pairs:
            self._update_list_summary()

    def _set_all_bg_checked(self, state: bool):
        if self.is_running:
            return
        for iid, pair in self.pair_by_iid.items():
            if pair.is_ready:
                self.bg_checked[pair.stem] = state
                self._refresh_check_cell(iid)
        self._refresh_all_bg_check_heading()
        if self.pairs:
            self._update_list_summary()

    def _toggle_all_checked(self):
        ready = [p for p in self.pairs if p.is_ready]
        any_off = any(not self.checked.get(p.stem) for p in ready)
        self._set_all_checked(any_off)

    def _toggle_all_text_checked(self):
        ready = [p for p in self.pairs if p.is_ready]
        any_off = any(not self.text_checked.get(p.stem, True) for p in ready)
        self._set_all_text_checked(any_off)

    def _toggle_all_bg_checked(self):
        ready = [p for p in self.pairs if p.is_ready]
        any_off = any(not self.bg_checked.get(p.stem, True) for p in ready)
        self._set_all_bg_checked(any_off)

    def _clicked_column(self, event) -> str:
        """Tên cột DỮ LIỆU của ô vừa bấm (identify_column trả số theo thứ tự HIỂN THỊ)."""
        col = self.tree.identify_column(event.x)
        try:
            return self._display_cols[int(col.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return ""

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        name = self._clicked_column(event)
        if name not in ("chk", "text_chk", "bg_chk"):
            return None
        row = self.tree.identify_row(event.y)
        if row:
            if name == "chk":
                self._toggle_checked(row)
            elif name == "text_chk":
                self._toggle_text_checked(row)
            else:
                self._toggle_bg_checked(row)
        # Bấm vào ô tick thì KHÔNG đổi dòng đang được chọn (bôi đen)
        return "break"

    def on_tree_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self._clicked_column(event) in ("chk", "text_chk", "bg_chk"):
            return
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.on_preview()

    def on_delete_rows(self):
        """Xóa các dòng đang TICK ☑ (cột ghép) khỏi danh sách — chỉ xóa khỏi
        danh sách trong app, KHÔNG xóa file video/audio trên đĩa."""
        if self.is_running:
            messagebox.showinfo(APP_TITLE, "Đang ghép — hãy đợi xong hoặc bấm Dừng trước khi xóa dòng.")
            return
        children = list(self.tree.get_children())
        to_remove = [iid for iid in children if self.checked.get(self.pair_by_iid[iid].stem)]
        if not to_remove:
            messagebox.showinfo(
                APP_TITLE,
                "Chưa có dòng nào được tick ☑ để xóa. Hãy tick vào ô ☑ ở đầu dòng (hoặc bấm "
                "\"Tick tất cả\"), rồi bấm lại nút này.",
            )
            return

        first_idx = children.index(to_remove[0])

        removed_pairs = []
        for iid in to_remove:
            pair = self.pair_by_iid.pop(iid, None)
            if pair is not None:
                removed_pairs.append(pair)
                self.row_by_stem.pop(pair.stem, None)
                self.checked.pop(pair.stem, None)
                self.text_checked.pop(pair.stem, None)
                self.bg_checked.pop(pair.stem, None)
            if self.tree.exists(iid):
                self.tree.delete(iid)
        removed_ids = {id(p) for p in removed_pairs}
        self.pairs = [p for p in self.pairs if id(p) not in removed_ids]

        self._renumber_rows()

        # Tự chọn sang dòng kế tiếp để dễ nhìn tiếp tục thao tác
        remaining = list(self.tree.get_children())
        if remaining:
            nxt = remaining[min(first_idx, len(remaining) - 1)]
            self.tree.selection_set(nxt)
            self.tree.focus(nxt)
            self.tree.see(nxt)

        self._refresh_all_check_heading()
        self._refresh_all_text_check_heading()
        self._refresh_all_bg_check_heading()
        self._update_list_summary(prefix=f"Đã xóa {len(to_remove)} dòng khỏi danh sách. ")

    # --------------------------------------------------------- Tùy chọn --
    def _gather_merge_options(self) -> dict:
        aspect_label = self.aspect_var.get()
        aspect_value = ASPECT_RATIO_OPTIONS.get(aspect_label)
        if aspect_value == "custom":
            aspect_value = (int(self.custom_w_var.get()), int(self.custom_h_var.get()))

        return dict(
            target_short_side=RESOLUTION_OPTIONS.get(self.resolution_var.get()),
            no_upscale=bool(self.no_upscale_var.get()),
            crf=QUALITY_OPTIONS.get(self.quality_var.get()),
            aspect_ratio=aspect_value,
            fit_mode=FIT_MODE_OPTIONS.get(self.fit_mode_var.get(), "crop"),
            audio_mix_percent=int(self.mix_percent_var.get()),
            loop_audio=bool(self.loop_audio_var.get()),
            normalize_loudness=bool(self.normalize_var.get()),
            fade_seconds=float(self.fade_seconds_var.get()) if self.fade_enabled_var.get() else 0.0,
            text_overlay=self._gather_text_overlay(),
            trim_start=max(0.0, float(self.trim_start_var.get())),
            trim_end=max(0.0, float(self.trim_end_var.get())),
            **self._gather_bg_music_opts(),
        )

    def _gather_bg_music_opts(self) -> dict:
        """Trả về dict rỗng nếu đang tắt nhạc nền. Raise OverlayConfigError
        (thông báo hiển thị thẳng cho người dùng) nếu bật nhưng chưa chọn
        file hoặc file không còn tồn tại."""
        if not self.bg_music_enabled_var.get():
            return {}
        raw = self.bg_music_path_var.get().strip()
        if not raw:
            raise OverlayConfigError(
                "Bạn đã bật \"Thêm nhạc nền\" nhưng chưa chọn file nhạc (tab Nhạc nền)."
            )
        path = Path(raw)
        if not path.is_file():
            raise OverlayConfigError(f"Không tìm thấy file nhạc nền:\n{raw}")
        return dict(
            bg_music_path=path,
            bg_music_volume=max(0, min(100, int(self.bg_music_volume_var.get()))),
            bg_music_loop=bool(self.bg_music_loop_var.get()),
            bg_music_ducking=bool(self.bg_music_ducking_var.get()),
        )

    def _gather_text_overlay(self) -> list[dict] | None:
        """Trả về danh sách lớp chữ đã sẵn sàng cho ffmpeg, hoặc None nếu đang
        tắt. Raise OverlayConfigError với thông báo rõ ràng nếu chưa dùng được."""
        if not self.text_enabled_var.get():
            return None
        # Lớp blur không có chữ nhưng vẫn hợp lệ; lớp chữ trống thì bỏ qua.
        layers = [normalize_layer(x) for x in self.text_layers
                  if is_blur_layer(x) or str(x.get("text", "")).strip()]
        if not layers:
            raise OverlayConfigError(
                "Bạn đã bật \"Chèn chữ / blur\" nhưng chưa có lớp nào dùng được (chữ trống). "
                "Hãy bấm \"Mở trình chỉnh sửa chữ & blur\" (tab Chèn chữ & Blur) để thêm chữ hoặc blur."
            )
        has_text = any(not is_blur_layer(x) for x in layers)
        if has_text and self.ffmpeg_path and not ffmpeg_has_filter(self.ffmpeg_path, "drawtext"):
            raise OverlayConfigError(
                "Bản ffmpeg đang dùng không hỗ trợ chèn chữ (thiếu bộ lọc drawtext / "
                "libfreetype). Hãy tải bản ffmpeg \"full\" (VD: gyan.dev) rồi chọn lại ở "
                "tab Nâng cao."
            )
        native_align = bool(
            self.ffmpeg_path and ffmpeg_filter_has_option(self.ffmpeg_path, "drawtext", "text_align")
        )
        box_list_border = bool(
            self.ffmpeg_path
            and ffmpeg_filter_option_type(self.ffmpeg_path, "drawtext", "boxborderw") == "<string>"
        )
        try:
            workdir = str(self._ensure_temp_dir())
        except OSError as exc:
            raise OverlayConfigError(f"Không tạo được thư mục tạm cho chữ chèn: {exc}") from exc

        result = []
        for i, layer in enumerate(layers, 1):
            if is_blur_layer(layer):
                result.append(dict(layer))      # blur không cần font/chữ
                continue
            custom = layer["font"].strip()
            if custom and not Path(custom).is_file():
                raise OverlayConfigError(f"Lớp chữ {i}: không tìm thấy file font\n{custom}")
            font = resolve_layer_font(layer)
            if not font:
                raise OverlayConfigError(
                    f"Lớp chữ {i}: không tìm thấy font hệ thống để vẽ chữ. Hãy mở trình "
                    "chỉnh sửa chữ và chọn font (mục \"🔤 Chọn font trong máy\")."
                )
            result.append(dict(
                layer,
                workdir=workdir,
                fontfile=font,
                bold=bool(layer["bold"]) and not custom,
                wrap=True,
                native_align=native_align,
                box_list_border=box_list_border,
            ))
        return result

    def _save_options_to_cfg(self):
        self.cfg["merge_resolution"] = self.resolution_var.get()
        self.cfg["merge_no_upscale"] = bool(self.no_upscale_var.get())
        self.cfg["merge_trim_start"] = max(0.0, float(self.trim_start_var.get()))
        self.cfg["merge_trim_end"] = max(0.0, float(self.trim_end_var.get()))
        self.cfg["merge_quality"] = self.quality_var.get()
        self.cfg["merge_format"] = self.format_var.get()
        self.cfg["merge_aspect"] = self.aspect_var.get()
        self.cfg["merge_custom_w"] = int(self.custom_w_var.get())
        self.cfg["merge_custom_h"] = int(self.custom_h_var.get())
        self.cfg["merge_fit_mode"] = self.fit_mode_var.get()
        self.cfg["merge_audio_mix_percent"] = int(self.mix_percent_var.get())
        self.cfg["merge_loop_audio"] = bool(self.loop_audio_var.get())
        self.cfg["merge_normalize"] = bool(self.normalize_var.get())
        self.cfg["merge_fade_enabled"] = bool(self.fade_enabled_var.get())
        self.cfg["merge_fade_seconds"] = float(self.fade_seconds_var.get())
        for old_key in ("merge_watermark_enabled", "merge_watermark_path", "merge_watermark_opacity"):
            self.cfg.pop(old_key, None)    # tính năng watermark đã bỏ
        self.cfg["merge_export_csv"] = bool(self.export_csv_var.get())
        self.cfg["merge_preview_seconds"] = int(self.preview_seconds)
        self.cfg["merge_bg_music_enabled"] = bool(self.bg_music_enabled_var.get())
        self.cfg["merge_bg_music_path"] = self.bg_music_path_var.get().strip()
        self.cfg["merge_bg_music_volume"] = max(0, min(100, int(self.bg_music_volume_var.get())))
        self.cfg["merge_bg_music_loop"] = bool(self.bg_music_loop_var.get())
        self.cfg["merge_bg_music_ducking"] = bool(self.bg_music_ducking_var.get())
        self._save_text_options_to_cfg()
        save_config(self.cfg)

    def _save_text_options_to_cfg(self):
        """Lưu cấu hình chèn chữ (bật/tắt + toàn bộ các lớp chữ)."""
        try:
            self.cfg["merge_text_enabled"] = bool(self.text_enabled_var.get())
        except tk.TclError:
            pass
        self.cfg["merge_text_layers"] = [dict(layer) for layer in self.text_layers]

    # ------------------------------------------------------------ Xem thử --
    def _purge_preview_files(self):
        """Thử xóa các file xem thử cũ. File nào đang được trình phát giữ
        (thường gặp trên Windows) thì bỏ qua, sẽ được dọn sau."""
        remaining = []
        for f in self.preview_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                remaining.append(f)
        self.preview_files = remaining

    def _ensure_temp_dir(self) -> Path:
        """Thư mục tạm của phiên làm việc (chứa bản xem thử + file chữ chèn)."""
        if self.preview_dir is None or not self.preview_dir.exists():
            self.preview_dir = create_preview_dir()
        return self.preview_dir

    def _new_preview_path(self) -> Path:
        self._ensure_temp_dir()
        self._purge_preview_files()
        self._preview_counter += 1
        path = self.preview_dir / f"preview_{self._preview_counter}.mp4"
        self.preview_files.append(path)
        return path

    def cleanup_previews(self):
        """Xóa toàn bộ bản xem thử (gọi khi thoát app). An toàn khi gọi
        nhiều lần."""
        self._purge_preview_files()
        if self.preview_dir is not None:
            shutil.rmtree(self.preview_dir, ignore_errors=True)
            self.preview_dir = None

    def shutdown(self) -> bool:
        """Gọi khi đóng cửa sổ chính. Trả về True nếu đang có tiến trình ghép
        được yêu cầu dừng (để cửa sổ chính đợi ffmpeg kịp thoát)."""
        was_running = self.is_running
        self.stop_event.set()
        self.cleanup_previews()
        return was_running

    def on_preview(self):
        if self.is_running:
            messagebox.showinfo(APP_TITLE, "Đang ghép — hãy đợi xong hoặc bấm Dừng rồi mới xem thử.")
            return
        if self.is_previewing:
            self.status_var.set("Đang tạo bản xem thử trước đó, vui lòng đợi một chút...")
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(
                APP_TITLE, "Hãy bấm chọn 1 dòng trong danh sách để xem thử (hoặc bấm đúp vào dòng)."
            )
            return
        pair = self.pair_by_iid.get(sel[0])
        if not pair or not pair.is_ready:
            messagebox.showinfo(APP_TITLE, "Dòng này chưa có đủ video + audio để xem thử.")
            return

        self._detect_ffmpeg()
        if not self.ffmpeg_path or not self.ffprobe_path:
            messagebox.showerror(APP_TITLE, "Chưa tìm thấy đủ ffmpeg/ffprobe.")
            return

        self._on_preview_seconds_changed()
        try:
            opts = self._gather_merge_options()
        except OverlayConfigError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        except (tk.TclError, ValueError):
            messagebox.showwarning(APP_TITLE, "Có tùy chọn ghép chứa giá trị không hợp lệ, hãy kiểm tra lại.")
            return
        # Xem thử phải khớp đúng những gì dòng NÀY sẽ nhận khi ghép thật: nếu
        # dòng chưa tick ☑ ở cột "Chữ" thì bỏ chữ ra khỏi bản xem thử, dù tab
        # 4 đang bật chèn chữ cho các dòng khác.
        if not self.text_checked.get(pair.stem, True):
            opts = dict(opts, text_overlay=None)
        if not self.bg_checked.get(pair.stem, True):
            opts = dict(opts, bg_music_path=None)
        self._save_text_options_to_cfg()
        save_config(self.cfg)

        try:
            preview_path = self._new_preview_path()
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không tạo được thư mục tạm cho bản xem thử: {exc}")
            return

        self.is_previewing = True
        self.preview_btn.state(["disabled"])
        seconds = float(self.preview_seconds)
        video, audio = pair.video, pair.audio
        ffmpeg_path, ffprobe_path = self.ffmpeg_path, self.ffprobe_path
        self.status_var.set(f"Đang tạo bản xem thử {self.preview_seconds}s cho {video.name}...")

        def worker():
            try:
                info = probe_media_info(ffprobe_path, video)
                cmd = build_preview_command(
                    ffmpeg_path, video, audio, preview_path, info,
                    preview_seconds=seconds, **opts,
                )
                run_ffmpeg_merge(cmd, preview_path)
            except (MergeError, MergeCancelled) as exc:
                self.task_queue.put(("preview_error", str(exc)))
                return
            except Exception as exc:  # noqa: BLE001 - luôn nhả nút xem thử ra khi có lỗi bất ngờ
                self.task_queue.put(("preview_error", f"Lỗi không mong đợi: {exc}"))
                return
            self.task_queue.put(("preview_ready", preview_path))

        threading.Thread(target=worker, daemon=True).start()

    def _open_file_with_default_app(self, path: Path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showinfo(
                APP_TITLE,
                f"Đã tạo bản xem thử nhưng không tự mở được bằng trình phát mặc định:\n{exc}",
            )

    # ------------------------------------------------------------- Ghép --
    def on_start(self):
        if self.is_running:
            return
        self._detect_ffmpeg()
        if not self.ffmpeg_path or not self.ffprobe_path:
            messagebox.showerror(
                APP_TITLE,
                "Chưa tìm thấy đủ ffmpeg + ffprobe. Hãy bấm \"Tải ffmpeg\" để cài "
                "đặt (chọn bản \"full\"), hoặc \"Chọn ffmpeg thủ công\".",
            )
            return
        if not self.output_dir:
            messagebox.showwarning(APP_TITLE, "Hãy chọn Thư mục xuất trước.")
            return

        ready_pairs = [p for p in self.pairs if p.is_ready and self.checked.get(p.stem)]
        if not ready_pairs:
            if any(p.is_ready for p in self.pairs):
                msg = "Chưa tick dòng nào để ghép. Hãy bấm vào ô ☐ ở đầu dòng (hoặc \"Tick tất cả\")."
            else:
                msg = "Không có cặp video/audio nào sẵn sàng. Hãy bấm \"Quét & Ghép cặp\" trước."
            messagebox.showinfo(APP_TITLE, msg)
            return

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không tạo được thư mục xuất: {exc}")
            return

        if self.is_previewing:
            messagebox.showinfo(APP_TITLE, "Đang tạo bản xem thử — hãy đợi vài giây rồi bấm ghép.")
            return

        try:
            # Đọc mọi tùy chọn trên LUỒNG CHÍNH (widget Tk không an toàn khi
            # đọc từ luồng phụ) rồi truyền sang luồng ghép.
            opts = self._gather_merge_options()
            self._save_options_to_cfg()
        except OverlayConfigError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        except (tk.TclError, ValueError):
            messagebox.showwarning(APP_TITLE, "Có tùy chọn ghép chứa giá trị không hợp lệ, hãy kiểm tra lại.")
            return
        self._merge_opts = dict(opts)
        # Chữ/nhạc nền áp dụng theo TỪNG dòng (cột ☑ "Chữ"/"Nhạc"), không
        # phải toàn bộ — tách riêng khỏi opts dùng chung, gắn lại đúng dòng
        # trong worker. bg_music_volume/loop/ducking vẫn ở lại trong opts vì
        # đó là cấu hình chung, vô hại khi bg_music_path=None (không áp gì).
        self._merge_text_overlay = self._merge_opts.pop("text_overlay", None)
        self._merge_bg_music_path = self._merge_opts.pop("bg_music_path", None)

        self.is_running = True
        self.stop_event.clear()
        self.start_btn.state(["disabled"])
        self.preview_btn.state(["disabled"])
        self.delete_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.status_var.set(f"Đang ghép {len(ready_pairs)} cặp video/audio...")

        threading.Thread(target=self._merge_worker, args=(ready_pairs,), daemon=True).start()

    def on_stop(self):
        if not self.is_running:
            return
        self.stop_event.set()
        self.status_var.set("Đang dừng... (dừng ngay cả cặp đang ghép dở)")
        self.stop_btn.state(["disabled"])

    def _merge_worker(self, pairs: list[MediaPair]):
        total = len(pairs)
        max_workers = max(1, min(self.merge_workers, total))
        counters = {"done": 0, "errors": 0, "cancelled": 0}
        lock = threading.Lock()
        log_rows: list[dict] = []
        used_pairs_this_run: set[tuple[str, str]] = set()

        opts = self._merge_opts
        out_format = self.format_var.get() or DEFAULT_OUTPUT_FORMAT
        out_ext = f".{out_format.lstrip('.')}"
        is_random_mode = PAIRING_MODE_OPTIONS.get(self.pairing_mode_var.get()) == PAIRING_MODE_RANDOM

        # Tên file xuất = tên video gốc; trùng thì thêm (1), (2)... theo thứ tự
        # danh sách. Cấp tên trước ở đây (tuần tự) để các luồng song song
        # không giành cùng một tên.
        output_paths = dict(zip(
            (p.stem for p in pairs),
            assign_unique_output_paths(self.output_dir, [p.video.stem for p in pairs], out_ext),
        ))

        def merge_one(pair: MediaPair):
            now = datetime.now().strftime("%H:%M:%S")
            if self.stop_event.is_set():
                self.task_queue.put(("row_status", (pair.stem, "Đã dừng")))
                return

            self.task_queue.put(("row_status", (pair.stem, "Đang ghép...")))
            output_path = output_paths[pair.stem]

            try:
                info = probe_media_info(self.ffprobe_path, pair.video)
                # Chữ chỉ áp cho dòng này nếu còn tick ☑ "Chữ" (có thể đã bị
                # bỏ tick sau khi bấm Bắt đầu nhưng TRƯỚC khi worker này chạy
                # tới lượt — self.text_checked đọc trực tiếp lúc này nên vẫn
                # phản ánh đúng, không dùng giá trị chốt từ lúc bấm nút).
                text_overlay = (
                    self._merge_text_overlay if self.text_checked.get(pair.stem, True) else None
                )
                bg_music_path = (
                    self._merge_bg_music_path if self.bg_checked.get(pair.stem, True) else None
                )
                cmd = build_ffmpeg_command(
                    self.ffmpeg_path, pair.video, pair.audio, output_path, info,
                    text_overlay=text_overlay, bg_music_path=bg_music_path, **opts,
                )
                run_ffmpeg_merge(cmd, output_path, stop_flag=self.stop_event.is_set)
            except MergeCancelled:
                self.task_queue.put(("row_status", (pair.stem, "Đã dừng")))
                with lock:
                    counters["cancelled"] += 1
                    done_count = sum(counters.values())
                log_rows.append({"video": pair.video.name, "audio": pair.audio.name, "output_file": "",
                                  "status": "Đã dừng", "message": "", "time": now})
                self.task_queue.put(("progress", (done_count, total, f"[{done_count}/{total}] Đã dừng: {pair.stem}")))
                return
            except MergeError as exc:
                self.task_queue.put(("row_status", (pair.stem, "Lỗi")))
                with lock:
                    counters["errors"] += 1
                    done_count = sum(counters.values())
                log_rows.append({"video": pair.video.name, "audio": pair.audio.name, "output_file": "",
                                  "status": "Lỗi", "message": str(exc)[:300], "time": now})
                self.task_queue.put(
                    ("progress", (done_count, total, f"[{done_count}/{total}] Lỗi khi ghép {pair.stem}: {exc}"))
                )
                return

            self.task_queue.put(("row_status", (pair.stem, "Đã xong")))
            with lock:
                counters["done"] += 1
                done_count = sum(counters.values())
                used_pairs_this_run.add((pair.video.name, pair.audio.name))
            log_rows.append({"video": pair.video.name, "audio": pair.audio.name,
                              "output_file": output_path.name, "status": "Đã xong", "message": "", "time": now})
            self.task_queue.put(
                ("progress", (done_count, total, f"[{done_count}/{total}] Đã ghép xong: {output_path.name}"))
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(merge_one, p) for p in pairs]
            for future in as_completed(futures):
                future.result()

        # Lưu lịch sử cặp đã dùng (chỉ áp dụng thực sự hữu ích ở chế độ Random Mix)
        if is_random_mode and used_pairs_this_run:
            history = load_pair_history(self.cfg)
            history |= used_pairs_this_run
            save_pair_history(self.cfg, history, MERGE_HISTORY_MAX)
            save_config(self.cfg)

        csv_path = None
        if self.export_csv_var.get() and log_rows:
            try:
                csv_path = write_merge_log_csv(self.output_dir, log_rows)
            except OSError:
                csv_path = None

        if self.stop_event.is_set():
            summary = (
                f"Đã DỪNG ghép theo yêu cầu — hoàn tất {counters['done']}/{total} cặp trước khi dừng "
                f"({counters['cancelled']} cặp bị hủy giữa chừng"
            )
            if counters["errors"]:
                summary += f", {counters['errors']} lỗi"
            summary += ")."
        else:
            summary = f"Hoàn tất: đã ghép {counters['done']}/{total} cặp vào {self.output_dir}"
            if counters["errors"]:
                summary += f" ({counters['errors']} lỗi)."
        if csv_path:
            summary += f" Đã ghi log: {csv_path.name}"
        self.task_queue.put(("done", summary))

    # -------------------------------------------------------- Poll queue --
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.task_queue.get_nowait()
                if kind == "row_status":
                    key, status = payload
                    self._update_row_status(key, status)
                elif kind == "progress":
                    done, total, label = payload
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.status_var.set(label)
                elif kind == "done":
                    self.is_running = False
                    self.start_btn.state(["!disabled"])
                    self.delete_btn.state(["!disabled"])
                    if not self.is_previewing:
                        self.preview_btn.state(["!disabled"])
                    self.stop_btn.state(["disabled"])
                    self.status_var.set(payload)
                    self.history_count_var.set(
                        f"Hiện đang lưu {len(load_pair_history(self.cfg))} cặp trong lịch sử."
                    )
                elif kind == "duration":
                    token, key, text = payload
                    if token == self._probe_token:
                        iid = self.row_by_stem.get(key)
                        if iid:
                            self._set_row_duration(iid, text)
                elif kind == "preview_ready":
                    self.is_previewing = False
                    self.preview_btn.state(["!disabled"])
                    self.status_var.set(
                        "Đã tạo xong bản xem thử (chỉ lưu tạm, tự xóa khi đóng app) — đang mở trình phát."
                    )
                    self._open_file_with_default_app(payload)
                elif kind == "preview_error":
                    self.is_previewing = False
                    self.preview_btn.state(["!disabled"])
                    self.status_var.set("Lỗi khi tạo bản xem thử.")
                    messagebox.showerror(APP_TITLE, f"Không tạo được bản xem thử:\n{payload}")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll_queue)
