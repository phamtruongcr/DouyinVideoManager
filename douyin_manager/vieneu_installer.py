"""
vieneu_installer.py
===================
Cài thư viện `vieneu` (backend VieNeu-TTS) ngay từ giao diện, không cần gõ lệnh.

  - `is_installed()`            : đã dùng được VieNeu chưa.
  - `installed_version()`       : phiên bản đang cài ('' nếu chưa).
  - `install_vieneu()`          : chạy đồng bộ, trả (ok, thông điệp tiếng Việt).
  - `install_vieneu_in_background`: chạy ở luồng nền, báo từng dòng tiến độ qua `on_progress`.

Hai chế độ:
  * Chạy từ mã nguồn  : `python -m pip install vieneu` bằng đúng Python đang chạy app,
    VieNeu chạy ngay trong tiến trình app.
  * App đóng gói (.exe/.app, `sys.frozen`): `sys.executable` là chính app nên không chạy pip
    được, và nạp numpy/onnxruntime từ ngoài file đóng gói hay lỗi DLL. Vì vậy app tìm một
    Python 3.10+ có sẵn trên máy, tạo venv riêng (`~/.douyin_vieneu_venv`), cài vieneu vào đó
    và chạy VieNeu ở TIẾN TRÌNH RIÊNG bằng Python của venv (xem `vieneu_remote.py`).
    Không cần trùng phiên bản Python với bản đóng gói.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

PACKAGE = "vieneu"
MIN_PYTHON = (3, 10)
PIP_TIMEOUT_S = 1800            # tải model/phụ thuộc có thể nặng (torch/onnx...), cho tối đa 30 phút
VENV_DIR = Path.home() / ".douyin_vieneu_venv"      # chỉ dùng cho app đóng gói

# App .app mở từ Finder chỉ có PATH tối thiểu (/usr/bin:/bin:...) nên `shutil.which` không thấy
# Python của Homebrew / MacPorts / python.org. Dò thẳng các thư mục này (chỉ dùng trên macOS).
MAC_PYTHON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")
MAC_PYTHON_FRAMEWORK = "/Library/Frameworks/Python.framework/Versions/{v}/bin/python3"
PREFERRED_VERSIONS = ("3.12", "3.11", "3.10", "3.13")

ProgressCb = Optional[Callable[[str], None]]

_install_lock = threading.Lock()


# ============================================================ dò trạng thái ==
def _nowin() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW


def venv_python() -> Path:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return VENV_DIR.joinpath(*sub)


def _venv_site_dirs() -> list:
    return [*VENV_DIR.glob("Lib/site-packages"), *VENV_DIR.glob("lib/python*/site-packages")]


def _venv_has_vieneu() -> bool:
    return any((d / PACKAGE).is_dir() for d in _venv_site_dirs()) and venv_python().is_file()


def _bundled_in_app() -> bool:
    """vieneu được PyInstaller đóng sẵn vào app (build với BUNDLE_VIENEU=1) hoặc đang chạy từ mã nguồn."""
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec(PACKAGE) is not None
    except (ImportError, ValueError):
        return False


def use_worker() -> bool:
    """True = chạy VieNeu ở tiến trình riêng (app đóng gói và không đóng sẵn vieneu)."""
    return bool(getattr(sys, "frozen", False)) and not _bundled_in_app()


def is_installed() -> bool:
    if getattr(sys, "frozen", False):
        return _bundled_in_app() or _venv_has_vieneu()
    return _bundled_in_app()


def installed_version() -> str:
    if use_worker():
        for d in _venv_site_dirs():
            for info in d.glob(f"{PACKAGE}-*.dist-info"):
                return info.name[len(PACKAGE) + 1:-len(".dist-info")]
        return ""
    try:
        from importlib import metadata
        return metadata.version(PACKAGE)
    except Exception:  # noqa: BLE001
        return ""


def python_ok() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


# ================================================================= chạy pip ==
def _probe_python(cmd: list) -> Optional[tuple]:
    try:
        out = subprocess.run(
            [*cmd, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=20, **_nowin(),
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"^(\d+)\.(\d+)$", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _mac_python_candidates() -> list:
    """Python ở các vị trí cố định của macOS (Homebrew Apple Silicon/Intel, MacPorts, python.org)."""
    out: list = []
    for v in PREFERRED_VERSIONS:
        for d in MAC_PYTHON_BIN_DIRS:
            exe = Path(d) / f"python{v}"
            if exe.is_file():
                out.append([str(exe)])
        fw = Path(MAC_PYTHON_FRAMEWORK.format(v=v))
        if fw.is_file():
            out.append([str(fw)])
    return out


def _find_external_python() -> Optional[list]:
    """Tìm Python >= 3.10 ngoài app (dùng khi app đã đóng gói). Ưu tiên 3.12/3.11/3.10
    vì các thư viện nặng thường có sẵn bản dựng cho các bản này. Trả tiền tố lệnh."""
    cands: list = []
    if os.name == "nt" and shutil.which("py"):
        cands += [["py", f"-{v}"] for v in PREFERRED_VERSIONS]
    for v in PREFERRED_VERSIONS[:3]:
        exe = shutil.which(f"python{v}")
        if exe:
            cands.append([exe])
    if sys.platform == "darwin":
        # Đặt TRƯỚC python3/python chung để bản 3.12 của Homebrew thắng 3.9 của Apple / 3.14 mới tinh.
        cands += _mac_python_candidates()
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe:
            cands.append([exe])
    if os.name == "nt":
        # Vừa cài Python xong thì PATH của tiến trình này chưa cập nhật: dò thêm thư mục mặc định.
        roots = [os.environ.get("LOCALAPPDATA", "") and Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python",
                 os.environ.get("ProgramFiles", "") and Path(os.environ["ProgramFiles"])]
        for v in ("312", "311", "310", "313"):
            for root in roots:
                if root:
                    exe = Path(root) / f"Python{v}" / "python.exe"
                    if exe.is_file():
                        cands.append([str(exe)])
    seen: set = set()
    for cmd in cands:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        ver = _probe_python(cmd)
        if ver and ver >= MIN_PYTHON:
            return cmd
    return None


def _install_python_hint() -> str:
    if sys.platform == "darwin":
        return (
            "Hãy cài Python 3.12 (Terminal: brew install python@3.12, hoặc tải từ python.org) "
            "rồi bấm cài lại. Lưu ý: python3 có sẵn của macOS chỉ là 3.9, không dùng được."
        )
    return (
        "Hãy cài Python 3.12 từ python.org (nhớ tick \"Add to PATH\" hoặc \"py launcher\") "
        "rồi bấm cài lại, hoặc chạy app từ mã nguồn."
    )


def _ensure_venv(on_progress: ProgressCb) -> tuple:
    """Tạo venv riêng cho app đóng gói. Trả (python_cmd, lỗi)."""
    if venv_python().is_file():
        return [str(venv_python())], ""
    ext = _find_external_python()
    if not ext:
        return None, (
            "App đang chạy dạng đóng gói và không tìm thấy Python 3.10+ trên máy để cài VieNeu. "
            + _install_python_hint()
        )
    if on_progress:
        on_progress("Đang tạo môi trường Python riêng cho VieNeu...")
    try:
        r = subprocess.run([*ext, "-m", "venv", str(VENV_DIR)], capture_output=True,
                           text=True, timeout=300, **_nowin())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Không tạo được môi trường Python cho VieNeu: {exc}"
    if r.returncode != 0 or not venv_python().is_file():
        return None, "Không tạo được môi trường Python cho VieNeu: " + (_tail(r.stdout + "\n" + r.stderr) or "lỗi không rõ")
    return [str(venv_python())], ""


def _build_cmd(upgrade: bool, on_progress: ProgressCb = None) -> tuple:
    """Dựng lệnh pip. Trả (lệnh, lỗi). Lệnh None nghĩa là không cài tự động được."""
    args = ["-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary"]
    if upgrade:
        args.append("-U")
    args.append(PACKAGE)
    if getattr(sys, "frozen", False):
        py, err = _ensure_venv(on_progress)
        return (None, err) if py is None else ([*py, *args], "")
    if not python_ok():
        return None, (
            f"VieNeu cần Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} trở lên "
            f"(đang chạy {sys.version_info.major}.{sys.version_info.minor}). "
            "Hãy cài Python mới hơn rồi chạy lại app, hoặc dùng backend Gemini TTS."
        )
    return [sys.executable, *args], ""


def _purge_vieneu_modules() -> None:
    """Bỏ vieneu khỏi cache import + đặt lại engine đã nạp, để lần dùng kế tiếp nạp bản mới."""
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    try:
        from . import tts_local
        with tts_local._VIENEU_LOCK:
            eng, tts_local._VIENEU_ENGINE = tts_local._VIENEU_ENGINE, None
        close = getattr(eng, "close", None)
        if callable(close):
            close()      # tiến trình VieNeu cũ phải tắt trước khi pip ghi đè file (Windows khóa file)
    except Exception:  # noqa: BLE001
        pass


def _tail(text: str, n: int = 3) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:300]


def _is_build_error(text: str) -> bool:
    t = text.lower()
    return ("failed-wheel-build" in t or "failed to build" in t
            or "microsoft visual c++" in t or "cmake" in t and "error" in t)


def _build_error_message(text: str) -> str:
    m = re.search(r"(?:Failed to build|building wheel for)\s+([A-Za-z0-9_.\-]+)", text, re.I)
    pkg = m.group(1) if m else "một thư viện phụ thuộc"
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"Cài VieNeu thất bại: pip phải tự biên dịch {pkg} vì chưa có bản dựng sẵn cho "
        f"Python {ver} của bạn (máy lại chưa có trình biên dịch C++).\n\n"
        "Cách khắc phục (chọn 1):\n"
        "1) Dùng Python 3.12 (có bản dựng sẵn cho hầu hết thư viện): cài Python 3.12 từ python.org, "
        "chạy lại: py -3.12 -m pip install -r requirements.txt vieneu rồi mở app bằng py -3.12 main.py.\n"
        "2) Hoặc cài \"Microsoft C++ Build Tools\" (workload Desktop development with C++) "
        "rồi bấm cài lại.\n"
        "3) Hoặc chọn backend Gemini TTS (không cần cài gì)."
    )


def install_vieneu(on_progress: ProgressCb = None, upgrade: bool = False) -> tuple[bool, str]:
    """Cài (hoặc nâng cấp) vieneu. Chạy đồng bộ — gọi từ luồng nền."""
    if not _install_lock.acquire(blocking=False):
        return False, "Đang có một lần cài VieNeu khác chạy, vui lòng đợi."
    try:
        if is_installed() and not upgrade:
            return True, f"VieNeu đã được cài (phiên bản {installed_version() or 'không rõ'})."
        _purge_vieneu_modules()     # tắt tiến trình VieNeu đang chạy (nếu có) trước khi cài/nâng cấp
        cmd, err = _build_cmd(upgrade, on_progress)
        if cmd is None:
            return False, err
        before = installed_version()
        kwargs = _nowin()
        if on_progress:
            on_progress("Đang chạy pip install vieneu (có thể mất vài phút, tải nhiều thư viện)...")
        collected: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", **kwargs,
            )
        except OSError as exc:
            return False, f"Không chạy được pip: {exc}"

        timer = threading.Timer(PIP_TIMEOUT_S, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                collected.append(line)
                if on_progress and re.match(r"^(Collecting|Downloading|Installing|Successfully|Building|Using cached)", line):
                    on_progress(line[:140])
            rc = proc.wait()
        finally:
            timer.cancel()

        if rc != 0:
            full = "\n".join(collected)
            if _is_build_error(full):
                return False, _build_error_message(full)
            return False, "Cài VieNeu thất bại: " + (_tail(full) or f"mã lỗi {rc}")

        _purge_vieneu_modules()
        if not is_installed():
            return False, (
                "pip báo xong nhưng app vẫn chưa nhập được VieNeu. "
                "Hãy đóng và mở lại app; nếu vẫn lỗi hãy chạy: pip install vieneu"
            )
        after = installed_version()
        if upgrade and before and after and before != after:
            return True, f"Đã nâng cấp VieNeu {before} → {after}. Lần đọc đầu tiên sẽ tải model nên hơi lâu."
        return True, f"Đã cài VieNeu {after or ''}. Lần đọc đầu tiên sẽ tải model nên hơi lâu.".replace("  ", " ")
    finally:
        _install_lock.release()


def install_vieneu_in_background(
    on_progress: ProgressCb = None,
    on_done: Optional[Callable[[bool, str], None]] = None,
    upgrade: bool = False,
) -> threading.Thread:
    """Chạy `install_vieneu` ở luồng nền. Cả `on_progress` lẫn `on_done` được gọi từ LUỒNG NỀN
    (nơi gọi tự chuyển về luồng giao diện, ví dụ qua task_queue)."""

    def run():
        try:
            ok, msg = install_vieneu(on_progress, upgrade=upgrade)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"Lỗi không mong đợi khi cài VieNeu: {exc}"
        if on_done:
            try:
                on_done(ok, msg)
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=run, name="vieneu-installer", daemon=True)
    t.start()
    return t