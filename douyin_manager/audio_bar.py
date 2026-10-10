"""
audio_bar.py
============
Thanh nghe thử kết quả "Tạo giọng nói": tên giọng, dạng sóng bấm để tua, ↺5s / Phát-Tạm dừng / 5s↻,
nút Tải về và nút đóng. Phát bằng AudioPlayer dùng chung của tab (file WAV tạm cắt từ vị trí hiện tại).
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

from . import theme
from .tts_local import slice_wav, wav_duration_seconds
from .widgets import RoundedButton

SKIP_SECONDS = 5.0
_BAR_PLAYED = theme.FG
_BAR_REST = "#3a3f4a"


def format_clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class AudioBar(tk.Frame):
    def __init__(
        self, master, player, tmp_dir: Callable[[], Path],
        on_download: Callable[[], None], on_close: Callable[[], None],
        on_status: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(master, bg=theme.CARD)
        self._player = player
        self._tmp_dir = tmp_dir
        self._on_status = on_status or (lambda _m: None)
        self._wav: bytes = b""
        self._peaks: list[float] = []
        self._dur = 0.0
        self._pos = 0.0            # vị trí khi đang tạm dừng
        self._offset = 0.0         # vị trí lúc bắt đầu phát
        self._started: Optional[float] = None   # monotonic lúc bắt đầu phát; None = không phát
        self._seq = 0
        self._last_tmp: Optional[Path] = None
        self._tick_job = None

        head = tk.Frame(self, bg=theme.CARD)
        head.pack(fill="x")
        self.title_var = tk.StringVar(value="")
        tk.Label(
            head, textvariable=self.title_var, bg=theme.CARD, fg=theme.FG,
            font=("", 10, "bold"), anchor="w",
        ).pack(side="left", fill="x", expand=True)
        close = tk.Label(head, text="✕", bg=theme.CARD, fg=theme.MUTED, cursor="hand2", padx=6)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: on_close())
        close.bind("<Enter>", lambda e: close.configure(fg=theme.FG))
        close.bind("<Leave>", lambda e: close.configure(fg=theme.MUTED))
        RoundedButton(
            head, text="⬇  Tải về", command=on_download, bg=theme.FG, hover_bg="#ffffff", fg=theme.BG,
            padx=14, pady=4, radius=10, parent_bg=theme.CARD,
        ).pack(side="right", padx=(6, 6))

        ctrl = tk.Frame(self, bg=theme.CARD)
        ctrl.pack(pady=(8, 2))
        self._skip_btn(ctrl, "↺ 5", lambda: self.seek(-SKIP_SECONDS)).pack(side="left", padx=14)
        self.play_cv = tk.Canvas(ctrl, width=44, height=44, bg=theme.CARD, highlightthickness=0, cursor="hand2")
        self.play_cv.pack(side="left")
        self.play_cv.bind("<Button-1>", lambda e: self.toggle())
        self._skip_btn(ctrl, "5 ↻", lambda: self.seek(SKIP_SECONDS)).pack(side="left", padx=14)

        wave_row = tk.Frame(self, bg=theme.CARD)
        wave_row.pack(fill="x", pady=(2, 0))
        self.cur_var = tk.StringVar(value="0:00")
        self.tot_var = tk.StringVar(value="0:00")
        tk.Label(wave_row, textvariable=self.cur_var, bg=theme.CARD, fg=theme.MUTED, font=("", 9), width=4).pack(side="left")
        self.wave = tk.Canvas(wave_row, height=36, bg=theme.CARD, highlightthickness=0, cursor="hand2")
        self.wave.pack(side="left", fill="x", expand=True, padx=4)
        tk.Label(wave_row, textvariable=self.tot_var, bg=theme.CARD, fg=theme.MUTED, font=("", 9), width=4).pack(side="left")
        self.wave.bind("<Configure>", lambda e: self._draw())
        self.wave.bind("<Button-1>", self._on_wave_click)
        self._draw_play_icon()

    # ------------------------------------------------------------ dựng --
    def _skip_btn(self, parent, text, command):
        lbl = tk.Label(parent, text=text, bg=theme.CARD, fg=theme.FG, cursor="hand2", font=("", 10))
        lbl.bind("<Button-1>", lambda e: command())
        lbl.bind("<Enter>", lambda e: lbl.configure(fg="#ffffff"))
        lbl.bind("<Leave>", lambda e: lbl.configure(fg=theme.FG))
        return lbl

    def _draw_play_icon(self):
        cv = self.play_cv
        cv.delete("all")
        cv.create_oval(2, 2, 42, 42, fill=theme.FG, outline=theme.FG)
        if self.playing:
            cv.create_rectangle(15, 14, 20, 30, fill=theme.BG, outline=theme.BG)
            cv.create_rectangle(24, 14, 29, 30, fill=theme.BG, outline=theme.BG)
        else:
            cv.create_polygon(17, 13, 17, 31, 32, 22, fill=theme.BG, outline=theme.BG)

    def _draw(self):
        cv = self.wave
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w <= 4:
            return
        peaks = self._peaks or [0.15] * 60
        n = len(peaks)
        slot = w / n
        bw = max(2.0, slot - 2.0)
        frac = (self.position() / self._dur) if self._dur > 0 else 0.0
        for i, pk in enumerate(peaks):
            x = i * slot + (slot - bw) / 2
            bh = max(3.0, pk * (h - 4))
            colour = _BAR_PLAYED if (i + 0.5) / n <= frac else _BAR_REST
            cv.create_rectangle(x, (h - bh) / 2, x + bw, (h + bh) / 2, fill=colour, outline=colour)
        self.cur_var.set(format_clock(self.position()))

    # ------------------------------------------------------------ trạng thái --
    @property
    def playing(self) -> bool:
        return self._started is not None

    def position(self) -> float:
        if self._started is not None:
            return min(self._dur, self._offset + (time.monotonic() - self._started))
        return self._pos

    def load(self, wav_bytes: bytes, title: str, peaks: Optional[list[float]] = None):
        self.stop()
        self._wav = wav_bytes
        self._peaks = list(peaks or [])
        self._dur = wav_duration_seconds(wav_bytes)
        self._pos = 0.0
        self.title_var.set(title)
        self.tot_var.set(format_clock(self._dur))
        self._draw_play_icon()
        self._draw()

    # ------------------------------------------------------------ điều khiển --
    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self._play_from(self._pos if self._pos < self._dur - 0.05 else 0.0)

    def pause(self):
        self._pos = self.position()
        self._started = None
        self._player.stop()
        self._cancel_tick()
        self._draw_play_icon()
        self._draw()

    def stop(self):
        """Dừng hẳn và về đầu (dùng khi nạp audio mới / đóng thanh)."""
        was = self.playing
        self._started = None
        self._pos = 0.0
        self._cancel_tick()
        if was:
            self._player.stop()
        self._draw_play_icon()
        self._draw()

    def seek(self, delta: float):
        if not self._wav:
            return
        self._seek_to(self.position() + delta)

    def _on_wave_click(self, event):
        w = self.wave.winfo_width()
        if self._wav and w > 0:
            self._seek_to(self._dur * max(0.0, min(1.0, event.x / w)))

    def _seek_to(self, seconds: float):
        seconds = max(0.0, min(self._dur, seconds))
        if self.playing:
            self._play_from(seconds)
        else:
            self._pos = seconds
            self._draw()

    def _play_from(self, seconds: float):
        if not self._wav:
            return
        self._seq += 1
        path = self._tmp_dir() / f"player_{self._seq}.wav"
        try:
            path.write_bytes(slice_wav(self._wav, seconds) if seconds > 0.01 else self._wav)
        except OSError as exc:
            self._on_status(f"Không ghi được file phát tạm: {exc}")
            return
        if not self._player.play(path):
            self._on_status("Không phát được âm thanh tự động — hãy bấm Tải về rồi mở file.")
            return
        if self._last_tmp and self._last_tmp != path:
            try:
                self._last_tmp.unlink()
            except OSError:
                pass
        self._last_tmp = path
        self._offset = seconds
        self._started = time.monotonic()
        self._draw_play_icon()
        self._cancel_tick()
        self._tick()

    def _tick(self):
        if not self.playing:
            return
        if self.position() >= self._dur - 0.02:      # phát hết -> về đầu, nút thành "Phát"
            self._started = None
            self._pos = 0.0
            self._draw_play_icon()
            self._draw()
            return
        self._draw()
        self._tick_job = self.after(100, self._tick)

    def _cancel_tick(self):
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except tk.TclError:
                pass
            self._tick_job = None
