"""Test worker VieNeu chạy tiến trình riêng, dùng gói `vieneu` giả (không cần cài thật)."""
import os
import sys
import tempfile
import textwrap
import unittest
import wave
from pathlib import Path
from unittest import mock

from douyin_manager import vieneu_installer as vi
from douyin_manager import vieneu_remote as vr

FAKE_VIENEU = textwrap.dedent('''
    import wave
    class Vieneu:
        def infer(self, text, temperature=None, ref_audio=None, **kw):
            if text == "boom":
                raise RuntimeError("hỏng rồi")
            if kw.get("strict") and temperature is not None:
                raise TypeError("no temperature")
            return text
        def save(self, audio, path):
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
                w.writeframes(b"\\x01\\x00" * (100 + len(audio)))
''')


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        pkg = cls.tmp / "fake" / "vieneu"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(FAKE_VIENEU, encoding="utf-8")
        cls._env = mock.patch.dict(os.environ, {"PYTHONPATH": str(cls.tmp / "fake")})
        cls._env.start()
        cls._p1 = mock.patch.object(vi, "VENV_DIR", cls.tmp / "venv")
        cls._p2 = mock.patch.object(vi, "venv_python", return_value=Path(sys.executable))
        cls._p1.start(); cls._p2.start()
        cls.worker = vr.VieNeuWorker()

    @classmethod
    def tearDownClass(cls):
        cls.worker.close()
        cls._p1.stop(); cls._p2.stop(); cls._env.stop()

    def test_infer_and_save_roundtrip(self):
        audio = self.worker.infer("xin chào")
        out = self.tmp / "out.wav"
        self.worker.save(audio, str(out))
        with wave.open(str(out)) as w:
            self.assertEqual(w.getnframes(), 100 + len("xin chào"))
        self.assertFalse(Path(audio).exists())     # file tạm đã được chuyển đi

    def test_error_is_reported_not_fatal(self):
        with self.assertRaises(RuntimeError) as cm:
            self.worker.infer("boom")
        self.assertIn("hỏng rồi", str(cm.exception))
        self.worker.infer("vẫn sống")             # worker vẫn dùng được sau lỗi

    def test_typeerror_is_preserved_for_temperature_fallback(self):
        with self.assertRaises(TypeError):
            self.worker.infer("a", temperature=0.5, strict=True)

    def test_stdout_noise_does_not_break_protocol(self):
        # 'print' trong thư viện đi vào stderr, giao thức vẫn nguyên vẹn
        self.assertTrue(Path(self.worker.infer("ok")).exists())

    def test_log_written(self):
        self.assertTrue((vi.VENV_DIR / "worker.log").exists())



class FrozenLoaderTests(unittest.TestCase):
    def test_frozen_not_installed_gives_install_hint(self):
        from douyin_manager import tts_local
        with mock.patch.object(tts_local, "_VIENEU_ENGINE", None), \
                mock.patch.object(vi, "use_worker", return_value=True), \
                mock.patch.object(vi, "is_installed", return_value=False):
            with self.assertRaises(tts_local.TTSError) as cm:
                tts_local._load_vieneu_engine()
        self.assertIn("Cài VieNeu", str(cm.exception))

    def test_dll_error_gets_vc_redist_hint(self):
        from douyin_manager import tts_local
        exc = ImportError("DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found.")
        self.assertIn("vc_redist.x64.exe", tts_local._vieneu_hint(exc))
        self.assertEqual(tts_local._vieneu_hint(RuntimeError("khác")), "")


if __name__ == "__main__":
    unittest.main()
