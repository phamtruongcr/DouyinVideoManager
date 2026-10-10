"""
script_voice_gui.py
===================
TAB "Kịch bản & Giọng đọc" (Giai đoạn 1 / MVP của tính năng review có giọng đọc).

Luồng:  chọn video (lịch sử tải HOẶC thư mục)  ->  Gemini viết kịch bản (hiện
trong ô soạn thảo, sửa tự do)  ->  bấm "Tạo giọng nói" -> nghe thử trong thanh phát; bấm "Tải về" mới lưu <tên video>.mp3 vào
THƯ MỤC AUDIO của tab "Ghép Audio vào Video" (để tab đó tự ghép cặp theo tên).

Logic nằm ở review_script.py (kịch bản) và tts_local.py (giọng đọc); file này
chỉ lo giao diện + chạy việc nặng ở luồng nền. Mỗi lần chạy có 1 "job id": bấm
Dừng (hoặc chạy việc mới) thì kết quả của job cũ bị bỏ, không ghi đè giao diện.
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
import os
import subprocess
import sys
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Optional

from . import theme
from .app_logger import get_logger
from .audio_merger import list_media_files, probe_media_info
from .config import (
    APP_TITLE,
    AUDIO_EXTENSIONS,
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_TTS_BACKEND,
    DEFAULT_TTS_GEMINI_MODEL,
    DEFAULT_TTS_GEMINI_VOICE,
    GEMINI_TTS_VOICES,
    MAX_REVIEW_DURATION,
    MAX_TTS_PAUSE_COMMA,
    MAX_TTS_PAUSE_SENTENCE,
    MAX_TTS_SPEED,
    MAX_TTS_STABILITY,
    MIN_REVIEW_DURATION,
    MIN_TTS_PAUSE,
    MIN_TTS_SPEED,
    MIN_TTS_STABILITY,
    REVIEW_STYLE_KOC,
    REVIEW_STYLE_OPTIONS,
    TTS_BACKEND_OPTIONS,
    TTS_BACKEND_VIENEU,
    TTS_HISTORY_MAX,
    VIDEO_EXTENSIONS,
    read_koc_brief,
    read_review_settings,
    read_tts_settings,
    save_config,
)
from .download_history import DownloadHistory
from .review_script import (
    KocBrief,
    count_words,
    estimate_read_seconds,
    blend_words_per_second,
    estimate_target_words,
    generate_review_script,
    measure_words_per_second,
    numbers_to_vietnamese,
)
from .tts_local import (
    AudioPlayer,
    TTSCancelled,
    TTSOptions,
    apply_speed,
    first_sentence,
    make_backend,
    save_audio_file,
    synthesize_with_options,
    wav_duration_seconds,
    wav_peaks,
)
from . import voice_library as vlib
from . import vieneu_installer as vinst
from .audio_bar import AudioBar, format_clock
from .ui_icons import Tooltip
from .widgets import (
    RoundedButton,
    ScrollableFrame,
    SegmentedTabs,
    WrapFrame,
    bind_wraplength,
    make_card,
)

log = get_logger("script_voice_gui")

SOURCE_HISTORY = "Lịch sử tải"
SOURCE_FOLDER = "Thư mục video"


class ScriptVoiceTab(ttk.Frame):
    """Tab "Kịch bản & Giọng đọc"."""

    def __init__(
        self,
        master,
        cfg: dict,
        history: DownloadHistory,
        get_audio_dir: Callable[[], Optional[Path]],
        set_audio_dir: Callable[[Path], None],
        get_ffmpeg: Callable[[], Optional[str]],
        get_ffprobe: Callable[[], Optional[str]],
    ):
        super().__init__(master, style="Page.TFrame")
        self.cfg = cfg
        self.history = history
        self._get_audio_dir = get_audio_dir
        self._set_audio_dir = set_audio_dir
        self._get_ffmpeg = get_ffmpeg
        self._get_ffprobe = get_ffprobe

        self.video_path: Optional[Path] = None
        self.video_by_iid: dict[str, Path] = {}
        self.task_queue: queue.Queue = queue.Queue()
        self._job_id = 0                       # tăng mỗi lần bắt đầu/dừng việc
        self._cancel = threading.Event()       # cờ dừng của job hiện tại
        self._busy = False
        self._tmp_dir: Optional[Path] = None   # chứa file nghe thử (xóa khi thoát)
        self.player = AudioPlayer()
        self._video_dir: Optional[Path] = (
            Path(cfg["review_video_dir"]) if cfg.get("review_video_dir") else None
        )
        self._last_target: Optional[int] = None   # thời lượng (giây) của lần tạo kịch bản gần nhất
        self._slider_busy = False                 # chống đệ quy khi làm tròn giá trị thanh trượt
        self._last_audio: Optional[Path] = None   # file audio đã lưu gần nhất
        self._result: Optional[dict] = None       # giọng vừa tạo (còn trong bộ nhớ, chưa chắc đã lưu)
        self._voice_name = ""
        self._dur_cache: dict[str, tuple[float, float]] = {}   # đường dẫn -> (mtime, số giây)
        self._dur_queue: queue.Queue = queue.Queue()
        self._dur_gen = 0                                       # tăng mỗi lần làm mới danh sách
        self._autofill_pending: Optional[Path] = None          # video vừa chọn, đang chờ đọc thời lượng

        self._install_queue: queue.Queue = queue.Queue()   # tiến độ cài VieNeu (tách khỏi job đọc giọng)
        self._installing = False

        self._build_ui()
        self.refresh_video_list()
        self.after(100, self._poll_queue)

    # ============================================================== UI ==
    def _info(self, parent, text: str) -> Tooltip:
        """Biểu tượng ⓘ nhỏ — rê chuột vào mới hiện phần giải thích (thay cho dòng chữ xám)."""
        lbl = ttk.Label(parent, text="ⓘ", style="Link.TLabel", cursor="hand2")
        lbl.pack(side="left", padx=(6, 0))
        return Tooltip(lbl, text, wraplength=320)

    def _icon_btn(self, parent, text: str, tip: str, command) -> ttk.Button:
        btn = ttk.Button(parent, text=text, style="Icon.TButton", command=command)
        Tooltip(btn, tip)
        return btn

    def _build_ui(self):
        PAD = 10

        # ---- ĐÁY (luôn hiện, pack trước): tiến độ + trạng thái + CÁC NÚT HÀNH ĐỘNG ----
        bottom = ttk.Frame(self, style="Page.TFrame", padding=(PAD, 4, PAD, 10))
        bottom.pack(side="bottom", fill="x")
        bottom.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        self.status_var = tk.StringVar(value="Chọn một video để bắt đầu.")
        status = ttk.Label(bottom, textvariable=self.status_var, style="PageMuted.TLabel", justify="left")
        status.grid(row=1, column=0, sticky="w")
        bind_wraplength(status, bottom, margin=620, min_width=160)
        # Nút PHỤ: xám, viền nhạt  |  nút CHÍNH: xanh lá to, bo góc mềm
        self.preview_btn = ttk.Button(
            bottom, text="🔊  Nghe thử 1 câu", style="Secondary.TButton", command=self.on_preview,
        )
        self.preview_btn.grid(row=1, column=1, padx=(12, 0), sticky="e")
        self.read_btn = RoundedButton(
            bottom, text="🎙  TẠO GIỌNG NÓI", command=self.on_read,
            bg=theme.GREEN, hover_bg=theme.GREEN_HOVER, font=("", 12, "bold"),
            padx=30, pady=10, radius=16, parent_bg=theme.BG,
        )
        self.read_btn.grid(row=1, column=2, padx=(10, 0), sticky="e")
        self.stop_btn = RoundedButton(
            bottom, text="■  DỪNG", command=self.on_stop,
            bg=theme.RED, hover_bg=theme.RED_HOVER,
            disabled_bg=theme.DISABLED_RED_BG, disabled_fg=theme.DISABLED_RED_FG,
            font=("", 11, "bold"), padx=18, pady=10, radius=16, parent_bg=theme.BG,
        )
        self.stop_btn.grid(row=1, column=3, padx=(8, 0), sticky="e")
        self.stop_btn.state(["disabled"])

        # ---- THÂN: 2 cột — trái: video + kịch bản (giãn); phải: giọng đọc (rộng cố định) ----
        body = ttk.Frame(self, style="Page.TFrame")
        body.pack(side="top", fill="both", expand=True, padx=PAD, pady=(PAD, 0))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, style="Page.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(body, style="Page.TFrame", width=440)
        right.grid(row=0, column=1, sticky="nsew")
        right.pack_propagate(False)

        # ============ THẺ 1: chọn video ============
        c1_outer, c1 = make_card(left, "1 · Chọn video")
        c1_outer.pack(fill="x", pady=(0, 6))

        src_row = WrapFrame(c1, hgap=8, vgap=4, valign="center")
        src_row.pack(fill="x")
        self.source_var = tk.StringVar(
            value=SOURCE_FOLDER if self.cfg.get("review_video_source") == "folder" else SOURCE_HISTORY
        )
        src_lbl = ttk.Label(src_row, text="Nguồn:")
        src_combo = ttk.Combobox(
            src_row, textvariable=self.source_var, state="readonly", width=16,
            values=[SOURCE_HISTORY, SOURCE_FOLDER],
        )
        src_combo.bind("<<ComboboxSelected>>", lambda e: self._on_source_changed())
        self.choose_dir_btn = ttk.Button(src_row, text="📁  Chọn thư mục...", command=self._choose_video_dir)
        pick_file_btn = ttk.Button(src_row, text="🎞  Chọn 1 file...", command=self._choose_video_file)
        refresh_btn = ttk.Button(src_row, text="🔄  Làm mới", command=self.refresh_video_list)
        for w in (src_lbl, src_combo, self.choose_dir_btn, pick_file_btn, refresh_btn):
            src_row.add(w)

        tree_box = ttk.Frame(c1)
        tree_box.pack(fill="x", pady=(6, 0))
        self.tree = ttk.Treeview(
            tree_box, columns=("name", "dur", "info"), show="headings", height=4, selectmode="browse",
        )
        self.tree.heading("name", text="Video", anchor="w")
        self.tree.heading("dur", text="Thời lượng", anchor="w")
        self.tree.heading("info", text="Ghi chú", anchor="w")
        self.tree.column("name", width=300, minwidth=140, anchor="w")
        self.tree.column("dur", width=84, minwidth=70, anchor="w", stretch=False)
        self.tree.column("info", width=150, minwidth=80, anchor="w", stretch=False)
        vsb = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select())

        self.selected_var = tk.StringVar(value="Chưa chọn video.")
        sel_lbl = ttk.Label(c1, textvariable=self.selected_var, style="Muted.TLabel", justify="left")
        sel_lbl.pack(anchor="w", pady=(4, 0), fill="x")
        bind_wraplength(sel_lbl, c1)

        # ============ THẺ 2: kịch bản ============
        c2_outer, c2 = make_card(left, "2 · Kịch bản")
        c2_outer.pack(fill="both", expand=True)

        settings = read_review_settings(self.cfg)
        brief = read_koc_brief(self.cfg)

        # ---- Form cấu hình: lưới 2 cột đều nhau, nhãn thẳng hàng ----
        form = ttk.Frame(c2)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1, uniform="cfg")
        form.columnconfigure(1, weight=1, uniform="cfg")
        col1 = ttk.Frame(form)
        col1.grid(row=0, column=0, sticky="new", padx=(0, 10))
        col2 = ttk.Frame(form)
        col2.grid(row=0, column=1, sticky="new", padx=(10, 0))
        for col in (col1, col2):
            col.columnconfigure(0, minsize=96)
            col.columnconfigure(1, weight=1)

        def lab(col, row, text, tip=None):
            lbl = ttk.Label(col, text=text)
            lbl.grid(row=row, column=0, sticky="w", pady=3, padx=(0, 6))
            if tip:
                Tooltip(lbl, tip, wraplength=300)
            return lbl

        style_label = next(
            (lbl for lbl, val in REVIEW_STYLE_OPTIONS.items() if val == settings["style"]),
            next(iter(REVIEW_STYLE_OPTIONS)),
        )
        self.style_var = tk.StringVar(value=style_label)
        style_combo = ttk.Combobox(
            col1, textvariable=self.style_var, state="readonly",
            values=list(REVIEW_STYLE_OPTIONS.keys()),
        )
        style_combo.bind("<<ComboboxSelected>>", lambda e: self._on_brief_changed())
        self.product_var = tk.StringVar(value=brief["product"])
        product_entry = ttk.Entry(col1, textvariable=self.product_var)
        product_entry.bind("<FocusOut>", lambda e: self._on_brief_changed())
        self.slang_var = tk.StringVar(value=brief["slang"])
        self.slang_entry = ttk.Entry(col1, textvariable=self.slang_var)
        self.slang_entry.bind("<FocusOut>", lambda ev: self._save_settings())
        lab(col1, 0, "Sản phẩm")
        product_entry.grid(row=0, column=1, sticky="ew", pady=3)
        lab(col1, 1, "Phong cách")
        style_combo.grid(row=1, column=1, sticky="ew", pady=3)
        lab(col1, 2, "Từ khóa review", "Chỉ dùng cho phong cách KOC: các từ/cụm hay dùng khi review.")
        self.slang_entry.grid(row=2, column=1, sticky="ew", pady=3)

        self.duration_var = tk.StringVar(value=str(brief["duration"]))
        dur_box = ttk.Frame(col2)
        duration_spin = ttk.Spinbox(
            dur_box, from_=MIN_REVIEW_DURATION, to=MAX_REVIEW_DURATION, width=5,
            textvariable=self.duration_var, command=self._on_brief_changed,
        )
        duration_spin.bind("<FocusOut>", lambda e: self._on_brief_changed())
        duration_spin.bind("<Return>", lambda e: self._on_brief_changed())
        self.use_len_var = tk.BooleanVar(value=brief["use_video_len"])
        use_len_chk = ttk.Checkbutton(
            dur_box, text="Theo video", variable=self.use_len_var,
            command=self._on_brief_changed,
        )
        duration_spin.pack(side="left")
        use_len_chk.pack(side="left", padx=(10, 0))
        Tooltip(use_len_chk, "Lấy thời lượng kịch bản theo độ dài thật của video đã chọn.", wraplength=260)
        self.address_var = tk.StringVar(value=brief["address_terms"])
        self.address_entry = ttk.Entry(col2, textvariable=self.address_var)
        self.address_entry.bind("<FocusOut>", lambda ev: self._save_settings())
        self.extra_var = tk.StringVar(value=str(self.cfg.get("review_extra", "") or ""))
        extra_entry = ttk.Entry(col2, textvariable=self.extra_var)
        extra_entry.bind("<FocusOut>", lambda e: self._save_settings())
        lab(col2, 0, "Thời lượng (s)")
        dur_box.grid(row=0, column=1, sticky="w", pady=3)
        lab(col2, 1, "Xưng hô", "Chỉ dùng cho phong cách KOC. VD: mình – các bạn.")
        self.address_entry.grid(row=1, column=1, sticky="ew", pady=3)
        lab(col2, 2, "Ghi chú", "Ghi chú / tính năng nổi bật của sản phẩm để đưa vào kịch bản.")
        extra_entry.grid(row=2, column=1, sticky="ew", pady=3)

        # ---- Nút tạo kịch bản: ngay dưới form, phía trên khung soạn thảo ----
        gen_row = ttk.Frame(c2)
        gen_row.pack(fill="x", pady=(8, 0))
        self.gen_btn = RoundedButton(
            gen_row, text="✨  Tạo kịch bản", command=self.on_generate,
            bg=theme.AI_BG, hover_bg=theme.AI_HOVER, outline=theme.AI_BORDER,
            padx=22, pady=7, radius=12, parent_bg=theme.CARD,
        )
        self.gen_btn.pack(side="left")
        self.target_info_var = tk.StringVar(value="")
        ttk.Label(gen_row, textvariable=self.target_info_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))

        # ---- Khung soạn thảo: mở rộng tối đa; số từ/giây nằm góc dưới phải bên trong khung ----
        editor_box = ttk.Frame(c2)
        editor_box.pack(fill="both", expand=True, pady=(8, 0))
        self.text = tk.Text(editor_box, wrap="word", undo=True, height=6, padx=10, pady=8)
        tvsb = ttk.Scrollbar(editor_box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tvsb.set)
        self.text.pack(side="left", fill="both", expand=True)
        tvsb.pack(side="left", fill="y")
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.words_info_var = tk.StringVar(value="0 từ")
        words_lbl = tk.Label(
            editor_box, textvariable=self.words_info_var, bg=theme.FIELD, fg=theme.MUTED,
            font=("", 9), padx=6, pady=1,
        )
        words_lbl.place(relx=1.0, rely=1.0, x=-22, y=-4, anchor="se")

        # ============ CỘT PHẢI: tab "Cài đặt" | "Lịch sử" (nền trang, các khối là thẻ riêng) ============
        # Thẻ "Audio vừa tạo" (có nút Tải về) chỉ hiện sau khi đọc xong — pack theo _show_result().
        self._build_result_card(right)
        tabs = SegmentedTabs(right, variant="sub", outer_bg=theme.BG)
        tabs.pack(fill="both", expand=True)
        self._right_tabs = tabs
        settings_scroll = ScrollableFrame(tabs, body_style="Page.TFrame", canvas_bg=theme.BG)
        hist_outer, history_tab = make_card(tabs, None, padding=(8, 8))
        tabs.add(settings_scroll, text="Cài đặt")
        tabs.add(hist_outer, text="Lịch sử")
        c3 = ttk.Frame(settings_scroll.body, style="Page.TFrame", padding=(0, 0, 8, 6))   # chừa chỗ cho thanh cuộn
        c3.pack(fill="both", expand=True)

        def card_title(parent, text, tip=None):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text=text, style="CardTitle.TLabel").pack(side="left")
            return self._info(row, tip) if tip else None

        # ---------- KHỐI 1: chọn giọng ----------
        v_outer, vbox = make_card(c3, None, padding=(12, 10))
        v_outer.pack(fill="x", pady=(0, 8))
        self._backend_tip = card_title(vbox, "Giọng đọc", "…")
        backend_box = ttk.Frame(vbox)
        backend_box.pack(fill="x")
        self._backend_row = backend_box
        backend_box.columnconfigure(0, minsize=100)
        backend_box.columnconfigure(1, weight=1)
        ttk.Label(backend_box, text="Backend").grid(row=0, column=0, sticky="w", pady=3)
        backend_val = self.cfg.get("tts_backend", DEFAULT_TTS_BACKEND)
        backend_label = next(
            (lbl for lbl, val in TTS_BACKEND_OPTIONS.items() if val == backend_val),
            next(iter(TTS_BACKEND_OPTIONS)),
        )
        self.backend_var = tk.StringVar(value=backend_label)
        backend_combo = ttk.Combobox(
            backend_box, textvariable=self.backend_var, state="readonly",
            values=list(TTS_BACKEND_OPTIONS.keys()),
        )
        backend_combo.grid(row=0, column=1, sticky="ew", pady=3)
        backend_combo.bind("<<ComboboxSelected>>", lambda e: self._on_backend_changed())

        # VieNeu: trạng thái cài đặt + nút cài ngay trên giao diện
        self.install_row = ttk.Frame(vbox)
        self.install_row.columnconfigure(0, minsize=100)
        self.install_row.columnconfigure(1, weight=1)
        ttk.Label(self.install_row, text="Thư viện").grid(row=0, column=0, sticky="w", pady=3)
        ir = ttk.Frame(self.install_row)
        ir.grid(row=0, column=1, sticky="ew", pady=3)
        self.install_status_var = tk.StringVar(value="")
        ttk.Label(ir, textvariable=self.install_status_var, style="Muted.TLabel").pack(side="left")
        self.install_btn = ttk.Button(ir, text="⬇ Cài VieNeu", command=self.on_install_vieneu)
        self.install_btn.pack(side="right")
        self._refresh_install_state()

        # VieNeu: giọng mẫu + giọng đã lưu
        self.sample_row = ttk.Frame(vbox)
        self.sample_row.columnconfigure(0, minsize=100)
        self.sample_row.columnconfigure(1, weight=1)
        ttk.Label(self.sample_row, text="Giọng mẫu").grid(row=0, column=0, sticky="w", pady=3)
        r1 = ttk.Frame(self.sample_row)
        r1.grid(row=0, column=1, sticky="ew", pady=3)
        self.sample_var = tk.StringVar(value=settings["voice_sample"])
        sample_entry = ttk.Entry(r1, textvariable=self.sample_var)
        sample_entry.pack(side="left", fill="x", expand=True)
        self._icon_btn(r1, "📁", "Chọn file giọng mẫu...", self._choose_voice_sample).pack(side="left", padx=(6, 0))
        self._icon_btn(r1, "❌", "Bỏ file giọng mẫu", lambda: self._set_voice_sample("")).pack(side="left", padx=(4, 0))
        sample_entry.bind("<FocusOut>", lambda e: self._save_settings())

        ttk.Label(self.sample_row, text="Giọng đã lưu").grid(row=1, column=0, sticky="w", pady=3)
        r2 = ttk.Frame(self.sample_row)
        r2.grid(row=1, column=1, sticky="ew", pady=3)
        self.saved_voice_var = tk.StringVar(value="")
        self.saved_combo = ttk.Combobox(r2, textvariable=self.saved_voice_var, state="readonly")
        self.saved_combo.pack(side="left", fill="x", expand=True)
        self.saved_combo.bind("<<ComboboxSelected>>", lambda e: self._on_saved_voice_selected())
        self._icon_btn(r2, "💾", "Lưu file giọng mẫu hiện tại thành giọng đã lưu", self._save_current_voice).pack(side="left", padx=(6, 0))
        self._icon_btn(r2, "🗑", "Xóa giọng đã lưu đang chọn", self._delete_saved_voice).pack(side="left", padx=(4, 0))
        self._refresh_saved_voices()

        # Gemini: chọn giọng dựng sẵn
        self.gemini_row = ttk.Frame(vbox)
        self.gemini_row.columnconfigure(0, minsize=100)
        self.gemini_row.columnconfigure(1, weight=1)
        ttk.Label(self.gemini_row, text="Giọng Gemini").grid(row=0, column=0, sticky="w", pady=3)
        self.gemini_voice_var = tk.StringVar(
            value=self.cfg.get("tts_gemini_voice") or DEFAULT_TTS_GEMINI_VOICE
        )
        voice_combo = ttk.Combobox(self.gemini_row, textvariable=self.gemini_voice_var, values=GEMINI_TTS_VOICES)
        voice_combo.grid(row=0, column=1, sticky="ew", pady=3)
        voice_combo.bind("<<ComboboxSelected>>", lambda e: self._save_settings())
        voice_combo.bind("<FocusOut>", lambda e: self._save_settings())
        ttk.Label(self.gemini_row, text="Model TTS").grid(row=1, column=0, sticky="w", pady=3)
        self.gemini_model_var = tk.StringVar(
            value=self.cfg.get("tts_gemini_model") or DEFAULT_TTS_GEMINI_MODEL
        )
        model_entry = ttk.Entry(self.gemini_row, textvariable=self.gemini_model_var)
        model_entry.grid(row=1, column=1, sticky="ew", pady=3)
        model_entry.bind("<FocusOut>", lambda e: self._save_settings())

        # ---------- KHỐI 2: tinh chỉnh âm thanh (thanh trượt mỏng, thẳng hàng) ----------
        tts_set = read_tts_settings(self.cfg)
        t_outer, tbox = make_card(c3, None, padding=(12, 10))
        t_outer.pack(fill="x", pady=(0, 8))
        card_title(
            tbox, "Tinh chỉnh âm thanh",
            "Độ ổn định: cao hơn = giọng đều, điềm tĩnh hơn; thấp hơn = nhiều biểu cảm.\n"
            "Tốc độ đọc: đổi bằng ffmpeg, giữ nguyên cao độ giọng.\n"
            "Ngắt nghỉ: đọc từng câu/mệnh đề rồi chèn khoảng lặng để đoạn văn không bị đọc một mạch "
            "(Gemini TTS chỉ ngắt theo câu để đỡ tốn quota).",
        )
        tune = ttk.Frame(tbox)
        tune.pack(fill="x")
        tune.columnconfigure(0, minsize=112)
        tune.columnconfigure(1, weight=1)
        tune.columnconfigure(2, minsize=56)
        self.stability_var = tk.DoubleVar(value=tts_set["stability"])
        self._make_slider(
            tune, 0, "Độ ổn định", self.stability_var, MIN_TTS_STABILITY, MAX_TTS_STABILITY,
            lambda v: f"{v:.1f}", step=0.1,
            hint="Giọng đọc ổn định đến đâu. Cao hơn = đều và điềm tĩnh hơn.",
        )
        self.speed_var = tk.DoubleVar(value=tts_set["speed"])
        self._make_slider(
            tune, 1, "Tốc độ đọc", self.speed_var, MIN_TTS_SPEED, MAX_TTS_SPEED,
            lambda v: f"{v:.2f}×", step=0.05,
            hint="Đổi tốc độ bằng ffmpeg (giữ nguyên cao độ giọng).",
        )
        self.autofit_var = tk.BooleanVar(value=tts_set["autofit"])
        ttk.Checkbutton(
            tune, text="Tự chỉnh tốc độ để khớp thời lượng mục tiêu", variable=self.autofit_var,
            command=self._on_slider_changed,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 2))
        ttk.Separator(tune, orient="horizontal").grid(row=3, column=0, columnspan=3, sticky="ew", pady=6)
        pause_row = ttk.Frame(tune)
        pause_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 2))
        self.pause_enabled_var = tk.BooleanVar(value=tts_set["pause_enabled"])
        ttk.Checkbutton(
            pause_row, text="Tự chèn ngắt nghỉ theo dấu câu", variable=self.pause_enabled_var,
            command=self._on_slider_changed,
        ).pack(side="left")
        self._info(
            pause_row,
            "Đọc từng câu/mệnh đề rồi chèn khoảng lặng, để đoạn văn không bị đọc một mạch. "
            "Gemini TTS chỉ ngắt theo câu (để đỡ tốn quota).",
        )
        self.pause_sentence_var = tk.DoubleVar(value=tts_set["pause_sentence"])
        self._make_slider(
            tune, 5, "Sau câu", self.pause_sentence_var, MIN_TTS_PAUSE, MAX_TTS_PAUSE_SENTENCE,
            lambda v: f"{v:.2f}s", step=0.05, hint="Khoảng lặng sau dấu  . ! ? …",
        )
        self.pause_comma_var = tk.DoubleVar(value=tts_set["pause_comma"])
        self._make_slider(
            tune, 6, "Sau dấu phẩy", self.pause_comma_var, MIN_TTS_PAUSE, MAX_TTS_PAUSE_COMMA,
            lambda v: f"{v:.2f}s", step=0.05, hint="Khoảng lặng sau dấu  , ; :",
        )

        # ---------- KHỐI 3: thư mục lưu audio (đường dẫn + nút trên cùng 1 hàng) ----------
        o_outer, obox = make_card(c3, None, padding=(12, 10))
        o_outer.pack(fill="x")
        card_title(obox, "Thư mục lưu audio", "Dùng chung với tab \"Ghép Audio vào Video\".")
        dir_row = ttk.Frame(obox)
        dir_row.pack(fill="x")
        self.outdir_var = tk.StringVar(value="")
        ttk.Entry(dir_row, textvariable=self.outdir_var, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(dir_row, text="Chọn...", style="Icon.TButton", width=7, command=self._choose_output_dir).pack(side="left", padx=(6, 0))
        self._icon_btn(dir_row, "📂", "Mở thư mục lưu audio", self._open_output_dir).pack(side="left", padx=(4, 0))
        self.out_info_var = tk.StringVar(value="")
        out_lbl = ttk.Label(obox, textvariable=self.out_info_var, style="Dim.TLabel", justify="left")
        out_lbl.pack(anchor="w", pady=(4, 0), fill="x")
        bind_wraplength(out_lbl, obox, margin=8, min_width=160)

        # ---- Tab Lịch sử: các file audio đã lưu từ tab này ----
        hist_box = ttk.Frame(history_tab)
        hist_box.pack(fill="both", expand=True)
        hist_btns = ttk.Frame(hist_box)
        hist_btns.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(hist_btns, text="▶  Phát", command=self._history_play).pack(side="left")
        ttk.Button(hist_btns, text="⬇  Tải về", command=self._history_download).pack(side="left", padx=(6, 0))
        self._icon_btn(hist_btns, "📁", "Mở thư mục chứa file", self._history_open_folder).pack(side="left", padx=(6, 0))
        self._icon_btn(hist_btns, "🗑", "Xóa lịch sử (không xóa file audio trên máy)", self._history_clear).pack(side="right")
        tree_wrap = ttk.Frame(hist_box)
        tree_wrap.pack(side="top", fill="both", expand=True)
        self.hist_tree = ttk.Treeview(
            tree_wrap, columns=("time", "file", "dur"), show="headings", selectmode="browse",
        )
        self.hist_tree.heading("time", text="Lúc", anchor="w")
        self.hist_tree.heading("file", text="File", anchor="w")
        self.hist_tree.heading("dur", text="Dài", anchor="w")
        self.hist_tree.column("time", width=100, minwidth=80, anchor="w", stretch=False)
        self.hist_tree.column("file", width=150, minwidth=80, anchor="w")
        self.hist_tree.column("dur", width=48, minwidth=40, anchor="w", stretch=False)
        hsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=hsb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        hsb.pack(side="left", fill="y")
        self.hist_tree.bind("<Double-1>", lambda e: self._history_play())
        self.hist_paths: dict[str, Path] = {}
        self._refresh_history()

        self._on_backend_changed(initial=True)
        self._refresh_out_info()
        self._on_brief_changed(save=False)

    def _build_result_card(self, parent):
        """Thẻ kết quả "Tạo giọng nói": thanh nghe (dạng sóng, tua, phát/dừng) + Tải về + đóng. Ẩn tới khi có kết quả."""
        self.result_outer, box = make_card(parent, None, padding=(12, 10))
        self.audio_bar = AudioBar(
            box, self.player, self._tmp, on_download=self._result_download,
            on_close=self._result_close, on_status=self.status_var.set,
        )
        self.audio_bar.pack(fill="x")

    # ==================================================== thanh trượt ==
    def _make_slider(
        self, parent, row: int, title: str, var: tk.DoubleVar, lo: float, hi: float,
        fmt: Callable[[float], str], step: float = 0.1, hint: str = "",
    ):
        """Một hàng thanh trượt trong lưới: nhãn (+ⓘ) | thanh mỏng | ô giá trị. Các hàng thẳng cột nhau."""
        lbl_box = ttk.Frame(parent)
        lbl_box.grid(row=row, column=0, sticky="w", pady=4)
        ttk.Label(lbl_box, text=title).pack(side="left")
        if hint:
            self._info(lbl_box, hint)
        badge = tk.Label(parent, text=fmt(var.get()), bg=theme.SEG_BG, fg=theme.FG, width=6, font=("", 9), pady=1)
        badge.grid(row=row, column=2, sticky="e")

        def changed(_value=None):
            if self._slider_busy:
                return
            self._slider_busy = True
            try:
                snapped = round(round(float(var.get()) / step) * step, 4)
                snapped = max(lo, min(hi, snapped))
                if abs(snapped - float(var.get())) > 1e-9:
                    var.set(snapped)
                badge.configure(text=fmt(snapped))
            finally:
                self._slider_busy = False
            self._on_slider_changed(save=False)

        scale = ttk.Scale(
            parent, from_=lo, to=hi, variable=var, orient="horizontal",
            style="Thin.Horizontal.TScale", command=changed,
        )
        scale.grid(row=row, column=1, sticky="ew", padx=8)
        scale.bind("<ButtonRelease-1>", lambda e: self._save_settings())
        return scale

    def _on_slider_changed(self, save: bool = True):
        if save:
            self._save_settings()
        self._on_brief_changed(save=False)

    def _on_brief_changed(self, save: bool = True):
        """Sản phẩm / thời lượng / phong cách đổi: lưu, bật-tắt ô KOC, cập nhật số từ mục tiêu."""
        if save:
            self._save_settings()
        koc_on = REVIEW_STYLE_OPTIONS.get(self.style_var.get()) == REVIEW_STYLE_KOC
        for e in (self.address_entry, self.slang_entry):
            e.configure(state="normal" if koc_on else "disabled")
        self._update_target_info()
        self._update_words_info()

    # ======================================================== cấu hình ==
    def _backend_kind(self) -> str:
        return TTS_BACKEND_OPTIONS.get(self.backend_var.get(), DEFAULT_TTS_BACKEND)

    def _on_backend_changed(self, initial: bool = False):
        kind = self._backend_kind()
        self.sample_row.pack_forget()
        self.gemini_row.pack_forget()
        self.install_row.pack_forget()
        anchor = self._backend_row
        if kind == TTS_BACKEND_VIENEU:
            self._refresh_install_state()
            self.install_row.pack(fill="x", after=anchor)
            self.sample_row.pack(fill="x", after=self.install_row)
            self._backend_tip.text = (
                "VieNeu-TTS chạy trên máy (Python 3.10+; bấm \"Cài VieNeu\" bên dưới nếu chưa có). Có file giọng mẫu thì "
                "nhân bản giọng đó (khoảng 3–8 giây, rõ tiếng, không nhạc nền); để trống thì dùng giọng mặc định."
            )
        else:
            self.gemini_row.pack(fill="x", after=anchor)
            self._backend_tip.text = (
                "Gemini TTS không dùng giọng mẫu — chọn một giọng dựng sẵn. Dùng chung Gemini API Key "
                "trong Cài đặt (có thể tốn quota)."
            )
        if not initial:
            self._save_settings()
        self._refresh_out_info()

    def _choose_voice_sample(self):
        start = Path(self.sample_var.get()).parent if self.sample_var.get().strip() else Path.home()
        chosen = filedialog.askopenfilename(
            title="Chọn file giọng mẫu", parent=self,
            initialdir=str(start) if start.is_dir() else str(Path.home()),
            filetypes=[("Audio", " ".join(f"*{e}" for e in sorted(AUDIO_EXTENSIONS))), ("Tất cả", "*.*")],
        )
        if chosen:
            self._set_voice_sample(chosen)

    def _set_voice_sample(self, path: str):
        self.sample_var.set(path)
        self._save_settings()
        self._sync_saved_combo()

    def _refresh_saved_voices(self):
        self.saved_combo.configure(values=vlib.voice_names(self.cfg))
        self._sync_saved_combo()

    def _sync_saved_combo(self):
        """Ô 'Giọng đã lưu' hiện tên giọng nếu file đang chọn chính là giọng đã lưu."""
        v = vlib.find_voice_by_path(self.cfg, self.sample_var.get())
        self.saved_voice_var.set(v["name"] if v else "")

    def _on_saved_voice_selected(self):
        v = vlib.find_voice(self.cfg, self.saved_voice_var.get())
        if not v:
            return
        if not Path(v["path"]).is_file():
            messagebox.showerror(
                APP_TITLE, f"File của giọng \"{v['name']}\" không còn:\n{v['path']}\n"
                "Hãy xóa giọng này và lưu lại.", parent=self)
            self._sync_saved_combo()
            return
        self._set_voice_sample(v["path"])
        self.status_var.set(f"Đã chọn giọng \"{v['name']}\".")

    def _save_current_voice(self):
        sample = self.sample_var.get().strip()
        if not sample or not Path(sample).is_file():
            messagebox.showinfo(APP_TITLE, "Hãy chọn một file giọng mẫu hợp lệ trước khi lưu.", parent=self)
            return
        current = vlib.find_voice_by_path(self.cfg, sample)
        name = simpledialog.askstring(
            APP_TITLE, "Đặt tên cho giọng này (trùng tên sẽ thay giọng cũ):",
            initialvalue=current["name"] if current else Path(sample).stem[:40], parent=self,
        )
        if name is None:
            return
        existing = vlib.find_voice(self.cfg, name)
        if existing and existing["path"] != sample and not messagebox.askyesno(
            APP_TITLE, f"Đã có giọng \"{existing['name']}\". Thay bằng file này?", parent=self
        ):
            return
        try:
            entry = vlib.save_voice(self.cfg, name, sample)
        except vlib.VoiceLibraryError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return
        self.sample_var.set(entry["path"])      # từ giờ dùng bản sao của app
        self._save_settings()
        self._refresh_saved_voices()
        self.status_var.set(f"Đã lưu giọng \"{entry['name']}\" (bản sao nằm trong thư mục riêng của app).")

    def _delete_saved_voice(self):
        name = self.saved_voice_var.get().strip()
        if not name:
            messagebox.showinfo(APP_TITLE, "Hãy chọn một giọng trong ô \"Giọng đã lưu\" để xóa.", parent=self)
            return
        if not messagebox.askyesno(APP_TITLE, f"Xóa giọng \"{name}\"?", parent=self):
            return
        v = vlib.find_voice(self.cfg, name)
        vlib.delete_voice(self.cfg, name)
        if v and self.sample_var.get().strip() == v["path"]:
            self.sample_var.set("")             # file bản sao đã bị xóa
        self._save_settings()
        self._refresh_saved_voices()
        self.status_var.set(f"Đã xóa giọng \"{name}\".")

    # ------------------------------------------------ thư mục lưu audio --
    def _choose_output_dir(self):
        current = self._get_audio_dir()
        chosen = filedialog.askdirectory(
            parent=self, title="Chọn thư mục lưu audio (dùng chung với tab Ghép Audio vào Video)",
            initialdir=str(current) if current and current.is_dir() else str(Path.home()),
        )
        if chosen:
            self._set_audio_dir(Path(chosen))
            self._refresh_out_info()
            self.status_var.set(f"Audio sẽ được lưu vào: {chosen}")

    def _open_output_dir(self):
        d = self._get_audio_dir()
        if not d:
            messagebox.showinfo(APP_TITLE, "Chưa chọn thư mục lưu audio.", parent=self)
            return
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không mở được thư mục:\n{exc}", parent=self)
            return
        self._open_path(d)

    def _open_path(self, path: Path):
        """Mở file/thư mục bằng chương trình mặc định của hệ điều hành."""
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không mở được:\n{exc}", parent=self)

    def _save_settings(self):
        self.cfg["review_style"] = REVIEW_STYLE_OPTIONS.get(self.style_var.get(), REVIEW_STYLE_KOC)
        self.cfg["review_product"] = self.product_var.get().strip()
        try:
            self.cfg["review_duration"] = max(
                MIN_REVIEW_DURATION, min(MAX_REVIEW_DURATION, int(float(self.duration_var.get())))
            )
        except (TypeError, ValueError):
            pass            # ô đang gõ dở -> giữ giá trị đã lưu
        self.cfg["review_use_video_len"] = bool(self.use_len_var.get())
        self.cfg["review_address_terms"] = self.address_var.get().strip()
        self.cfg["review_slang"] = self.slang_var.get().strip()
        self.cfg["review_extra"] = self.extra_var.get().strip()
        self.cfg["tts_stability"] = round(float(self.stability_var.get()), 1)
        self.cfg["tts_speed"] = round(float(self.speed_var.get()), 2)
        self.cfg["tts_autofit"] = bool(self.autofit_var.get())
        self.cfg["tts_pause_enabled"] = bool(self.pause_enabled_var.get())
        self.cfg["tts_pause_sentence"] = round(float(self.pause_sentence_var.get()), 2)
        self.cfg["tts_pause_comma"] = round(float(self.pause_comma_var.get()), 2)
        self.cfg["review_voice_sample"] = self.sample_var.get().strip()
        self.cfg["tts_backend"] = self._backend_kind()
        self.cfg["tts_gemini_voice"] = self.gemini_voice_var.get().strip()
        self.cfg["tts_gemini_model"] = self.gemini_model_var.get().strip()
        self.cfg["review_video_source"] = "folder" if self.source_var.get() == SOURCE_FOLDER else "history"
        save_config(self.cfg)

    # ============================================================ video ==
    def _on_source_changed(self):
        self._save_settings()
        self.refresh_video_list()

    def _choose_video_dir(self):
        chosen = filedialog.askdirectory(
            parent=self, initialdir=str(self._video_dir or Path.home()),
        )
        if chosen:
            self._video_dir = Path(chosen)
            self.cfg["review_video_dir"] = chosen
            self.source_var.set(SOURCE_FOLDER)
            self._save_settings()
            self.refresh_video_list()

    def _choose_video_file(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        chosen = filedialog.askopenfilename(
            title="Chọn video", parent=self,
            initialdir=str(self._video_dir or Path.home()),
            filetypes=[("Video", exts), ("Tất cả", "*.*")],
        )
        if chosen:
            path = Path(chosen)
            self._select_video(path)
            self._scan_durations([path], self._dur_gen)
            self._autofill_duration(path)

    def on_tab_shown(self):
        """Khi chuyển sang tab này: làm mới danh sách + dòng 'Sẽ lưu' (thư mục Audio có thể đã đổi)."""
        if not self._busy:
            self.refresh_video_list()
        self._refresh_out_info()

    def refresh_video_list(self):
        self.tree.delete(*self.tree.get_children())
        self.video_by_iid.clear()
        from_folder = self.source_var.get() == SOURCE_FOLDER
        self.choose_dir_btn.state(["!disabled"])
        if from_folder:
            files = list_media_files(self._video_dir, VIDEO_EXTENSIONS)
            for f in files:
                self._add_row(f.name, str(self._video_dir.name), f)
            if not files:
                self.selected_var.set(
                    "Chưa có video — bấm \"Chọn thư mục...\"." if not self._video_dir
                    else f"Không thấy video nào trong {self._video_dir}."
                )
        else:
            records = self.history.list_recent(300)
            shown = 0
            for r in records:
                if not r.file_exists:
                    continue
                title = r.title or Path(r.file_path).stem
                self._add_row(f"{title}  —  {Path(r.file_path).name}", r.downloaded_at_text, Path(r.file_path))
                shown += 1
            if not shown:
                self.selected_var.set(
                    "Lịch sử tải chưa có video nào còn trên máy — hãy tải video ở tab \"Tải video\" "
                    "hoặc chọn nguồn \"Thư mục video\"."
                )
        # giữ lại lựa chọn trước đó nếu vẫn còn trong danh sách
        if self.video_path:
            for iid, p in self.video_by_iid.items():
                if p == self.video_path:
                    self.tree.selection_set(iid)
                    break
        self._start_duration_scan()

    def _add_row(self, name: str, info: str, path: Path):
        iid = self.tree.insert("", "end", values=(name, self._dur_text(path), info))
        self.video_by_iid[iid] = path

    # ---------------------------------------------- thời lượng video --
    def _cached_duration(self, path: Path) -> Optional[float]:
        hit = self._dur_cache.get(str(path))
        if not hit:
            return None
        try:
            return hit[1] if hit[0] == path.stat().st_mtime else None
        except OSError:
            return None

    def _dur_text(self, path: Path) -> str:
        secs = self._cached_duration(path)
        if secs is not None:
            return format_clock(secs)
        return "…" if self._get_ffprobe() else "—"

    def _start_duration_scan(self):
        """Đọc thời lượng các video trong danh sách (chưa có trong bộ nhớ đệm) bằng ffprobe ở luồng nền."""
        self._dur_gen += 1
        self._scan_durations(list(self.video_by_iid.values()), self._dur_gen)

    def _scan_durations(self, paths: list, gen: int):
        ffprobe = self._get_ffprobe()
        todo = [p for p in paths if self._cached_duration(p) is None]
        if not ffprobe or not todo:
            return

        def work():
            for path in todo:
                if gen != self._dur_gen:
                    return                      # danh sách đã đổi -> bỏ
                try:
                    mtime = path.stat().st_mtime
                    secs = probe_media_info(ffprobe, path).duration
                except Exception:  # noqa: BLE001 - file hỏng/mất: để dấu "—"
                    self._dur_queue.put((gen, path, None, 0.0))
                    continue
                self._dur_queue.put((gen, path, secs, mtime))

        threading.Thread(target=work, daemon=True).start()

    def _drain_durations(self):
        try:
            while True:
                gen, path, secs, mtime = self._dur_queue.get_nowait()
                if secs is not None:
                    self._dur_cache[str(path)] = (mtime, secs)
                if gen != self._dur_gen:
                    continue
                text = format_clock(secs) if secs is not None else "—"
                for iid, p in self.video_by_iid.items():
                    if p == path and self.tree.exists(iid):
                        self.tree.set(iid, "dur", text)
                if self.video_path == path:
                    self._select_video(path, from_tree=True)     # cập nhật dòng "Đã chọn"
                    if self._autofill_pending == path:
                        self._autofill_duration(path)
        except queue.Empty:
            pass

    def _on_tree_select(self):
        sel = self.tree.selection()
        if sel and sel[0] in self.video_by_iid:
            path = self.video_by_iid[sel[0]]
            changed = path != self.video_path     # làm mới danh sách chọn lại đúng video cũ -> không ghi đè số giây đã sửa tay
            self._select_video(path, from_tree=True)
            if changed:
                self._autofill_duration(path)

    def _autofill_duration(self, path: Path):
        """Chọn video -> ô "Thời lượng (s)" = độ dài video (làm tròn, kẹp trong khoảng cho phép)."""
        secs = self._cached_duration(path)
        if secs is None:
            self._autofill_pending = path if self._get_ffprobe() else None
            return
        self._autofill_pending = None
        value = max(MIN_REVIEW_DURATION, min(MAX_REVIEW_DURATION, int(round(secs))))
        self.duration_var.set(str(value))
        self._last_target = value
        self._on_brief_changed()

    def _select_video(self, path: Path, from_tree: bool = False):
        self.video_path = path
        secs = self._cached_duration(path)
        self.selected_var.set(
            f"Đã chọn: {path.name}" + (f"  ·  {format_clock(secs)}" if secs is not None else "")
        )
        self._refresh_out_info()
        if not from_tree:
            self.tree.selection_remove(*self.tree.selection())

    def _refresh_out_info(self):
        out_dir = self._get_audio_dir()
        self.outdir_var.set(str(out_dir) if out_dir else "")
        if not self.video_path:
            self.out_info_var.set("")
            return
        ext = "mp3" if self._get_ffmpeg() else "wav (chưa có ffmpeg nên không đổi được sang mp3)"
        where = str(out_dir) if out_dir else "(chưa chọn — sẽ hỏi khi lưu)"
        self.out_info_var.set(f"Khi tải về sẽ lưu: {self.video_path.stem}.{ext}  →  {where}")

    # ========================================================== kịch bản ==
    def _on_text_modified(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._update_words_info()

    def _timing(self) -> dict:
        """Thông số thời gian đang chọn: tốc độ/ngắt nghỉ DÙNG ĐỂ ƯỚC LƯỢNG và thời lượng mục tiêu.
        Bật 'tự khớp' thì ước lượng theo tốc độ 1.0 (vì tốc độ sẽ được tự chỉnh cho vừa)."""
        tts_set = read_tts_settings(self.cfg)
        brief = read_koc_brief(self.cfg)
        pauses = tts_set["pause_enabled"]
        target = brief["duration"]
        if brief["use_video_len"] and self._last_target:
            target = self._last_target
        return {
            "wps": read_review_settings(self.cfg)["words_per_second"],
            "speed": 1.0 if tts_set["autofit"] else tts_set["speed"],
            "ps": tts_set["pause_sentence"] if pauses else 0.0,
            "pc": tts_set["pause_comma"] if pauses else 0.0,
            "target": target,
            "autofit": tts_set["autofit"],
        }

    def _update_target_info(self):
        t = self._timing()
        if read_koc_brief(self.cfg)["use_video_len"] and not self._last_target:
            self.target_info_var.set("Thời lượng lấy theo độ dài video")
            return
        words = estimate_target_words(t["target"], t["wps"], t["speed"], t["ps"], t["pc"])
        self.target_info_var.set(f"Mục tiêu {t['target']} giây  ≈  {words} từ")

    def _update_words_info(self):
        text = self.text.get("1.0", "end")
        n = count_words(text)
        t = self._timing()
        secs = estimate_read_seconds(text, t["wps"], t["speed"], t["ps"], t["pc"])
        chars = len(text.strip())
        msg = f"{chars:,} ký tự  ·  {n} từ  ·  ~{secs:.0f}s / {t['target']}s"
        if n:
            diff = secs - t["target"]
            if t["autofit"]:
                msg += "  ·  tự khớp"
            elif abs(diff) <= max(1.0, t["target"] * 0.1):
                msg += "  ·  ✓"
            else:
                msg += f"  ·  ⚠ {'dài' if diff > 0 else 'ngắn'} {abs(diff):.0f}s"
        self.words_info_var.set(msg)

    def _script_text(self) -> str:
        return self.text.get("1.0", "end").strip()

    def _set_script(self, text: str):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_reset()      # không cho Ctrl+Z quay về kịch bản cũ/trống
        self._update_words_info()

    def on_generate(self):
        if self._busy:
            return
        if not self.video_path or not self.video_path.is_file():
            messagebox.showinfo(APP_TITLE, "Hãy chọn một video còn tồn tại trước.", parent=self)
            return
        api_key = str(self.cfg.get("gemini_api_key", "") or "").strip()
        if not api_key:
            messagebox.showinfo(APP_TITLE, "Chưa có Gemini API Key. Vào Cài đặt → Cấu hình Gemini API.", parent=self)
            return
        if self._script_text() and not messagebox.askyesno(
            APP_TITLE, "Ô kịch bản đang có nội dung. Thay bằng kịch bản mới?", parent=self
        ):
            return
        self._save_settings()

        settings = read_review_settings(self.cfg)
        brief = read_koc_brief(self.cfg)
        use_koc = settings["style"] == REVIEW_STYLE_KOC
        # KOC: hướng dẫn phong cách đã nằm trong system instruction -> chỉ gửi ghi chú của người dùng.
        extra = "\n".join(
            x for x in (None if use_koc else settings["style_hint"], self.cfg.get("review_extra", "")) if x
        )
        model = (self.cfg.get("review_script_model") or self.cfg.get("gemini_model") or DEFAULT_GEMINI_MODEL)
        fallback = self.cfg.get("gemini_fallback_model", DEFAULT_GEMINI_FALLBACK_MODEL)
        ffmpeg, ffprobe = self._get_ffmpeg(), self._get_ffprobe()
        video = self.video_path
        t = self._timing()

        job, stop = self._begin_job("Đang chuẩn bị video...")

        def work():
            try:
                seconds = brief["duration"]
                if brief["use_video_len"] and ffprobe:
                    try:
                        real = probe_media_info(ffprobe, video).duration
                        seconds = max(MIN_REVIEW_DURATION, min(MAX_REVIEW_DURATION, int(round(real))))
                    except Exception as exc:  # noqa: BLE001 - không đọc được thời lượng thì dùng số giây đã nhập
                        log.warning("Không đọc được thời lượng video: %s", exc)
                koc = KocBrief.from_duration(
                    brief["product"], seconds, address_terms=brief["address_terms"],
                    slang=brief["slang"], words_per_second=t["wps"], speed=t["speed"],
                    pause_sentence=t["ps"], pause_comma=t["pc"],
                )
                res = generate_review_script(
                    video, api_key, ffmpeg_path=ffmpeg,
                    max_words=koc.max_words, model=model, fallback_model=fallback,
                    extra_instruction=extra, koc=koc if use_koc else None, stop_flag=stop,
                    progress_cb=lambda m: self.task_queue.put((job, "status", m)),
                )
                self.task_queue.put((job, "script", (res, seconds)))
            except Exception as exc:  # noqa: BLE001 - mọi lỗi đều báo ra giao diện
                self.task_queue.put((job, "error", ("kịch bản", exc)))

        threading.Thread(target=work, daemon=True).start()

    # ============================================================ giọng ==
    def _prepare_tts(self, text: str):
        """Chuẩn hoá lời đọc (số -> chữ phòng khi người dùng gõ thêm), dựng backend và tuỳ chọn
        đọc (ngắt nghỉ, tốc độ, khớp thời lượng)."""
        text = numbers_to_vietnamese(text)
        self._save_settings()
        opts = TTSOptions.from_cfg(self.cfg, target_seconds=self._timing()["target"])
        return make_backend(self._backend_kind(), self.cfg), text, opts

    def on_preview(self):
        if self._busy:
            return
        script = self._script_text()
        sentence = first_sentence(script)
        if not sentence:
            messagebox.showinfo(APP_TITLE, "Ô kịch bản đang trống — hãy tạo hoặc gõ kịch bản trước.", parent=self)
            return
        try:
            backend, sentence, opts = self._prepare_tts(sentence)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return
        opts.autofit = False        # nghe thử 1 câu: chỉ áp tốc độ đã chọn, không tự khớp thời lượng
        ffmpeg = self._get_ffmpeg()
        job, stop = self._begin_job(f"Đang đọc thử 1 câu bằng {backend.label}...")
        self.player.stop()

        def work():
            try:
                wav = backend.synthesize(sentence, stop)
                if stop():
                    raise TTSCancelled("Đã dừng.")
                wav, _ = apply_speed(wav, opts, ffmpeg)
                path = self._preview_path()
                path.write_bytes(wav)
                self.task_queue.put((job, "preview", (path, wav_duration_seconds(wav))))
            except Exception as exc:  # noqa: BLE001
                self.task_queue.put((job, "error", ("nghe thử", exc)))

        threading.Thread(target=work, daemon=True).start()

    def _tmp(self) -> Path:
        if self._tmp_dir is None or not self._tmp_dir.is_dir():
            self._tmp_dir = Path(tempfile.mkdtemp(prefix="dvm_tts_preview_"))
        return self._tmp_dir

    def _preview_path(self) -> Path:
        return self._tmp() / f"preview_{self._job_id}.wav"

    def on_read(self):
        if self._busy:
            return
        script = self._script_text()
        if not self.video_path:
            messagebox.showinfo(APP_TITLE, "Hãy chọn video trước (để đặt tên file audio trùng tên video).", parent=self)
            return
        if not script:
            messagebox.showinfo(APP_TITLE, "Ô kịch bản đang trống — hãy tạo hoặc gõ kịch bản trước.", parent=self)
            return

        ffmpeg = self._get_ffmpeg()
        stem = self.video_path.stem          # tên file audio khi bấm Tải về (trùng tên video)
        if not ffmpeg:
            messagebox.showinfo(
                APP_TITLE,
                "Chưa tìm thấy ffmpeg nên sẽ KHÔNG đổi được tốc độ đọc / tự khớp thời lượng, và khi bấm "
                "Tải về file sẽ lưu dạng .wav thay vì .mp3. Cài ffmpeg để dùng đủ tính năng.",
                parent=self,
            )
        try:
            backend, text, opts = self._prepare_tts(script)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return

        job, stop = self._begin_job(f"Đang tạo giọng nói bằng {backend.label}...")
        self.player.stop()
        words = count_words(text)

        def work():
            try:
                wav = synthesize_with_options(
                    backend, text, opts, stop,
                    progress_cb=lambda m: self.task_queue.put((job, "status", m)),
                )
                if stop():
                    raise TTSCancelled("Đã dừng.")
                self.task_queue.put((job, "status", "Đang chỉnh tốc độ..."))
                wav, used = apply_speed(wav, opts, ffmpeg)
                info = {
                    "stem": stem, "words": words, "speed": used, "target": opts.target_seconds,
                    "wanted_speed": opts.autofit or abs(opts.speed - 1.0) >= 0.01,
                    "wav": wav, "peaks": wav_peaks(wav),
                    "wps_measured": measure_words_per_second(
                        text, wav_duration_seconds(wav), used,
                        opts.pause_sentence if opts.pause_enabled else 0.0,
                        opts.pause_comma if opts.pause_enabled else 0.0,
                    ),
                }
                self.task_queue.put((job, "generated", (wav_duration_seconds(wav), info)))
            except Exception as exc:  # noqa: BLE001
                self.task_queue.put((job, "error", ("đọc giọng", exc)))

        threading.Thread(target=work, daemon=True).start()

    # ============================================================ lịch sử ==
    def _history_items(self) -> list:
        items = self.cfg.get("tts_history")
        return [it for it in items if isinstance(it, dict) and it.get("path")] if isinstance(items, list) else []

    def _add_history(self, path: Path, seconds: float, words: int):
        items = [it for it in self._history_items() if it.get("path") != str(path)]
        items.insert(0, {
            "path": str(path), "time": time.strftime("%d/%m %H:%M"),
            "seconds": round(float(seconds), 1), "words": int(words),
        })
        self.cfg["tts_history"] = items[:TTS_HISTORY_MAX]
        save_config(self.cfg)
        self._refresh_history()

    def _refresh_history(self):
        self.hist_tree.delete(*self.hist_tree.get_children())
        self.hist_paths.clear()
        for it in self._history_items():
            path = Path(it["path"])
            name = path.name if path.is_file() else f"{path.name} (đã mất)"
            iid = self.hist_tree.insert(
                "", "end", values=(it.get("time", ""), name, f"{float(it.get('seconds', 0)):.0f}s"),
            )
            self.hist_paths[iid] = path

    def _history_selected(self) -> Optional[Path]:
        sel = self.hist_tree.selection()
        if not sel or sel[0] not in self.hist_paths:
            messagebox.showinfo(APP_TITLE, "Hãy chọn một dòng trong lịch sử.", parent=self)
            return None
        return self.hist_paths[sel[0]]

    def _play_file(self, path: Path):
        if not path.is_file():
            messagebox.showinfo(APP_TITLE, f"File không còn:\n{path}", parent=self)
            return
        self.player.stop()
        if path.suffix.lower() == ".wav" and self.player.play(path):
            self.status_var.set(f"Đang phát {path.name}.")
        else:
            self._open_path(path)       # mp3: mở bằng trình phát mặc định của hệ điều hành

    def _history_play(self):
        path = self._history_selected()
        if path:
            self._play_file(path)

    def _history_download(self):
        path = self._history_selected()
        if path:
            self._download_audio(path)

    # ------------------------------------------- audio vừa tạo + tải về --
    def _voice_title(self) -> str:
        """Tên giọng hiển thị trên thanh nghe: giọng đã lưu > file mẫu > giọng Gemini/mặc định."""
        if self._backend_kind() == TTS_BACKEND_VIENEU:
            sample = self.sample_var.get().strip()
            if sample:
                v = vlib.find_voice_by_path(self.cfg, sample)
                return v["name"] if v else Path(sample).stem
            return "Giọng mặc định"
        return f"Gemini · {self.gemini_voice_var.get().strip() or DEFAULT_TTS_GEMINI_VOICE}"

    def _calibrate_wps(self, measured: Optional[float]) -> str:
        """Hiệu chỉnh số từ/giây theo lần đọc thực tế để số từ mục tiêu lần sau sát hơn."""
        if not measured:
            return ""
        old = read_review_settings(self.cfg)["words_per_second"]
        samples = int(self.cfg.get("review_wps_samples", 0) or 0)
        new = round(blend_words_per_second(old, samples, measured), 2)
        self.cfg["review_words_per_second"] = new
        self.cfg["review_wps_samples"] = samples + 1
        save_config(self.cfg)
        self._on_brief_changed(save=False)          # cập nhật "Mục tiêu ... ≈ ... từ"
        return f"đã hiệu chỉnh ước lượng {new:.1f} từ/giây"

    def _show_generated(self, info: dict, seconds: float):
        """Hiện thanh nghe cho giọng vừa tạo. CHƯA lưu file — chỉ lưu khi bấm Tải về."""
        self._result = {
            "wav": info["wav"], "stem": info["stem"], "secs": float(seconds),
            "words": int(info.get("words", 0)), "saved": None,
        }
        self._voice_name = self._voice_title()
        self.audio_bar.load(info["wav"], f"{self._voice_name}  ·  chưa lưu", info.get("peaks"))
        if not self.result_outer.winfo_ismapped():
            self.result_outer.pack(side="bottom", fill="x", pady=(6, 0), before=self._right_tabs)

    def _result_close(self):
        self.audio_bar.stop()
        self.result_outer.pack_forget()
        self._result = None

    def _result_download(self):
        """Tải về: LƯU giọng vừa tạo thành <tên video>.mp3 vào thư mục lưu audio."""
        res = self._result
        if not res:
            messagebox.showinfo(APP_TITLE, "Chưa có giọng nói nào để tải về — hãy bấm \"Tạo giọng nói\" trước.", parent=self)
            return
        out_dir = self._get_audio_dir()
        if out_dir is None:
            chosen = filedialog.askdirectory(
                parent=self, title="Chọn thư mục lưu audio (dùng chung với tab Ghép Audio vào Video)",
                initialdir=str(Path.home()),
            )
            if not chosen:
                return
            out_dir = Path(chosen)
            self._set_audio_dir(out_dir)
            self._refresh_out_info()
        ffmpeg = self._get_ffmpeg()
        target = out_dir / f"{res['stem']}.{'mp3' if ffmpeg else 'wav'}"
        if target.exists() and not messagebox.askyesno(
            APP_TITLE, f"File '{target.name}' đã có trong thư mục. Ghi đè?", parent=self
        ):
            return
        self.status_var.set("Đang lưu file audio...")
        self.update_idletasks()
        try:
            saved = save_audio_file(res["wav"], out_dir, res["stem"], ffmpeg)
        except Exception as exc:  # noqa: BLE001 - TTSError hoặc lỗi ghi file
            log.error("Lỗi lưu audio: %s", exc)
            self.status_var.set(f"Lỗi khi lưu: {exc}")
            messagebox.showerror(APP_TITLE, f"Không lưu được file audio:\n{exc}", parent=self)
            return
        res["saved"] = saved
        self._last_audio = saved
        self._add_history(saved, res["secs"], res["words"])
        self.audio_bar.title_var.set(f"{self._voice_name}  ·  đã lưu")
        self.status_var.set(f"Đã lưu {saved.name}  →  {saved.parent}")

    def _download_audio(self, path: Optional[Path]):
        """Tải về = chép file audio ra nơi người dùng chọn (mặc định thư mục Downloads)."""
        if not path or not path.is_file():
            messagebox.showinfo(APP_TITLE, "Chưa có file audio để tải về (file đã mất hoặc chưa tạo).", parent=self)
            return
        start = Path.home() / "Downloads"
        if not start.is_dir():
            start = Path.home()
        ext = path.suffix.lower()
        dest = filedialog.asksaveasfilename(
            parent=self, title="Tải audio về...", initialdir=str(start), initialfile=path.name,
            defaultextension=ext, filetypes=[("Audio", f"*{ext}"), ("Tất cả", "*.*")],
        )
        if not dest:
            return
        dest = Path(dest)
        try:
            if dest.exists() and dest.resolve() == path.resolve():
                self.status_var.set("File đã nằm sẵn ở vị trí đó.")
                return
            shutil.copy2(path, dest)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Không tải về được:\n{exc}", parent=self)
            return
        self.status_var.set(f"Đã tải về {dest.name}  →  {dest.parent}")

    def _history_open_folder(self):
        path = self._history_selected()
        if path:
            self._open_path(path.parent if path.parent.is_dir() else path)

    def _history_clear(self):
        if not self._history_items():
            return
        if messagebox.askyesno(
            APP_TITLE, "Xóa toàn bộ lịch sử? (Không xóa các file audio trên máy.)", parent=self
        ):
            self.cfg["tts_history"] = []
            save_config(self.cfg)
            self._refresh_history()

    # ======================================================= cài VieNeu ==
    def _refresh_install_state(self):
        """Cập nhật dòng trạng thái + nhãn nút theo việc đã cài VieNeu hay chưa."""
        if self._installing:
            return
        if vinst.is_installed():
            ver = vinst.installed_version()
            self.install_status_var.set(f"✓ Đã cài{' ' + ver if ver else ''}")
            self.install_btn.configure(text="⟳ Nâng cấp")
        else:
            if vinst.python_ok() or getattr(sys, "frozen", False):
                self.install_status_var.set("Chưa cài")
            else:
                self.install_status_var.set(
                    f"Chưa cài — cần Python {vinst.MIN_PYTHON[0]}.{vinst.MIN_PYTHON[1]}+"
                )
            self.install_btn.configure(text="⬇ Cài VieNeu")
        self.install_btn.state(["!disabled"])

    def on_install_vieneu(self):
        if self._installing:
            return
        upgrade = vinst.is_installed()
        if not upgrade:
            if not messagebox.askyesno(
                APP_TITLE,
                "Cài thư viện VieNeu-TTS bằng pip?\n\n"
                "• Cần Internet, dung lượng tải có thể vài trăm MB và mất vài phút.\n"
                "• Lần đọc giọng đầu tiên sẽ tải thêm model nên hơi lâu.\n"
                "• Trong lúc cài, giao diện vẫn dùng bình thường.",
                parent=self,
            ):
                return
        self._installing = True
        self.install_btn.state(["disabled"])
        self.install_status_var.set("Đang cài...")
        self.status_var.set("Đang cài VieNeu... (bạn vẫn có thể làm việc khác)")
        vinst.install_vieneu_in_background(
            on_progress=lambda m: self._install_queue.put(("progress", m)),
            on_done=lambda ok, m: self._install_queue.put(("done", (ok, m))),
            upgrade=upgrade,
        )

    def _drain_install(self):
        try:
            while True:
                kind, payload = self._install_queue.get_nowait()
                if kind == "progress":
                    self.install_status_var.set("Đang cài...")
                    self.status_var.set(f"Cài VieNeu: {payload}")
                elif kind == "done":
                    ok, msg = payload
                    self._installing = False
                    self._refresh_install_state()
                    self.status_var.set(msg)
                    if ok:
                        log.info("Cài VieNeu: %s", msg)
                        messagebox.showinfo(APP_TITLE, msg, parent=self)
                    else:
                        log.error("Cài VieNeu thất bại: %s", msg)
                        messagebox.showerror(APP_TITLE, msg, parent=self)
        except queue.Empty:
            pass

    # ===================================================== điều phối job ==
    def _begin_job(self, status: str):
        """Bắt đầu 1 việc nền: khoá nút, bật thanh tiến độ. Trả (job_id, stop_fn)."""
        self._job_id += 1
        job = self._job_id
        self._cancel = threading.Event()
        cancel = self._cancel
        self._set_busy(True)
        self.status_var.set(status)
        return job, cancel.is_set

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = ["disabled"] if busy else ["!disabled"]
        for b in (self.gen_btn, self.preview_btn, self.read_btn):
            b.state(state)
        self.stop_btn.state(["!disabled"] if busy else ["disabled"])
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def on_stop(self):
        """Dừng việc đang chạy: đặt cờ dừng, bỏ kết quả của job này, mở lại nút ngay.
        (Một lời gọi mạng/engine đang dở không ngắt được giữa chừng nên luồng nền chạy
        nốt rồi tự bỏ kết quả.)"""
        self._cancel.set()
        self._job_id += 1          # mọi thông điệp của job cũ bị bỏ qua
        self.player.stop()
        if self._busy:
            self._set_busy(False)
            self.status_var.set("Đã dừng.")

    def _poll_queue(self):
        self._drain_durations()
        self._drain_install()
        try:
            while True:
                job, kind, payload = self.task_queue.get_nowait()
                if job != self._job_id:
                    continue                       # kết quả của việc đã dừng
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "script":
                    self._set_busy(False)
                    res, seconds = payload
                    self._last_target = seconds
                    self._set_script(res.text)
                    self._update_target_info()
                    note = " (đã cắt bớt cho vừa số từ tối đa)" if res.truncated else ""
                    self.status_var.set(
                        f"Đã tạo kịch bản {res.word_count} từ cho {seconds} giây{note}. "
                        "Sửa nếu cần rồi bấm \"Tạo giọng nói\"."
                    )
                elif kind == "preview":
                    self._set_busy(False)
                    path, secs = payload
                    if self.player.play(path):
                        self.status_var.set(f"Đang phát thử ({secs:.1f} giây).")
                    else:
                        self.status_var.set(f"Không phát được âm thanh tự động; file nghe thử: {path}")
                elif kind == "generated":
                    self._set_busy(False)
                    secs, info = payload
                    parts = [f"{secs:.1f} giây"]
                    if info.get("target"):
                        parts.append(f"mục tiêu {info['target']} giây")
                    if info.get("speed", 1.0) != 1.0:
                        parts.append(f"tốc độ {info['speed']:.2f}×")
                    elif info.get("wanted_speed"):
                        parts.append("chưa đổi được tốc độ vì thiếu ffmpeg")
                    self._show_generated(info, secs)
                    note = self._calibrate_wps(info.get("wps_measured"))
                    if note:
                        parts.append(note)
                    self.status_var.set(
                        f"Đã tạo giọng nói ({', '.join(parts)}). Nghe thử, ưng thì bấm \"Tải về\" để lưu vào thư mục."
                    )
                elif kind == "error":
                    self._set_busy(False)
                    what, exc = payload
                    if isinstance(exc, TTSCancelled):
                        self.status_var.set("Đã dừng.")
                    else:
                        log.error("Lỗi %s: %s", what, exc)
                        self.status_var.set(f"Lỗi khi {what}: {exc}")
                        if "Chưa cài VieNeu" in str(exc) and not self._installing:
                            if messagebox.askyesno(
                                APP_TITLE, "Chưa cài VieNeu-TTS. Cài ngay bây giờ?", parent=self,
                            ):
                                self.on_install_vieneu()
                        else:
                            messagebox.showerror(APP_TITLE, f"Lỗi khi {what}:\n{exc}", parent=self)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    # =========================================================== thoát ==
    def shutdown(self):
        self._cancel.set()
        self.player.stop()
        if self._tmp_dir and self._tmp_dir.is_dir():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
