"""
tts_local.py
============
Đọc kịch bản thành giọng nói (TTS) cho tính năng "Review + giọng đọc".

Hai backend dùng chung giao diện `TTSBackend.synthesize(text, stop_flag) -> bytes WAV`:
  - `VieNeuTTS`  : chạy NGAY TRÊN MÁY bằng thư viện `vieneu` (pip install vieneu,
                   Python >= 3.10). Hỗ trợ nhân bản giọng từ file giọng mẫu.
  - `GeminiTTS`  : gọi Gemini TTS qua REST (dùng lại retry/backoff của
                   gemini_client). Gemini trả PCM thô 16-bit 24 kHz mono nên phải
                   bọc thành WAV (`pcm_to_wav`). KHÔNG hỗ trợ giọng mẫu — chọn
                   một trong các giọng dựng sẵn (xem config.GEMINI_TTS_VOICES).

Sau khi có WAV, `save_audio_file` đổi sang <stem>.mp3 bằng ffmpeg (không có
ffmpeg thì lưu <stem>.wav — tab Ghép Audio đọc được cả hai).

Module KHÔNG phụ thuộc giao diện; mạng và engine VieNeu đều có thể thay bằng
hàm giả khi test (xem tests/test_tts_local.py). Mọi lỗi là `TTSError`
(RuntimeError, thông báo tiếng Việt hiển thị thẳng cho người dùng). Không bao
giờ log API key hay nội dung kịch bản.
"""

from __future__ import annotations

import base64
import io
from array import array
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

from .app_logger import get_logger
from dataclasses import dataclass

from .config import (
    AUTOFIT_MAX_SPEED,
    AUTOFIT_MIN_SPEED,
    DEFAULT_TTS_BACKEND,
    DEFAULT_TTS_GEMINI_MODEL,
    DEFAULT_TTS_GEMINI_VOICE,
    DEFAULT_TTS_STABILITY,
    MAX_TTS_STABILITY,
    MIN_TTS_STABILITY,
    TTS_BACKEND_GEMINI,
    TTS_BACKEND_VIENEU,
    TTS_PREVIEW_MAX_WORDS,
    read_tts_settings,
)
from . import gemini_client

logger = get_logger("tts_local")

GEMINI_TTS_SAMPLE_RATE = 24000   # Gemini TTS: PCM s16le, 24 kHz, mono
GEMINI_TTS_TIMEOUT = 120
VIENEU_CHUNK_CHARS = 250         # VieNeu đọc từng đoạn ngắn cho ổn định + dừng được giữa chừng
_FFMPEG_TIMEOUT_S = 300


class TTSError(RuntimeError):
    """Lỗi khi đọc giọng (thông báo đã là tiếng Việt)."""


class TTSCancelled(TTSError):
    """Người dùng bấm dừng."""


StopFlag = Optional[Callable[[], bool]]


def _check_stop(stop_flag: StopFlag) -> None:
    if stop_flag and stop_flag():
        raise TTSCancelled("Đã dừng.")


# ============================================================================
# WAV / PCM
# ============================================================================

def pcm_to_wav(
    pcm: bytes, sample_rate: int = GEMINI_TTS_SAMPLE_RATE,
    channels: int = 1, sample_width: int = 2,
) -> bytes:
    """Bọc PCM thô (little-endian có dấu) thành file WAV hoàn chỉnh. Byte lẻ ở cuối
    (không đủ 1 mẫu) bị bỏ để header luôn khớp dữ liệu."""
    if not pcm:
        raise TTSError("Không có dữ liệu âm thanh để ghi.")
    if sample_rate <= 0 or channels <= 0 or sample_width not in (1, 2, 3, 4):
        raise TTSError("Thông số âm thanh không hợp lệ.")
    frame = channels * sample_width
    pcm = pcm[: len(pcm) - (len(pcm) % frame)]
    if not pcm:
        raise TTSError("Dữ liệu âm thanh quá ngắn.")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def parse_sample_rate(mime_type: str, default: int = GEMINI_TTS_SAMPLE_RATE) -> int:
    """Lấy tần số lấy mẫu từ mime kiểu 'audio/L16;codec=pcm;rate=24000'."""
    m = re.search(r"rate\s*=\s*(\d+)", mime_type or "", re.IGNORECASE)
    if m:
        rate = int(m.group(1))
        if 4000 <= rate <= 192000:
            return rate
    return default


def audio_to_wav(data: bytes, mime_type: str = "") -> bytes:
    """Chuẩn hoá audio Gemini trả về thành WAV: nếu đã là WAV (RIFF) thì giữ
    nguyên, ngược lại coi là PCM thô và bọc header theo `rate` trong mime."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return data
    return pcm_to_wav(data, parse_sample_rate(mime_type))


def concat_wavs(wavs: list[bytes]) -> bytes:
    """Nối nhiều WAV cùng định dạng thành một."""
    if not wavs:
        raise TTSError("Không có đoạn âm thanh nào.")
    if len(wavs) == 1:
        return wavs[0]
    params = None
    frames: list[bytes] = []
    for blob in wavs:
        try:
            with wave.open(io.BytesIO(blob), "rb") as w:
                p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = p
                elif p != params:
                    raise TTSError("Các đoạn âm thanh có định dạng khác nhau, không nối được.")
                frames.append(w.readframes(w.getnframes()))
        except (wave.Error, EOFError) as exc:
            raise TTSError(f"Đoạn âm thanh không phải WAV hợp lệ: {exc}") from exc
    return pcm_to_wav(b"".join(frames), params[2], params[0], params[1])


def wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, EOFError):
        return 0.0


def wav_peaks(wav_bytes: bytes, bars: int = 96) -> list[float]:
    """Biên độ chuẩn hoá 0..1 của `bars` đoạn đều nhau — để vẽ dạng sóng. Không đọc được -> []."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels, width, frames = w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return []
    if width != 2 or not frames:
        return []
    samples = array("h")
    samples.frombytes(frames[: len(frames) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = samples[::channels]
    total = len(samples)
    if total == 0:
        return []
    bars = max(1, min(bars, total))
    peaks = []
    for i in range(bars):
        chunk = samples[i * total // bars:(i + 1) * total // bars]
        peaks.append(float(max(max(chunk), -min(chunk))) if chunk else 0.0)
    top = max(peaks) or 1.0
    return [p / top for p in peaks]


def slice_wav(wav_bytes: bytes, start_seconds: float) -> bytes:
    """Cắt WAV từ giây `start_seconds` tới hết (để phát tiếp / tua). Lỗi -> trả nguyên bản."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return wav_bytes
    skip = max(0, int(start_seconds * params.framerate)) * params.sampwidth * params.nchannels
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(frames[skip:])
    return out.getvalue()


# ============================================================================
# Cắt câu
# ============================================================================

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(text: str, max_chars: int = VIENEU_CHUNK_CHARS) -> list[str]:
    """Tách `text` thành các đoạn <= `max_chars` ký tự, ưu tiên ranh giới câu;
    câu quá dài thì cắt theo từ."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for sentence in _SENTENCE_RE.split(text):
        # câu quá dài: cắt theo từ
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            piece, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
        if not sentence:
            continue
        if cur and len(cur) + 1 + len(sentence) > max_chars:
            chunks.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def first_sentence(text: str, max_words: int = TTS_PREVIEW_MAX_WORDS) -> str:
    """Câu đầu tiên của `text` (dùng cho nghe thử), cắt còn tối đa `max_words` từ."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    sentence = _SENTENCE_RE.split(text)[0]
    words = sentence.split()
    return " ".join(words[:max_words]) if len(words) > max_words else sentence


# ============================================================================
# Backend
# ============================================================================

class TTSBackend:
    """Giao diện chung. `synthesize` trả về bytes của một file WAV."""

    label = "TTS"
    supports_voice_sample = False
    # True = gọi nhiều đoạn ngắn không tốn quota (chạy trên máy) -> được tách cả theo dấu phẩy.
    cheap_calls = False

    def synthesize(self, text: str, stop_flag: StopFlag = None) -> bytes:  # pragma: no cover
        raise NotImplementedError


def _extract_audio(data: dict) -> tuple[str, bytes]:
    """Lấy (mime, bytes) audio từ JSON generateContent; ValueError nếu không có."""
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        if reason:
            raise ValueError(f"Gemini từ chối xử lý nội dung (blockReason={reason})")
        raise ValueError("Gemini không trả về candidates")
    for part in (candidates[0].get("content") or {}).get("parts") or []:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or ""
            return mime, base64.b64decode(inline["data"])
    raise ValueError("Gemini không trả về âm thanh (model có thể không hỗ trợ TTS)")


class GeminiTTS(TTSBackend):
    label = "Gemini TTS"

    def __init__(
        self, api_key: str, model: str = DEFAULT_TTS_GEMINI_MODEL,
        voice: str = DEFAULT_TTS_GEMINI_VOICE, generate: Optional[Callable] = None,
        temperature: Optional[float] = None,
    ):
        self.temperature = temperature      # None = mặc định của model
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or DEFAULT_TTS_GEMINI_MODEL
        self.voice = (voice or "").strip() or DEFAULT_TTS_GEMINI_VOICE
        self._generate = generate or gemini_client.generate_content

    def build_payload(self, text: str) -> dict:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}
                },
            },
        }
        if self.temperature is not None:
            payload["generationConfig"]["temperature"] = self.temperature
        return payload

    def synthesize(self, text: str, stop_flag: StopFlag = None) -> bytes:
        if not self.api_key:
            raise TTSError("Chưa có Gemini API Key. Vào mục Cài đặt để nhập API Key trước.")
        text = (text or "").strip()
        if not text:
            raise TTSError("Kịch bản trống, không có gì để đọc.")
        _check_stop(stop_flag)
        # fallback_model="" -> TẮT model dự phòng (model dự phòng mặc định của app
        # là model văn bản, không đọc giọng được).
        mime, data = self._generate(
            self.build_payload(text), self.api_key, model=self.model, fallback_model="",
            timeout=GEMINI_TTS_TIMEOUT, purpose="đọc giọng", extract=_extract_audio,
        )
        _check_stop(stop_flag)
        return audio_to_wav(data, mime)


_VIENEU_ENGINE = None
_VIENEU_LOCK = threading.Lock()


def hf_cache_dir() -> Path:
    """Thư mục cache model của HuggingFace (theo biến môi trường nếu có)."""
    env = os.environ.get("HF_HUB_CACHE")
    if env:
        return Path(env)
    home = os.environ.get("HF_HOME")
    return (Path(home) / "hub") if home else (Path.home() / ".cache" / "huggingface" / "hub")


def materialize_hf_symlinks(cache_dir: Optional[Path] = None) -> int:
    """Thay các symlink trong `snapshots/` của cache HuggingFace (trỏ sang `blobs/`) bằng
    file thật (hardlink, không tốn thêm dung lượng; khác ổ đĩa thì sao chép). Chỉ động
    vào snapshot có file .onnx. Cần vì onnxruntime >= 1.24.1 từ chối file dữ liệu ngoài
    nằm ngoài thư mục model ("External data path escapes model directory"). Trả về số
    file đã thay."""
    root = Path(cache_dir) if cache_dir else hf_cache_dir()
    replaced = 0
    if not root.is_dir():
        return 0
    for snap in root.glob("models--*/snapshots/*"):
        if not snap.is_dir() or not any(snap.rglob("*.onnx")):
            continue
        for path in snap.rglob("*"):
            if not path.is_symlink():
                continue
            target = Path(os.path.realpath(path))
            if not target.is_file():
                continue
            tmp = path.with_name(path.name + ".tmp_real")
            try:
                tmp.unlink(missing_ok=True)
                try:
                    os.link(target, tmp)
                except OSError:
                    shutil.copy2(target, tmp)
                os.replace(tmp, path)
                replaced += 1
            except OSError as exc:
                logger.warning("Không thay được symlink %s: %s", path.name, exc)
                tmp.unlink(missing_ok=True)
    return replaced


def _is_ort_external_path_error(exc: BaseException) -> bool:
    msg = str(exc)
    return "External data path" in msg or "escapes model directory" in msg


_ORT_HINT = (
    " Cách khác: pip install \"onnxruntime<1.24.1\" (bản mới chặn file model nằm ngoài "
    "thư mục), hoặc chọn backend Gemini TTS."
)


def _load_vieneu_engine():
    """Nạp engine VieNeu 1 lần rồi dùng lại (nạp model khá nặng)."""
    global _VIENEU_ENGINE
    with _VIENEU_LOCK:
        if _VIENEU_ENGINE is None:
            try:
                from vieneu import Vieneu  # type: ignore
            except ImportError as exc:
                raise TTSError(
                    "Chưa cài VieNeu-TTS. Chạy: pip install vieneu (cần Python 3.10 trở lên), "
                    "hoặc chọn backend Gemini TTS."
                ) from exc
            try:
                try:
                    _VIENEU_ENGINE = Vieneu()
                except Exception as exc:  # noqa: BLE001 - thư viện ngoài, lỗi đa dạng
                    if not _is_ort_external_path_error(exc):
                        raise
                    # onnxruntime mới không chịu symlink của cache HuggingFace:
                    # đổi sang file thật rồi thử lại 1 lần.
                    n = materialize_hf_symlinks()
                    logger.warning("onnxruntime chặn symlink cache HF; đã thay %d file, thử lại", n)
                    if not n:
                        raise
                    _VIENEU_ENGINE = Vieneu()
            except Exception as exc:  # noqa: BLE001
                hint = _ORT_HINT if _is_ort_external_path_error(exc) else ""
                raise TTSError(f"Không khởi động được VieNeu-TTS: {exc}.{hint}") from exc
        return _VIENEU_ENGINE


class VieNeuTTS(TTSBackend):
    label = "VieNeu-TTS"
    supports_voice_sample = True
    cheap_calls = True

    def __init__(
        self, voice_sample: str = "", preset_voice: str = "",
        engine_factory: Optional[Callable] = None, temperature: Optional[float] = None,
    ):
        self.temperature = temperature      # None = mặc định của engine
        self.voice_sample = (voice_sample or "").strip()
        self.preset_voice = (preset_voice or "").strip()
        self._engine_factory = engine_factory or _load_vieneu_engine

    def _infer_kwargs(self) -> dict:
        if self.voice_sample:
            if not Path(self.voice_sample).is_file():
                raise TTSError(f"Không tìm thấy file giọng mẫu: {self.voice_sample}")
            return {"ref_audio": self.voice_sample, "denoise": True}
        if self.preset_voice:
            return {"voice": self.preset_voice}
        return {}

    def _infer(self, engine, chunk: str, kwargs: dict):
        """Gọi engine.infer, thêm `temperature` nếu có. Bản VieNeu không nhận tham số này
        (TypeError) thì thử lại không có nó và nhớ luôn để các đoạn sau khỏi thử lại."""
        if self.temperature is not None and not getattr(self, "_no_temperature", False):
            try:
                return engine.infer(chunk, temperature=self.temperature, **kwargs)
            except TypeError:
                self._no_temperature = True
                logger.warning("VieNeu không nhận tham số temperature — bỏ qua độ ổn định giọng")
        return engine.infer(chunk, **kwargs)

    def synthesize(self, text: str, stop_flag: StopFlag = None) -> bytes:
        chunks = split_sentences(text)
        if not chunks:
            raise TTSError("Kịch bản trống, không có gì để đọc.")
        kwargs = self._infer_kwargs()
        engine = self._engine_factory()
        wavs: list[bytes] = []
        with tempfile.TemporaryDirectory(prefix="vieneu_") as tmp:
            for i, chunk in enumerate(chunks):
                _check_stop(stop_flag)
                try:
                    audio = self._infer(engine, chunk, kwargs)
                    out = Path(tmp) / f"part{i}.wav"
                    engine.save(audio, str(out))
                    wavs.append(out.read_bytes())
                except TTSError:
                    raise
                except Exception as exc:  # noqa: BLE001 - thư viện ngoài
                    raise TTSError(f"VieNeu-TTS lỗi khi đọc: {exc}") from exc
        _check_stop(stop_flag)
        return concat_wavs(wavs)


def stability_to_temperature(stability: float) -> float:
    """Độ ổn định giọng (0 = biểu cảm .. 5 = ổn định) -> temperature lấy mẫu của model
    (cao = nhiều biến thiên/biểu cảm, thấp = đều và điềm tĩnh). 0 -> 1.5, 5 -> 0.1."""
    try:
        st = max(MIN_TTS_STABILITY, min(MAX_TTS_STABILITY, float(stability)))
    except (TypeError, ValueError):
        st = DEFAULT_TTS_STABILITY
    return round(1.5 - 0.28 * st, 2)


def make_backend(kind: str, cfg: dict) -> TTSBackend:
    """Dựng backend theo `kind` và cấu hình `cfg` (giá trị lạ -> mặc định). Chỉ khi cfg có
    khóa `tts_stability` mới truyền temperature (không có = giữ mặc định của model)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    temperature = None
    if "tts_stability" in cfg:
        temperature = stability_to_temperature(read_tts_settings(cfg)["stability"])
    if kind == TTS_BACKEND_GEMINI:
        return GeminiTTS(
            cfg.get("gemini_api_key", ""),
            model=cfg.get("tts_gemini_model", ""),
            voice=cfg.get("tts_gemini_voice", ""),
            temperature=temperature,
        )
    if kind == TTS_BACKEND_VIENEU:
        return VieNeuTTS(
            voice_sample=cfg.get("review_voice_sample", ""),
            preset_voice=cfg.get("tts_vieneu_voice", ""),
            temperature=temperature,
        )
    raise TTSError(f"Backend giọng đọc không hợp lệ: {kind or DEFAULT_TTS_BACKEND}")


# ============================================================================
# Ngắt nghỉ theo dấu câu, tốc độ, khớp thời lượng
# ============================================================================

_CLAUSE_RE = re.compile(r"(?<=[,;:])\s+")
_MIN_CLAUSE_WORDS = 3   # mệnh đề ngắn hơn thì gộp vào mệnh đề kế (đọc đoạn quá ngắn nghe cụt)


@dataclass
class TTSOptions:
    """Tuỳ chọn đọc: ngắt nghỉ, tốc độ, khớp thời lượng. Mặc định = hành vi cũ (không đổi gì)."""
    pause_enabled: bool = False
    pause_sentence: float = 0.0     # giây im lặng sau . ! ? …
    pause_comma: float = 0.0        # giây im lặng sau , ; :
    speed: float = 1.0
    target_seconds: Optional[float] = None
    autofit: bool = False           # True + có target_seconds -> tự chỉnh tốc độ cho khớp

    @classmethod
    def from_cfg(cls, cfg: dict, target_seconds: Optional[float] = None) -> "TTSOptions":
        st = read_tts_settings(cfg)
        return cls(
            pause_enabled=st["pause_enabled"], pause_sentence=st["pause_sentence"],
            pause_comma=st["pause_comma"], speed=st["speed"],
            target_seconds=target_seconds, autofit=st["autofit"],
        )


def split_for_pauses(
    text: str, pause_sentence: float, pause_comma: float, split_commas: bool = True,
) -> list[tuple[str, float]]:
    """Tách `text` thành các đoạn [(đoạn, số giây im lặng SAU đoạn đó)]. Đoạn cuối luôn 0.
    Hết câu (. ! ? …) -> `pause_sentence`; hết mệnh đề (, ; :) -> `pause_comma` (chỉ khi
    `split_commas`, và mệnh đề dưới 3 từ được gộp vào mệnh đề kế)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    out: list[tuple[str, float]] = []
    for sentence in _SENTENCE_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if split_commas and pause_comma > 0:
            clauses = [c.strip() for c in _CLAUSE_RE.split(sentence) if c.strip()]
        else:
            clauses = [sentence]
        merged: list[str] = []
        buf = ""
        for i, clause in enumerate(clauses):
            buf = f"{buf} {clause}".strip()
            if len(buf.split()) >= _MIN_CLAUSE_WORDS or i == len(clauses) - 1:
                merged.append(buf)
                buf = ""
        for j, piece in enumerate(merged):
            last = j == len(merged) - 1
            out.append((piece, pause_sentence if last else pause_comma))
    if out:
        out[-1] = (out[-1][0], 0.0)
    return out


def silence_wav(seconds: float, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """WAV im lặng dài `seconds` giây, cùng định dạng với các đoạn đọc."""
    frames = max(0, int(round(float(seconds) * sample_rate)))
    return pcm_to_wav(b"\x00" * (frames * channels * sample_width) or b"\x00" * channels * sample_width,
                      sample_rate, channels, sample_width)


def _wav_format(blob: bytes) -> tuple[int, int, int]:
    try:
        with wave.open(io.BytesIO(blob), "rb") as w:
            return w.getframerate(), w.getnchannels(), w.getsampwidth()
    except (wave.Error, EOFError) as exc:
        raise TTSError(f"Đoạn âm thanh không phải WAV hợp lệ: {exc}") from exc


def join_with_pauses(parts: list[tuple[bytes, float]]) -> bytes:
    """Nối các (WAV, giây im lặng sau đó) thành một WAV, chèn khoảng lặng cùng định dạng."""
    if not parts:
        raise TTSError("Không có đoạn âm thanh nào.")
    rate, ch, width = _wav_format(parts[0][0])
    wavs: list[bytes] = []
    for i, (blob, pause) in enumerate(parts):
        wavs.append(blob)
        if pause > 0 and i < len(parts) - 1:
            wavs.append(silence_wav(pause, rate, ch, width))
    return concat_wavs(wavs)


def synthesize_with_options(
    backend: TTSBackend, text: str, opts: Optional[TTSOptions] = None,
    stop_flag: StopFlag = None, progress_cb: Optional[Callable[[str], None]] = None,
) -> bytes:
    """Đọc `text`: nếu bật ngắt nghỉ thì đọc từng câu/mệnh đề rồi chèn khoảng lặng, ngược
    lại đọc nguyên đoạn như trước. Backend online (tốn quota) chỉ tách theo câu."""
    if not opts or not opts.pause_enabled or (opts.pause_sentence <= 0 and opts.pause_comma <= 0):
        return backend.synthesize(text, stop_flag)
    segments = split_for_pauses(
        text, opts.pause_sentence, opts.pause_comma, split_commas=backend.cheap_calls,
    )
    if len(segments) <= 1:
        return backend.synthesize(text, stop_flag)
    parts: list[tuple[bytes, float]] = []
    for i, (seg, pause) in enumerate(segments, 1):
        _check_stop(stop_flag)
        if progress_cb:
            progress_cb(f"Đang đọc {backend.label} ({i}/{len(segments)})...")
        parts.append((backend.synthesize(seg, stop_flag), pause))
    return join_with_pauses(parts)


def fit_speed(duration: float, target: float,
              lo: float = AUTOFIT_MIN_SPEED, hi: float = AUTOFIT_MAX_SPEED) -> float:
    """Hệ số tốc độ để audio dài `duration` giây thành `target` giây (kẹp trong lo..hi)."""
    if duration <= 0 or target <= 0:
        return 1.0
    return max(lo, min(hi, duration / target))


def change_speed_wav(wav_bytes: bytes, speed: float, ffmpeg_path: Optional[str]) -> bytes:
    """Đổi tốc độ WAV bằng ffmpeg `atempo` (giữ nguyên cao độ). speed≈1 hoặc không có
    ffmpeg -> trả nguyên WAV. Ném TTSError nếu ffmpeg lỗi."""
    if not ffmpeg_path or abs(speed - 1.0) < 0.01:
        return wav_bytes
    speed = max(0.5, min(2.0, float(speed)))     # atempo hợp lệ trong 0.5..2.0
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="tts_speed_") as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
        src.write_bytes(wav_bytes)
        cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
               "-filter:a", f"atempo={speed:.4f}", str(dst)]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_S, creationflags=flags)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSError(f"Không đổi được tốc độ đọc (ffmpeg): {exc}") from exc
        if res.returncode != 0 or not dst.is_file():
            tail = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            raise TTSError("ffmpeg lỗi khi đổi tốc độ đọc: " + " | ".join(tail))
        return dst.read_bytes()


def apply_speed(
    wav_bytes: bytes, opts: Optional[TTSOptions], ffmpeg_path: Optional[str],
) -> tuple[bytes, float]:
    """Áp tốc độ theo `opts` (tự khớp thời lượng nếu bật). Trả (WAV, tốc độ thực tế đã áp);
    tốc độ thực tế = 1.0 nếu không áp được (không có ffmpeg)."""
    if not opts:
        return wav_bytes, 1.0
    speed = opts.speed
    if opts.autofit and opts.target_seconds:
        speed = fit_speed(wav_duration_seconds(wav_bytes), float(opts.target_seconds))
    if not ffmpeg_path or abs(speed - 1.0) < 0.01:
        return wav_bytes, 1.0
    return change_speed_wav(wav_bytes, speed, ffmpeg_path), speed


# ============================================================================
# Lưu file (mp3 bằng ffmpeg, dự phòng wav)
# ============================================================================

def wav_to_mp3(wav_path: Path, mp3_path: Path, ffmpeg_path: str) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_path),
        "-codec:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(mp3_path),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_S, creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TTSError(f"Không đổi được sang mp3 (ffmpeg): {exc}") from exc
    if res.returncode != 0:
        tail = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise TTSError("ffmpeg lỗi khi đổi sang mp3: " + " | ".join(tail))


def save_audio_file(
    wav_bytes: bytes, out_dir: Path, stem: str, ffmpeg_path: Optional[str],
) -> Path:
    """Lưu `wav_bytes` thành <out_dir>/<stem>.mp3 (cần ffmpeg). Không có ffmpeg
    thì lưu <stem>.wav. Ghi qua file tạm rồi đổi tên nên không bao giờ để lại file
    dở dang. Ghi đè nếu đã tồn tại. Trả về đường dẫn file đã lưu."""
    stem = (stem or "").strip()
    if not stem:
        raise TTSError("Thiếu tên file (stem) để lưu audio.")
    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TTSError(f"Không tạo được thư mục audio: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="tts_save_") as tmp:
        tmp_wav = Path(tmp) / "in.wav"
        tmp_wav.write_bytes(wav_bytes)
        if ffmpeg_path:
            tmp_out = Path(tmp) / "out.mp3"
            wav_to_mp3(tmp_wav, tmp_out, ffmpeg_path)
            final = out_dir / f"{stem}.mp3"
        else:
            tmp_out = tmp_wav
            final = out_dir / f"{stem}.wav"
        try:
            shutil.move(str(tmp_out), str(final))   # an toàn khác ổ đĩa
        except OSError as exc:
            raise TTSError(f"Không ghi được file '{final.name}': {exc}") from exc
    logger.info("Đã lưu audio | %s", final.name)
    return final


def synthesize_to_file(
    backend: TTSBackend, text: str, out_dir: Path, stem: str,
    ffmpeg_path: Optional[str] = None, stop_flag: StopFlag = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    opts: Optional[TTSOptions] = None,
) -> Path:
    """Đọc `text` bằng `backend` rồi lưu <stem>.mp3 (hoặc .wav) vào `out_dir`.
    `opts` (ngắt nghỉ + tốc độ) mặc định None = đọc như trước."""
    if progress_cb:
        progress_cb(f"Đang đọc bằng {backend.label}...")
    wav = synthesize_with_options(backend, text, opts, stop_flag, progress_cb)
    _check_stop(stop_flag)
    wav, _ = apply_speed(wav, opts, ffmpeg_path)
    _check_stop(stop_flag)
    if progress_cb:
        progress_cb("Đang lưu file audio...")
    return save_audio_file(wav, out_dir, stem, ffmpeg_path)


# ============================================================================
# Phát thử
# ============================================================================

class AudioPlayer:
    """Phát file WAV để nghe thử, dừng được. Windows dùng winsound; macOS afplay;
    Linux thử ffplay/paplay/aplay."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None

    def play(self, wav_path: Path) -> bool:
        self.stop()
        path = str(wav_path)
        if sys.platform.startswith("win"):
            try:
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return True
            except Exception:  # noqa: BLE001
                return False
        if sys.platform == "darwin":
            cmds = [["afplay", path]]
        else:
            cmds = [
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                ["paplay", path], ["aplay", "-q", path],
            ]
        for cmd in cmds:
            if shutil.which(cmd[0]):
                try:
                    self._proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    return True
                except OSError:
                    continue
        return False

    def stop(self) -> None:
        if sys.platform.startswith("win"):
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:  # noqa: BLE001
                pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._proc = None
