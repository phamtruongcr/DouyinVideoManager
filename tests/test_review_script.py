"""Test viết kịch bản review (không gọi mạng). Chạy: python -m unittest discover -s tests -v"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from douyin_manager import review_script as rs


class NumberReadingTests(unittest.TestCase):
    def test_integers(self):
        cases = {
            0: "không", 1: "một", 5: "năm", 10: "mười", 11: "mười một",
            14: "mười bốn", 15: "mười lăm", 20: "hai mươi", 21: "hai mươi mốt",
            24: "hai mươi tư", 25: "hai mươi lăm", 100: "một trăm",
            105: "một trăm lẻ năm", 115: "một trăm mười lăm", 999: "chín trăm chín mươi chín",
            1000: "một nghìn", 1001: "một nghìn không trăm lẻ một",
            2024: "hai nghìn không trăm hai mươi tư",
            1050000: "một triệu không trăm năm mươi nghìn",
            1000005: "một triệu không trăm lẻ năm",
        }
        for n, expected in cases.items():
            self.assertEqual(rs.int_to_vietnamese(n), expected, n)

    def test_text_conversion(self):
        self.assertEqual(rs.numbers_to_vietnamese("giảm 25%"), "giảm hai mươi lăm phần trăm")
        self.assertEqual(rs.numbers_to_vietnamese("giá 1.500.000 đồng"),
                         "giá một triệu năm trăm nghìn đồng")
        self.assertEqual(rs.numbers_to_vietnamese("nặng 3,5 kg"), "nặng ba phẩy năm kg")
        self.assertEqual(rs.numbers_to_vietnamese("gọi 0912345678"),
                         "gọi không chín một hai ba bốn năm sáu bảy tám")
        self.assertEqual(rs.numbers_to_vietnamese("ngày 12/10/2026"),
                         "ngày mười hai tháng mười năm hai nghìn không trăm hai mươi sáu")
        self.assertEqual(rs.numbers_to_vietnamese("12/10/2026"),
                         "mười hai tháng mười năm hai nghìn không trăm hai mươi sáu")

    def test_model_names_untouched(self):
        self.assertEqual(rs.numbers_to_vietnamese("iPhone15 và H264"), "iPhone15 và H264")


class SanitizeTests(unittest.TestCase):
    def test_removes_markdown_emoji_hashtag_notes_timestamps(self):
        raw = (
            "# Kịch bản review\n"
            "Dưới đây là kịch bản:\n"
            "**Lời đọc:** Chiếc áo này 😍 đẹp thật (cười) #review\n"
            "- Chất vải mát [nhạc nền] lắm\n"
            "00:05 - 00:10 Giá chỉ 200 nghìn.\n"
        )
        text, truncated = rs.sanitize_script(raw, 100)
        self.assertFalse(truncated)
        for bad in ("#", "*", "😍", "(", "[", "00:05", "Dưới đây", "Lời đọc", "Kịch bản"):
            self.assertNotIn(bad, text)
        self.assertIn("Chiếc áo này đẹp thật", text)
        self.assertIn("hai trăm nghìn", text)

    def test_truncate_at_sentence_boundary(self):
        raw = "Câu một có năm từ. Câu hai cũng có năm từ. Câu ba cũng có năm từ."
        text, truncated = rs.sanitize_script(raw, 11)
        self.assertTrue(truncated)
        self.assertEqual(text, "Câu một có năm từ. Câu hai cũng có năm từ.")
        self.assertLessEqual(rs.count_words(text), 11)

    def test_hard_cut_when_first_sentence_too_long(self):
        raw = " ".join(["từ"] * 50)
        text, truncated = rs.sanitize_script(raw, 10)
        self.assertTrue(truncated)
        self.assertEqual(rs.count_words(text), 10)
        self.assertTrue(text.endswith("."))

    def test_empty(self):
        self.assertEqual(rs.sanitize_script("", 10), ("", False))
        self.assertEqual(rs.sanitize_script("😀😀 #tag", 10)[0], "")


class WordCountTests(unittest.TestCase):
    def test_count_words(self):
        self.assertEqual(rs.count_words(""), 0)
        self.assertEqual(rs.count_words(None), 0)
        self.assertEqual(rs.count_words("  một   hai\nba\tbốn  "), 4)
        self.assertEqual(rs.count_words("Xin chào, cả nhà!"), 4)

    def test_result_word_count_matches_text(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "v.mp4"
            video.write_bytes(b"x")
            with mock.patch.object(rs, "generate_content", return_value="Một hai ba. Bốn năm."):
                res = rs.generate_review_script(video, "KEY", max_words=50)
        self.assertEqual(res.word_count, rs.count_words(res.text))
        self.assertEqual(res.word_count, 5)


class PromptTests(unittest.TestCase):
    def test_system_instruction_has_limit_and_rules(self):
        s = rs.build_system_instruction(77)
        self.assertIn("77", s)
        self.assertNotIn("{max_words}", s)
        for kw in ("markdown", "emoji", "hashtag", "mốc thời gian", "con số", "bịa"):
            self.assertIn(kw, s)

    def test_suggest_max_words(self):
        self.assertEqual(rs.suggest_max_words(20), round(20 * rs.DEFAULT_REVIEW_WORDS_PER_SECOND))   # theo mặc định từ/giây
        self.assertEqual(rs.suggest_max_words(20, 2.0), 40)     # tốc độ tuỳ chỉnh
        self.assertEqual(rs.suggest_max_words(1), 20)
        self.assertEqual(rs.suggest_max_words(10_000), 400)
        self.assertEqual(rs.suggest_max_words("x"), rs.DEFAULT_MAX_WORDS)


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.video = Path(self._tmp.name) / "v.mp4"
        self.video.write_bytes(b"fake-video-bytes")

    def test_payload_and_filtering(self):
        with mock.patch.object(rs, "generate_content",
                               return_value="**Xin chào** 😀 cả nhà #hi") as gen:
            res = rs.generate_review_script(self.video, "KEY", max_words=50)
        self.assertEqual(gen.call_args.kwargs["purpose"], "viết kịch bản")
        payload = gen.call_args.args[0]
        self.assertIn("systemInstruction", payload)
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0]["inlineData"]["mimeType"], "video/mp4")
        self.assertEqual(res.text, "Xin chào cả nhà")
        self.assertFalse(res.truncated)

    def test_no_api_key(self):
        with self.assertRaises(rs.ReviewScriptError):
            rs.generate_review_script(self.video, "  ")

    def test_empty_result_raises(self):
        with mock.patch.object(rs, "generate_content", return_value="😀"):
            with self.assertRaises(rs.ReviewScriptError):
                rs.generate_review_script(self.video, "KEY")

    def test_big_video_without_ffmpeg(self):
        with mock.patch.object(rs, "MAX_INLINE_VIDEO_BYTES", 5):
            with self.assertRaises(rs.ReviewScriptError) as ctx:
                rs.prepare_video_for_gemini(self.video, None, Path(self._tmp.name))
        self.assertIn("ffmpeg", str(ctx.exception))

    def test_missing_video(self):
        with self.assertRaises(rs.ReviewScriptError):
            rs.prepare_video_for_gemini(Path(self._tmp.name) / "nope.mp4", None, Path(self._tmp.name))


if __name__ == "__main__":
    unittest.main()
