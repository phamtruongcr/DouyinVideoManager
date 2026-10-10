import unittest

from douyin_manager import review_script as rs


class CalibrationTests(unittest.TestCase):
    def test_measure_matches_real_run(self):
        # Thực tế: 86 từ, đọc hết 18,2 giây ở tốc độ 1,15×, không ngắt nghỉ -> ~4,1 từ/giây
        text = " ".join(["từ"] * 86)
        wps = rs.measure_words_per_second(text, 18.2, 1.15)
        self.assertAlmostEqual(wps, 4.11, places=1)

    def test_estimate_and_measure_are_inverse(self):
        text = "Chiếc bếp này rất tiện, nấu nhanh lắm. " * 6
        secs = rs.estimate_read_seconds(text, 4.0, 1.15, 0.35, 0.15)
        back = rs.measure_words_per_second(text, secs, 1.15, 0.35, 0.15)
        self.assertAlmostEqual(back, 4.0, places=2)

    def test_target_words_follow_measured_speed(self):
        # Mục tiêu 20s ở 1,15×: ước lượng cũ (3 từ/giây) chỉ ~69 từ -> chỉ đọc ~14,6s; mức mới sát thực tế
        old = rs.estimate_target_words(20, 3.0, 1.15)
        new = rs.estimate_target_words(20, 4.1, 1.15)
        self.assertEqual(old, 69)
        self.assertEqual(new, 94)

    def test_pauses_scale_with_speed(self):
        text = "Câu một ở đây nhé. Câu hai cũng vậy nhé. Câu ba nữa. " * 3
        slow = rs.estimate_read_seconds(text, 4.0, 1.0, 0.35, 0.15)
        fast = rs.estimate_read_seconds(text, 4.0, 2.0, 0.35, 0.15)
        self.assertAlmostEqual(fast, slow / 2)

    def test_bad_samples_ignored(self):
        self.assertIsNone(rs.measure_words_per_second("quá ngắn", 5, 1.0))
        self.assertIsNone(rs.measure_words_per_second(" ".join(["từ"] * 40), 1.0, 1.0))
        self.assertIsNone(rs.measure_words_per_second(" ".join(["từ"] * 200), 10, 1.0))   # 20 từ/giây: vô lý

    def test_blend(self):
        self.assertEqual(rs.blend_words_per_second(3.0, 0, 4.2), 4.2)
        self.assertAlmostEqual(rs.blend_words_per_second(4.0, 1, 4.4), 4.2)
        self.assertAlmostEqual(rs.blend_words_per_second(4.0, 99, 5.0), 4.2)


if __name__ == "__main__":
    unittest.main()
