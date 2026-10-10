"""Test prompt KOC (số từ theo thời lượng, viết lại khi lệch) + cài đặt giọng đọc
(ổn định, tốc độ, ngắt nghỉ, khớp thời lượng). Không gọi mạng."""

import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from douyin_manager import config as C
from douyin_manager import review_script as rs
from douyin_manager import tts_local as tts

FFMPEG = shutil.which("ffmpeg")


def tone_wav(seconds, rate=24000):
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                      for i in range(int(rate * seconds)))
    return tts.pcm_to_wav(frames, rate)


class ReadSettingsTests(unittest.TestCase):
    def test_tts_defaults_and_clamp(self):
        d = C.read_tts_settings({})
        self.assertEqual((d["stability"], d["speed"], d["pause_enabled"]), (2.8, 1.0, True))
        r = C.read_tts_settings({"tts_stability": 99, "tts_speed": "abc", "tts_pause_sentence": -3,
                                 "tts_pause_comma": 99, "tts_pause_enabled": "x"})
        self.assertEqual(r["stability"], C.MAX_TTS_STABILITY)
        self.assertEqual(r["speed"], C.DEFAULT_TTS_SPEED)
        self.assertEqual(r["pause_sentence"], 0.0)
        self.assertEqual(r["pause_comma"], C.MAX_TTS_PAUSE_COMMA)
        self.assertTrue(r["pause_enabled"])
        self.assertEqual(C.read_tts_settings(None)["speed"], 1.0)

    def test_koc_brief_defaults_and_clamp(self):
        b = C.read_koc_brief({})
        self.assertEqual((b["duration"], b["product"]), (15, ""))
        self.assertIn("Bà con", b["address_terms"])
        self.assertIn("chân ái", b["slang"])
        self.assertEqual(C.read_koc_brief({"review_duration": 1})["duration"], C.MIN_REVIEW_DURATION)
        self.assertEqual(C.read_koc_brief({"review_duration": "9999"})["duration"], C.MAX_REVIEW_DURATION)
        self.assertEqual(C.read_koc_brief({"review_duration": "x"})["duration"], 15)


class TargetWordsTests(unittest.TestCase):
    def test_no_pause_is_duration_times_rate(self):
        self.assertEqual(rs.estimate_target_words(15, 3.0), 45)
        self.assertEqual(rs.estimate_target_words(15, 3.0, speed=1.1), 50)

    def test_pauses_reduce_words(self):
        plain = rs.estimate_target_words(15, 3.0)
        paused = rs.estimate_target_words(15, 3.0, pause_sentence=0.35, pause_comma=0.15)
        self.assertLess(paused, plain)
        self.assertTrue(30 <= paused <= 42, paused)

    def test_estimate_seconds_roundtrip(self):
        n = rs.estimate_target_words(15, 3.0, 1.0, 0.35, 0.15)
        text = " ".join(["từ"] * n)
        secs = rs.estimate_read_seconds(text, 3.0, 1.0, 0.35, 0.15)
        self.assertLess(abs(secs - 15), 2.5)       # chưa có dấu câu -> ước lượng lệch nhẹ là bình thường

    def test_estimate_counts_real_punctuation(self):
        t = "Một hai ba, bốn năm sáu. Bảy tám chín."
        self.assertAlmostEqual(rs.estimate_read_seconds(t, 3.0, 1.0, 0.5, 0.2), 9 / 3 + 0.5 + 0.2)

    def test_bad_input(self):
        self.assertEqual(rs.estimate_target_words("x"), rs.DEFAULT_MAX_WORDS)
        self.assertEqual(rs.estimate_read_seconds(""), 0.0)


class KocPromptTests(unittest.TestCase):
    def brief(self):
        return rs.KocBrief.from_duration("Camera an ninh", 15, address_terms="Bác nào, Bà con",
                                         slang="chân ái, rinh ngay", pause_sentence=.35, pause_comma=.15)

    def test_prompt_contains_all_requirements(self):
        b = self.brief()
        s = rs.build_system_instruction(0, b)
        for kw in ("KOC/Reviewer", "TikTok và Facebook Reels", "Camera an ninh", "15 giây",
                   str(b.target_words), str(b.min_words), str(b.max_words), "Bác nào, Bà con",
                   "chân ái, rinh ngay", "Hook", "2 đến 3 tính năng", "chốt đơn",
                   "markdown", "emoji", "hashtag", "mốc thời gian", "con số", "bịa"):
            self.assertIn(kw, s)
        self.assertNotIn("{", s)
        self.assertNotIn("thả tim", s)       # KOC phải được kêu gọi chốt đơn

    def test_empty_product_and_defaults(self):
        s = rs.build_system_instruction(0, rs.KocBrief.from_duration("", 20))
        self.assertIn("tự nhận biết sản phẩm", s)
        self.assertIn("Bà con", s)
        self.assertIn("rinh ngay", s)

    def test_user_prompt(self):
        p = rs.build_user_prompt("Có zoom 360 độ", self.brief())
        self.assertIn("Camera an ninh", p)
        self.assertIn("Có zoom 360 độ", p)

    def test_plain_mode_unchanged(self):
        s = rs.build_system_instruction(77)
        self.assertIn("77", s)
        self.assertNotIn("KOC", s)


class KocGenerateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.video = Path(self._tmp.name) / "v.mp4"
        self.video.write_bytes(b"x")
        self.koc = rs.KocBrief(product="Camera", target_seconds=15, target_words=10,
                               min_words=9, max_words=11)

    def run_gen(self, replies):
        with mock.patch.object(rs, "generate_content", side_effect=replies) as gen:
            res = rs.generate_review_script(self.video, "K", koc=self.koc)
        return res, gen

    def test_in_range_first_try(self):
        res, gen = self.run_gen(["Bác nào ngán cảnh trộm. Camera này chân ái. Rinh ngay."])
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(res.word_count, 11)

    def test_too_long_is_rewritten_not_cut(self):
        long_ = "Bác nào ngán cảnh trộm cắp quanh nhà. " * 3 + "Rinh ngay nhé bà con."
        good = "Bác nào ngán cảnh trộm. Camera này chân ái. Rinh ngay."
        res, gen = self.run_gen([long_, good])
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(res.text, good)
        self.assertFalse(res.truncated)
        second = gen.call_args_list[1].args[0]["contents"]
        self.assertEqual([c["role"] for c in second], ["user", "model", "user"])
        self.assertIn("quá dài", second[2]["parts"][0]["text"])

    def test_too_short_is_rewritten(self):
        res, gen = self.run_gen(["Ngắn quá.", "Bác nào ngán cảnh trộm. Camera này chân ái. Rinh ngay."])
        self.assertEqual(gen.call_count, 2)
        self.assertIn("quá ngắn", gen.call_args_list[1].args[0]["contents"][2]["parts"][0]["text"])

    def test_still_too_long_is_cut_at_sentence(self):
        long_ = "Một hai ba bốn năm sáu. Bảy tám chín mười. Mười một mười hai mười ba mười bốn."
        res, _ = self.run_gen([long_, long_])
        self.assertTrue(res.truncated)
        self.assertLessEqual(res.word_count, self.koc.max_words)
        self.assertTrue(res.text.endswith("."))

    def test_picks_closer_draft(self):
        far = "Một hai."
        near = "Một hai ba bốn năm sáu bảy tám."       # 8 từ, gần khoảng 9..11 hơn
        res, _ = self.run_gen([far, near])
        self.assertEqual(res.text, near)

    def test_payload_has_koc_prompt(self):
        _, gen = self.run_gen(["Bác nào ngán cảnh trộm. Camera này chân ái. Rinh ngay."])
        payload = gen.call_args.args[0]
        self.assertIn("KOC", payload["systemInstruction"]["parts"][0]["text"])


class SplitPauseTests(unittest.TestCase):
    def test_sentence_and_comma_pauses(self):
        segs = tts.split_for_pauses("Bác nào ngán cảnh trộm, camera này chân ái lắm. Rinh ngay nhé!", 0.4, 0.2)
        self.assertEqual([s for s, _ in segs],
                         ["Bác nào ngán cảnh trộm,", "camera này chân ái lắm.", "Rinh ngay nhé!"])
        self.assertEqual([p for _, p in segs], [0.2, 0.4, 0.0])

    def test_short_clause_merged(self):
        segs = tts.split_for_pauses("Ôi, camera này chân ái lắm.", 0.4, 0.2)
        self.assertEqual(len(segs), 1)

    def test_no_comma_split_for_online_backends(self):
        segs = tts.split_for_pauses("Bác nào ngán cảnh trộm, camera này chân ái. Rinh ngay.", .4, .2, split_commas=False)
        self.assertEqual(len(segs), 2)

    def test_empty(self):
        self.assertEqual(tts.split_for_pauses("  ", .4, .2), [])


class JoinAndSynthesizeTests(unittest.TestCase):
    class Backend(tts.TTSBackend):
        label = "Giả"
        cheap_calls = True

        def __init__(self):
            self.calls = []

        def synthesize(self, text, stop_flag=None):
            self.calls.append(text)
            return tone_wav(0.5)

    def test_join_inserts_silence(self):
        out = tts.join_with_pauses([(tone_wav(0.5), 0.3), (tone_wav(0.5), 0.9)])
        self.assertAlmostEqual(tts.wav_duration_seconds(out), 1.3, places=2)   # lặng cuối bị bỏ

    def test_silence_format(self):
        w = tts.silence_wav(0.25, 24000)
        self.assertAlmostEqual(tts.wav_duration_seconds(w), 0.25, places=3)

    def test_synthesize_with_pauses(self):
        b = self.Backend()
        opts = tts.TTSOptions(pause_enabled=True, pause_sentence=0.4, pause_comma=0.2)
        wav = tts.synthesize_with_options(
            b, "Bác nào ngán cảnh trộm, camera này chân ái lắm. Rinh ngay nhé!", opts)
        self.assertEqual(len(b.calls), 3)
        self.assertAlmostEqual(tts.wav_duration_seconds(wav), 1.5 + 0.2 + 0.4, places=2)

    def test_disabled_reads_whole_text_once(self):
        b = self.Backend()
        tts.synthesize_with_options(b, "Một hai ba. Bốn năm sáu.", tts.TTSOptions())
        tts.synthesize_with_options(b, "Một hai ba. Bốn năm sáu.", None)
        self.assertEqual(b.calls, ["Một hai ba. Bốn năm sáu."] * 2)

    def test_stop_between_segments(self):
        b = self.Backend()
        n = {"i": 0}

        def stop():
            n["i"] += 1
            return n["i"] > 1

        with self.assertRaises(tts.TTSCancelled):
            tts.synthesize_with_options(
                b, "Một hai ba bốn. Năm sáu bảy tám. Chín mười mười một.",
                tts.TTSOptions(pause_enabled=True, pause_sentence=.3), stop)

    def test_online_backend_not_split_on_commas(self):
        b = self.Backend()
        b.cheap_calls = False
        tts.synthesize_with_options(
            b, "Bác nào ngán cảnh trộm, camera này chân ái. Rinh ngay nhé.",
            tts.TTSOptions(pause_enabled=True, pause_sentence=.4, pause_comma=.2))
        self.assertEqual(len(b.calls), 2)


class StabilityTests(unittest.TestCase):
    def test_mapping_monotonic(self):
        self.assertEqual(tts.stability_to_temperature(0), 1.5)
        self.assertEqual(tts.stability_to_temperature(5), 0.1)
        self.assertGreater(tts.stability_to_temperature(1), tts.stability_to_temperature(4))
        self.assertEqual(tts.stability_to_temperature("x"), tts.stability_to_temperature(2.8))

    def test_gemini_payload_has_temperature_only_when_set(self):
        self.assertNotIn("temperature", tts.GeminiTTS("K").build_payload("x")["generationConfig"])
        self.assertEqual(
            tts.GeminiTTS("K", temperature=0.7).build_payload("x")["generationConfig"]["temperature"], 0.7)

    def test_make_backend_passes_temperature_from_cfg(self):
        v = tts.make_backend(C.TTS_BACKEND_VIENEU, {"tts_stability": 5})
        self.assertEqual(v.temperature, 0.1)
        g = tts.make_backend(C.TTS_BACKEND_GEMINI, {"gemini_api_key": "K", "tts_stability": 0})
        self.assertEqual(g.temperature, 1.5)
        self.assertIsNone(tts.make_backend(C.TTS_BACKEND_VIENEU, {}).temperature)

    def test_vieneu_passes_temperature_and_falls_back(self):
        class Eng:
            def __init__(self, accepts):
                self.accepts, self.calls = accepts, []

            def infer(self, text, **kw):
                if not self.accepts and "temperature" in kw:
                    raise TypeError("unexpected keyword argument 'temperature'")
                self.calls.append(kw)
                return text

            def save(self, audio, path):
                Path(path).write_bytes(tone_wav(0.1))

        e = Eng(True)
        tts.VieNeuTTS(engine_factory=lambda: e, temperature=0.5).synthesize("Xin chào.")
        self.assertEqual(e.calls[0], {"temperature": 0.5})
        e2 = Eng(False)
        tts.VieNeuTTS(engine_factory=lambda: e2, temperature=0.5).synthesize("Xin chào. Tạm biệt nhé.")
        self.assertTrue(all("temperature" not in c for c in e2.calls))
        self.assertEqual(len(e2.calls), 1)       # 1 câu gộp trong 1 đoạn; không thử lại cho mỗi đoạn


@unittest.skipUnless(FFMPEG, "không có ffmpeg")
class SpeedTests(unittest.TestCase):
    def test_change_speed(self):
        out = tts.change_speed_wav(tone_wav(2.0), 2.0, FFMPEG)
        self.assertAlmostEqual(tts.wav_duration_seconds(out), 1.0, delta=0.1)

    def test_noop_cases(self):
        w = tone_wav(1.0)
        self.assertIs(tts.change_speed_wav(w, 1.0, FFMPEG), w)
        self.assertIs(tts.change_speed_wav(w, 1.5, None), w)

    def test_autofit_hits_target(self):
        w = tone_wav(18.0)
        out, used = tts.apply_speed(w, tts.TTSOptions(autofit=True, target_seconds=15), FFMPEG)
        self.assertAlmostEqual(used, 1.2, places=2)
        self.assertAlmostEqual(tts.wav_duration_seconds(out), 15.0, delta=0.2)

    def test_autofit_clamped(self):
        w = tone_wav(30.0)
        out, used = tts.apply_speed(w, tts.TTSOptions(autofit=True, target_seconds=15), FFMPEG)
        self.assertEqual(used, C.AUTOFIT_MAX_SPEED)

    def test_manual_speed_and_no_ffmpeg(self):
        w = tone_wav(3.0)
        out, used = tts.apply_speed(w, tts.TTSOptions(speed=1.5), FFMPEG)
        self.assertEqual(used, 1.5)
        self.assertAlmostEqual(tts.wav_duration_seconds(out), 2.0, delta=0.1)
        same, used2 = tts.apply_speed(w, tts.TTSOptions(speed=1.5), None)
        self.assertEqual((same, used2), (w, 1.0))

    def test_full_save_with_options(self):
        class B(tts.TTSBackend):
            cheap_calls = True

            def synthesize(self, text, stop_flag=None):
                return tone_wav(1.0)

        with tempfile.TemporaryDirectory() as d:
            opts = tts.TTSOptions(pause_enabled=True, pause_sentence=0.5, speed=1.25)
            mp3 = tts.synthesize_to_file(B(), "Một hai ba bốn. Năm sáu bảy tám.", Path(d), "clip", FFMPEG, opts=opts)
            self.assertTrue(mp3.is_file())


if __name__ == "__main__":
    unittest.main()
