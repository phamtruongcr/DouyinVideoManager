import tempfile
import unittest
from pathlib import Path
from unittest import mock

from douyin_manager import audio_merger


class BundledFfmpegTests(unittest.TestCase):
    def test_finds_ffmpeg_next_to_app_before_path(self):
        d = Path(tempfile.mkdtemp())
        (d / "ffmpeg.exe").write_text("")
        (d / "ffprobe.exe").write_text("")
        with mock.patch.object(audio_merger, "_bundled_ffmpeg_dirs", return_value=[d]), \
                mock.patch.object(audio_merger.shutil, "which", return_value="/usr/bin/ffmpeg"):
            ff = audio_merger.find_ffmpeg("")
            self.assertEqual(ff, str(d / "ffmpeg.exe"))
            self.assertEqual(audio_merger.find_ffprobe(ff), str(d / "ffprobe.exe"))

    def test_custom_path_still_wins(self):
        d = Path(tempfile.mkdtemp())
        (d / "ffmpeg.exe").write_text("")
        other = Path(tempfile.mkdtemp())
        (other / "ffmpeg.exe").write_text("")
        with mock.patch.object(audio_merger, "_bundled_ffmpeg_dirs", return_value=[other]):
            self.assertEqual(audio_merger.find_ffmpeg(str(d)), str(d / "ffmpeg.exe"))

    def test_falls_back_to_path(self):
        with mock.patch.object(audio_merger, "_bundled_ffmpeg_dirs", return_value=[Path("/nonexistent")]), \
                mock.patch.object(audio_merger.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(audio_merger.find_ffmpeg(""), "/usr/bin/ffmpeg")


if __name__ == "__main__":
    unittest.main()
