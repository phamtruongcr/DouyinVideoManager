"""
review_script.py
================
Giai đoạn 1 của tính năng "Review + giọng đọc": từ 1 video đã tải, nhờ Gemini
viết KỊCH BẢN REVIEW — CHỈ là lời đọc (văn bản thuần), để các giai đoạn sau
đưa vào TTS tiếng Việt rồi ghép audio vào video.

Luồng:
  1. `prepare_video_for_gemini`  : video nhỏ (<= ~14 MB, .mp4) dùng nguyên; video
                                   lớn hơn thì nén nhỏ bằng ffmpeg (Gemini giới
                                   hạn request 20 MB sau khi mã hoá base64).
  2. `build_system_instruction` : system instruction (2 chế độ: KOC review đời
                                   thường theo `KocBrief`, hoặc kiểu chung) chặt (không tiêu đề,
                                   markdown, emoji, hashtag, ghi chú, mốc thời
                                   gian; số viết thành chữ; tối đa N từ).
  3. `generate_review_script`   : gọi Gemini (dùng lại retry/fallback model của
                                   gemini_client) rồi chạy `sanitize_script`.
  4. `sanitize_script`          : lớp lọc cuối — KHÔNG tin hoàn toàn vào model:
                                   bỏ phần thừa còn sót, đổi số thành chữ,
                                   cắt theo câu nếu vượt số từ tối đa.

Mọi lỗi đều là RuntimeError (ReviewScriptError hoặc lỗi Gemini*) để giao diện
bắt và báo rõ. Không bao giờ log API key hay nội dung video (base64).
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .app_logger import get_logger
from .config import (
    DEFAULT_REVIEW_WORDS_PER_SECOND,
    MAX_REVIEW_WORDS_PER_SECOND,
    MIN_REVIEW_WORDS_PER_SECOND,
)
from .gemini_client import generate_content

logger = get_logger("review_script")

# Gemini giới hạn tổng request ~20 MB; base64 phình ~33% -> video thô tối đa ~14 MB.
MAX_INLINE_VIDEO_BYTES = 14 * 1024 * 1024
# Gửi video lớn lên mạng chậm cần timeout dài hơn REQUEST_TIMEOUT mặc định (15s).
REVIEW_REQUEST_TIMEOUT = 180
DEFAULT_MAX_WORDS = 120
# (chiều cao, crf) thử lần lượt khi nén video cho tới khi đủ nhỏ.
_COMPRESS_STEPS = ((480, 30), (360, 34), (240, 36))
_FFMPEG_TIMEOUT_S = 600


class ReviewScriptError(RuntimeError):
    """Lỗi khi chuẩn bị video / viết kịch bản (thông báo đã là tiếng Việt, hiển thị thẳng cho người dùng)."""


@dataclass
class ReviewScriptResult:
    text: str          # lời đọc đã lọc, sẵn sàng đưa vào TTS
    raw_text: str      # nguyên văn Gemini trả về (để người dùng đối chiếu/gỡ lỗi)
    word_count: int
    truncated: bool    # True nếu phải cắt bớt cho vừa số từ tối đa


# ============================================================================
# Gợi ý số từ theo độ dài video
# ============================================================================

def suggest_max_words(
    duration_seconds: float, words_per_second: Optional[float] = None
) -> int:
    """Số từ tối đa hợp lý để đọc vừa độ dài video (audio dài hơn video sẽ bị
    cắt khi ghép). `words_per_second` lấy từ cấu hình (mặc định
    `config.DEFAULT_REVIEW_WORDS_PER_SECOND` = 3). Kẹp trong khoảng 20..400 từ."""
    wps = words_per_second if words_per_second else DEFAULT_REVIEW_WORDS_PER_SECOND
    try:
        words = int(float(duration_seconds) * float(wps))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORDS
    return max(20, min(400, words))


# ============================================================================
# Ước lượng số từ theo thời lượng (có tính tốc độ đọc + ngắt nghỉ)
# ============================================================================

# Trung bình 1 câu ~10 tiếng, 1 dấu phẩy ~7 tiếng — dùng để ước lượng số lần ngắt
# nghỉ khi CHƯA có văn bản (lúc tính số từ mục tiêu để đưa vào prompt).
_WORDS_PER_SENTENCE = 10.0
_WORDS_PER_COMMA = 7.0
WORDS_TOLERANCE_LOW = 0.90    # ít nhất 90% số từ mục tiêu
WORDS_TOLERANCE_HIGH = 1.08   # nhiều nhất 108% số từ mục tiêu


def estimate_target_words(
    duration_seconds: float,
    words_per_second: Optional[float] = None,
    speed: float = 1.0,
    pause_sentence: float = 0.0,
    pause_comma: float = 0.0,
) -> int:
    """Số từ (tiếng) cần viết để đọc xong ĐÚNG `duration_seconds`.

    Thời gian đọc = (số_từ / từ_mỗi_giây + thời gian ngắt nghỉ) / tốc_độ — ngắt nghỉ được chèn
    TRƯỚC khi đổi tốc độ nên cũng bị nhanh/chậm theo. Ngắt nghỉ tỉ lệ với số từ (trung bình
    1 câu ~10 từ, 1 dấu phẩy ~7 từ) nên giải ra:
        số_từ = thời_lượng × tốc_độ / (1/wps + ngắt_câu/10 + ngắt_phẩy/7)
    Kẹp trong 10..400 từ."""
    wps = words_per_second if words_per_second else DEFAULT_REVIEW_WORDS_PER_SECOND
    try:
        dur = float(duration_seconds)
        per_word = (1.0 / float(wps)
                    + max(0.0, float(pause_sentence)) / _WORDS_PER_SENTENCE
                    + max(0.0, float(pause_comma)) / _WORDS_PER_COMMA)
        words = int(round(dur * max(0.1, float(speed)) / per_word))
    except (TypeError, ValueError, ZeroDivisionError):
        return DEFAULT_MAX_WORDS
    return max(10, min(400, words))


def _count_pauses(text: str) -> tuple[int, int]:
    """(số chỗ ngắt sau câu, số chỗ ngắt sau dấu phẩy) trong văn bản; câu cuối không ngắt."""
    sentences = max(0, len(re.findall(r"[.!?…]+(?:\s|$)", text)) - 1)
    commas = len(re.findall(r"[,;:](?:\s|$)", text))
    return sentences, commas


def estimate_read_seconds(
    text: str,
    words_per_second: Optional[float] = None,
    speed: float = 1.0,
    pause_sentence: float = 0.0,
    pause_comma: float = 0.0,
) -> float:
    """Ước lượng số giây đọc xong `text` (đếm đúng số câu/dấu phẩy có trong văn bản)."""
    wps = words_per_second if words_per_second else DEFAULT_REVIEW_WORDS_PER_SECOND
    text = text or ""
    words = count_words(text)
    if not words:
        return 0.0
    sentences, commas = _count_pauses(text)
    return (words / float(wps) + sentences * max(0.0, pause_sentence)
            + commas * max(0.0, pause_comma)) / max(0.1, float(speed))


def measure_words_per_second(
    text: str, actual_seconds: float, speed_used: float = 1.0,
    pause_sentence: float = 0.0, pause_comma: float = 0.0,
) -> Optional[float]:
    """Đo tốc độ đọc THỰC TẾ (từ/giây ở tốc độ 1.0, không tính ngắt nghỉ) từ 1 lần đọc đã xong —
    nghịch đảo của estimate_read_seconds. Mẫu quá ngắn/bất thường -> None (không dùng để hiệu chỉnh)."""
    words = count_words(text)
    try:
        actual, speed = float(actual_seconds), max(0.1, float(speed_used))
    except (TypeError, ValueError):
        return None
    if words < 15 or actual < 3:
        return None
    sentences, commas = _count_pauses(text)
    speech = actual * speed - sentences * max(0.0, pause_sentence) - commas * max(0.0, pause_comma)
    if speech <= 1.0:
        return None
    wps = words / speech
    if not (MIN_REVIEW_WORDS_PER_SECOND <= wps <= MAX_REVIEW_WORDS_PER_SECOND):
        return None
    return wps


def blend_words_per_second(old: float, samples: int, measured: float) -> float:
    """Trộn số đo mới vào giá trị hiện có (trung bình dần, tối đa 4 mẫu cũ). Chưa có mẫu nào -> dùng số đo."""
    n = max(0, min(int(samples), 4))
    if n == 0:
        return measured
    return (old * n + measured) / (n + 1)


@dataclass
class KocBrief:
    """Thông tin đầu vào cho prompt KOC/Reviewer (thay các ô [Sản phẩm], [Thời lượng mục tiêu]...)."""
    product: str = ""
    target_seconds: float = 15
    target_words: int = 40
    min_words: int = 36
    max_words: int = 43
    address_terms: str = ""
    slang: str = ""

    @classmethod
    def from_duration(
        cls, product: str, target_seconds: float, *, address_terms: str = "", slang: str = "",
        words_per_second: Optional[float] = None, speed: float = 1.0,
        pause_sentence: float = 0.0, pause_comma: float = 0.0,
    ) -> "KocBrief":
        target = estimate_target_words(
            target_seconds, words_per_second, speed, pause_sentence, pause_comma
        )
        return cls(
            product=(product or "").strip(), target_seconds=target_seconds,
            target_words=target,
            min_words=max(5, int(round(target * WORDS_TOLERANCE_LOW))),
            max_words=max(6, int(round(target * WORDS_TOLERANCE_HIGH))),
            address_terms=(address_terms or "").strip(), slang=(slang or "").strip(),
        )


# ============================================================================
# System instruction
# ============================================================================

_SYSTEM_TEMPLATE = """Bạn là người viết lời bình (voice-over) review video ngắn bằng tiếng Việt.
Bạn sẽ xem một video và viết DUY NHẤT phần lời để người dẫn đọc thành tiếng.

QUY TẮC BẮT BUỘC:
1. Chỉ trả về lời đọc thuần văn bản, giọng tự nhiên, trôi chảy khi đọc thành tiếng.
2. KHÔNG tiêu đề. KHÔNG markdown (không **, #, gạch đầu dòng, đánh số). KHÔNG emoji. KHÔNG hashtag.
3. KHÔNG ghi chú hay chỉ dẫn trong ngoặc (ví dụ (cười), [nhạc nền], [cảnh 1]). KHÔNG mốc thời gian. KHÔNG nhãn như "Lời đọc:" hay "Kịch bản:".
4. Viết mọi con số thành chữ (ví dụ 25% thành hai mươi lăm phần trăm, 2024 thành hai nghìn không trăm hai mươi tư).
5. Tối đa {max_words} từ. Ngắn hơn cũng được.
6. Chỉ dựa trên những gì thực sự thấy hoặc nghe được trong video. KHÔNG bịa chi tiết (giá, thương hiệu, thông số, địa điểm, tên người...) nếu video không thể hiện rõ; không chắc thì nói chung chung hoặc bỏ qua.
7. Câu đầu phải cuốn hút ngay, phần kết gọn gàng. Không chào hỏi dài dòng, không kêu gọi theo dõi/thả tim.
8. Không nhắc rằng bạn là AI, không giải thích cách làm, không xin lỗi, không thêm lời dẫn trước hoặc sau phần lời đọc."""


_KOC_TEMPLATE = """Bạn là một KOC/Reviewer chuyên nghiệp, chuyên làm video review đồ gia dụng, đồ tiện ích trên TikTok và Facebook Reels.
Bạn sẽ xem một video (hoặc đọc tính năng sản phẩm người dùng cung cấp) rồi dịch và viết lại thành DUY NHẤT phần lời lồng tiếng review để người dẫn đọc thành tiếng.

THÔNG TIN ĐẦU VÀO:
- Sản phẩm: {product}
- Thời lượng mục tiêu: {target_seconds} giây. Viết khoảng {target_words} từ (trong khoảng từ {min_words} đến {max_words} từ; mỗi tiếng ngăn cách bằng dấu cách tính là một từ). Đây là con số đã tính theo tốc độ đọc bình thường để khi đọc ra khớp đúng thời lượng, vì vậy không viết dài hơn hay ngắn hơn đáng kể.

PHONG CÁCH VÀ VĂN PHONG (cực kỳ quan trọng):
- Tone giọng: gần gũi, nhiệt tình, đậm chất đời thường, dùng ngôn ngữ nói tự nhiên của người Việt Nam, câu ngắn, dễ đọc thành tiếng. Không văn vở, không quảng cáo sáo rỗng.
- Cách xưng hô: dùng các từ như {address_terms}.
- Từ lóng/từ khóa review: ưu tiên chèn tự nhiên 2 đến 3 cụm nhấn mạnh độ tiện lợi, chọn trong: {slang}. Không nhồi nhét, không lặp một cụm hai lần.
- Nếu video có lời nói hoặc chữ tiếng Trung/ngoại ngữ, hãy hiểu ý rồi viết lại bằng văn nói tiếng Việt, tuyệt đối không dịch từng chữ.

CẤU TRÚC:
1. Mở đầu (Hook): đi thẳng vào nỗi đau hoặc nhu cầu, ví dụ "Bác nào ngán cảnh...", "Góc bếp hẹp đến mấy cũng...". Không chào hỏi dài dòng.
2. Thân bài: chỉ tóm tắt 2 đến 3 tính năng nổi bật nhất và sự tiện lợi khi sử dụng. Không liệt kê thông số dài dòng.
3. Kết luận: chốt lại lợi ích (sạch sẽ, an toàn, nhàn hạ...) và kêu gọi hành động (mua sắm/chốt đơn).

{common_rules}"""

_PLAIN_TEMPLATE = """Bạn là người viết lời bình (voice-over) review video ngắn bằng tiếng Việt.
Bạn sẽ xem một video và viết DUY NHẤT phần lời để người dẫn đọc thành tiếng.

{common_rules}
9. Tối đa {max_words} từ. Ngắn hơn cũng được.
10. Câu đầu phải cuốn hút ngay, phần kết gọn gàng. Không chào hỏi dài dòng, không kêu gọi theo dõi/thả tim."""

# Quy tắc kỹ thuật dùng chung (đánh số 1-4, 6, 8 — bản "plain" bổ sung thêm 9 và 10).
_COMMON_RULES = """QUY TẮC BẮT BUỘC:
1. Chỉ trả về lời đọc thuần văn bản, giọng tự nhiên, trôi chảy khi đọc thành tiếng, viết liền thành một đoạn.
2. KHÔNG tiêu đề. KHÔNG markdown (không **, #, gạch đầu dòng, đánh số). KHÔNG emoji. KHÔNG hashtag.
3. KHÔNG ghi chú hay chỉ dẫn trong ngoặc (ví dụ (cười), [nhạc nền], [cảnh 1]). KHÔNG mốc thời gian. KHÔNG nhãn như "Lời đọc:" hay "Kịch bản:".
4. Viết mọi con số thành chữ (ví dụ 25% thành hai mươi lăm phần trăm, 2024 thành hai nghìn không trăm hai mươi tư).
6. Chỉ dựa trên những gì thực sự thấy hoặc nghe được trong video (và thông tin người dùng cung cấp). KHÔNG bịa chi tiết (giá, thương hiệu, thông số, địa điểm, tên người...) nếu không thể hiện rõ; không chắc thì nói chung chung hoặc bỏ qua.
8. Không nhắc rằng bạn là AI, không giải thích cách làm, không xin lỗi, không thêm lời dẫn trước hoặc sau phần lời đọc."""

# Tương thích tên cũ (nếu nơi khác còn import).
_SYSTEM_TEMPLATE = _PLAIN_TEMPLATE.replace("{common_rules}", _COMMON_RULES)


def build_system_instruction(
    max_words: int = DEFAULT_MAX_WORDS, koc: Optional[KocBrief] = None,
) -> str:
    """Dựng system instruction. Có `koc` -> prompt KOC/Reviewer (Hook - Thân - Kết, xưng
    hô, từ khóa, số từ theo thời lượng); không có -> prompt chung. Dùng replace() thay vì
    format() để các dấu { } khác (nếu có) không gây lỗi."""
    try:
        n = max(10, int(max_words))
    except (TypeError, ValueError):
        n = DEFAULT_MAX_WORDS
    if koc is None:
        text = _PLAIN_TEMPLATE
        values = {"common_rules": _COMMON_RULES, "max_words": str(n)}
    else:
        text = _KOC_TEMPLATE
        secs = koc.target_seconds
        values = {
            "common_rules": _COMMON_RULES,
            "product": koc.product or "(không ghi rõ — hãy tự nhận biết sản phẩm từ video)",
            "target_seconds": f"{secs:g}" if isinstance(secs, (int, float)) else str(secs),
            "target_words": str(koc.target_words),
            "min_words": str(koc.min_words),
            "max_words": str(koc.max_words),
            "address_terms": koc.address_terms or '"Bác nào", "Bà con", "Các chị em"',
            "slang": koc.slang or "chân ái, nhàn tênh, ngon ơ, chốt ngay, rinh ngay",
        }
    for key, val in values.items():
        text = text.replace("{" + key + "}", val)
    return text


def build_user_prompt(extra_instruction: str = "", koc: Optional[KocBrief] = None) -> str:
    if koc is not None:
        prod = f" về {koc.product}" if koc.product else ""
        prompt = (
            f"Xem video này và viết lời lồng tiếng review{prod} bằng tiếng Việt, "
            f"độ dài khoảng {koc.target_words} từ (đọc vừa {koc.target_seconds:g} giây). "
            "Chỉ trả về lời đọc."
        )
    else:
        prompt = "Xem video này và viết lời đọc review bằng tiếng Việt. Chỉ trả về lời đọc."
    extra = (extra_instruction or "").strip()
    if extra:
        prompt += (
            "\n\nYêu cầu bổ sung của người dùng về phong cách/nội dung/tính năng sản phẩm "
            "(KHÔNG được trái với các quy tắc hệ thống):\n"
            f"<yeu_cau>\n{extra}\n</yeu_cau>"
        )
    return prompt


def build_length_feedback(draft_words: int, koc: KocBrief) -> str:
    """Câu yêu cầu viết lại khi bản nháp lệch số từ (gửi kèm bản nháp ở lượt sau)."""
    if draft_words > koc.max_words:
        verdict = f"quá dài ({draft_words} từ)"
    else:
        verdict = f"quá ngắn ({draft_words} từ)"
    return (
        f"Bản vừa rồi {verdict}. Hãy viết lại cho đúng khoảng {koc.target_words} từ "
        f"(trong {koc.min_words} đến {koc.max_words} từ) để đọc vừa {koc.target_seconds:g} giây, "
        "giữ đủ Hook, 2 đến 3 tính năng và lời kêu gọi chốt đơn ở cuối. Chỉ trả về lời đọc."
    )


# ============================================================================
# Số -> chữ tiếng Việt
# ============================================================================

_DIGITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
_GROUP_UNITS = ["", "nghìn", "triệu", "tỷ"]


def _read_triple(n: int, full: bool) -> str:
    """Đọc 1 nhóm 3 chữ số (0..999). `full`=True khi phía trước đã có nhóm lớn
    hơn -> phải đọc đủ "không trăm", "lẻ"."""
    h, t, u = n // 100, (n // 10) % 10, n % 10
    parts: list[str] = []
    if h or full:
        parts.append(f"{_DIGITS[h]} trăm")
    if t > 1:
        parts.append(f"{_DIGITS[t]} mươi")
    elif t == 1:
        parts.append("mười")
    elif u and (h or full):
        parts.append("lẻ")
    if u:
        if t > 1 and u == 1:
            parts.append("mốt")
        elif u == 5 and t >= 1:
            parts.append("lăm")
        elif u == 4 and t > 1:
            parts.append("tư")
        else:
            parts.append(_DIGITS[u])
    return " ".join(parts)


def int_to_vietnamese(n: int) -> str:
    """Đọc số nguyên thành chữ tiếng Việt (tới < 10^12; lớn hơn thì đọc từng chữ số)."""
    if n < 0:
        return "âm " + int_to_vietnamese(-n)
    if n == 0:
        return "không"
    if n >= 10 ** 12:
        return " ".join(_DIGITS[int(c)] for c in str(n))
    groups: list[int] = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    out: list[str] = []
    started = False
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        text = _read_triple(g, full=started)
        out.append(text + (f" {_GROUP_UNITS[i]}" if _GROUP_UNITS[i] else ""))
        started = True
    return " ".join(out)


def _spell_digits(s: str) -> str:
    return " ".join(_DIGITS[int(c)] for c in s if c.isdigit())


def _read_number_token(token: str) -> str:
    """Đọc 1 cụm số (có thể kèm dấu . , ngăn cách) thành chữ."""
    # Số điện thoại / mã dài bắt đầu bằng 0: đọc từng chữ số
    if token.isdigit() and token.startswith("0") and len(token) >= 4:
        return _spell_digits(token)
    if token.isdigit():
        return int_to_vietnamese(int(token))
    # 1.000.000 hoặc 1,000,000 (dấu ngăn cách hàng nghìn)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):
        return int_to_vietnamese(int(re.sub(r"[.,]", "", token)))
    # 3,5 hoặc 3.5 (thập phân)
    m = re.fullmatch(r"(\d+)[.,](\d+)", token)
    if m:
        return f"{int_to_vietnamese(int(m.group(1)))} phẩy {_spell_digits(m.group(2))}"
    # Trường hợp lạ: đọc từng phần cách nhau bởi dấu
    return " ".join(
        int_to_vietnamese(int(p)) for p in re.split(r"[.,]", token) if p.isdigit()
    )


_DATE_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})(?![\d/])")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])(\d+(?:[.,]\d+)*)(\s*%)?")


def numbers_to_vietnamese(text: str) -> str:
    """Đổi mọi con số trong `text` thành chữ (kể cả %, ngày dd/mm/yyyy -> "mười hai tháng mười năm ..."). Số dính
    liền chữ cái (iPhone15, H264...) được giữ nguyên vì thường là tên/mã."""
    text = _DATE_RE.sub(
        lambda m: (
            f"{int_to_vietnamese(int(m.group(1)))} "
            f"tháng {int_to_vietnamese(int(m.group(2)))} "
            f"năm {int_to_vietnamese(int(m.group(3)))}"
        ),
        text,
    )

    def repl(m: re.Match) -> str:
        words = _read_number_token(m.group(1))
        return f"{words} phần trăm" if m.group(2) else words

    return _NUMBER_RE.sub(repl, text)


# ============================================================================
# Lọc kết quả
# ============================================================================

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2B00-\u2BFF\u2300-\u23FF\u200d\ufe0f\u20e3]"
)
_BRACKET_RES = [
    re.compile(r"\([^()]*\)"),
    re.compile(r"（[^（）]*）"),
    re.compile(r"\[[^\[\]]*\]"),
    re.compile(r"【[^【】]*】"),
    re.compile(r"\{[^{}]*\}"),
    re.compile(r"<[^<>]*>"),
]
_TIMESTAMP_RE = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–—~]\s*\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)"
)
_HASHTAG_RE = re.compile(r"#\S+")
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+.*$")
_SEPARATOR_LINE_RE = re.compile(r"^\s*(?:[-=_*~]\s*){3,}$")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*•·+]\s+|\d{1,2}[.)]\s+)")
_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:lời đọc|lời bình|lời dẫn|kịch bản|voice ?over|narration|narrator|"
    r"người đọc|người dẫn)\s*[:：]\s*",
    re.IGNORECASE,
)
_PREAMBLE_LINE_RE = re.compile(
    r"^\s*(?:dưới đây|đây là|sau đây|kịch bản|lời đọc|lời bình|voice ?over|"
    r"narration|script|review)\b[^\n]{0,80}[:：]\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def count_words(text: str) -> int:
    return len((text or "").split())


def _truncate_to_words(text: str, max_words: int) -> tuple[str, bool]:
    """Cắt `text` còn tối đa `max_words` từ, ưu tiên cắt ở ranh giới câu."""
    if count_words(text) <= max_words:
        return text, False
    kept: list[str] = []
    total = 0
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        n = count_words(sentence)
        if total + n > max_words:
            break
        kept.append(sentence)
        total += n
    if kept:
        return " ".join(kept).strip(), True
    # Câu đầu tiên đã dài hơn giới hạn -> cắt cứng theo từ
    cut = " ".join(text.split()[:max_words]).rstrip(",;:- ")
    return (cut if cut.endswith((".", "!", "?", "…")) else cut + "."), True


def sanitize_script(text: str, max_words: int = DEFAULT_MAX_WORDS) -> tuple[str, bool]:
    """Lọc kết quả của Gemini thành lời đọc thuần. Trả về (text, đã_bị_cắt)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("```", "\n")

    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw
        if _HEADING_LINE_RE.match(line) or _SEPARATOR_LINE_RE.match(line):
            continue  # tiêu đề markdown / đường kẻ -> bỏ cả dòng
        line = _LIST_PREFIX_RE.sub("", line)
        line = re.sub(r"^\s*>\s*", "", line)
        line = re.sub(r"[*_`~|]+", "", line)  # bỏ ký hiệu markdown TRƯỚC khi dò nhãn "Lời đọc:"
        lines.append(line)

    # Bỏ 1-2 dòng dẫn nhập ở đầu (VD "Dưới đây là kịch bản:")
    while lines and not lines[0].strip():
        lines.pop(0)
    for _ in range(2):
        if lines and _PREAMBLE_LINE_RE.match(lines[0]):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)

    text = "\n".join(_LABEL_PREFIX_RE.sub("", ln) for ln in lines)

    for rx in _BRACKET_RES:
        # lặp vài lần để gỡ ngoặc lồng nhau
        for _ in range(3):
            text = rx.sub(" ", text)
    text = _TIMESTAMP_RE.sub(" ", text)
    text = _HASHTAG_RE.sub(" ", text)
    text = _EMOJI_RE.sub("", text)

    text = numbers_to_vietnamese(text)

    # Gộp thành 1 đoạn văn, chuẩn hoá khoảng trắng/dấu câu
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = text.strip(" \"'“”‘’«»")

    return _truncate_to_words(text, max_words)


# ============================================================================
# Chuẩn bị video
# ============================================================================

def _run_ffmpeg(cmd: list[str], stop_flag: Optional[Callable[[], bool]]) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags
        )
    except OSError as exc:
        raise ReviewScriptError(f"Không chạy được ffmpeg: {exc}") from exc

    started = time.time()
    while True:
        try:
            _, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if stop_flag and stop_flag():
                proc.kill()
                proc.communicate()
                raise ReviewScriptError("Đã hủy.")
            if time.time() - started > _FFMPEG_TIMEOUT_S:
                proc.kill()
                proc.communicate()
                raise ReviewScriptError("ffmpeg chạy quá lâu khi nén video, đã dừng.")
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise ReviewScriptError("ffmpeg lỗi khi nén video: " + " | ".join(tail))


def prepare_video_for_gemini(
    video_path: Path,
    ffmpeg_path: Optional[str],
    work_dir: Path,
    stop_flag: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Path:
    """Trả về đường dẫn file mp4 đủ nhỏ để gửi inline cho Gemini. Video nhỏ sẵn
    thì dùng nguyên; ngược lại nén vào `work_dir` (ffmpeg bắt buộc)."""
    video_path = Path(video_path)
    if not video_path.is_file():
        raise ReviewScriptError(f"Không tìm thấy file video: {video_path}")
    size = video_path.stat().st_size
    if video_path.suffix.lower() == ".mp4" and size <= MAX_INLINE_VIDEO_BYTES:
        return video_path

    if not ffmpeg_path:
        raise ReviewScriptError(
            "Video lớn hơn giới hạn gửi Gemini (~14 MB) hoặc không phải .mp4 nên cần "
            "ffmpeg để nén, nhưng chưa tìm thấy ffmpeg (đặt đường dẫn ffmpeg trong tab Ghép audio)."
        )

    out = Path(work_dir) / "gemini_input.mp4"
    for height, crf in _COMPRESS_STEPS:
        if progress_cb:
            progress_cb(f"Đang nén video xuống {height}p để gửi Gemini...")
        cmd = [
            ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", f"scale=-2:{height},fps=10",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "48k", "-ac", "1",
            "-movflags", "+faststart", str(out),
        ]
        logger.info("Nén video | %s | %.1f MB | %dp crf=%d", video_path.name, size / 1048576, height, crf)
        _run_ffmpeg(cmd, stop_flag)
        if out.is_file() and out.stat().st_size <= MAX_INLINE_VIDEO_BYTES:
            return out
    raise ReviewScriptError(
        "Video quá dài/nặng, nén xuống 240p vẫn vượt giới hạn gửi Gemini (~14 MB). "
        "Hãy cắt ngắn video rồi thử lại."
    )


# ============================================================================
# Gọi Gemini
# ============================================================================

def _call_gemini(
    contents: list, system_text: str, api_key: str, *, model, fallback_model, temperature: float = 0.8,
) -> str:
    payload = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature},
    }
    return generate_content(
        payload, api_key, model=model, fallback_model=fallback_model,
        timeout=REVIEW_REQUEST_TIMEOUT, purpose="viết kịch bản",
    )


def generate_review_script(
    video_path: Path,
    api_key: str,
    *,
    ffmpeg_path: Optional[str] = None,
    max_words: int = DEFAULT_MAX_WORDS,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    extra_instruction: str = "",
    koc: Optional[KocBrief] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> ReviewScriptResult:
    """Xem `video_path` và viết lời đọc review. Chạy ĐỒNG BỘ (hàm gọi nên đặt
    trong luồng nền). Ném RuntimeError (ReviewScriptError / GeminiAuthError /
    GeminiQuotaExceededError ...) nếu thất bại.

    Có `koc` -> dùng prompt KOC; số từ tối đa lấy từ `koc.max_words` (bỏ qua `max_words`)
    và nếu bản đầu lệch khỏi khoảng [min_words, max_words] thì NHỜ GEMINI VIẾT LẠI 1 LẦN
    (gửi kèm bản nháp) để khớp thời lượng, thay vì cắt cụt mất đoạn kết kêu gọi chốt đơn.
    Hết lượt thử vẫn dài quá thì mới cắt theo câu."""
    if not (api_key or "").strip():
        raise ReviewScriptError("Chưa có Gemini API Key. Vào mục Cài đặt để nhập API Key trước.")

    cap = koc.max_words if koc else max_words
    video_path = Path(video_path)
    with tempfile.TemporaryDirectory(prefix="review_script_") as tmp:
        prepared = prepare_video_for_gemini(
            video_path, ffmpeg_path, Path(tmp), stop_flag, progress_cb
        )
        if stop_flag and stop_flag():
            raise ReviewScriptError("Đã hủy.")
        try:
            data = prepared.read_bytes()
        except OSError as exc:
            raise ReviewScriptError(f"Không đọc được file video: {exc}") from exc
        encoded = base64.b64encode(data).decode("ascii")
        size_mb = len(data) / 1048576
        del data

    system_text = build_system_instruction(cap, koc)
    contents: list = [{
        "role": "user",
        "parts": [
            {"inlineData": {"mimeType": "video/mp4", "data": encoded}},
            {"text": build_user_prompt(extra_instruction, koc)},
        ],
    }]

    if progress_cb:
        progress_cb("Đang nhờ Gemini viết kịch bản review...")
    logger.info("Viết kịch bản | %s | %.1f MB | tối đa %d từ | KOC=%s | model=%s",
                video_path.name, size_mb, cap, bool(koc), model or "(mặc định)")

    attempts = 2 if koc else 1
    best_text, best_raw, best_trunc, best_gap = "", "", False, None
    for attempt in range(attempts):
        raw = _call_gemini(contents, system_text, api_key, model=model, fallback_model=fallback_model)
        # Chưa cắt ở đây: cần biết độ dài THẬT của bản nháp để quyết định có viết lại không.
        full_text, _ = sanitize_script(raw, 10_000)
        words = count_words(full_text)
        if not koc:
            best_text, best_trunc = sanitize_script(raw, cap)
            best_raw = raw
            break
        gap = 0 if koc.min_words <= words <= koc.max_words else (
            koc.min_words - words if words < koc.min_words else words - koc.max_words)
        if words and (best_gap is None or gap < best_gap):
            best_text, best_raw, best_gap = full_text, raw, gap
        if words and gap == 0:
            break
        if attempt + 1 < attempts and words:
            if stop_flag and stop_flag():
                raise ReviewScriptError("Đã hủy.")
            if progress_cb:
                progress_cb(f"Bản nháp {words} từ, lệch mục tiêu ({koc.min_words}-{koc.max_words}) — viết lại...")
            contents = contents + [
                {"role": "model", "parts": [{"text": full_text}]},
                {"role": "user", "parts": [{"text": build_length_feedback(words, koc)}]},
            ]
    del encoded, contents

    if koc:
        best_text, best_trunc = _truncate_to_words(best_text, cap)
    text = best_text
    if not text:
        raise ReviewScriptError(
            "Gemini không trả về lời đọc nào (video có thể bị từ chối hoặc không có nội dung "
            "nhận diện được). Thử lại hoặc đổi Model trong Cài đặt."
        )
    logger.info("Kịch bản xong | %d từ | bị cắt=%s", count_words(text), best_trunc)
    return ReviewScriptResult(
        text=text, raw_text=best_raw, word_count=count_words(text), truncated=best_trunc
    )
