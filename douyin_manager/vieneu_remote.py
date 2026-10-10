"""
vieneu_remote.py
================
Chạy VieNeu ở TIẾN TRÌNH RIÊNG (Python của venv `~/.douyin_vieneu_venv`) cho app đóng gói
(.exe/.app). Nạp numpy/onnxruntime/torch trong file PyInstaller từ thư mục ngoài hay lỗi DLL;
chạy bằng Python thường thì không.

`VieNeuWorker` có cùng giao diện với engine `vieneu.Vieneu` mà `tts_local.VieNeuTTS` dùng:
    engine.infer(text, **kwargs) -> token     engine.save(token, path)
Giao thức: mỗi dòng stdin/stdout là một JSON.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Any

from . import vieneu_installer as vi

WORKER_FILE = "dvm_vieneu_worker.py"

# Mã chạy trong Python của venv: chỉ dùng thư viện chuẩn + vieneu, không import gì của app.
WORKER_SOURCE = r'''
import io, json, os, sys, traceback
from pathlib import Path

_proto = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stdout = sys.stderr          # thư viện in gì ra stdout cũng không làm hỏng giao thức


def reply(obj):
    _proto.write(json.dumps(obj) + "\n")
    _proto.flush()


def _is_ort_path_error(exc):
    m = str(exc)
    return "External data path" in m or "escapes model directory" in m


def _materialize_hf_symlinks():
    import shutil
    root = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")) / "hub"
    n = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = Path(os.path.realpath(path))
        if not target.is_file():
            continue
        tmp = path.with_name(path.name + ".tmp_real")
        try:
            tmp.unlink(missing_ok=True)
            try:
                os.link(target, tmp)
            except OSError:
                shutil.copy2(target, tmp)
            os.replace(tmp, path)
            n += 1
        except OSError:
            pass
    return n


def load_engine():
    from vieneu import Vieneu
    try:
        return Vieneu()
    except Exception as exc:
        if _is_ort_path_error(exc) and _materialize_hf_symlinks():
            return Vieneu()
        raise


def main():
    engine = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "quit":
                reply({"ok": True})
                return
            if op == "init":
                if engine is None:
                    engine = load_engine()
                reply({"ok": True})
            elif op == "infer":
                if engine is None:
                    engine = load_engine()
                kw = dict(req.get("kwargs") or {})
                if req.get("temperature") is not None:
                    kw["temperature"] = req["temperature"]
                audio = engine.infer(req["text"], **kw)
                engine.save(audio, req["out"])
                reply({"ok": True})
            else:
                reply({"ok": False, "etype": "ValueError", "error": "op không hợp lệ: %r" % (op,)})
        except Exception as exc:
            traceback.print_exc()
            reply({"ok": False, "etype": type(exc).__name__, "error": str(exc)})


if __name__ == "__main__":
    main()
'''


def write_worker() -> str:
    vi.VENV_DIR.mkdir(parents=True, exist_ok=True)
    path = vi.VENV_DIR / WORKER_FILE
    if not path.is_file() or path.read_text(encoding="utf-8") != WORKER_SOURCE:
        path.write_text(WORKER_SOURCE, encoding="utf-8")
    return str(path)


class VieNeuWorker:
    """Engine VieNeu chạy ở tiến trình con. Tạo đối tượng = nạp model (có thể lâu lần đầu)."""

    def __init__(self):
        py = vi.venv_python()
        if not py.is_file():
            raise RuntimeError("Chưa cài VieNeu-TTS (không thấy môi trường Python riêng).")
        script = write_worker()
        self._log_path = vi.VENV_DIR / "worker.log"
        self._log = open(self._log_path, "wb")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [str(py), "-u", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, encoding="utf-8", bufsize=1, env=env, **vi._nowin(),
        )
        atexit.register(self.close)
        self._call({"op": "init"})

    # ------------------------------------------------------------------ nội bộ
    def _log_tail(self, n: int = 4) -> str:
        try:
            self._log.flush()
            lines = [l.strip() for l in self._log_path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            return " | ".join(lines[-n:])[:500]
        except OSError:
            return ""

    def _call(self, req: dict) -> dict:
        with self._lock:
            proc = self._proc
            try:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Mất kết nối tới tiến trình VieNeu: {exc}. {self._log_tail()}") from exc
            if not line:
                rc = proc.poll()
                raise RuntimeError(f"Tiến trình VieNeu đã dừng (mã {rc}). {self._log_tail()}")
            try:
                resp = json.loads(line)
            except ValueError as exc:
                raise RuntimeError(f"Phản hồi lạ từ tiến trình VieNeu: {line[:200]!r}") from exc
        if resp.get("ok"):
            return resp
        msg = resp.get("error") or "lỗi không rõ"
        if resp.get("etype") == "TypeError":
            raise TypeError(msg)
        raise RuntimeError(msg)

    # ------------------------------------------- giao diện giống vieneu.Vieneu
    def infer(self, text: str, temperature: Any = None, **kwargs):
        fd, out = tempfile.mkstemp(prefix="vieneu_", suffix=".wav")
        os.close(fd)
        try:
            self._call({"op": "infer", "text": text, "kwargs": kwargs,
                        "temperature": temperature, "out": out})
        except BaseException:
            try:
                os.unlink(out)
            except OSError:
                pass
            raise
        return out

    def save(self, audio, path: str) -> None:
        shutil.move(str(audio), str(path))

    def close(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        finally:
            try:
                self._log.close()
            except OSError:
                pass
