"""
audio_merger.py
================
Logic ghép audio cho video bằng ffmpeg — KHÔNG phụ thuộc GUI.

NGUYÊN TẮC CỐT LÕI (giữ xuyên suốt mọi chế độ/tùy chọn bên dưới): VIDEO
luôn là gốc. Luồng HÌNH ảnh lấy từ video (chỉ resize/crop/pad nếu người
dùng chọn), luồng TIẾNG gốc của video bị bỏ và THAY bằng file audio chỉ
định (trừ khi bật "giữ % audio gốc" để trộn thêm). ĐỘ DÀI file xuất ra
LUÔN bằng đúng độ dài của VIDEO gốc (không phải "cái ngắn hơn" như trước
nữa) — audio dài hơn sẽ bị cắt, ngắn hơn sẽ để im lặng phần còn thiếu
(hoặc lặp lại nếu bật tùy chọn lặp audio).

YÊU CẦU: máy phải có ffmpeg VÀ ffprobe (đi kèm sẵn trong mọi bản tải
ffmpeg). Tải miễn phí tại https://ffmpeg.org hoặc bản dựng sẵn Windows tại
https://www.gyan.dev/ffmpeg/builds/
"""

from __future__ import annotations

import csv
import functools
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegNotFoundError(RuntimeError):
    pass


class MergeCancelled(Exception):
    """Người dùng bấm '⏹ Dừng' trong lúc cặp video/audio này đang ghép dở."""
    pass


class MergeError(RuntimeError):
    pass


# ============================================================================
# Dò tìm ffmpeg / ffprobe
# ============================================================================

def _bundled_ffmpeg_dirs() -> list:
    """Thư mục ffmpeg đi kèm app (bộ cài đặt ffmpeg ở `<thư mục app>\\ffmpeg`)."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return [base / "ffmpeg", base]


def find_ffmpeg(custom_path: str = "") -> Optional[str]:
    """Tìm đường dẫn thực thi ffmpeg: ưu tiên `custom_path` (chấp nhận trỏ
    thẳng tới ffmpeg.exe HOẶC trỏ tới thư mục chứa nó), sau đó tìm trong
    thư mục ffmpeg đi kèm app, rồi PATH hệ thống. Trả về None nếu không tìm thấy."""
    custom_path = (custom_path or "").strip()
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return str(p)
        if p.is_dir():
            for name in ("ffmpeg.exe", "ffmpeg"):
                candidate = p / name
                if candidate.is_file():
                    return str(candidate)
    for d in _bundled_ffmpeg_dirs():           # ffmpeg đi kèm bộ cài, không cần sửa PATH
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = d / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffmpeg")


def find_ffprobe(ffmpeg_path: Optional[str], custom_path: str = "") -> Optional[str]:
    """Tìm ffprobe: ưu tiên `custom_path`, sau đó thử tìm CÙNG THƯ MỤC với
    `ffmpeg_path` đã tìm được (mọi bản phân phối ffmpeg đều đi kèm
    ffprobe cùng chỗ), cuối cùng tìm trong PATH hệ thống."""
    custom_path = (custom_path or "").strip()
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return str(p)
    if ffmpeg_path:
        p = Path(ffmpeg_path)
        for name in ("ffprobe.exe", "ffprobe"):
            candidate = p.parent / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffprobe")


# ============================================================================
# Liệt kê & ghép cặp file
# ============================================================================

def list_media_files(folder: Optional[Path], extensions: set[str]) -> list[Path]:
    """Liệt kê file trong `folder` (không đệ quy) có phần mở rộng nằm
    trong `extensions` (không phân biệt hoa/thường), sắp theo tên."""
    if not folder or not folder.is_dir():
        return []
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in extensions
    ]
    return sorted(files, key=lambda f: f.name.lower())


class MediaPair:
    """1 dòng trong danh sách ghép."""

    __slots__ = ("stem", "video", "audio")

    def __init__(self, stem: str, video: Optional[Path], audio: Optional[Path]):
        self.stem = stem
        self.video = video
        self.audio = audio

    @property
    def status(self) -> str:
        if self.video and self.audio:
            return "Sẵn sàng"
        if self.video and not self.audio:
            return "Thiếu audio khớp tên"
        return "Thiếu video khớp tên"

    @property
    def is_ready(self) -> bool:
        return self.video is not None and self.audio is not None


def match_video_audio_pairs(
    video_files: list[Path], audio_files: list[Path]
) -> list[MediaPair]:
    """CHẾ ĐỘ "Khớp theo tên file": ghép theo TÊN FILE (bỏ phần mở rộng),
    không phân biệt hoa/thường."""
    video_by_stem = {v.stem.lower(): v for v in video_files}
    audio_by_stem = {a.stem.lower(): a for a in audio_files}
    all_stems = sorted(set(video_by_stem) | set(audio_by_stem))

    pairs = []
    for stem_lower in all_stems:
        video = video_by_stem.get(stem_lower)
        audio = audio_by_stem.get(stem_lower)
        display_stem = video.stem if video else audio.stem
        pairs.append(MediaPair(display_stem, video, audio))
    return pairs


def build_random_mix_pairs(
    video_files: list[Path],
    audio_files: list[Path],
    target_count: Optional[int] = None,
    used_history: Optional[set[tuple[str, str]]] = None,
    rng: Optional[random.Random] = None,
) -> list[MediaPair]:
    """CHẾ ĐỘ "Trộn ngẫu nhiên": ghép mỗi video với 1 audio NGẪU NHIÊN.

    Nguyên tắc công bằng:
      - `target_count` (K) = số video muốn xuất ra. None/0 = mỗi video
        đúng 1 lượt (K = số video).
      - MỖI VIDEO được dùng đúng 1 LƯỢT trước khi bất kỳ video nào được
        dùng lại lượt 2 (xáo ngẫu nhiên toàn bộ danh sách rồi lấy lần
        lượt; hết danh sách mới xáo lại vòng mới) — đúng yêu cầu "ưu tiên
        mỗi video ghép 1 lượt trước".
      - Audio cũng được xáo & xoay vòng tương tự, để không audio nào bị
        dùng dồn dập trong khi audio khác chưa được dùng.
      - TRÁNH LẶP LẠI cặp (video, audio) đã dùng — kể cả từ CÁC LẦN CHẠY
        TRƯỚC nếu truyền `used_history` — chỉ chấp nhận lặp khi đã dùng
        hết mọi tổ hợp có thể (không còn cách nào khác)."""
    if not video_files or not audio_files:
        return []
    rng = rng or random.Random()
    used_history = used_history or set()
    k = target_count if target_count and target_count > 0 else len(video_files)

    videos_cycle = video_files.copy()
    rng.shuffle(videos_cycle)
    vid_idx = 0

    def next_video() -> Path:
        nonlocal vid_idx, videos_cycle
        if vid_idx >= len(videos_cycle):
            videos_cycle = video_files.copy()
            rng.shuffle(videos_cycle)
            vid_idx = 0
        v = videos_cycle[vid_idx]
        vid_idx += 1
        return v

    audio_cycle = audio_files.copy()
    rng.shuffle(audio_cycle)
    aud_idx = 0
    used_this_run: set[tuple[str, str]] = set()

    def next_audio_for(video: Path) -> Path:
        nonlocal aud_idx, audio_cycle
        max_attempts = len(audio_files) * 2 + 5
        best_fallback = None
        for _ in range(max_attempts):
            if aud_idx >= len(audio_cycle):
                audio_cycle = audio_files.copy()
                rng.shuffle(audio_cycle)
                aud_idx = 0
            a = audio_cycle[aud_idx]
            aud_idx += 1
            best_fallback = a
            key = (video.name, a.name)
            if key not in used_history and key not in used_this_run:
                return a
        # Đã thử hết mà vẫn toàn cặp trùng -> hết cách, đành chấp nhận lặp
        return best_fallback

    pairs: list[MediaPair] = []
    for i in range(k):
        v = next_video()
        a = next_audio_for(v)
        used_this_run.add((v.name, a.name))
        pairs.append(MediaPair(f"{v.stem}__{a.stem}", v, a))
    return pairs


# ============================================================================
# Lịch sử cặp đã dùng (tránh lặp giữa các lần chạy)
# ============================================================================

def load_pair_history(cfg: dict) -> set[tuple[str, str]]:
    raw = cfg.get("merge_used_pairs_history") or []
    result = set()
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result.add((str(item[0]), str(item[1])))
    return result


def save_pair_history(cfg: dict, history: set[tuple[str, str]], max_entries: int) -> None:
    items = list(history)
    if len(items) > max_entries:
        items = items[-max_entries:]
    cfg["merge_used_pairs_history"] = [[v, a] for v, a in items]


# ============================================================================
# Xuất log CSV
# ============================================================================

def write_merge_log_csv(output_dir: Path, rows: list[dict]) -> Path:
    """Ghi log CSV các cặp đã ghép trong lần chạy này: video, audio, file
    xuất, trạng thái, chi tiết lỗi (nếu có), thời điểm. Trả về path file
    CSV đã ghi."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = output_dir / f"merge_log_{ts}.csv"
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["video", "audio", "output_file", "status", "message", "time"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return log_path


# ============================================================================
# ffprobe: lấy thông tin media (thời lượng, kích thước, có audio hay không)
# ============================================================================

class MediaInfo:
    __slots__ = ("duration", "width", "height", "has_audio")

    def __init__(self, duration: float, width: int, height: int, has_audio: bool):
        self.duration = duration
        self.width = width
        self.height = height
        self.has_audio = has_audio


def probe_media_info(ffprobe_path: str, path: Path) -> MediaInfo:
    """Gọi ffprobe lấy thời lượng + kích thước + video có audio hay
    không. Raise MergeError nếu không đọc được (file hỏng/không phải
    media)."""
    cmd = [
        ffprobe_path, "-v", "error",
        "-show_entries", "format=duration:stream=width,height,codec_type",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MergeError(f"Không đọc được thông tin file '{path.name}': {exc}") from exc

    try:
        data = json.loads(result.stdout or "{}")
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        width = height = 0
        has_audio = False
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not width:
                width = int(s.get("width") or 0)
                height = int(s.get("height") or 0)
            if s.get("codec_type") == "audio":
                has_audio = True
        if duration <= 0:
            raise ValueError("thời lượng = 0 hoặc không đọc được")
        return MediaInfo(duration, width, height, has_audio)
    except (ValueError, KeyError, TypeError) as exc:
        raise MergeError(
            f"Không đọc được thông tin file '{path.name}' (file có thể bị hỏng): {exc}"
        ) from exc


# ============================================================================
# Dựng lệnh ffmpeg
# ============================================================================

def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def compute_output_size(
    aspect_ratio: Optional[tuple[int, int]],
    target_short_side: Optional[int],
    source_width: int,
    source_height: int,
    no_upscale: bool = False,
) -> Optional[tuple[int, int]]:
    """Tính kích thước (rộng, cao) CUỐI CÙNG của video xuất ra, theo cách
    của CapCut/TikTok: độ phân giải (720p, 1080p, 2K, 4K...) là độ dài của
    CẠNH NGẮN. Vd video dọc 9:16 chọn 1080p -> 1080x1920; video ngang 16:9
    chọn 1080p -> 1920x1080; video vuông 1:1 chọn 2K -> 1440x1440.

    - `aspect_ratio`: tỉ lệ khung hình đích (None = giữ tỉ lệ của video gốc).
    - `target_short_side`: None = giữ nguyên độ phân giải gốc.
    - `no_upscale`: True = không phóng to quá cạnh ngắn của video gốc.
    Trả về None nếu KHÔNG cần đổi kích thước (để có thể copy luồng video)."""
    src_short = min(source_width, source_height) if source_width and source_height else 0
    eff = target_short_side
    if eff and no_upscale and src_short and eff > src_short:
        eff = src_short

    if aspect_ratio:
        ar_w, ar_h = aspect_ratio
        base = eff or src_short or 1080
        if ar_w <= ar_h:          # dọc hoặc vuông: cạnh ngắn là chiều rộng
            w, h = base, base * ar_h / ar_w
        else:                     # ngang: cạnh ngắn là chiều cao
            w, h = base * ar_w / ar_h, base
        return _even(w), _even(h)

    if not eff or not source_width or not source_height:
        return None
    if source_width <= source_height:
        w, h = eff, eff * source_height / source_width
    else:
        w, h = eff * source_width / source_height, eff
    w, h = _even(w), _even(h)
    if (w, h) == (source_width, source_height):
        return None
    return w, h


def _fit_filter_parts(cur: str, cw: int, ch: int, fit_mode: str, out_label: str) -> list[str]:
    """Các bước filter đưa video `cur` vào khung `cw`x`ch` theo `fit_mode`
    (crop / pad_black / pad_blur). Dùng chung cho xuất video và cho khung
    hình xem trước trong trình chỉnh sửa chữ để hai bên luôn giống nhau."""
    if fit_mode == "pad_blur":
        return [
            f"{cur}split=2[vbg][vfg]",
            f"[vbg]scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch},gblur=sigma=20[vbgblur]",
            f"[vfg]scale={cw}:{ch}:force_original_aspect_ratio=decrease[vfgs]",
            f"[vbgblur][vfgs]overlay=(W-w)/2:(H-h)/2{out_label}",
        ]
    if fit_mode == "pad_black":
        return [
            f"{cur}scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black{out_label}"
        ]
    return [  # "crop" (mặc định)
        f"{cur}scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch}{out_label}"
    ]


# ---------------------------------------------------------------------------
# Chèn chữ (drawtext)
# ---------------------------------------------------------------------------

_FONT_CANDIDATES_REGULAR = [
    # Windows
    "{WINDIR}/Fonts/arial.ttf", "{WINDIR}/Fonts/segoeui.ttf", "{WINDIR}/Fonts/tahoma.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]
_FONT_CANDIDATES_BOLD = [
    "{WINDIR}/Fonts/arialbd.ttf", "{WINDIR}/Fonts/segoeuib.ttf", "{WINDIR}/Fonts/tahomabd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def find_default_font(bold: bool = False) -> Optional[str]:
    """Tìm 1 font hệ thống có hỗ trợ tiếng Việt (Arial / Segoe UI / DejaVu...).
    Ưu tiên bản đậm nếu `bold`; nếu không có bản đậm thì rơi về bản thường."""
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows"
    groups = [_FONT_CANDIDATES_BOLD, _FONT_CANDIDATES_REGULAR] if bold else [_FONT_CANDIDATES_REGULAR]
    for group in groups:
        for tpl in group:
            path = Path(tpl.replace("{WINDIR}", windir))
            if path.is_file():
                return str(path)
    return None


try:  # Pillow là TÙY CHỌN: có thì đo độ rộng chữ chính xác theo file font
    from PIL import ImageFont as _PILImageFont
except Exception:  # noqa: BLE001 - thiếu/hỏng Pillow thì dùng ước lượng
    _PILImageFont = None


# ---------------------------------------------------------------------------
# Danh sách font cài trên máy (để người dùng chọn theo TÊN thay vì tự tìm
# file .ttf/.otf thủ công)
# ---------------------------------------------------------------------------

_FONT_FILE_EXTS = (".ttf", ".otf", ".ttc")


def _font_search_dirs() -> list[Path]:
    """Các thư mục chứa font của hệ điều hành hiện tại (chỉ trả về thư mục
    thực sự tồn tại)."""
    dirs: list[Path] = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows"
        dirs.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [
            Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"), Path.home() / "Library/Fonts",
        ]
    else:
        dirs += [
            Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
            Path.home() / ".fonts", Path.home() / ".local/share/fonts",
        ]
    return [d for d in dirs if d.is_dir()]


_REGULAR_STYLE_NAMES = {"", "regular", "normal", "book", "roman"}


def _read_font_name(path: Path) -> tuple[str, str]:
    """(họ_font, kiểu) đọc từ chính file font, VD ("Roboto", "Bold"). Cần
    Pillow để đọc chính xác; không có Pillow thì đoán tạm theo tên file."""
    if _PILImageFont is not None:
        try:
            font = _PILImageFont.truetype(str(path), 24)
            family, style = font.getname()
            if family:
                return family.strip(), (style or "").strip()
        except Exception:  # noqa: BLE001 - file font lỗi/không đọc được, bỏ qua
            pass
    return path.stem, ""


_system_fonts_cache: Optional[dict[str, dict[str, str]]] = None


def list_system_fonts(force_rescan: bool = False) -> dict[str, dict[str, str]]:
    """Quét font cài trên máy, trả về {tên_họ_font: {"regular": path,
    "bold": path}} (thiếu biến thể nào thì khóa đó vắng mặt). Chỉ quét 1 lần
    mỗi phiên làm việc rồi lưu vào bộ nhớ (đọc hàng trăm file font khá chậm);
    gọi lại với `force_rescan=True` để quét lại (VD người dùng vừa cài thêm
    font). An toàn khi không có Pillow — khi đó chỉ nhóm được theo tên file,
    không đọc được kiểu Regular/Bold thật của font."""
    global _system_fonts_cache
    if _system_fonts_cache is not None and not force_rescan:
        return _system_fonts_cache

    result: dict[str, dict[str, str]] = {}
    fallback_any: dict[str, str] = {}
    seen_files: set[str] = set()
    for folder in _font_search_dirs():
        try:
            paths = list(folder.rglob("*"))
        except OSError:
            continue
        for path in paths:
            key = str(path)
            if key in seen_files or path.suffix.lower() not in _FONT_FILE_EXTS:
                continue
            if not path.is_file():
                continue
            seen_files.add(key)
            family, style = _read_font_name(path)
            if not family:
                continue
            style_l = style.lower().strip()
            is_italic = "italic" in style_l or "oblique" in style_l
            # CHỈ nhận đúng kiểu "Regular"/"Bold" thuần cho 2 khe regular/bold;
            # các biến thể khác cùng họ (Condensed, ExtraLight, Light, Black,
            # SemiBold...) bị BỎ QUA để không tranh chỗ 1 cách ngẫu nhiên với
            # bản Regular/Bold thật — nếu không, thứ tự quét đĩa (không cố
            # định) có thể khiến app chọn nhầm 1 bản mảnh/đậm khác hẳn.
            slot = None
            if not is_italic:
                if style_l == "bold":
                    slot = "bold"
                elif style_l in _REGULAR_STYLE_NAMES:
                    slot = "regular"
            entry = result.setdefault(family, {})
            if slot and slot not in entry:
                entry[slot] = key
            fallback_any.setdefault(family, key)  # bản ĐẦU TIÊN gặp của họ này

    # "any": dùng khi họ font này không có bản Regular/Bold thuần nào — ưu
    # tiên Regular > Bold > bản bất kỳ đầu tiên gặp được (thà có gì đó còn
    # hơn không chọn được font nào).
    for family, entry in result.items():
        entry["any"] = entry.get("regular") or entry.get("bold") or fallback_any[family]
    _system_fonts_cache = result
    return result


def resolve_family_font(family: str, bold: bool = False) -> Optional[str]:
    """File font (.ttf/.otf) khớp với họ font `family` (đã quét bởi
    `list_system_fonts`), ưu tiên đúng độ đậm yêu cầu; không có họ đó thì
    trả về None (nơi gọi tự rơi về `find_default_font`)."""
    entry = list_system_fonts().get(family)
    if not entry:
        return None
    primary, fallback = ("bold", "regular") if bold else ("regular", "bold")
    return entry.get(primary) or entry.get(fallback) or entry.get("any")


@functools.lru_cache(maxsize=64)
def _pil_font(fontfile: str, size: int):
    return _PILImageFont.truetype(fontfile, size)


def make_width_fn(fontfile: Optional[str], fontsize: int, bold: bool = False):
    """Trả về hàm đo độ rộng (pixel) của 1 chuỗi. Có Pillow + file font hợp
    lệ -> đo CHÍNH XÁC theo font; ngược lại -> ước lượng theo ký tự."""
    if _PILImageFont is not None and fontfile:
        try:
            font = _pil_font(str(fontfile), max(1, int(fontsize)))
            return lambda t: float(font.getlength(t))
        except Exception:  # noqa: BLE001 - font lỗi/không đọc được
            pass
    scale = fontsize * (1.06 if bold else 1.0)
    return lambda t: sum(_char_width_factor(c) for c in t) * scale


_filter_cache: dict[tuple[str, str], bool] = {}


def ffmpeg_has_filter(ffmpeg_path: str, name: str) -> bool:
    """Bản ffmpeg có hỗ trợ filter `name` không (VD: `drawtext` cần libfreetype;
    một số bản build rút gọn không có)."""
    key = (ffmpeg_path, name)
    if key in _filter_cache:
        return _filter_cache[key]
    ok = False
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, creationflags=_CREATE_NO_WINDOW,
        ).stdout
        ok = any(
            len(parts := line.split()) >= 2 and parts[1] == name for line in out.splitlines()
        )
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _filter_cache[key] = ok
    return ok


def write_text_overlay_file(directory: Path, text: str) -> Path:
    """Ghi nội dung chữ ra file UTF-8 để ffmpeg đọc qua `textfile=` — cách
    này KHÔNG cần escape ký tự đặc biệt (: ' \\ % , ...) và giữ nguyên xuống
    dòng + tiếng Việt có dấu. Tên file lấy theo mã băm nội dung và ghi kiểu
    nguyên tử (ghi tạm rồi đổi tên) nên nhiều tiến trình ghép song song dùng
    chung 1 file không bao giờ đọc phải file ghi dở."""
    directory.mkdir(parents=True, exist_ok=True)
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    name = "overlay_" + hashlib.md5(body.encode("utf-8")).hexdigest()[:12] + ".txt"
    path = directory / name
    if not path.is_file():
        tmp = directory / (name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    return path


def _char_width_factor(ch: str) -> float:
    """Ước lượng bề rộng 1 ký tự theo tỉ lệ với cỡ chữ (font sans-serif)."""
    if ch == " ":
        return 0.30
    if ord(ch) > 0x2E80:          # CJK, emoji...: rộng gần bằng chiều cao chữ
        return 1.0
    if ch.isupper() or ch.isdigit():
        return 0.68
    if ch in "iljtfI.,;:!|'":
        return 0.32
    if ch in "mwMW":
        return 0.90
    return 0.56


def wrap_text_for_width(
    text: str, fontsize: int, max_width: float, bold: bool = False, width_fn=None
) -> str:
    """Tự ngắt dòng theo bề rộng (drawtext KHÔNG tự xuống dòng nên câu dài sẽ
    bị cắt mất ở mép khung hình). Giữ nguyên các dấu xuống dòng người dùng đã
    gõ; từ nào dài hơn cả dòng thì bị bẻ tại ký tự. `width_fn` (xem
    make_width_fn) cho phép đo theo font thật; mặc định là ước lượng."""
    width = width_fn or make_width_fn(None, fontsize, bold)

    out_lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        line = ""
        for word in words:
            cand = word if not line else f"{line} {word}"
            if width(cand) <= max_width:
                line = cand
                continue
            if line:
                out_lines.append(line)
                line = ""
            # từ đơn quá dài -> bẻ theo ký tự
            while word and width(word) > max_width:
                cut = 1
                while cut < len(word) and width(word[: cut + 1]) <= max_width:
                    cut += 1
                out_lines.append(word[:cut])
                word = word[cut:]
            line = word
        out_lines.append(line)
    return "\n".join(out_lines)


def _text_fontsize(overlay: dict, out_height: int) -> int:
    return max(8, round(max(int(out_height or 0), 100) * float(overlay.get("size_pct", 5)) / 100))


def _box_pad_pct(overlay: dict, key: str) -> float:
    """Lề nền chữ (% cỡ chữ) theo chiều ngang (box_pad_x) hoặc dọc (box_pad_y)."""
    try:
        return _clamp(float(overlay.get(key, 30)), 0.0, 150.0)
    except (TypeError, ValueError):
        return 30.0


def _box_pad_px(fontsize: int, pct: float) -> int:
    return 0 if pct <= 0 else max(2, round(fontsize * pct / 100))


def prepare_text_overlay(overlay: dict, out_width: int, out_height: int) -> dict:
    """Nếu `overlay` chứa `text` (chưa có `textfile`): tự ngắt dòng cho vừa
    khung hình `out_width` x `out_height`, canh giữa các dòng, ghi ra file
    trong `overlay['workdir']` và trả về bản overlay mới đã có `textfile`.

    Canh giữa: ffmpeg mới (>= 6.1) có sẵn `text_align=center` (khi
    `overlay['native_align']` = True). Bản cũ hơn thì tự chèn khoảng trắng
    đầu dòng để các dòng ngắn nằm giữa khối chữ."""
    if overlay.get("textfile"):
        return overlay
    ov = dict(overlay)
    text = str(ov.get("text", ""))
    fontsize = _text_fontsize(ov, out_height)
    bold = bool(ov.get("bold"))
    width = make_width_fn(ov.get("fontfile"), fontsize, bold)
    if ov.get("wrap", True):
        pad = 2 * fontsize * _box_pad_pct(ov, "box_pad_x") / 100 if ov.get("box") else 0
        border = 2 * max(1, round(fontsize * 0.06)) if ov.get("outline", True) else 0
        max_w = max(fontsize * 2, out_width * 0.94 - pad - border)
        text = wrap_text_for_width(text, fontsize, max_w, bold, width)

    lines = text.split("\n")
    if len(lines) > 1 and not ov.get("native_align"):
        widths = [width(ln) for ln in lines]
        widest = max(widths)
        space_w = max(1.0, width("  ") - width(" ")) if width(" ") else fontsize * 0.3
        lines = [
            " " * max(0, round((widest - w) / 2 / space_w)) + ln for ln, w in zip(lines, widths)
        ]
        text = "\n".join(lines)

    ov["textfile"] = str(write_text_overlay_file(Path(ov["workdir"]), text))
    return ov


def _ff_escape(text: str, specials: str) -> str:
    return "".join("\\" + ch if ch in specials else ch for ch in text)


def _ff_quote_path(p: str) -> str:
    """Escape đường dẫn để dùng làm giá trị option trong filter ffmpeg. ffmpeg
    escape 2 TẦNG (tầng option của filter rồi tầng filtergraph) nên mỗi ký
    tự đặc biệt (\\ ' : , ; [ ]) phải được escape đúng cả 2 tầng — cách này
    an toàn với ổ đĩa Windows (C:), khoảng trắng và cả dấu nháy trong tên
    thư mục người dùng."""
    s = str(p).replace("\\", "/")
    level2 = _ff_escape(s, "\\':")
    return _ff_escape(level2, "\\'[],;")


def _hex6(color: str, default: str = "FFFFFF") -> str:
    c = str(color or "").lstrip("#")
    if len(c) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in c):
        return default
    return c.upper()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def build_drawtext_filter(overlay: dict, out_width: int, out_height: int) -> str:
    """Dựng chuỗi filter `drawtext=...` cho 1 LỚP chữ. Các khóa của `overlay`:
      textfile, fontfile (bắt buộc)
      size_pct   cỡ chữ = % chiều cao khung hình xuất ra
      cx, cy     vị trí TÂM khối chữ, dạng tỉ lệ 0..1 của khung hình
                 (0.5, 0.5 = chính giữa) — như kéo thả trong CapCut
      color, outline (+outline_color), box (+box_color, box_opacity 0-100)
      start, duration (giây; duration 0 = hiện đến hết video)
      native_align  ffmpeg có `text_align` (canh giữa nhiều dòng)
    Kích thước chữ/viền/lề tính theo PIXEL từ chiều cao khung hình nên chữ
    luôn cân đối dù xuất 360p hay 4K. Vị trí luôn được kẹp trong khung hình
    nên chữ không bao giờ bị tràn ra ngoài mép."""
    out_height = max(int(out_height or 0), 100)
    fontsize = _text_fontsize(overlay, out_height)
    cx = _clamp(float(overlay.get("cx", 0.5)), 0.0, 1.0)
    cy = _clamp(float(overlay.get("cy", 0.85)), 0.0, 1.0)
    boxed = bool(overlay.get("box"))
    pad_x = _box_pad_px(fontsize, _box_pad_pct(overlay, "box_pad_x")) if boxed else 0
    pad_y = _box_pad_px(fontsize, _box_pad_pct(overlay, "box_pad_y")) if boxed else 0
    pad = max(pad_x, pad_y)
    border = max(1, round(fontsize * 0.06)) if overlay.get("outline", True) else 0
    # Lề an toàn: chừa chỗ cho viền chữ + nền + phần chữ thò xuống dưới dòng
    # (text_w/text_h của ffmpeg không tính các phần này) -> chữ KHÔNG bao giờ
    # bị cắt mép khi kéo sát cạnh khung hình.
    m = pad + border + max(2, round(fontsize * 0.08))

    x_expr = f"max({m},min(w-text_w-{m},w*{cx:.4f}-text_w/2))"
    y_expr = f"max({m},min(h-text_h-{m},h*{cy:.4f}-text_h/2))"
    opts = [
        f"fontfile={_ff_quote_path(overlay['fontfile'])}",
        f"textfile={_ff_quote_path(overlay['textfile'])}",
        "expansion=none",
        f"fontsize={fontsize}",
        f"fontcolor=0x{_hex6(overlay.get('color'))}",
        f"x='{x_expr}'",
        f"y='{y_expr}'",
    ]
    if overlay.get("native_align"):
        opts.append("text_align=center")
    if overlay.get("outline", True):
        opts += [
            f"borderw={max(1, round(fontsize * 0.06))}",
            f"bordercolor=0x{_hex6(overlay.get('outline_color'), '000000')}",
        ]
    if overlay.get("box", False):
        alpha = _clamp(int(overlay.get("box_opacity", 50)), 0, 100) / 100
        opts += [
            "box=1",
            f"boxcolor=0x{_hex6(overlay.get('box_color'), '000000')}@{alpha}",
            # ffmpeg mới: boxborderw nhận "trên|phải|dưới|trái" -> chỉnh riêng
            # ngang/dọc. ffmpeg cũ chỉ nhận 1 số -> dùng trung bình 2 chiều.
            f"boxborderw={pad_y}|{pad_x}|{pad_y}|{pad_x}" if overlay.get("box_list_border")
            else f"boxborderw={round((pad_x + pad_y) / 2)}",
        ]

    start = max(0.0, float(overlay.get("start", 0) or 0))
    duration = max(0.0, float(overlay.get("duration", 0) or 0))
    if start > 0 or duration > 0:
        end_expr = f"{start + duration:g}" if duration > 0 else "1e9"
        opts.append(f"enable='between(t,{start:g},{end_expr})'")
    return "drawtext=" + ":".join(opts)


def _normalize_text_layers(text_overlay) -> list[dict]:
    if not text_overlay:
        return []
    if isinstance(text_overlay, dict):
        return [text_overlay]
    return [layer for layer in text_overlay if layer]


# ---------------------------------------------------------------------------
# Lớp BLUR (làm mờ 1 vùng hình chữ nhật, không có chữ)
# ---------------------------------------------------------------------------

# Độ mờ (sigma của gaussian) = BLUR_SIGMA_PER_STRENGTH * strength% * chiều cao
# khung. Tính theo TỈ LỆ chiều cao nên vùng mờ trông như nhau ở mọi độ phân
# giải; khung xem trước trong trình chỉnh sửa dùng CHUNG hệ số này.
BLUR_SIGMA_PER_STRENGTH = 0.0004   # strength 100 -> sigma = 4% chiều cao khung


def is_blur_layer(layer: dict) -> bool:
    return str(layer.get("kind", "text")) == "blur"


def order_overlay_layers(layers: list[dict]) -> list[dict]:
    """Thứ tự áp dụng khi xuất: mọi lớp blur trước, rồi tới lớp chữ (giữ
    nguyên thứ tự tương đối trong mỗi nhóm)."""
    return [x for x in layers if is_blur_layer(x)] + [x for x in layers if not is_blur_layer(x)]


def blur_sigma(strength: float, frame_height: float) -> float:
    """Sigma (pixel) của gaussian blur ứng với độ mờ `strength` (1..100) trên
    khung hình cao `frame_height` pixel."""
    return max(1.0, float(frame_height) * _clamp(float(strength), 1.0, 100.0) * BLUR_SIGMA_PER_STRENGTH)


def blur_region_px(layer: dict, out_width: int, out_height: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) PIXEL của vùng blur trong khung `out_width` x `out_height`.
    Lưu dạng tỉ lệ 0..1 (tâm cx, cy; kích thước bw, bh) nên khớp mọi độ phân
    giải. Toàn bộ giá trị được làm CHẴN và kẹp trong khung (yuv420 cần tọa độ
    chẵn để vùng mờ khớp khít, không lệch 1 pixel)."""
    out_w, out_h = max(int(out_width), 4), max(int(out_height), 4)
    bw = _clamp(float(layer.get("bw", 0.5)), 0.02, 1.0)
    bh = _clamp(float(layer.get("bh", 0.12)), 0.02, 1.0)
    cx = _clamp(float(layer.get("cx", 0.5)), 0.0, 1.0)
    cy = _clamp(float(layer.get("cy", 0.85)), 0.0, 1.0)
    max_w, max_h = out_w // 2 * 2, out_h // 2 * 2
    w = min(max_w, max(4, round(bw * out_w) // 2 * 2))
    h = min(max_h, max(4, round(bh * out_h) // 2 * 2))
    x = int(_clamp(round(cx * out_w - w / 2), 0, out_w - w)) // 2 * 2
    y = int(_clamp(round(cy * out_h - h / 2), 0, out_h - h)) // 2 * 2
    return x, y, w, h


def build_blur_filters(
    layer: dict, in_label: str, out_label: str, index: int, out_width: int, out_height: int
) -> list[str]:
    """Các đoạn filter_complex làm mờ 1 vùng: tách luồng làm 2, cắt vùng ở
    bản thứ nhất, làm mờ (gblur), rồi dán lại đúng chỗ cũ trên bản gốc. Chỉ
    đụng tới vùng đã chọn; hiện trong khoảng thời gian [start, start+duration]
    của lớp (duration 0 = đến hết video), cùng quy ước với chữ."""
    x, y, w, h = blur_region_px(layer, out_width, out_height)
    sigma = blur_sigma(layer.get("blur", 60), out_height)
    base, src, blurred = f"[bb{index}]", f"[bs{index}]", f"[bl{index}]"
    start = max(0.0, float(layer.get("start", 0) or 0))
    duration = max(0.0, float(layer.get("duration", 0) or 0))
    enable = ""
    if start > 0 or duration > 0:
        end_expr = f"{start + duration:g}" if duration > 0 else "1e9"
        enable = f":enable='between(t,{start:g},{end_expr})'"
    return [
        f"{in_label}split=2{base}{src}",
        f"{src}crop={w}:{h}:{x}:{y},gblur=sigma={sigma:.2f}:steps=3{blurred}",
        f"{base}{blurred}overlay={x}:{y}{enable}{out_label}",
    ]


_filter_option_cache: dict[tuple[str, str, str], bool] = {}
_option_type_cache: dict[tuple[str, str, str], str] = {}


def ffmpeg_filter_option_type(ffmpeg_path: str, filter_name: str, option: str) -> str:
    """Kiểu dữ liệu (VD "<int>", "<string>") ffmpeg khai báo cho option của
    filter; "" nếu không dò được. Dùng để biết `boxborderw` có nhận danh sách
    4 giá trị (kiểu <string>, ffmpeg mới) hay chỉ 1 số (<int>, ffmpeg cũ)."""
    key = (ffmpeg_path, filter_name, option)
    if key in _option_type_cache:
        return _option_type_cache[key]
    kind = ""
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-h", f"filter={filter_name}"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == option:
                kind = parts[1]
                break
    except (OSError, subprocess.TimeoutExpired):
        kind = ""
    _option_type_cache[key] = kind
    return kind



def ffmpeg_filter_has_option(ffmpeg_path: str, filter_name: str, option: str) -> bool:
    """Filter `filter_name` của bản ffmpeg này có option `option` không (VD:
    `drawtext` có `text_align` từ ffmpeg 6.1)."""
    key = (ffmpeg_path, filter_name, option)
    if key in _filter_option_cache:
        return _filter_option_cache[key]
    ok = False
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-h", f"filter={filter_name}"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        ).stdout
        ok = any(line.split()[:1] == [option] for line in out.splitlines() if line.strip())
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _filter_option_cache[key] = ok
    return ok


def extract_preview_frame(
    ffmpeg_path: str,
    video_path: Path,
    at_seconds: float,
    out_width: int,
    out_height: int,
    out_path: Path,
    aspect_ratio: Optional[tuple[int, int]] = None,
    fit_mode: str = "crop",
) -> None:
    """Trích 1 khung hình PNG tại `at_seconds`, đã đưa về đúng khung
    `out_width`x`out_height` bằng CHÍNH các bước filter khi xuất video (crop /
    viền đen / nền mờ) -> khung xem trước trong trình chỉnh sửa chữ giống
    hệt video xuất ra. Raise MergeError nếu ffmpeg lỗi."""
    if aspect_ratio:
        parts = _fit_filter_parts("[0:v:0]", out_width, out_height, fit_mode, "[vfit]")
    else:
        parts = [f"[0:v:0]scale={out_width}:{out_height}[vfit]"]
    cmd = [
        ffmpeg_path, "-y", "-v", "error", "-ss", f"{max(0.0, at_seconds):.3f}", "-i", str(video_path),
        "-filter_complex", ";".join(parts), "-map", "[vfit]", "-frames:v", "1", str(out_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MergeError(f"Không trích được khung hình xem trước: {exc}") from exc
    if result.returncode != 0 or not out_path.is_file():
        raise MergeError(f"Không trích được khung hình xem trước:\n{(result.stderr or '')[-400:]}")


def build_ffmpeg_command(
    ffmpeg_path: str,
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    video_info: MediaInfo,
    target_short_side: Optional[int] = None,
    crf: Optional[int] = None,
    aspect_ratio: Optional[tuple[int, int]] = None,
    fit_mode: str = "crop",
    audio_mix_percent: int = 0,
    loop_audio: bool = False,
    normalize_loudness: bool = False,
    fade_seconds: float = 0.0,
    text_overlay=None,
    no_upscale: bool = False,
    trim_start: float = 0.0,
    trim_end: float = 0.0,
    bg_music_path: Optional[Path] = None,
    bg_music_volume: int = 20,
    bg_music_loop: bool = True,
    bg_music_ducking: bool = False,
    _duration_cap: Optional[float] = None,
) -> list[str]:
    """Dựng câu lệnh ffmpeg đầy đủ, hỗ trợ mọi tùy chọn nâng cao. VIDEO
    luôn là gốc quyết định ĐỘ DÀI file xuất ra, thay cho `-shortest` trước
    đây.

    `trim_start`/`trim_end`: số giây CẮT BỎ ở đầu/cuối video TRƯỚC khi ghép
    (tính trên video GỐC — chưa trừ nhau): video dùng để ghép chỉ còn đoạn
    từ giây `trim_start` đến `video_info.duration - trim_end`. Audio ghép
    vào vẫn luôn phát từ đầu của chính nó (không bị cắt theo mốc này) rồi
    lặp/đệm cho khớp độ dài video sau khi đã cắt — giữ đúng thiết kế sẵn có
    là audio và video không ràng buộc mốc thời gian với nhau. Raise
    `MergeError` nếu `trim_start + trim_end` bằng hoặc vượt quá độ dài gốc
    (không còn gì để ghép).
    `_duration_cap`: giới hạn thêm độ dài OUTPUT sau khi đã trừ trim — dùng
    nội bộ bởi `build_preview_command`, không dùng trực tiếp.

    `target_short_side`: độ phân giải theo CẠNH NGẮN (720, 1080, 1440 = 2K,
    2160 = 4K) — xem `compute_output_size`.
    `text_overlay`: 1 dict hoặc 1 DANH SÁCH dict (nhiều lớp). Mỗi lớp là
    CHỮ (`kind` = "text", xem `build_drawtext_filter`) hoặc VÙNG BLUR không
    chữ (`kind` = "blur", xem `build_blur_filters`). Mọi lớp blur luôn được
    áp TRƯỚC, rồi mới vẽ chữ lên trên — để chữ mới đè lên vùng đã làm mờ
    (VD: che phụ đề cũ rồi viết phụ đề mới) mà không bị mờ theo.

    `bg_music_path`: 1 file nhạc nền được TRỘN THÊM (không thay thế) vào
    audio chính (audio gốc video + audio ghép, theo đúng tỉ lệ đã trộn ở
    trên) — luôn phát từ ĐẦU bài nhạc, lặp lại nếu `bg_music_loop=True`
    (giống hệt cách audio chính lặp), cắt ngắn nếu dài hơn video. Dùng
    chung `fade_seconds` với audio chính (fade áp cho TOÀN BỘ audio đã
    trộn — kể cả nhạc nền — ở đầu/cuối output). `bg_music_volume`: 0-100%.
    `bg_music_ducking=True`: tự động hạ nhỏ âm lượng nhạc nền mỗi khi audio
    chính (gốc video + audio ghép) đang có tiếng, để giọng luôn nghe rõ —
    dùng `sidechaincompress` của ffmpeg."""
    trim_start = max(0.0, float(trim_start or 0))
    trim_end = max(0.0, float(trim_end or 0))
    effective_duration = video_info.duration - trim_start - trim_end
    if effective_duration < 0.2:
        raise MergeError(
            f"Video chỉ dài {video_info.duration:.1f}s, không đủ để cắt "
            f"{trim_start:.1f}s đầu + {trim_end:.1f}s cuối (còn lại phải ít "
            "nhất 0.2s). Hãy giảm số giây cắt."
        )
    if _duration_cap is not None:
        effective_duration = min(effective_duration, _duration_cap)
    needs_trim = trim_start > 0 or trim_end > 0

    layers = _normalize_text_layers(text_overlay)
    out_size = compute_output_size(
        aspect_ratio, target_short_side, video_info.width, video_info.height, no_upscale
    )
    needs_video_filter = bool(out_size) or bool(layers)
    needs_audio_filter = (
        (audio_mix_percent > 0 and video_info.has_audio)
        or normalize_loudness
        or fade_seconds > 0
        # Khi KHÔNG lặp audio, luôn phải qua filter để `apad` đệm im lặng
        # cho phần audio ngắn hơn video (nếu không, luồng audio xuất ra
        # sẽ bị ngắn hơn video — đã kiểm chứng bằng ffmpeg thật).
        or not loop_audio
        or bool(bg_music_path)
    )

    cmd = [ffmpeg_path, "-y"]
    if trim_start > 0:
        # "-ss" đặt TRƯỚC "-i" của video: chỉ cắt riêng input video (audio
        # ghép vẫn phát từ đầu của nó). Kết hợp với việc ép encode lại bên
        # dưới (không dùng "-c:v copy" khi đang cắt) để cắt CHÍNH XÁC tới
        # từng khung hình — đã kiểm chứng bằng ffmpeg thật, không chỉ snap
        # về từ khóa gần nhất.
        cmd += ["-ss", f"{trim_start:.3f}"]
    cmd += ["-i", str(video_path)]

    # Input audio — nếu bật lặp, dùng -stream_loop -1 NGAY TRƯỚC -i của nó
    if loop_audio:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(audio_path)]

    next_input_idx = 2

    bg_music_input_idx = None
    if bg_music_path:
        # Nhạc nền luôn phát từ ĐẦU bài (không có mốc đồng bộ nào với video
        # để căn theo), nên "-stream_loop -1" trước "-i" của nó là đủ, giống
        # hệt cách audio chính lặp lại.
        if bg_music_loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(bg_music_path)]
        bg_music_input_idx = next_input_idx
        next_input_idx += 1

    filter_complex_parts: list[str] = []
    video_out_label = "0:v:0"  # mặc định: dùng thẳng luồng gốc, không qua filter
    audio_out_label = "1:a:0"

    if needs_video_filter:
        cur = "[0:v:0]"
        if out_size:
            ow, oh = out_size
            if aspect_ratio:
                filter_complex_parts += _fit_filter_parts(cur, ow, oh, fit_mode, "[vfit]")
            else:
                filter_complex_parts.append(f"{cur}scale={ow}:{oh}[vfit]")
            cur = "[vfit]"

        if layers:
            # Blur áp trước, chữ vẽ sau cùng (nằm trên blur); trong mỗi nhóm,
            # lớp sau đè lên lớp trước — giống thứ tự lớp trong CapCut.
            out_w, out_h = out_size if out_size else (video_info.width, video_info.height)
            ordered = order_overlay_layers(layers)
            for i, layer in enumerate(ordered):
                label = "[vout]" if i == len(ordered) - 1 else f"[vt{i}]"
                if is_blur_layer(layer):
                    filter_complex_parts += build_blur_filters(layer, cur, label, i, out_w, out_h)
                else:
                    overlay = prepare_text_overlay(layer, out_w, out_h)
                    filter_complex_parts.append(
                        f"{cur}{build_drawtext_filter(overlay, out_w, out_h)}{label}"
                    )
                cur = label
        else:
            # đổi tên label cuối thành [vout] cho thống nhất
            last = filter_complex_parts[-1]
            filter_complex_parts[-1] = last[: last.rfind("[")] + "[vout]"
        video_out_label = "[vout]"

    if needs_audio_filter:
        cur = "[1:a:0]"
        if audio_mix_percent > 0 and video_info.has_audio:
            orig_w = max(0, min(100, audio_mix_percent)) / 100
            new_w = 1 - orig_w
            filter_complex_parts.append(f"[0:a:0]volume={orig_w}[aorig]")
            filter_complex_parts.append(f"{cur}volume={new_w}[anew]")
            filter_complex_parts.append(
                "[aorig][anew]amix=inputs=2:duration=longest:dropout_transition=0[amix]"
            )
            cur = "[amix]"

        if not loop_audio:
            # Đệm im lặng cho audio chính để nó DÀI BẰNG video (video là gốc,
            # xem `-t` bên dưới). PHẢI làm TRƯỚC khi trộn nhạc nền: các bước
            # amix bên dưới dùng `duration=first` (lấy theo audio chính), nếu
            # audio chính ngắn hơn video mà chưa được đệm thì amix sẽ kết thúc
            # sớm và CẢ nhạc nền (dù đã bật lặp) cũng bị cắt theo -> phần sau
            # của video bị im lặng hoàn toàn.
            filter_complex_parts.append(f"{cur}apad[apadded]")
            cur = "[apadded]"

        if bg_music_path:
            bg_vol = max(0, min(100, bg_music_volume)) / 100
            filter_complex_parts.append(f"[{bg_music_input_idx}:a:0]volume={bg_vol}[bgvol]")
            if bg_music_ducking:
                # Tách audio chính làm 2 bản giống hệt: 1 bản để mix vào kết
                # quả cuối, 1 bản chỉ dùng làm TÍN HIỆU KÍCH để sidechaincompress
                # tự hạ nhỏ nhạc nền mỗi khi audio chính đang có tiếng.
                filter_complex_parts.append(f"{cur}asplit=2[mainmix][mainsc]")
                filter_complex_parts.append(
                    "[bgvol][mainsc]sidechaincompress="
                    "threshold=0.04:ratio=8:attack=5:release=400:makeup=1[bgducked]"
                )
                filter_complex_parts.append(
                    "[mainmix][bgducked]amix=inputs=2:duration=first:dropout_transition=0[withbg]"
                )
            else:
                filter_complex_parts.append(
                    f"{cur}[bgvol]amix=inputs=2:duration=first:dropout_transition=0[withbg]"
                )
            cur = "[withbg]"

        if normalize_loudness:
            filter_complex_parts.append(f"{cur}loudnorm=I=-16:TP=-1.5:LRA=11[anorm]")
            cur = "[anorm]"

        if fade_seconds > 0:
            fade_out_start = max(effective_duration - fade_seconds, 0)
            filter_complex_parts.append(
                f"{cur}afade=t=in:st=0:d={fade_seconds},"
                f"afade=t=out:st={fade_out_start}:d={fade_seconds}[afaded]"
            )
            cur = "[afaded]"

        last = filter_complex_parts[-1]
        # đảm bảo nhãn cuối của nhánh audio là [aout] (không đụng nhánh video)
        if cur != "[aout]":
            filter_complex_parts[-1] = last[: last.rfind("[")] + "[aout]"
        audio_out_label = "[aout]"

    if filter_complex_parts:
        cmd += ["-filter_complex", ";".join(filter_complex_parts)]

    cmd += ["-map", video_out_label, "-map", audio_out_label]

    if needs_video_filter or needs_trim:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf if crf is not None else 23)]
    elif crf is not None:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-t", f"{effective_duration:.3f}"]
    cmd += [str(output_path)]
    return cmd


# ============================================================================
# Chạy ffmpeg (có thể dừng giữa chừng)
# ============================================================================

def unique_output_path(output_dir: Path, stem: str, ext: str) -> Path:
    candidate = output_dir / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    n = 1
    while True:
        candidate = output_dir / f"{stem} ({n}){ext}"
        if not candidate.exists():
            return candidate
        n += 1


def assign_unique_output_paths(output_dir: Path, base_stems: list[str], ext: str) -> list[Path]:
    """Cấp sẵn đường dẫn file xuất cho cả danh sách, theo đúng thứ tự.

    - Tên mặc định = tên gốc (base_stem), ví dụ "video.mp4".
    - Nếu trùng (với file đã có trong thư mục xuất, hoặc với dòng khác trong
      cùng lượt ghép) thì thêm số thứ tự: "video (1).mp4", "video (2).mp4"...
    - Cấp tên TUẦN TỰ ở luồng chính trước khi chạy song song, nên các tiến
      trình ffmpeg không bao giờ tranh nhau cùng một tên file.
    - So sánh không phân biệt hoa/thường (Windows/macOS coi "A.mp4" = "a.mp4").
    """
    used: set[str] = set()
    result: list[Path] = []
    for stem in base_stems:
        n = 0
        while True:
            name = stem if n == 0 else f"{stem} ({n})"
            candidate = output_dir / f"{name}{ext}"
            key = candidate.name.lower()
            if key not in used and not candidate.exists():
                break
            n += 1
        used.add(key)
        result.append(candidate)
    return result


def run_ffmpeg_merge(
    cmd: list[str],
    output_path: Path,
    stop_flag: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    poll_interval: float = 0.2,
) -> None:
    """Chạy ffmpeg theo `cmd`. Dừng NGAY khi `stop_flag()` trả True (kể
    cả đang chạy dở), xóa file output dở dang, raise `MergeCancelled`.
    Raise `MergeError` nếu ffmpeg thoát mã lỗi khác 0."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise MergeError(f"Không chạy được ffmpeg: {exc}") from exc

    stderr_lines: list[str] = []

    def reader():
        if proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.rstrip("\n")
            if line:
                stderr_lines.append(line)
                if on_log:
                    on_log(line)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    cancelled = False
    while True:
        try:
            proc.wait(timeout=poll_interval)
            break
        except subprocess.TimeoutExpired:
            if stop_flag and stop_flag():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

    reader_thread.join(timeout=2)

    if cancelled:
        output_path.unlink(missing_ok=True)
        raise MergeCancelled("Đã dừng theo yêu cầu người dùng giữa chừng.")

    if proc.returncode != 0:
        output_path.unlink(missing_ok=True)
        tail = "\n".join(stderr_lines[-15:])
        raise MergeError(f"ffmpeg lỗi (mã {proc.returncode}):\n{tail}")


# ============================================================================
# Xem thử nhanh (preview)
# ============================================================================

def build_preview_command(
    ffmpeg_path: str,
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    video_info: MediaInfo,
    preview_seconds: float = 5.0,
    **kwargs,
) -> list[str]:
    """Giống `build_ffmpeg_command` nhưng CHỈ xuất tối đa `preview_seconds`
    giây — dùng để xem thử nhanh trước khi chạy toàn bộ danh sách. Nếu
    `kwargs` có `trim_start`/`trim_end`, bản xem thử vẫn tôn trọng đúng
    đoạn cắt đó (xem trước bắt đầu từ ĐÚNG điểm sẽ cắt trong video thật,
    không phải từ giây 0 của video gốc)."""
    return build_ffmpeg_command(
        ffmpeg_path, video_path, audio_path, output_path, video_info,
        _duration_cap=preview_seconds, **kwargs,
    )


# ============================================================================
# Thư mục tạm cho bản xem thử (KHÔNG lưu vào thư mục xuất của người dùng)
# ============================================================================

PREVIEW_DIR_PREFIX = "douyin_merge_preview_"


def create_preview_dir() -> Path:
    """Tạo 1 thư mục tạm RIÊNG của phiên làm việc hiện tại (nằm trong thư mục
    temp của hệ điều hành) để chứa các bản xem thử. Thư mục này được xóa khi
    thoát app; file xem thử KHÔNG BAO GIỜ nằm trong thư mục xuất."""
    return Path(tempfile.mkdtemp(prefix=PREVIEW_DIR_PREFIX))


def cleanup_stale_preview_dirs(max_age_seconds: float = 6 * 3600) -> None:
    """Dọn các thư mục xem thử còn sót lại từ những lần chạy trước (app bị
    tắt đột ngột / trình phát còn giữ file nên lần trước xóa không được).
    Chỉ đụng tới thư mục cũ hơn `max_age_seconds` để không xóa nhầm bản xem
    thử của 1 cửa sổ app khác đang chạy cùng lúc."""
    now = time.time()
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(f"{PREVIEW_DIR_PREFIX}*"))
    except OSError:
        return
    for d in candidates:
        try:
            if d.is_dir() and now - d.stat().st_mtime > max_age_seconds:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue