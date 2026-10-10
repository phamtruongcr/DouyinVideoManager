"""
gui.py
======
Cửa sổ chính của ứng dụng (class DouyinApp), gồm 2 TAB:
  1. "Tải video": nạp danh sách video (kênh Douyin/TikTok/Facebook HOẶC
     dán link video đơn lẻ — 1 hay nhiều link), chọn/bỏ chọn, dịch tiêu đề,
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

from .ui_icons import ICON_COLORS, ICON_DISABLED, Tooltip, make_photo
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
    FETCH_ORDER_OPTIONS,
    FETCH_ORDER_NEWEST,
    DEFAULT_FETCH_ORDER,
    DEFAULT_FETCH_MAX_ITEMS,
    LOG_LEVEL_OPTIONS,
)
from .audio_merge_gui import AudioMergeTab
from .script_voice_gui import ScriptVoiceTab
from .download_history import DownloadHistory, make_key
from . import app_logger
from .app_logger import get_logger
from .douyin_client import DouyinAPIError, DouyinClient, DownloadCancelled
from .tiktok_client import TikTokAPIError, TikTokClient
from .facebook_client import FacebookAPIError, FacebookClient
from .ytdlp_updater import update_ytdlp_in_background
from .audio_merger import find_ffmpeg
from .browser_cookies import (
    BROWSERS as COOKIE_BROWSERS, BrowserCookieError, read_cookie_strings, list_profiles,
)
from .gemini_translator import translate_batch_with_gemini, list_available_models
from .utils import (
    extract_all_links, resolve_link, resolve_tiktok_link, resolve_facebook_link,
    detect_platform,
    format_post_time, safe_filename, unique_filename,
)
from .fetch_filters import FetchFilters, ItemCollector, parse_count_text, parse_date_text
from .widgets import (
    AccentButton, WrapFrame, SegmentedTabs, PlaceholderEntry, CalendarPopup,
    make_card, bind_wraplength,
)
from . import theme

log = get_logger("gui")

CHECK_ON = "\u2611"   # ☑
CHECK_OFF = "\u2610"  # ☐

# Trạng thái hiển thị cho video đã có trong lịch sử tải (xem download_history.py)
STATUS_NOT_DOWNLOADED = "Chưa tải"
STATUS_IN_HISTORY = "Đã tải trước đó"


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
        # Lịch sử video đã tải (SQLite) — chống tải trùng giữa các lần chạy
        self.history = DownloadHistory()
        self.client = DouyinClient(cookie=self.cfg.get("cookie", ""))
        # TikTok dùng yt-dlp; cookie TikTok là TÙY CHỌN (cần cho kênh bị chặn/riêng tư)
        # use_playwright (mặc định bật): lấy danh sách bằng trình duyệt ẩn + bắt JSON API,
        # lỗi thì tự lùi về yt-dlp. Tắt bằng "use_playwright": false trong file config.
        use_pw = bool(self.cfg.get("use_playwright", True))
        self.tiktok_client = TikTokClient(cookie=self.cfg.get("tiktok_cookie", ""), use_playwright=use_pw)
        # Facebook cũng dùng yt-dlp; cookie là TÙY CHỌN (cần cho video riêng tư/giới hạn)
        self.facebook_client = FacebookClient(cookie=self.cfg.get("facebook_cookie", ""), use_playwright=use_pw)
        # Tự lấy Cookie từ trình duyệt đang đăng nhập (xem browser_cookies.py)
        self.cookie_browser = self.cfg.get("cookie_browser", "firefox")
        if self.cookie_browser not in COOKIE_BROWSERS:
            self.cookie_browser = "firefox"
        self.auto_cookie = bool(self.cfg.get("auto_cookie", False))
        # Profile trình duyệt (vd "Default", "Profile 1"); rỗng = profile mặc định
        self.cookie_profile = str(self.cfg.get("cookie_profile", "") or "")
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
        self.is_loading = False   # đang lấy danh sách (nút chuyển thành "Dừng")
        self.is_translating = False

        # Điều kiện lấy danh sách (nhớ giữa các lần mở app; ngày thì không nhớ)
        self.fetch_order_label = next(
            (lbl for lbl, val in FETCH_ORDER_OPTIONS.items()
             if val == self.cfg.get("fetch_order", DEFAULT_FETCH_ORDER)),
            next(iter(FETCH_ORDER_OPTIONS)),
        )
        self._date_target = "from"   # ô ngày mà nút lịch 📅 sẽ điền vào (ô được bấm gần nhất)

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
        self._fit_window_height()
        self.after(100, self._poll_queue)
        self.after(150, self._reposition_action_buttons)
        # Tự cập nhật yt-dlp ở luồng nền mỗi lần mở app (tối đa 12 giờ/lần)
        self.after(1500, self._update_ytdlp)

    def report_callback_exception(self, exc, val, tb):
        """Lỗi trong callback Tkinter (bấm nút...) — ghi vào file log kèm traceback
        rồi vẫn giữ hành vi mặc định (in ra terminal)."""
        log.critical("Lỗi trong callback giao diện", exc_info=(exc, val, tb))
        super().report_callback_exception(exc, val, tb)

    # ------------------------------------------------- Cửa sổ / đóng app --
    def _update_ytdlp(self):
        """Tự cập nhật yt-dlp bằng pip ở luồng nền lúc mở app (không có nút bấm tay).
        Chỉ báo ở thanh trạng thái khi có thay đổi hoặc lỗi, tránh làm phiền."""

        def done(ok: bool, msg: str):
            if not ok or "→" in msg:
                self.task_queue.put(("status", msg))

        update_ytdlp_in_background(on_done=done, cfg=self.cfg, save_cfg=save_config)

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

    def _fit_window_height(self):
        """Cao đủ để bảng video hiện trọn 10 dòng (nếu màn hình đủ cao)."""
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = self.winfo_width() if self.winfo_width() > 1 else min(1280, max(900, sw - 100))
        h = max(self.winfo_reqheight() + 8, 600)
        h = min(h, sh - 90)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_close(self):
        """Đóng app: dừng ghép đang chạy (nếu có) và xóa các bản xem thử tạm."""
        script_tab = getattr(self, "script_tab", None)
        if script_tab is not None:
            try:
                script_tab.shutdown()
            except Exception:  # noqa: BLE001
                pass
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
        # Tab kịch bản + giọng đọc: lưu <tên video>.mp3 vào thư mục Audio của tab Ghép
        self.script_tab = ScriptVoiceTab(
            self.notebook, self.cfg, self.history,
            get_audio_dir=lambda: self.merge_tab.audio_dir,
            set_audio_dir=self.merge_tab.set_audio_dir,
            get_ffmpeg=lambda: self.merge_tab.ffmpeg_path,
            get_ffprobe=lambda: self.merge_tab.ffprobe_path,
        )
        self.notebook.add(self.tab_download, text="⬇  Tải video")
        self.notebook.add(self.script_tab, text="✎  Kịch bản & Giọng đọc")
        self.notebook.add(self.merge_tab, text="♫  Ghép Audio vào Video")
        # Mở tab kịch bản thì làm mới danh sách (có thể vừa tải xong video mới)
        self.notebook.bind(
            "<<TabChanged>>",
            lambda e: self.script_tab.on_tab_shown()
            if self.notebook.current == 1 else None,
        )

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
        source = ttk.LabelFrame(top, text=" 1 - Nguồn dữ liệu & Điều kiện lấy ", padding=8)
        source.pack(fill="x")
        source.columnconfigure(0, weight=1)

        ttk.Label(
            source,
            text=(
                "Link kênh HOẶC link video đơn lẻ — Douyin / TikTok / Facebook "
                "(dán nhiều link video: mỗi link 1 dòng/cách nhau; có thể dán cả đoạn text lộn xộn)"
            ),
        ).grid(row=0, column=0, sticky="w")

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
        # Đang lấy danh sách thì Enter KHÔNG được kích hoạt nút (lúc đó nút là "Dừng")
        self.link_entry.bind(
            "<Return>", lambda e: None if self.is_loading else self.on_load_click()
        )

        entry_bg = style.lookup("TEntry", "fieldbackground") or "white"
        self._clear_btn = tk.Label(
            self.link_entry, text="✕", fg="#8a8a8a", bg=entry_bg,
            cursor="hand2", font=("", 10),
        )
        self._clear_btn.bind("<Button-1>", lambda e: self.on_clear_link())
        self._clear_btn.bind("<Enter>", lambda e: self._clear_btn.config(fg="#d93025"))
        self._clear_btn.bind("<Leave>", lambda e: self._clear_btn.config(fg="#8a8a8a"))
        self.link_var.trace_add("write", lambda *_: self._update_clear_button())

        # ---- Điều kiện lấy (áp dụng khi lấy danh sách video của 1 KÊNH) ----
        # Thanh tiêu đề bấm được để thu gọn / mở rộng khung điều kiện
        cond_head = ttk.Frame(source)
        cond_head.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._cond_chevron = ttk.Label(cond_head, text="▾", cursor="hand2")
        self._cond_chevron.pack(side="left")
        self._cond_title = ttk.Label(cond_head, text="Điều kiện lấy", cursor="hand2")
        self._cond_title.pack(side="left", padx=(6, 0))
        self._cond_summary_var = tk.StringVar()
        self._cond_summary = ttk.Label(
            cond_head, textvariable=self._cond_summary_var, style="Muted.TLabel", cursor="hand2"
        )
        for w in (cond_head, self._cond_chevron, self._cond_title, self._cond_summary):
            w.bind("<Button-1>", lambda e: self._toggle_conditions())

        cond = ttk.Frame(source)
        cond.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self._cond_frame = cond
        self._cond_collapsed = False
        for col, (weight, minw) in enumerate(((5, 330), (3, 190), (4, 290), (3, 150))):
            cond.columnconfigure(col, weight=weight, minsize=minw)

        def cond_card(col: int, title: str) -> ttk.Frame:
            outer, box = make_card(cond, padding=(10, 6))
            outer.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            ttk.Label(box, text=title).pack(anchor="w")
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(4, 0))
            return row

        # Thẻ 1: Thời gian (từ ngày - đến ngày + lịch)
        row = cond_card(0, "Thời gian:")
        ttk.Label(row, text="Từ ngày").pack(side="left")
        self.date_from_entry = PlaceholderEntry(row, "[ DD/MM/YYYY ]", width=14)
        self.date_from_entry.pack(side="left", padx=(6, 8))
        ttk.Label(row, text="đến").pack(side="left")
        self.date_to_entry = PlaceholderEntry(row, "[ DD/MM/YYYY ]", width=14)
        self.date_to_entry.pack(side="left", padx=(6, 6))
        self.date_from_entry.bind(
            "<FocusIn>", lambda e: setattr(self, "_date_target", "from"), add="+"
        )
        self.date_to_entry.bind(
            "<FocusIn>", lambda e: setattr(self, "_date_target", "to"), add="+"
        )
        cal_btn = tk.Label(row, text="📅", bg=theme.CARD, fg=theme.MUTED, cursor="hand2")
        cal_btn.pack(side="left")
        cal_btn.bind("<Button-1>", lambda e: self._open_calendar())
        cal_btn.bind("<Enter>", lambda e: cal_btn.configure(fg=theme.FG))
        cal_btn.bind("<Leave>", lambda e: cal_btn.configure(fg=theme.MUTED))
        Tooltip(cal_btn, "Chọn ngày bằng lịch (điền vào ô ngày đang chọn)")

        # Thẻ 2: Thứ tự
        row = cond_card(1, "Thứ tự:")
        self.fetch_order_var = tk.StringVar(value=self.fetch_order_label)
        ttk.Combobox(
            row, textvariable=self.fetch_order_var, state="readonly",
            values=list(FETCH_ORDER_OPTIONS.keys()), width=19,
        ).pack(side="left", fill="x", expand=True)

        # Thẻ 3: Lượt xem & lượt tym tối thiểu
        row = cond_card(2, "Lượt tương tác & tym")
        self.min_views_var = tk.StringVar(value=str(self.cfg.get("fetch_min_views", 0) or 0))
        self.min_likes_var = tk.StringVar(value=str(self.cfg.get("fetch_min_likes", 0) or 0))
        ttk.Label(row, text="👁 view >=").pack(side="left")
        ttk.Entry(row, textvariable=self.min_views_var, width=9).pack(side="left", padx=(6, 12))
        ttk.Label(row, text="♥ Lượt tym >=").pack(side="left")
        ttk.Entry(row, textvariable=self.min_likes_var, width=9).pack(side="left", padx=(6, 0))

        # Thẻ 4: Tối đa số video
        row = cond_card(3, "Tối đa số video:")
        max_default = self.cfg.get("fetch_max_items", DEFAULT_FETCH_MAX_ITEMS)
        self.max_items_var = tk.StringVar(value=str(max_default) if max_default else "")
        ttk.Entry(row, textvariable=self.max_items_var, width=10).pack(side="left", fill="x", expand=True)

        source_btns = ttk.Frame(source)
        source_btns.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.load_btn = AccentButton(
            source_btns, text="🔍 Lấy danh sách video", command=self.on_load_click,
            bg="#1a73e8", hover_bg="#1557b0", padx=18, pady=6,
        )
        self.load_btn.pack(side="left")
        ttk.Button(
            source_btns, text="⚙ Cài đặt (Cookie)", command=self.open_settings
        ).pack(side="left", padx=(8, 0))
        if self.cfg.get("fetch_cond_collapsed", False):
            self._toggle_conditions(save=False)

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
        dl = ttk.LabelFrame(settings, text=" 2a - Cài đặt tải về ", padding=8)
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
        tr = ttk.LabelFrame(settings, text=" 2b - Xử lý / Dịch thuật ", padding=8)
        tr.grid(row=0, column=1, sticky="nsew")
        tr.columnconfigure(1, weight=1)

        self.auto_translate_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_translate_titles", False))
        )
        ttk.Checkbutton(
            tr, text="Dịch tiêu đề (tự động sau khi lấy danh sách)",
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
            toolbar, text="🌐 Dịch tiêu đề", command=self.on_translate_titles
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
            table_row, columns=cols, show="headings", selectmode="extended", height=10,
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
        self.tree.heading("download", text="Tải")
        self.tree.heading("log", text="Xem")
        self.tree.heading("edit", text="Sửa")
        self.tree.heading("delete", text="Xóa")

        self.tree.column("chk", width=32, anchor="center", stretch=False)
        self.tree.column("stt", width=46, minwidth=40, anchor="center", stretch=False)
        self.tree.column("title", width=260, minwidth=110, anchor="w")
        self.tree.column("url", width=220, minwidth=90, anchor="w")
        self.tree.column("duration", width=80, anchor="center", stretch=False)
        self.tree.column("post_time", width=120, anchor="center", stretch=False)
        self.tree.column("status", width=110, anchor="center", stretch=False)
        self.tree.column("download", width=54, anchor="center", stretch=False)
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
        style.configure("Treeview", rowheight=28)  # hàng cao hơn cho vừa icon
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
        """Cửa sổ Cài đặt gọn: 2 tab ("Quản lý Cookie" | "Cấu hình Gemini API"),
        ghi chú dài chuyển thành tooltip ⓘ, ô cookie 1 dòng, Lưu/Hủy ở đáy."""
        win = tk.Toplevel(self)
        win.title("Cài đặt")
        win.geometry("800x460")
        win.minsize(720, 420)
        win.transient(self)
        win.bind("<Escape>", lambda e: win.destroy())

        # ---- Đáy: Hủy | Lưu (pack TRƯỚC nên luôn hiện đủ) ----
        btn_row = ttk.Frame(win)
        btn_row.pack(side="bottom", fill="x", padx=12, pady=(4, 10))

        tabs = SegmentedTabs(win, variant="sub")
        tabs.pack(side="top", fill="both", expand=True, padx=12, pady=(10, 0))
        tab_cookie = ttk.Frame(tabs, padding=(2, 4))
        tab_gemini = ttk.Frame(tabs, padding=(2, 4))
        tab_history = ttk.Frame(tabs, padding=(2, 4))
        tab_log = ttk.Frame(tabs, padding=(2, 4))
        tabs.add(tab_cookie, text="🍪  Quản lý Cookie")
        tabs.add(tab_gemini, text="✨  Cấu hình Gemini API")
        tabs.add(tab_history, text="📜  Lịch sử tải")
        tabs.add(tab_log, text="🧾  Nhật ký")

        def info(parent, tip: str):
            """Biểu tượng ⓘ — rê chuột vào để xem hướng dẫn chi tiết."""
            lbl = ttk.Label(parent, text="ⓘ", style="Link.TLabel", cursor="question_arrow")
            Tooltip(lbl, tip, wraplength=360)
            return lbl

        def link(parent, text: str, url: str):
            lbl = ttk.Label(parent, text=text, style="Link.TLabel", cursor="hand2")
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
            return lbl

        # =====================================================================
        # TAB 1 · QUẢN LÝ COOKIE
        # =====================================================================
        # --- 1a. Tự lấy Cookie từ trình duyệt (đưa lên đầu: tiện nhất) ---
        auto_box = ttk.LabelFrame(tab_cookie, text=" Tự lấy Cookie từ trình duyệt ", padding=8)
        auto_box.pack(fill="x")

        row = ttk.Frame(auto_box)
        row.pack(fill="x")
        ttk.Label(row, text="Trình duyệt:").pack(side="left")
        browser_var = tk.StringVar(value=self.cookie_browser)
        browser_combo = ttk.Combobox(
            row, textvariable=browser_var, values=COOKIE_BROWSERS, state="readonly", width=9,
        )
        browser_combo.pack(side="left", padx=(6, 10))

        ttk.Label(row, text="Profile:").pack(side="left")
        profile_var = tk.StringVar()
        profile_combo = ttk.Combobox(row, textvariable=profile_var, width=18)
        profile_combo.pack(side="left", padx=(6, 10))
        Tooltip(
            profile_combo,
            "Profile của trình duyệt (Chrome/Edge/Brave... có thể có nhiều profile). "
            "Chọn profile mà bạn ĐÃ ĐĂNG NHẬP. Xem tên thư mục tại chrome://version "
            "→ dòng \"Profile Path\".",
            wraplength=320,
        )

        grab_btn = ttk.Button(row, text="🍪 Lấy Cookie ngay")
        grab_btn.pack(side="left")

        auto_cookie_var = tk.BooleanVar(value=self.auto_cookie)
        ttk.Checkbutton(
            row, text="Tự lấy lại mỗi lần Lấy danh sách", variable=auto_cookie_var,
        ).pack(side="left", padx=(14, 4))
        info(
            row,
            "Bật: mỗi lần bấm \"Lấy danh sách video\", app tự đọc Cookie MỚI từ trình "
            "duyệt/profile đã chọn nên không bao giờ bị hết hạn.\n"
            "Firefox đọc ổn định nhất. Chrome/Edge bản mới trên Windows có thể không đọc "
            "được (cookie mã hóa app-bound) — khi đó hãy dán Cookie thủ công bên dưới.",
        ).pack(side="left")

        grab_result_var = tk.StringVar(value="")
        grab_result_lbl = ttk.Label(
            auto_box, textvariable=grab_result_var, style="Muted.TLabel",
            wraplength=720, justify="left",
        )
        # chỉ hiện khi có kết quả -> không chiếm chỗ lúc chưa dùng
        def set_grab_result(msg: str):
            grab_result_var.set(msg)
            if msg:
                grab_result_lbl.pack(fill="x", pady=(6, 0))
            else:
                grab_result_lbl.pack_forget()

        # --- Profile: dò danh sách theo trình duyệt đang chọn ---
        def profile_label(folder: str, name: str) -> str:
            return folder if name == folder else f"{folder}  ({name})"

        def refresh_profiles(*_):
            found_profiles = list_profiles(browser_var.get())
            labels = ["(Mặc định)"] + [profile_label(f, n) for f, n in found_profiles]
            profile_combo["values"] = labels
            saved = self.cookie_profile if browser_var.get() == self.cookie_browser else ""
            match = next((l for l in labels[1:] if l.split("  (")[0] == saved), None)
            profile_var.set(match or saved or labels[0])

        def selected_profile() -> str:
            """Tên thư mục profile đã chọn ('' = mặc định). Cho phép tự gõ tay."""
            v = profile_var.get().strip()
            if not v or v == "(Mặc định)":
                return ""
            return v.split("  (")[0].strip()

        browser_combo.bind("<<ComboboxSelected>>", refresh_profiles)
        refresh_profiles()

        # --- 1b. Cookie thủ công: mỗi nền tảng 1 ô 1 dòng (ẩn bằng •) ---
        manual_box = ttk.LabelFrame(tab_cookie, text=" Dán Cookie thủ công ", padding=8)
        manual_box.pack(fill="x", pady=(10, 0))
        manual_box.columnconfigure(2, weight=1)

        cookie_entries: dict[str, ttk.Entry] = {}
        manual_rows = (
            ("douyin", "Douyin", self.client.cookie,
             "Cookie của douyin.com (lấy từ trình duyệt đã mở douyin.com — xem "
             "README.md). Giúp app lấy danh sách video ổn định hơn.",
             ("Mở douyin.com ↗", "https://www.douyin.com")),
            ("tiktok", "TikTok", self.tiktok_client.cookie,
             "TÙY CHỌN — chỉ cần khi kênh TikTok bị chặn/riêng tư. Dán Cookie của "
             "tiktok.com đã đăng nhập, dạng a=1; b=2.",
             ("Mở tiktok.com ↗", "https://www.tiktok.com")),
            ("facebook", "Facebook", self.facebook_client.cookie,
             "TÙY CHỌN — chỉ cần khi video riêng tư/giới hạn hoặc Facebook đòi đăng "
             "nhập. Dán Cookie của facebook.com đã đăng nhập (cần có c_user và xs).",
             ("Mở facebook.com ↗", "https://www.facebook.com")),
        )
        for r, (key, label, value, tip, (link_text, link_url)) in enumerate(manual_rows):
            ttk.Label(manual_box, text=label, width=9).grid(row=r, column=0, sticky="w", pady=3)
            info(manual_box, tip).grid(row=r, column=1, sticky="w", padx=(0, 8))
            entry = ttk.Entry(manual_box, show="•")
            entry.insert(0, value)
            entry.grid(row=r, column=2, sticky="ew", pady=3)
            link(manual_box, link_text, link_url).grid(row=r, column=3, sticky="w", padx=(10, 0))
            cookie_entries[key] = entry

        show_cookie_var = tk.BooleanVar(value=False)

        def toggle_show():
            for e in cookie_entries.values():
                e.configure(show="" if show_cookie_var.get() else "•")

        ttk.Checkbutton(
            manual_box, text="Hiện nội dung Cookie", variable=show_cookie_var,
            command=toggle_show,
        ).grid(row=len(manual_rows), column=2, sticky="w", pady=(4, 0))

        # --- Hành vi nút "Lấy Cookie ngay" ---
        def grab_cookies():
            grab_btn.state(["disabled"])
            set_grab_result("Đang đọc Cookie từ trình duyệt...")
            browser = browser_var.get()
            profile = selected_profile() or None
            # Luồng nền KHÔNG được đụng vào widget Tk -> chỉ bỏ kết quả vào
            # hàng đợi; luồng giao diện tự đọc ra bằng after() bên dưới.
            result_q: queue.Queue = queue.Queue()

            def worker():
                try:
                    result_q.put((read_cookie_strings(browser, profile), None))
                except BrowserCookieError as exc:
                    result_q.put(({}, str(exc)))
                except Exception as exc:  # lỗi bất ngờ vẫn phải mở lại nút
                    result_q.put(({}, f"Lỗi không xác định: {exc}"))

            def apply(found, err):
                grab_btn.state(["!disabled"])
                if err:
                    set_grab_result("❌ " + err)
                    return
                where = browser + (" / " + profile if profile else "")
                parts = []
                for key, label in (("douyin", "Douyin"), ("tiktok", "TikTok"), ("facebook", "Facebook")):
                    if key in found:
                        cookie_entries[key].delete(0, "end")
                        cookie_entries[key].insert(0, found[key])
                        parts.append(f"✔ {label}: {found[key].count(';') + 1} cookie")
                    else:
                        parts.append(f"✘ {label}: không có (hãy đăng nhập trong {where})")
                set_grab_result("   ".join(parts) + "\nBấm Lưu để áp dụng.")

            def poll():
                if not win.winfo_exists():
                    return
                try:
                    found, err = result_q.get_nowait()
                except queue.Empty:
                    win.after(100, poll)
                    return
                apply(found, err)

            threading.Thread(target=worker, daemon=True).start()
            win.after(100, poll)

        grab_btn.configure(command=grab_cookies)

        # =====================================================================
        # TAB 2 · CẤU HÌNH GEMINI API
        # =====================================================================
        tab_gemini.columnconfigure(0, weight=1, uniform="model")
        tab_gemini.columnconfigure(1, weight=1, uniform="model")

        key_head = ttk.Frame(tab_gemini)
        key_head.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(key_head, text="Gemini API Key").pack(side="left")
        info(
            key_head,
            "Dùng cho chức năng \"Dịch tiêu đề → Tiếng Việt\". Key được lưu trong file "
            "cấu hình trên máy bạn.",
        ).pack(side="left", padx=(6, 0))

        key_row = ttk.Frame(tab_gemini)
        key_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        key_row.columnconfigure(0, weight=1)
        gemini_var = tk.StringVar(value=self.gemini_api_key)
        gemini_entry = ttk.Entry(key_row, textvariable=gemini_var, show="•")
        gemini_entry.grid(row=0, column=0, sticky="ew")
        fetch_models_btn = ttk.Button(key_row, text="⟳ Tải danh sách model")
        fetch_models_btn.grid(row=0, column=1, padx=(8, 0))
        # Huy hiệu trạng thái: xanh lá = thành công, đỏ = lỗi (chi tiết trong tooltip)
        model_badge = tk.Label(key_row, text="", fg="#ffffff", padx=8, pady=2, font=("", 9))
        model_badge_tip = Tooltip(model_badge, "", wraplength=360)
        model_badge.grid(row=0, column=2, padx=(8, 0))
        model_badge.grid_remove()

        link(
            tab_gemini, "Lấy API Key miễn phí tại aistudio.google.com ↗",
            "https://aistudio.google.com/apikey",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 12))

        # --- Model chính | Model dự phòng: 2 cột song song ---
        main_head = ttk.Frame(tab_gemini)
        main_head.grid(row=3, column=0, sticky="w")
        ttk.Label(main_head, text="Model chính").pack(side="left")
        info(
            main_head,
            "Bấm \"Tải danh sách model\" để lấy TẤT CẢ model đang khả dụng cho đúng API "
            "Key này, hoặc tự gõ tên model khác.",
        ).pack(side="left", padx=(6, 0))

        fb_head = ttk.Frame(tab_gemini)
        fb_head.grid(row=3, column=1, sticky="w", padx=(12, 0))
        ttk.Label(fb_head, text="Model dự phòng").pack(side="left")
        info(
            fb_head,
            "Tự động chuyển sang model này khi model chính lỗi/quá tải/hết quota phút. "
            "Để TRỐNG để tắt fallback.",
        ).pack(side="left", padx=(6, 0))

        model_var = tk.StringVar(value=self.gemini_model)
        model_combo = ttk.Combobox(tab_gemini, textvariable=model_var, values=GEMINI_MODEL_SUGGESTIONS)
        model_combo.grid(row=4, column=0, sticky="ew", pady=(3, 0))
        fallback_model_var = tk.StringVar(value=self.gemini_fallback_model)
        fallback_model_combo = ttk.Combobox(
            tab_gemini, textvariable=fallback_model_var, values=[""] + GEMINI_MODEL_SUGGESTIONS,
        )
        fallback_model_combo.grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=(3, 0))

        def show_badge(text: str, ok: bool, detail: str = ""):
            model_badge.configure(text=text, bg=theme.TICK_GREEN_DIM if ok else theme.RED)
            model_badge_tip.text = detail
            model_badge.grid()

        def on_models_success(models: list[str]):
            fetch_models_btn.state(["!disabled"])
            if not models:
                show_badge(
                    "✘ Không có model", False,
                    "Gemini không trả về model nào hỗ trợ dịch text cho API Key này.",
                )
                return
            model_combo["values"] = models
            fallback_model_combo["values"] = [""] + models
            show_badge(f"✔ Đã tải {len(models)} model", True, "Chọn model trong 2 danh sách bên dưới.")

        def on_models_error(msg: str):
            fetch_models_btn.state(["!disabled"])
            show_badge("✘ Lỗi tải model", False, f"Không tải được danh sách model: {msg}")

        def fetch_models():
            api_key = gemini_var.get().strip()
            if not api_key:
                messagebox.showwarning(
                    APP_TITLE, "Nhập Gemini API Key trước khi tải danh sách model.", parent=win,
                )
                return
            model_badge.grid_remove()
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

        # =====================================================================
        # TAB 3 · LỊCH SỬ TẢI
        # =====================================================================
        hist_box = ttk.LabelFrame(tab_history, text=" Video đã tải ", padding=10)
        hist_box.pack(fill="x")
        hist_count_var = tk.StringVar()
        hist_msg_var = tk.StringVar(value="")

        def refresh_history_count():
            hist_count_var.set(f"Số video đã ghi nhận trong lịch sử: {self.history.count()}")
            if self.history.last_error:
                hist_msg_var.set(self.history.last_error)

        def clear_history():
            if not messagebox.askyesno(
                APP_TITLE,
                "Xóa TOÀN BỘ lịch sử tải?\n\nChỉ xóa danh sách ghi nhớ trong app, "
                "các file video đã tải trên máy KHÔNG bị xóa.",
                parent=win,
            ):
                return
            if self.history.clear():
                self._reset_history_statuses()
                hist_msg_var.set("Đã xóa lịch sử tải.")
            else:
                hist_msg_var.set(self.history.last_error)
            refresh_history_count()

        ttk.Label(hist_box, textvariable=hist_count_var).pack(anchor="w")
        ttk.Label(
            hist_box, wraplength=640, justify="left",
            text=(
                "Mỗi video tải thành công được ghi nhớ (theo nền tảng + ID). Khi lấy lại danh sách "
                "của cùng kênh, video đã tải sẽ hiện \"Đã tải trước đó\", và khi bấm \"Tải video "
                "đã chọn\" app sẽ hỏi có bỏ qua chúng không.\n"
                f"Lưu tại: {self.history.db_path}"
            ),
        ).pack(anchor="w", pady=(6, 10))
        ttk.Button(hist_box, text="🗑 Xóa toàn bộ lịch sử", command=clear_history).pack(anchor="w")
        ttk.Label(tab_history, textvariable=hist_msg_var).pack(anchor="w", pady=(8, 0))
        refresh_history_count()

        # =====================================================================
        # TAB 4 · NHẬT KÝ (LOG)
        # =====================================================================
        log_box = ttk.LabelFrame(tab_log, text=" Nhật ký hoạt động ", padding=10)
        log_box.pack(fill="x")
        ttk.Label(
            log_box, wraplength=680, justify="left",
            text=(
                "App ghi lại các bước lấy danh sách, tải video và mọi lỗi vào file log. Cookie, "
                "token, chữ ký và API key được TỰ ĐỘNG che nên có thể gửi file log cho người khác "
                "xem lỗi.\n"
                f"File log: {app_logger.log_file_path()}"
            ),
        ).pack(anchor="w", pady=(0, 8))

        level_row = ttk.Frame(log_box)
        level_row.pack(fill="x", pady=(0, 8))
        ttk.Label(level_row, text="Mức chi tiết:").pack(side="left")
        log_level_var = tk.StringVar(value=app_logger.current_level_name())
        ttk.Combobox(
            level_row, textvariable=log_level_var, values=LOG_LEVEL_OPTIONS,
            state="readonly", width=8,
        ).pack(side="left", padx=(6, 6))
        info(
            level_row,
            "INFO: ghi các bước chính + lỗi (gọn).\nDEBUG: ghi thêm chi tiết từng request/response — "
            "bật khi cần tìm nguyên nhân lỗi, rồi tái hiện lỗi và gửi file log.",
        ).pack(side="left")

        log_msg_var = tk.StringVar(value="")

        def do_open_folder():
            app_logger.log_dir().mkdir(parents=True, exist_ok=True)
            if not app_logger.open_path(app_logger.log_dir()):
                log_msg_var.set(f"Không mở được thư mục. Đường dẫn: {app_logger.log_dir()}")

        def do_open_file():
            app_logger.flush_handler()
            path = app_logger.log_file_path()
            if not path.exists() or not app_logger.open_path(path):
                log_msg_var.set(f"Không mở được file log. Đường dẫn: {path}")

        def do_copy_tail():
            app_logger.flush_handler()
            text = app_logger.read_tail(200)
            if not text:
                log_msg_var.set("File log đang trống.")
                return
            win.clipboard_clear()
            win.clipboard_append(text)
            log_msg_var.set("Đã sao chép 200 dòng log cuối vào clipboard — dán vào tin nhắn để gửi.")

        def do_clear_log():
            if not messagebox.askyesno(APP_TITLE, "Xóa toàn bộ file log?", parent=win):
                return
            log_msg_var.set("Đã xóa log." if app_logger.clear_logs() else "Không xóa hết được file log.")

        btns = ttk.Frame(log_box)
        btns.pack(fill="x")
        ttk.Button(btns, text="📂 Mở thư mục log", command=do_open_folder).pack(side="left")
        ttk.Button(btns, text="📄 Mở file log", command=do_open_file).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="📋 Sao chép 200 dòng cuối", command=do_copy_tail).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="🗑 Xóa log", command=do_clear_log).pack(side="left", padx=(8, 0))
        ttk.Label(tab_log, textvariable=log_msg_var, wraplength=700, justify="left").pack(
            anchor="w", pady=(8, 0)
        )

        # =====================================================================
        # ĐÁY: Hủy | Lưu
        # =====================================================================
        def save_and_close():
            self.cfg["log_level"] = log_level_var.get()
            app_logger.set_level(log_level_var.get())

            self.client.cookie = cookie_entries["douyin"].get().strip()
            self.cfg["cookie"] = self.client.cookie

            self.tiktok_client.cookie = cookie_entries["tiktok"].get().strip()
            self.cfg["tiktok_cookie"] = self.tiktok_client.cookie

            self.facebook_client.cookie = cookie_entries["facebook"].get().strip()
            self.cfg["facebook_cookie"] = self.facebook_client.cookie

            self.cookie_browser = browser_var.get()
            self.auto_cookie = bool(auto_cookie_var.get())
            self.cfg["cookie_browser"] = self.cookie_browser
            self.cookie_profile = selected_profile()
            self.cfg["cookie_profile"] = self.cookie_profile
            self.cfg["auto_cookie"] = self.auto_cookie

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

        AccentButton(
            btn_row, text="Lưu", command=save_and_close,
            bg=theme.ACCENT, hover_bg=theme.ACCENT_HOVER, padx=22, pady=5,
        ).pack(side="right")
        ttk.Button(btn_row, text="Hủy", command=win.destroy).pack(side="right", padx=(0, 8))

    def _reset_history_statuses(self):
        """Sau khi xóa lịch sử: các hàng đang hiện "Đã tải trước đó" trở về "Chưa tải"."""
        for vid, text in list(self.statuses.items()):
            if text == STATUS_IN_HISTORY:
                self.update_row_status(vid, STATUS_NOT_DOWNLOADED)
                self.logs.pop(vid, None)

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
    def _cond_summary_text(self) -> str:
        """Tóm tắt điều kiện hiện tại (đọc thô từ các ô, không báo lỗi) khi khung thu gọn."""
        parts = []
        d1, d2 = self.date_from_entry.value(), self.date_to_entry.value()
        if d1 or d2:
            parts.append(f"{d1 or '...'} → {d2 or '...'}")
        parts.append(self.fetch_order_var.get())
        for label, var in (("view", self.min_views_var), ("tym", self.min_likes_var)):
            v = var.get().strip()
            if v and v != "0":
                parts.append(f"{label} ≥ {v}")
        mx = self.max_items_var.get().strip()
        parts.append(f"tối đa {mx}" if mx and mx != "0" else "không giới hạn số lượng")
        return " · ".join(parts)

    def _toggle_conditions(self, save: bool = True):
        """Thu gọn / mở rộng khung điều kiện (trạng thái được nhớ cho lần mở sau)."""
        self._cond_collapsed = not self._cond_collapsed
        if self._cond_collapsed:
            self._cond_frame.grid_remove()
            self._cond_chevron.configure(text="▸")
            self._cond_summary_var.set(self._cond_summary_text())
            self._cond_summary.pack(side="left", padx=(12, 0))
        else:
            self._cond_frame.grid()
            self._cond_chevron.configure(text="▾")
            self._cond_summary.pack_forget()
        if save:
            self.cfg["fetch_cond_collapsed"] = self._cond_collapsed
            save_config(self.cfg)

    def _open_calendar(self):
        """Mở lịch chọn ngày; điền vào ô 'Từ ngày' hoặc 'Đến ngày' (ô được bấm gần nhất)."""
        if self._date_target == "to":
            entry, title = self.date_to_entry, "Chọn ngày kết thúc (Đến ngày)"
        else:
            entry, title = self.date_from_entry, "Chọn ngày bắt đầu (Từ ngày)"
        try:
            initial = parse_date_text(entry.value())
        except ValueError:
            initial = None
        CalendarPopup(
            self, entry, title, initial,
            lambda d, e=entry: e.set_value(d.strftime("%d/%m/%Y") if d else ""),
        )

    def _read_fetch_filters(self) -> FetchFilters | None:
        """Đọc + kiểm tra các ô điều kiện. Sai thì báo lỗi, đưa con trỏ về ô sai
        và trả None (không bắt đầu lấy danh sách)."""

        def bad(msg: str, widget):
            messagebox.showwarning(APP_TITLE, msg)
            widget.focus_set()
            return None

        try:
            date_from = parse_date_text(self.date_from_entry.value())
        except ValueError as exc:
            return bad(f"Ô \"Từ ngày\": {exc}", self.date_from_entry)
        try:
            date_to = parse_date_text(self.date_to_entry.value())
        except ValueError as exc:
            return bad(f"Ô \"Đến ngày\": {exc}", self.date_to_entry)
        if date_from and date_to and date_from > date_to:
            return bad("\"Từ ngày\" phải trước hoặc bằng \"Đến ngày\".", self.date_from_entry)

        try:
            min_views = parse_count_text(self.min_views_var.get())
        except ValueError as exc:
            return bad(f"Ô \"view >=\": {exc}", self.date_from_entry)
        try:
            min_likes = parse_count_text(self.min_likes_var.get())
        except ValueError as exc:
            return bad(f"Ô \"Lượt tym >=\": {exc}", self.date_from_entry)
        try:
            max_items = parse_count_text(self.max_items_var.get())
        except ValueError:
            return bad(
                "\"Tối đa số video\" phải là một số nguyên dương (hoặc để trống = không giới hạn).",
                self.date_from_entry,
            )

        order = FETCH_ORDER_OPTIONS.get(self.fetch_order_var.get(), DEFAULT_FETCH_ORDER)
        # Nhớ lại các điều kiện (trừ ngày) cho lần mở app sau
        self.cfg.update({
            "fetch_order": order,
            "fetch_min_views": min_views,
            "fetch_min_likes": min_likes,
            "fetch_max_items": max_items,
        })
        save_config(self.cfg)
        return FetchFilters(
            date_from=date_from, date_to=date_to,
            newest_first=(order == FETCH_ORDER_NEWEST),
            min_views=min_views, min_likes=min_likes, max_items=max_items,
        )

    def _set_loading(self, loading: bool):
        """Nút xanh 'Lấy danh sách' <-> nút đỏ 'Dừng lấy' trong lúc đang quét."""
        self.is_loading = loading
        if loading:
            self.load_btn.set_look("⏹ Dừng lấy danh sách", bg="#d93025", hover_bg="#a52714")
        else:
            self.load_btn.set_look("🔍 Lấy danh sách video", bg="#1a73e8", hover_bg="#1557b0")

    def on_load_click(self):
        if self.is_loading:
            # Đang quét: bấm lần nữa = dừng, vẫn giữ những video đã lấy được
            self.stop_loading_flag = True
            self.status_var.set("Đang dừng... (giữ lại các video đã lấy được)")
            return
        if self.is_busy:
            return
        raw = self.link_var.get().strip()
        links = extract_all_links(raw)
        if not links:
            messagebox.showwarning(
                APP_TITLE,
                "Không tìm thấy link Douyin, TikTok hoặc Facebook hợp lệ trong nội dung đã nhập.",
            )
            return
        filters = self._read_fetch_filters()
        if filters is None:
            return
        # cập nhật lại ô nhập bằng link đã làm sạch (nhiều link -> cách nhau bằng dấu cách)
        clean = links[0]
        self.link_var.set("  ".join(links))

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
        self._set_loading(True)
        self.download_btn.state(["disabled"])

        if len(links) > 1:
            # Nhiều link dán cùng lúc -> coi là danh sách video đơn lẻ (không lọc)
            self.status_var.set(f"Đang đọc {len(links)} link video...")
            threading.Thread(
                target=self._load_thread_main, args=(self._load_single_worker, links), daemon=True
            ).start()
        else:
            self.status_var.set(f"Đang phân giải link: {clean}  |  Điều kiện: {filters.describe()}")
            threading.Thread(
                target=self._load_thread_main, args=(self._load_worker, clean, filters),
                daemon=True,
            ).start()

    def _load_thread_main(self, fn, *args):
        """Chạy worker lấy danh sách; lỗi bất ngờ cũng phải trả lại trạng thái bình
        thường cho nút (không để kẹt ở chế độ 'Dừng')."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            log.exception("Lỗi không mong đợi khi lấy danh sách")
            self.task_queue.put(("error", f"Lỗi không mong đợi khi lấy danh sách: {exc}"))
            self.task_queue.put(("load_done", None))

    def _make_collector(self, filters: FetchFilters, client=None) -> ItemCollector:
        """Bộ thu thập áp điều kiện. `client` (TikTok/Facebook) dùng để bổ sung
        ngày/view/tym cho video mà danh sách kênh không kèm số liệu đó."""

        def progress(scanned: int, matched: int):
            self.task_queue.put(
                ("status", f"Đang quét kênh... đã quét {scanned} video · khớp điều kiện {matched}")
            )

        enrich = client.fetch_video_info if (client is not None and filters.is_active) else None
        return ItemCollector(
            filters, stop_flag=lambda: self.stop_loading_flag, enrich=enrich,
            progress_cb=progress,
        )

    def _post_fetch_summary(self, collector: ItemCollector, warning: str = ""):
        """Báo kết quả quét (đã quét bao nhiêu, vì sao video bị loại) ở thanh trạng thái."""
        if self.stop_loading_flag:
            collector.stopped_by_user = True
        if not collector.scanned:
            if collector.stopped_by_user:
                self.task_queue.put(("status", "Đã dừng lấy danh sách."))
            elif warning:
                self.task_queue.put(("status", "⚠ " + warning))
            return
        msg = collector.summary()
        if warning:
            msg += "  ⚠ " + warning
        self.task_queue.put(("status", msg))

    def _auto_refresh_cookies(self, platform: str):
        """Nếu bật "Tự lấy lại Cookie": đọc cookie mới từ trình duyệt đã chọn
        cho `platform` ("douyin"/"tiktok") và cập nhật vào client + config.
        Lỗi/không có cookie -> giữ nguyên cookie cũ, chỉ báo ở thanh trạng thái.
        Chạy trong luồng nền (đọc trình duyệt có thể mất vài giây)."""
        if not self.auto_cookie:
            return
        self.task_queue.put(("status", f"Đang lấy Cookie mới từ {self.cookie_browser}..."))
        try:
            found = read_cookie_strings(self.cookie_browser, self.cookie_profile or None)
        except BrowserCookieError as exc:
            self.task_queue.put(
                ("status", f"⚠ Không tự lấy được Cookie ({self.cookie_browser}), dùng Cookie đã lưu. {str(exc)[:120]}")
            )
            return
        fresh = found.get(platform)
        if not fresh:
            self.task_queue.put(
                ("status", f"⚠ {self.cookie_browser} chưa có Cookie {platform}, dùng Cookie đã lưu.")
            )
            return
        client = self._client_for(platform)
        client.cookie = fresh
        self.cfg[{"tiktok": "tiktok_cookie", "facebook": "facebook_cookie"}.get(platform, "cookie")] = fresh
        save_config(self.cfg)

    def _client_for(self, platform: str | None):
        """Client tương ứng nền tảng ("tiktok" | "facebook" | khác -> Douyin)."""
        if platform == "tiktok":
            return self.tiktok_client
        if platform == "facebook":
            return self.facebook_client
        return self.client

    def _fetch_single_item(self, link: str) -> dict:
        """Lấy thông tin 1 video đơn lẻ (KHÔNG tải về) từ link bất kỳ của 3 nền
        tảng. Trả về item cùng định dạng với danh sách kênh; raise
        DouyinAPIError / TikTokAPIError / FacebookAPIError / RuntimeError."""
        platform = detect_platform(link)
        if platform == "tiktok":
            return self.tiktok_client.fetch_video_info(link)
        if platform == "facebook":
            kind, _ = resolve_facebook_link(link)
            if kind != "video":
                raise RuntimeError("Đây là link trang/kênh Facebook, không phải video đơn lẻ.")
            return self.facebook_client.fetch_video_info(link)
        kind, ident = resolve_link(link)
        if kind != "video":
            raise RuntimeError("Đây là link kênh Douyin, không phải video đơn lẻ.")
        return self.client.fetch_video_by_id(ident)

    def _load_single_worker(self, links: list[str]):
        """Nạp 1 hoặc nhiều link VIDEO ĐƠN LẺ vào bảng (không cần link kênh),
        tự tick chọn sẵn để chỉ việc bấm "Tải video đã chọn"."""
        items: list[dict] = []
        seen: set[str] = set()
        failures: list[str] = []
        refreshed: set[str] = set()
        for i, link in enumerate(links, 1):
            if self.stop_loading_flag:
                break
            platform = detect_platform(link) or "douyin"
            if platform not in refreshed:
                refreshed.add(platform)
                self._auto_refresh_cookies(platform)
            self.task_queue.put(("status", f"Đang đọc video {i}/{len(links)}: {link}"))
            try:
                item = self._fetch_single_item(link)
            except (DouyinAPIError, TikTokAPIError, FacebookAPIError, RuntimeError) as exc:
                failures.append(f"• {link}\n  {str(exc).splitlines()[0][:200]}")
                continue
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)

        if failures:
            self.task_queue.put(
                (
                    "error",
                    f"Không đọc được {len(failures)}/{len(links)} link:\n\n" + "\n".join(failures),
                )
            )
        self.task_queue.put(("videos_loaded", items))
        if items:
            self.task_queue.put(("check_all", None))
            self.task_queue.put(
                ("status", f"Đã nạp {len(items)} video đơn lẻ (đã tick chọn sẵn) — bấm \"Tải video đã chọn\".")
            )
        self.task_queue.put(("load_done", None))

    def _load_worker(self, link: str, filters: FetchFilters):
        platform = detect_platform(link) or "douyin"
        log.info("Bắt đầu lấy danh sách | nền tảng=%s | link=%s | điều kiện: %s",
                 platform, link, filters.describe() if filters.is_active else "không")
        self._auto_refresh_cookies(platform)
        if platform == "tiktok":
            self._load_worker_tiktok(link, filters)
            return
        if platform == "facebook":
            self._load_worker_facebook(link, filters)
            return
        try:
            kind, ident = resolve_link(link)
        except RuntimeError as exc:
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        if kind == "video":
            # Link video đơn lẻ -> nạp thẳng 1 hàng vào bảng thay vì báo lỗi
            self._load_single_worker([link])
            return

        sec_uid = ident
        self.task_queue.put(("status", f"Đang tải danh sách video (sec_uid: {sec_uid[:12]}...)"))
        # Danh sách kênh Douyin đã kèm đủ ngày/tym nên không cần enrich từng video
        collector = self._make_collector(filters)
        try:
            items = self.client.fetch_all_user_posts(
                sec_uid,
                stop_flag=lambda: self.stop_loading_flag,
                collector=collector,
            )
        except DouyinAPIError as exc:
            log.error("Lấy danh sách Douyin thất bại: %s", str(exc).replace("\n", " | "))
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        log.info("Lấy danh sách Douyin xong: %d video", len(items))
        self.task_queue.put(("videos_loaded", items))
        self._post_fetch_summary(collector, self.client.last_warning)
        self.task_queue.put(("load_done", None))

    def _load_worker_tiktok(self, link: str, filters: FetchFilters):
        """Lấy danh sách video của 1 kênh TikTok (qua yt-dlp), đẩy kết quả vào
        queue đúng như luồng Douyin để phần hiển thị/tải dùng chung."""
        try:
            kind, ident = resolve_tiktok_link(link)
        except RuntimeError as exc:
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        if kind == "video":
            self._load_single_worker([link])
            return

        profile_url = ident
        self.task_queue.put(("status", f"Đang lấy danh sách video TikTok: {profile_url}"))
        collector = self._make_collector(filters, self.tiktok_client)
        try:
            items = self.tiktok_client.fetch_all_user_posts(
                profile_url,
                stop_flag=lambda: self.stop_loading_flag,
                collector=collector,
            )
        except TikTokAPIError as exc:
            log.error("Lấy danh sách TikTok thất bại: %s", str(exc).replace("\n", " | "))
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        self.task_queue.put(("videos_loaded", items))
        self._post_fetch_summary(collector, self.tiktok_client.last_warning)
        self.task_queue.put(("load_done", None))

    def _load_worker_facebook(self, link: str, filters: FetchFilters):
        """Link Facebook: video/reel đơn lẻ -> nạp 1 hàng; trang/kênh -> thử liệt
        kê video của trang (best-effort qua yt-dlp)."""
        try:
            kind, ident = resolve_facebook_link(link)
        except RuntimeError as exc:
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        if kind == "video":
            self._load_single_worker([link])
            return

        # Facebook không trả ngày/view/tym trong danh sách -> BỎ QUA mọi điều kiện lọc
        # (ngày, view, tym, thứ tự); chỉ lấy theo SỐ LƯỢNG video, từ MỚI -> CŨ.
        fb_filters = FetchFilters(
            newest_first=True, max_items=filters.max_items, keep_source_order=True,
        )
        ignored = filters.is_active or not filters.newest_first
        note = (
            " (bỏ qua điều kiện ngày/view/tym, chỉ lấy theo số lượng, mới → cũ)"
            if ignored else ""
        )
        self.task_queue.put(("status", f"Đang lấy danh sách video Facebook: {ident}{note}"))
        collector = self._make_collector(fb_filters, self.facebook_client)
        try:
            items = self.facebook_client.fetch_all_user_posts(
                ident,
                stop_flag=lambda: self.stop_loading_flag,
                collector=collector,
            )
        except FacebookAPIError as exc:
            log.error("Lấy danh sách Facebook thất bại: %s", str(exc).replace("\n", " | "))
            self.task_queue.put(("error", str(exc)))
            self.task_queue.put(("load_done", None))
            return

        self.task_queue.put(("videos_loaded", items))
        self._post_fetch_summary(collector, self.facebook_client.last_warning)
        self.task_queue.put(("load_done", None))

    # --------------------------------------------------------- Poll queue --
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.task_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    log.error("Hiện thông báo lỗi cho người dùng: %s", payload.replace("\n", " | "))
                    messagebox.showerror(APP_TITLE, payload)
                elif kind == "videos_loaded":
                    self._populate_tree(payload)
                elif kind == "check_all":
                    self.set_all_checked(True)
                elif kind == "load_done":
                    self.is_busy = False
                    self._set_loading(False)
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
                "mục Cài đặt (Douyin cần Cookie Douyin; TikTok/Facebook có thể cần Cookie "
                "của trang đó, hoặc mở lại app để tự cập nhật yt-dlp; với Facebook hãy thử dán từng link video)."
            )
            return
        # Tra lịch sử MỘT lần cho cả danh sách: video đã tải trước đó được
        # đánh dấu riêng (và vẫn tick chọn / tải lại được nếu người dùng muốn).
        past = self.history.get_many(
            [make_key(it.get("platform"), it["id"]) for it in items]
        )
        in_history = 0
        for item in items:
            vid = item["id"]
            self.videos[vid] = item
            self.order.append(vid)
            self.checked[vid] = False
            self.titles[vid] = item["desc"]
            rec = past.get(make_key(item.get("platform"), vid))
            if rec:
                in_history += 1
                status_text = STATUS_IN_HISTORY
                note = f"Đã tải trước đó lúc {rec.downloaded_at_text} (tổng {rec.download_count} lần)."
                if rec.file_path:
                    note += f"\nFile: {rec.file_path}"
                    if not rec.file_exists:
                        note += "\n(File này hiện không còn ở vị trí cũ — có thể đã bị xóa hoặc di chuyển.)"
                self.logs[vid] = note
            else:
                status_text = STATUS_NOT_DOWNLOADED
            self.statuses[vid] = status_text
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
                    status_text,
                    "",  # cột Tải về: để trống, nút thật sẽ đè lên (xem _create_row_widgets)
                    "",  # cột Log
                    "",  # cột Sửa
                    "",  # cột Xóa
                    len(self.order),  # cột STT (đánh số từ 1)
                ),
            )
            self._create_row_widgets(vid)
        msg = f"Đã tải xong {len(items)} video."
        if in_history:
            msg += f" ({in_history} video đã tải trước đó — xem cột Trạng thái.)"
        self.status_var.set(msg)

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

    # Chú thích (tooltip) cho từng icon
    _ICON_TIPS = {
        "download": "Tải video này",
        "log": "Xem log",
        "edit": "Sửa tiêu đề",
        "delete": "Xóa khỏi danh sách",
    }
    _ICON_SIZE = 20

    def _icon(self, key: str, state: str):
        """Lấy PhotoImage của icon `key` ở trạng thái normal/hover/disabled.
        Tạo lười (lần đầu cần) và cache lại; phải giữ tham chiếu để Tk không
        thu hồi ảnh."""
        cache = self.__dict__.setdefault("_icon_cache", {})
        ck = (key, state)
        if ck not in cache:
            color = ICON_DISABLED if state == "disabled" else ICON_COLORS[key]
            cache[ck] = make_photo(
                key, color, self._ICON_SIZE, hover=(state == "hover"), master=self
            )
        return cache[ck]

    def _create_row_widgets(self, vid: str):
        """Tạo 4 nút ICON (Tải / Xem log / Sửa / Xóa) đè lên hàng `vid` trong
        Treeview. Dùng Label chứa ảnh thay vì Button vì Button gốc trên macOS
        (Aqua) không tô được nền tùy ý. Rê chuột -> icon sáng lên (nền nhạt),
        dừng chuột -> hiện tooltip."""

        def make_icon_btn(key, command):
            lbl = tk.Label(
                self.tree, image=self._icon(key, "normal"),
                bg=self._select_bg if vid in self.tree.selection() else self._row_bg,
                bd=0, padx=0, pady=0, cursor="hand2",
            )
            lbl._enabled = True
            lbl._icon_key = key
            lbl.bind("<Button-1>", lambda e, c=command: c())
            lbl.bind(
                "<Enter>",
                lambda e, l=lbl, k=key: l._enabled and l.config(image=self._icon(k, "hover")),
            )
            lbl.bind(
                "<Leave>",
                lambda e, l=lbl, k=key: l._enabled and l.config(image=self._icon(k, "normal")),
            )
            Tooltip(lbl, self._ICON_TIPS[key])
            return lbl

        self.row_widgets[vid] = {
            "download": make_icon_btn("download", lambda v=vid: self.start_single_download(v)),
            "log": make_icon_btn("log", lambda v=vid: self.show_log_popup(v)),
            "edit": make_icon_btn("edit", lambda v=vid: self.edit_title(v)),
            "delete": make_icon_btn("delete", lambda v=vid: self.delete_single(v)),
        }

    def _set_download_link_enabled(self, vid: str, enabled: bool):
        """Bật/tắt icon Tải của 1 hàng (xám + không bấm được khi video đó
        đang tải, tránh bấm trùng)."""
        widgets = self.row_widgets.get(vid)
        if not widgets:
            return
        lbl = widgets["download"]
        lbl._enabled = enabled
        if enabled:
            lbl.config(image=self._icon("download", "normal"), cursor="hand2")
            lbl.bind("<Button-1>", lambda e, v=vid: self.start_single_download(v))
        else:
            lbl.config(image=self._icon("download", "disabled"), cursor="arrow")
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

        # Video đã có trong lịch sử tải: hỏi bỏ qua hay tải lại
        past = self.history.get_many(
            [make_key(self.videos[v].get("platform"), v) for v in ids if v in self.videos]
        )
        dup_ids = [
            v for v in ids
            if v in self.videos and make_key(self.videos[v].get("platform"), v) in past
        ]
        if dup_ids:
            answer = messagebox.askyesnocancel(
                APP_TITLE,
                f"{len(dup_ids)}/{len(ids)} video đã chọn đã được tải trước đó.\n\n"
                "• Có: BỎ QUA các video đã tải, chỉ tải phần còn lại\n"
                "• Không: tải LẠI tất cả (tạo thêm file mới)\n"
                "• Hủy: không tải gì cả",
            )
            if answer is None:
                return
            if answer:
                dup_set = set(dup_ids)
                ids = [v for v in ids if v not in dup_set]
                if not ids:
                    messagebox.showinfo(
                        APP_TITLE, "Tất cả video đã chọn đều đã được tải trước đó nên không có gì để tải."
                    )
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
                dl_client = self._client_for(item.get("platform"))
                extra = {}
                if item.get("platform") in ("facebook", "tiktok"):
                    # ffmpeg để ghép hình + tiếng chất lượng cao (rỗng = không có)
                    extra["ffmpeg_location"] = find_ffmpeg(
                        self.cfg.get("merge_ffmpeg_path", "")
                    ) or ""
                dl_client.download_video(
                    item["url"], dest, chunk_cb=chunk_cb,
                    stop_flag=self.download_stop_event.is_set, **extra,
                )
            except DownloadCancelled:
                log.info("Đã dừng tải giữa chừng | id=%s", vid)
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
            except (requests.RequestException, OSError, TikTokAPIError, FacebookAPIError) as exc:
                log.error("Tải thất bại | %s | id=%s | url=%s | %s: %s",
                          item.get("platform") or "douyin", vid, item.get("url", "")[:140],
                          type(exc).__name__, exc)
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
            # Ghi vào lịch sử (lỗi ghi chỉ lưu ở history.last_error, không làm hỏng việc tải)
            log.info("Tải xong | %s | id=%s | %s", item.get("platform") or "douyin", vid, dest)
            if not self.history.record(
                item.get("platform"), vid, title=title, original_title=item.get("desc", ""),
                url=item.get("url", ""), file_path=dest,
            ):
                log.warning("Không ghi được lịch sử tải: %s", self.history.last_error)
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