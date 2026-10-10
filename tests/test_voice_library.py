"""Test thư viện giọng mẫu đã lưu. Chạy: python -m unittest discover -s tests -v"""

import tempfile
import unittest
from pathlib import Path

from douyin_manager import voice_library as vl


class VoiceLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.vdir = self.root / "voices"
        self.src = self.root / "Giọng Của Tôi.WAV"
        self.src.write_bytes(b"AAAA")
        self.cfg: dict = {}

    def test_save_copies_file_and_lists(self):
        e = vl.save_voice(self.cfg, "  Giọng  Nữ  ", self.src, self.vdir)
        self.assertEqual(e["name"], "Giọng Nữ")
        dest = Path(e["path"])
        self.assertEqual(dest.parent, self.vdir)
        self.assertEqual(dest.name, "giong_nu.wav")
        self.assertEqual(dest.read_bytes(), b"AAAA")
        self.assertTrue(self.src.exists())               # file gốc còn nguyên
        self.assertEqual(vl.voice_names(self.cfg), ["Giọng Nữ"])

    def test_survives_deleting_original(self):
        e = vl.save_voice(self.cfg, "A", self.src, self.vdir)
        self.src.unlink()
        self.assertTrue(Path(e["path"]).is_file())

    def test_same_name_replaces(self):
        vl.save_voice(self.cfg, "A", self.src, self.vdir)
        other = self.root / "x.wav"
        other.write_bytes(b"BBBB")
        e = vl.save_voice(self.cfg, "a", other, self.vdir)       # không phân biệt hoa/thường
        self.assertEqual(len(vl.load_voices(self.cfg)), 1)
        self.assertEqual(Path(e["path"]).read_bytes(), b"BBBB")

    def test_replace_with_other_extension_removes_old_file(self):
        first = vl.save_voice(self.cfg, "A", self.src, self.vdir)
        mp3 = self.root / "y.mp3"
        mp3.write_bytes(b"CC")
        second = vl.save_voice(self.cfg, "A", mp3, self.vdir)
        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).exists())

    def test_sorted_and_find(self):
        for n in ("Zed", "alpha", "Beta"):
            vl.save_voice(self.cfg, n, self.src, self.vdir)
        self.assertEqual(vl.voice_names(self.cfg), ["alpha", "Beta", "Zed"])
        v = vl.find_voice(self.cfg, "BETA")
        self.assertEqual(v["name"], "Beta")
        self.assertEqual(vl.find_voice_by_path(self.cfg, v["path"])["name"], "Beta")
        self.assertIsNone(vl.find_voice(self.cfg, "nope"))
        self.assertIsNone(vl.find_voice_by_path(self.cfg, ""))

    def test_delete_removes_copy_only(self):
        e = vl.save_voice(self.cfg, "A", self.src, self.vdir)
        self.assertTrue(vl.delete_voice(self.cfg, "a", self.vdir))
        self.assertFalse(Path(e["path"]).exists())
        self.assertTrue(self.src.exists())
        self.assertEqual(vl.load_voices(self.cfg), [])
        self.assertFalse(vl.delete_voice(self.cfg, "a", self.vdir))

    def test_delete_never_touches_file_outside_voices_dir(self):
        self.cfg[vl.CFG_KEY] = [{"name": "Ngoai", "path": str(self.src)}]
        self.assertTrue(vl.delete_voice(self.cfg, "Ngoai", self.vdir))
        self.assertTrue(self.src.exists())

    def test_validation(self):
        for name, src in (("", self.src), ("   ", self.src), ("x" * 61, self.src),
                          ("A", self.root / "nope.wav"), ("A", "")):
            with self.assertRaises(vl.VoiceLibraryError):
                vl.save_voice(self.cfg, name, src, self.vdir)
        self.assertEqual(vl.load_voices(self.cfg), [])

    def test_load_ignores_garbage(self):
        self.cfg[vl.CFG_KEY] = [{"name": "A", "path": "/a"}, "x", {"name": "", "path": "/b"},
                                {"name": "a", "path": "/dup"}, {"path": "/c"}, None]
        self.assertEqual(vl.load_voices(self.cfg), [{"name": "A", "path": "/a"}])
        self.assertEqual(vl.load_voices({vl.CFG_KEY: "lạ"}), [])


if __name__ == "__main__":
    unittest.main()
