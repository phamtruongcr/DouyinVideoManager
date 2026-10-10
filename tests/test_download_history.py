"""Test lịch sử tải. Chạy: python -m unittest discover -s tests -v"""

import tempfile
import threading
import unittest
from pathlib import Path

from douyin_manager.download_history import DownloadHistory, make_key


class DownloadHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "history.db"
        self.h = DownloadHistory(self.db)

    def test_empty(self):
        self.assertEqual(self.h.count(), 0)
        self.assertFalse(self.h.contains("douyin", "1"))
        self.assertEqual(self.h.get_many([("douyin", "1")]), {})

    def test_record_and_get(self):
        f = Path(self._tmp.name) / "a.mp4"
        f.write_bytes(b"x")
        self.assertTrue(self.h.record("douyin", 123, "Tiêu đề", "原标题", "http://u", f))
        rec = self.h.get("douyin", "123")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.title, "Tiêu đề")
        self.assertEqual(rec.original_title, "原标题")
        self.assertEqual(rec.download_count, 1)
        self.assertTrue(rec.file_exists)

    def test_missing_platform_means_douyin(self):
        self.h.record(None, "7", "t")
        self.assertTrue(self.h.contains("douyin", "7"))
        self.assertTrue(self.h.contains("DOUYIN", 7))

    def test_same_id_different_platform_is_distinct(self):
        self.h.record("douyin", "1", "d")
        self.h.record("tiktok", "1", "t")
        self.assertEqual(self.h.count(), 2)
        self.assertEqual(self.h.get("tiktok", "1").title, "t")

    def test_redownload_updates_record(self):
        self.h.record("tiktok", "9", "cũ", file_path="/a")
        self.h.record("tiktok", "9", "mới", file_path="/b")
        self.assertEqual(self.h.count(), 1)
        rec = self.h.get("tiktok", "9")
        self.assertEqual((rec.title, rec.file_path, rec.download_count), ("mới", "/b", 2))

    def test_file_exists_false_when_deleted(self):
        self.h.record("douyin", "1", "t", file_path=Path(self._tmp.name) / "nope.mp4")
        self.assertFalse(self.h.get("douyin", "1").file_exists)

    def test_get_many_returns_only_known(self):
        self.h.record("douyin", "1", "a")
        self.h.record("facebook", "3", "c")
        found = self.h.get_many([("douyin", "1"), ("douyin", "2"), ("facebook", "3")])
        self.assertEqual(set(found), {make_key("douyin", "1"), make_key("facebook", "3")})

    def test_delete_and_clear(self):
        self.h.record("douyin", "1", "a")
        self.h.record("douyin", "2", "b")
        self.assertTrue(self.h.delete("douyin", "1"))
        self.assertEqual(self.h.count(), 1)
        self.assertTrue(self.h.clear())
        self.assertEqual(self.h.count(), 0)

    def test_list_recent_newest_first_and_limit(self):
        import time
        for i in range(5):
            self.h.record("douyin", str(i), f"t{i}")
            time.sleep(0.01)
        recent = self.h.list_recent(3)
        self.assertEqual([r.video_id for r in recent], ["4", "3", "2"])
        self.assertEqual(len(self.h.list_recent()), 5)
        self.assertEqual(DownloadHistory(Path(self._tmp.name) / "empty.db").list_recent(), [])

    def test_persists_across_instances(self):
        self.h.record("douyin", "1", "a")
        self.assertTrue(DownloadHistory(self.db).contains("douyin", "1"))

    def test_concurrent_writes(self):
        def work(start):
            for i in range(start, start + 25):
                self.h.record("douyin", str(i), f"t{i}")

        threads = [threading.Thread(target=work, args=(n * 25,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.h.count(), 100)

    def test_corrupt_db_does_not_raise(self):
        bad = Path(self._tmp.name) / "bad.db"
        bad.write_bytes(b"this is not a sqlite database" * 50)
        h = DownloadHistory(bad)
        self.assertEqual(h.count(), 0)
        self.assertFalse(h.record("douyin", "1", "x"))
        self.assertEqual(h.get_many([("douyin", "1")]), {})
        self.assertTrue(h.last_error)


if __name__ == "__main__":
    unittest.main()
