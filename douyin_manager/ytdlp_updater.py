"""
ytdlp_updater.py
================
Tự cập nhật yt-dlp bằng pip để luôn có bản vá mới nhất của cộng đồng khi
TikTok/Facebook đổi thuật toán:

    subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])

  - `update_ytdlp()`            : chạy đồng bộ, trả (ok, thông điệp tiếng Việt).
  - `update_ytdlp_in_background`: chạy ở luồng nền (không chặn giao diện), có
    giới hạn tần suất (mặc định 12 giờ/lần khi gọi kiểu "tự động").

LƯU Ý: khi app đã đóng gói bằng PyInstaller (`sys.frozen`), `sys.executable`
là chính file .exe/.app chứ không phải Python nên KHÔNG thể chạy pip. Khi đó
hàm trả về thông điệp hướng dẫn thay vì cố chạy (tránh mở ra nhiều cửa sổ app).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

AUTO_INTERVAL_S = 12 * 3600
PIP_TIMEOUT_S = 240


def installed_version() -> str:
    """Phiên bản yt-dlp đang cài ('' nếu chưa cài)."""
    try:
        from importlib import metadata
        return metadata.version("yt-dlp")
    except Exception:  # noqa: BLE001
        return ""


def _purge_ytdlp_modules():
    """Bỏ yt_dlp khỏi cache import để LẦN import kế tiếp nạp bản mới (không đụng
    tới các đối tượng yt-dlp đang chạy dở)."""
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


def update_ytdlp() -> tuple[bool, str]:
    if getattr(sys, "frozen", False):
        return False, (
            "App đang chạy dạng đóng gói (.exe/.app) nên không tự cập nhật yt-dlp được. "
            "Hãy build lại app với yt-dlp mới nhất (pip install -U yt-dlp)."
        )
    before = installed_version()
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check",
           "--quiet", "yt-dlp"]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: không nháy cửa sổ đen
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S, **kwargs
        )
    except subprocess.TimeoutExpired:
        return False, "Cập nhật yt-dlp quá thời gian (mạng chậm?). Sẽ thử lại lần sau."
    except OSError as exc:
        return False, f"Không chạy được pip để cập nhật yt-dlp: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, "Cập nhật yt-dlp thất bại: " + (tail[-1][:200] if tail else f"mã {proc.returncode}")
    after = installed_version()
    if after and after != before:
        _purge_ytdlp_modules()
        return True, f"Đã cập nhật yt-dlp {before or '?'} → {after}."
    return True, f"yt-dlp đã là bản mới nhất ({after or 'không rõ'})."


def update_ytdlp_in_background(
    on_done: Optional[Callable[[bool, str], None]] = None,
    cfg: Optional[dict] = None,
    save_cfg: Optional[Callable[[dict], None]] = None,
    force: bool = False,
) -> Optional[threading.Thread]:
    """Chạy `update_ytdlp` ở luồng nền. `on_done(ok, msg)` được gọi từ LUỒNG NỀN
    (nơi gọi tự chuyển về luồng giao diện, ví dụ qua task_queue).

    Khi `force=False` (tự động lúc khởi động): bỏ qua nếu cfg["auto_update_ytdlp"]
    là False hoặc đã cập nhật trong vòng AUTO_INTERVAL_S. `force=True` (nút bấm
    tay) luôn chạy. Trả về Thread, hoặc None nếu bị bỏ qua."""
    cfg = cfg if cfg is not None else {}
    if not force:
        if not cfg.get("auto_update_ytdlp", True):
            return None
        last = float(cfg.get("ytdlp_last_update", 0) or 0)
        if time.time() - last < AUTO_INTERVAL_S:
            return None

    def run():
        ok, msg = update_ytdlp()
        if ok and cfg is not None:
            cfg["ytdlp_last_update"] = time.time()
            if save_cfg:
                try:
                    save_cfg(cfg)
                except Exception:  # noqa: BLE001
                    pass
        if on_done:
            try:
                on_done(ok, msg)
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=run, name="ytdlp-updater", daemon=True)
    t.start()
    return t
