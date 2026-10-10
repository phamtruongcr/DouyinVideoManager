import io
import math
import struct
import unittest
import wave

from douyin_manager import tts_local as tts
from douyin_manager.audio_bar import format_clock


def make_wav(seconds=2.0, rate=8000, loud_from=1.0):
    frames = bytearray()
    for i in range(int(rate * seconds)):
        amp = 20000 if i / rate >= loud_from else 2000
        frames += struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * i / rate)))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return out.getvalue()


class WavHelperTests(unittest.TestCase):
    def test_peaks_normalised_and_follow_loudness(self):
        peaks = tts.wav_peaks(make_wav(), bars=10)
        self.assertEqual(len(peaks), 10)
        self.assertAlmostEqual(max(peaks), 1.0)
        self.assertLess(peaks[1], 0.3)
        self.assertGreater(peaks[8], 0.9)

    def test_peaks_bad_input(self):
        self.assertEqual(tts.wav_peaks(b"not a wav"), [])

    def test_slice_wav_drops_leading_seconds(self):
        wav = make_wav(2.0)
        cut = tts.slice_wav(wav, 0.5)
        self.assertAlmostEqual(tts.wav_duration_seconds(cut), 1.5, places=2)
        self.assertEqual(tts.slice_wav(b"junk", 1.0), b"junk")

    def test_format_clock(self):
        self.assertEqual(format_clock(0), "0:00")
        self.assertEqual(format_clock(65.4), "1:05")


if __name__ == "__main__":
    unittest.main()
