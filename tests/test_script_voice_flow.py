"""Chạy cả luồng của tab "Kịch bản & Giọng đọc" KHÔNG qua giao diện, với ffmpeg thật:
video -> kịch bản (Gemini giả) -> đọc (engine VieNeu giả) -> <stem>.mp3 -> tab Ghép tự ghép cặp theo tên."""

import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from douyin_manager import review_script as rs
from douyin_manager import tts_local as tts
from douyin_manager.audio_merger import match_video_audio_pairs, list_media_files
from douyin_manager.config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS

FFMPEG = shutil.which("ffmpeg")


class ToneEngine:
    """Thay VieNeu: mỗi câu -> 0,4 giây âm thanh thật."""

    def infer(self, text, **kwargs):
        return text

    def save(self, audio, path):
        rate = 24000
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * 0.4))
        )
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)


@unittest.skipUnless(FFMPEG, "không có ffmpeg")
class FlowTests(unittest.TestCase):
    def test_video_to_mp3_paired_by_stem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            video = d / "video_review_123.mp4"
            subprocess.run(
                [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
                 "-t", "2", "-pix_fmt", "yuv420p", str(video)], check=True)
            audio_dir = d / "audio"

            raw = "# Kịch bản\n**Lời đọc:** Chiếc áo này 😍 giá 200 nghìn (cười). Mặc rất mát #review"
            with mock.patch.object(rs, "generate_content", return_value=raw):
                script = rs.generate_review_script(video, "KEY", ffmpeg_path=FFMPEG, max_words=60)
            self.assertNotIn("#", script.text)
            self.assertIn("hai trăm nghìn", script.text)

            backend = tts.VieNeuTTS(engine_factory=ToneEngine)
            mp3 = tts.synthesize_to_file(backend, script.text, audio_dir, video.stem, FFMPEG)

            self.assertEqual(mp3, audio_dir / "video_review_123.mp3")
            self.assertGreater(mp3.stat().st_size, 500)
            probe = subprocess.run(
                [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_name", "-of", "csv=p=0", str(mp3)], capture_output=True, text=True)
            self.assertIn("mp3", probe.stdout)

            pairs = match_video_audio_pairs(
                list_media_files(d, VIDEO_EXTENSIONS), list_media_files(audio_dir, AUDIO_EXTENSIONS))
            self.assertEqual(len(pairs), 1)
            self.assertTrue(pairs[0].is_ready)


if __name__ == "__main__":
    unittest.main()
