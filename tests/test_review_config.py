"""Test khóa cấu hình Review. Chạy: python -m unittest discover -s tests -v"""

import unittest

from douyin_manager import config as C


class ReadReviewSettingsTests(unittest.TestCase):
    def test_defaults_on_empty_or_bad_cfg(self):
        for cfg in ({}, None, "x", []):
            r = C.read_review_settings(cfg)
            self.assertEqual(r["script_model"], C.DEFAULT_REVIEW_SCRIPT_MODEL)
            self.assertEqual(r["voice_sample"], "")
            self.assertEqual(r["words_per_second"], C.DEFAULT_REVIEW_WORDS_PER_SECOND)
            self.assertEqual(r["style"], C.DEFAULT_REVIEW_STYLE)
            self.assertEqual(r["style"], C.REVIEW_STYLE_KOC)
            self.assertEqual(r["style_hint"], C.REVIEW_STYLE_HINTS[C.REVIEW_STYLE_KOC])

    def test_values_are_read_and_trimmed(self):
        r = C.read_review_settings({
            "review_script_model": " gemini-2.5-pro ",
            "review_voice_sample": "  /Users/a/giong_mau.wav ",
            "review_words_per_second": "2.5",
            "review_style": "lively",
        })
        self.assertEqual(r["script_model"], "gemini-2.5-pro")
        self.assertEqual(r["voice_sample"], "/Users/a/giong_mau.wav")
        self.assertEqual(r["words_per_second"], 2.5)
        self.assertEqual(r["style_hint"], C.REVIEW_STYLE_HINTS[C.REVIEW_STYLE_LIVELY])

    def test_words_per_second_clamped_and_sanitized(self):
        for bad in ("abc", None, float("nan"), float("inf"), [], {}):
            self.assertEqual(C.read_review_settings({"review_words_per_second": bad})["words_per_second"], C.DEFAULT_REVIEW_WORDS_PER_SECOND)
        self.assertEqual(C.read_review_settings({"review_words_per_second": 0})["words_per_second"],
                         C.MIN_REVIEW_WORDS_PER_SECOND)
        self.assertEqual(C.read_review_settings({"review_words_per_second": 99})["words_per_second"],
                         C.MAX_REVIEW_WORDS_PER_SECOND)

    def test_unknown_style_falls_back(self):
        r = C.read_review_settings({"review_style": "hack"})
        self.assertEqual(r["style"], C.DEFAULT_REVIEW_STYLE)

    def test_custom_style_uses_prompt(self):
        r = C.read_review_settings({"review_style": "custom", "review_style_prompt": "  Nói như MC  "})
        self.assertEqual(r["style_hint"], "Nói như MC")
        empty = C.read_review_settings({"review_style": "custom"})
        self.assertEqual(empty["style_hint"], "")

    def test_style_tables_consistent(self):
        values = set(C.REVIEW_STYLE_OPTIONS.values())
        self.assertIn(C.DEFAULT_REVIEW_STYLE, values)
        self.assertEqual(set(C.REVIEW_STYLE_HINTS), values - {C.REVIEW_STYLE_CUSTOM})


if __name__ == "__main__":
    unittest.main()
