"""Test đọc giọng (không gọi mạng, không cần VieNeu). Chạy: python -m unittest discover -s tests -v"""

import base64
import io
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from douyin_manager import tts_local as tts
from douyin_manager.config import TTS_BACKEND_GEMINI, TTS_BACKEND_VIENEU


def read_wav(blob: bytes):
    with wave.open(io.BytesIO(blob), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


def make_pcm(n_frames: int, value: int = 1000) -> bytes:
    return value.to_bytes(2, "little", signed=True) * n_frames


class PcmToWavTests(unittest.TestCase):
    def test_header_and_data(self):
        pcm = make_pcm(240)
        wav = tts.pcm_to_wav(pcm, 24000)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        ch, width, rate, frames = read_wav(wav)
        self.assertEqual((ch, width, rate), (1, 2, 24000))
        self.assertEqual(frames, pcm)

    def test_custom_params(self):
        wav = tts.pcm_to_wav(make_pcm(100), 16000, channels=2)
        ch, _, rate, frames = read_wav(wav)
        self.assertEqual((ch, rate, len(frames)), (2, 16000, 200))

    def test_odd_trailing_byte_dropped(self):
        wav = tts.pcm_to_wav(make_pcm(10) + b"\x01", 24000)
        self.assertEqual(len(read_wav(wav)[3]), 20)

    def test_empty_or_invalid(self):
        for args in ((b"",), (b"\x01",), (make_pcm(4), 0), (make_pcm(4), 24000, 1, 5)):
            with self.assertRaises(tts.TTSError):
                tts.pcm_to_wav(*args)

    def test_duration(self):
        self.assertAlmostEqual(tts.wav_duration_seconds(tts.pcm_to_wav(make_pcm(24000), 24000)), 1.0)
        self.assertEqual(tts.wav_duration_seconds(b"not wav"), 0.0)


class AudioNormalizeTests(unittest.TestCase):
    def test_sample_rate_from_mime(self):
        self.assertEqual(tts.parse_sample_rate("audio/L16;codec=pcm;rate=16000"), 16000)
        self.assertEqual(tts.parse_sample_rate("audio/L16; RATE = 44100"), 44100)
        self.assertEqual(tts.parse_sample_rate("audio/L16"), 24000)
        self.assertEqual(tts.parse_sample_rate("rate=5"), 24000)   # vô lý -> mặc định
        self.assertEqual(tts.parse_sample_rate(""), 24000)

    def test_raw_pcm_is_wrapped_with_mime_rate(self):
        wav = tts.audio_to_wav(make_pcm(50), "audio/L16;codec=pcm;rate=16000")
        self.assertEqual(read_wav(wav)[2], 16000)

    def test_existing_wav_untouched(self):
        wav = tts.pcm_to_wav(make_pcm(50), 22050)
        self.assertIs(tts.audio_to_wav(wav, "audio/wav"), wav)

    def test_concat(self):
        a, b = tts.pcm_to_wav(make_pcm(10), 24000), tts.pcm_to_wav(make_pcm(5), 24000)
        self.assertEqual(len(read_wav(tts.concat_wavs([a, b]))[3]), 30)
        self.assertIs(tts.concat_wavs([a]), a)
        with self.assertRaises(tts.TTSError):
            tts.concat_wavs([a, tts.pcm_to_wav(make_pcm(5), 16000)])
        with self.assertRaises(tts.TTSError):
            tts.concat_wavs([])
        with self.assertRaises(tts.TTSError):
            tts.concat_wavs([a, b"garbage"])


class TextSplitTests(unittest.TestCase):
    def test_split_keeps_sentences_and_limit(self):
        text = "Câu một. Câu hai! Câu ba? " * 10
        chunks = tts.split_sentences(text, 40)
        self.assertTrue(all(len(c) <= 40 for c in chunks))
        self.assertEqual(" ".join(chunks), " ".join(text.split()))

    def test_long_sentence_cut_by_words(self):
        chunks = tts.split_sentences(" ".join(["abc"] * 100), 50)
        self.assertTrue(len(chunks) > 1)
        self.assertTrue(all(len(c) <= 50 for c in chunks))
        self.assertEqual(sum(len(c.split()) for c in chunks), 100)

    def test_empty(self):
        self.assertEqual(tts.split_sentences("  \n "), [])

    def test_first_sentence(self):
        self.assertEqual(tts.first_sentence("Một hai ba. Bốn năm sáu."), "Một hai ba.")
        self.assertEqual(tts.first_sentence("a b c d e f", max_words=3), "a b c")
        self.assertEqual(tts.first_sentence(""), "")


class GeminiTTSTests(unittest.TestCase):
    def fake_generate(self, pcm=None, mime="audio/L16;codec=pcm;rate=24000"):
        pcm = pcm if pcm is not None else make_pcm(240)
        calls = []

        def generate(payload, api_key, **kwargs):
            calls.append((payload, api_key, kwargs))
            return kwargs["extract"]({"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": mime, "data": base64.b64encode(pcm).decode()}}
            ]}}]})

        return generate, calls

    def test_synthesize_builds_request_and_wraps_pcm(self):
        gen, calls = self.fake_generate()
        backend = tts.GeminiTTS("KEY", model="m-tts", voice="Puck", generate=gen)
        wav = backend.synthesize("  Xin chào  ")
        payload, key, kw = calls[0]
        self.assertEqual(key, "KEY")
        self.assertEqual(kw["model"], "m-tts")
        self.assertEqual(kw["fallback_model"], "")        # tắt model dự phòng dạng văn bản
        self.assertEqual(kw["purpose"], "đọc giọng")
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "Xin chào")
        cfg = payload["generationConfig"]
        self.assertEqual(cfg["responseModalities"], ["AUDIO"])
        self.assertEqual(
            cfg["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Puck"
        )
        self.assertEqual(read_wav(wav)[:3], (1, 2, 24000))
        self.assertEqual(read_wav(wav)[3], make_pcm(240))

    def test_defaults_and_errors(self):
        b = tts.GeminiTTS("K", model="", voice="")
        self.assertTrue(b.model and b.voice)
        with self.assertRaises(tts.TTSError):
            tts.GeminiTTS("  ").synthesize("x")
        with self.assertRaises(tts.TTSError):
            tts.GeminiTTS("K", generate=lambda *a, **k: None).synthesize("   ")

    def test_stop_before_request(self):
        gen, calls = self.fake_generate()
        with self.assertRaises(tts.TTSCancelled):
            tts.GeminiTTS("K", generate=gen).synthesize("x", stop_flag=lambda: True)
        self.assertEqual(calls, [])

    def test_extract_audio_errors(self):
        with self.assertRaises(ValueError):
            tts._extract_audio({})
        with self.assertRaises(ValueError) as ctx:
            tts._extract_audio({"promptFeedback": {"blockReason": "SAFETY"}})
        self.assertIn("SAFETY", str(ctx.exception))
        with self.assertRaises(ValueError):
            tts._extract_audio({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})

    def test_goes_through_gemini_client_extract(self):
        """generate_content thật phải dùng `extract` truyền vào (không ép về text)."""
        from douyin_manager import gemini_client as gc

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;rate=24000",
                            "data": base64.b64encode(make_pcm(8)).decode()}}]}}]}
        with mock.patch.object(gc, "_call_gemini", return_value=resp) as call:
            wav = tts.GeminiTTS("K", model="m").synthesize("xin chào")
        self.assertEqual(call.call_args.kwargs["model"], "m")
        self.assertEqual(len(read_wav(wav)[3]), 16)


class FakeEngine:
    def __init__(self):
        self.infer_calls = []
        self.saved = 0

    def infer(self, text, **kwargs):
        self.infer_calls.append((text, kwargs))
        return text

    def save(self, audio, path):
        self.saved += 1
        Path(path).write_bytes(tts.pcm_to_wav(make_pcm(100), 24000))


class VieNeuTTSTests(unittest.TestCase):
    def test_chunks_and_concat(self):
        eng = FakeEngine()
        backend = tts.VieNeuTTS(engine_factory=lambda: eng)
        text = " ".join(f"Đây là câu số {i} khá là dài để bị tách." for i in range(20))
        wav = backend.synthesize(text)
        self.assertGreater(len(eng.infer_calls), 1)
        self.assertEqual(len(read_wav(wav)[3]), 200 * len(eng.infer_calls))
        self.assertTrue(all(kw == {} for _, kw in eng.infer_calls))

    def test_voice_sample_passed_as_ref_audio(self):
        with tempfile.TemporaryDirectory() as d:
            sample = Path(d) / "giong.wav"
            sample.write_bytes(b"x")
            eng = FakeEngine()
            tts.VieNeuTTS(voice_sample=str(sample), engine_factory=lambda: eng).synthesize("Xin chào.")
            self.assertEqual(eng.infer_calls[0][1], {"ref_audio": str(sample), "denoise": True})

    def test_preset_voice(self):
        eng = FakeEngine()
        tts.VieNeuTTS(preset_voice="Hải Đăng", engine_factory=lambda: eng).synthesize("Xin chào.")
        self.assertEqual(eng.infer_calls[0][1], {"voice": "Hải Đăng"})

    def test_missing_sample_and_empty_text(self):
        with self.assertRaises(tts.TTSError):
            tts.VieNeuTTS(voice_sample="/không/có/file.wav", engine_factory=FakeEngine).synthesize("x.")
        with self.assertRaises(tts.TTSError):
            tts.VieNeuTTS(engine_factory=FakeEngine).synthesize("  ")

    def test_stop_between_chunks(self):
        eng = FakeEngine()
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 1       # cho đọc đúng 1 đoạn rồi dừng

        text = " ".join(f"Câu số {i} khá dài để tách đoạn riêng biệt nhau." for i in range(30))
        with self.assertRaises(tts.TTSCancelled):
            tts.VieNeuTTS(engine_factory=lambda: eng).synthesize(text, stop)
        self.assertEqual(len(eng.infer_calls), 1)

    def test_engine_errors_wrapped(self):
        class Boom(FakeEngine):
            def infer(self, *a, **k):
                raise ValueError("hỏng")

        with self.assertRaises(tts.TTSError) as ctx:
            tts.VieNeuTTS(engine_factory=Boom).synthesize("Xin chào.")
        self.assertIn("hỏng", str(ctx.exception))

    def test_not_installed_message(self):
        with mock.patch.dict("sys.modules", {"vieneu": None}):
            tts._VIENEU_ENGINE = None
            with self.assertRaises(tts.TTSError) as ctx:
                tts._load_vieneu_engine()
        self.assertIn("pip install vieneu", str(ctx.exception))


@unittest.skipIf(os.name == "nt", "symlink cần quyền đặc biệt trên Windows")
class HfSymlinkTests(unittest.TestCase):
    def make_cache(self, root: Path, with_onnx=True):
        blobs = root / "models--a--vieneu" / "blobs"
        snap = root / "models--a--vieneu" / "snapshots" / "rev1"
        blobs.mkdir(parents=True)
        snap.mkdir(parents=True)
        (blobs / "h1").write_bytes(b"model")
        (blobs / "h2").write_bytes(b"data")
        (snap / ("m.onnx" if with_onnx else "m.bin")).symlink_to("../../blobs/h1")
        (snap / "m.data").symlink_to("../../blobs/h2")
        return snap

    def test_symlinks_become_real_files(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.make_cache(Path(d))
            self.assertEqual(tts.materialize_hf_symlinks(Path(d)), 2)
            for name, content in (("m.onnx", b"model"), ("m.data", b"data")):
                self.assertFalse((snap / name).is_symlink())
                self.assertEqual((snap / name).read_bytes(), content)
            self.assertEqual(sorted(x.name for x in snap.iterdir()), ["m.data", "m.onnx"])
            self.assertEqual(tts.materialize_hf_symlinks(Path(d)), 0)   # chạy lại không đổi gì

    def test_snapshot_without_onnx_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.make_cache(Path(d), with_onnx=False)
            self.assertEqual(tts.materialize_hf_symlinks(Path(d)), 0)
            self.assertTrue((snap / "m.data").is_symlink())

    def test_missing_cache(self):
        self.assertEqual(tts.materialize_hf_symlinks(Path("/không/có/cache")), 0)

    def test_engine_load_retries_after_ort_error(self):
        err = RuntimeError("[ONNXRuntimeError] External data path escapes model directory")
        calls = {"n": 0}

        def fake_vieneu():
            calls["n"] += 1
            if calls["n"] == 1:
                raise err
            return "ENGINE"

        fake_mod = mock.Mock(Vieneu=fake_vieneu)
        tts._VIENEU_ENGINE = None
        with mock.patch.dict("sys.modules", {"vieneu": fake_mod}), \
                mock.patch.object(tts, "materialize_hf_symlinks", return_value=3):
            self.assertEqual(tts._load_vieneu_engine(), "ENGINE")
        self.assertEqual(calls["n"], 2)
        tts._VIENEU_ENGINE = None

    def test_engine_load_gives_hint_when_nothing_to_fix(self):
        fake_mod = mock.Mock(Vieneu=mock.Mock(side_effect=RuntimeError("External data path escapes")))
        tts._VIENEU_ENGINE = None
        with mock.patch.dict("sys.modules", {"vieneu": fake_mod}), \
                mock.patch.object(tts, "materialize_hf_symlinks", return_value=0):
            with self.assertRaises(tts.TTSError) as ctx:
                tts._load_vieneu_engine()
        self.assertIn("onnxruntime<1.24.1", str(ctx.exception))


class MakeBackendTests(unittest.TestCase):
    def test_kinds(self):
        g = tts.make_backend(TTS_BACKEND_GEMINI, {"gemini_api_key": "K", "tts_gemini_voice": "Puck"})
        self.assertIsInstance(g, tts.GeminiTTS)
        self.assertEqual((g.api_key, g.voice), ("K", "Puck"))
        v = tts.make_backend(TTS_BACKEND_VIENEU, {"review_voice_sample": " /a/b.wav "})
        self.assertIsInstance(v, tts.VieNeuTTS)
        self.assertEqual(v.voice_sample, "/a/b.wav")
        with self.assertRaises(tts.TTSError):
            tts.make_backend("lạ", {})


class SaveAudioTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "audio"     # chưa tồn tại -> phải tự tạo
        self.wav = tts.pcm_to_wav(make_pcm(2400), 24000)

    def test_without_ffmpeg_saves_wav(self):
        p = tts.save_audio_file(self.wav, self.out, "video1", None)
        self.assertEqual(p, self.out / "video1.wav")
        self.assertEqual(p.read_bytes(), self.wav)
        self.assertEqual([x.name for x in self.out.iterdir()], ["video1.wav"])

    def test_with_ffmpeg_saves_mp3_atomically(self):
        def fake_convert(wav_path, mp3_path, ffmpeg):
            self.assertEqual(ffmpeg, "ffmpeg-bin")
            self.assertEqual(Path(wav_path).read_bytes(), self.wav)
            Path(mp3_path).write_bytes(b"MP3DATA")

        with mock.patch.object(tts, "wav_to_mp3", fake_convert):
            p = tts.save_audio_file(self.wav, self.out, "video1", "ffmpeg-bin")
        self.assertEqual(p, self.out / "video1.mp3")
        self.assertEqual(p.read_bytes(), b"MP3DATA")
        self.assertEqual([x.name for x in self.out.iterdir()], ["video1.mp3"])

    def test_overwrites_existing(self):
        (self.out).mkdir()
        (self.out / "v.wav").write_bytes(b"old")
        tts.save_audio_file(self.wav, self.out, "v", None)
        self.assertEqual((self.out / "v.wav").read_bytes(), self.wav)

    def test_ffmpeg_failure_leaves_nothing(self):
        with mock.patch.object(tts, "wav_to_mp3", side_effect=tts.TTSError("lỗi")):
            with self.assertRaises(tts.TTSError):
                tts.save_audio_file(self.wav, self.out, "v", "ffmpeg")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_empty_stem(self):
        with self.assertRaises(tts.TTSError):
            tts.save_audio_file(self.wav, self.out, "  ", None)

    @unittest.skipUnless(shutil.which("ffmpeg"), "không có ffmpeg")
    def test_real_ffmpeg_mp3(self):
        p = tts.save_audio_file(self.wav, self.out, "real", shutil.which("ffmpeg"))
        self.assertEqual(p.suffix, ".mp3")
        self.assertGreater(p.stat().st_size, 100)


class SynthesizeToFileTests(unittest.TestCase):
    def test_full_flow_with_mock_backend(self):
        class Mock(tts.TTSBackend):
            label = "Giả"

            def synthesize(self, text, stop_flag=None):
                self.text = text
                return tts.pcm_to_wav(make_pcm(240), 24000)

        msgs = []
        with tempfile.TemporaryDirectory() as d:
            b = Mock()
            p = tts.synthesize_to_file(b, "Lời đọc.", Path(d), "clip", None, progress_cb=msgs.append)
            self.assertEqual(p.name, "clip.wav")
        self.assertEqual(b.text, "Lời đọc.")
        self.assertEqual(len(msgs), 2)

    def test_cancel_after_synthesis_saves_nothing(self):
        class Mock(tts.TTSBackend):
            def synthesize(self, text, stop_flag=None):
                return tts.pcm_to_wav(make_pcm(240), 24000)

        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(tts.TTSCancelled):
                tts.synthesize_to_file(Mock(), "x.", Path(d), "clip", None, stop_flag=lambda: True)
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
