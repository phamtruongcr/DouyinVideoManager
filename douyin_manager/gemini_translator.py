"""
gemini_translator.py
=====================
Dịch văn bản (tiêu đề video) sang Tiếng Việt bằng Gemini API
(https://ai.google.dev). Cần API key, lấy miễn phí tại
https://aistudio.google.com/apikey.

Model dùng để dịch (model chính + model dự phòng) CÓ THỂ CHỌN được trong
mục Cài đặt của app (xem `config.DEFAULT_GEMINI_MODEL`,
`config.DEFAULT_GEMINI_FALLBACK_MODEL`) — không còn hardcode cố định.
Model dự phòng có thể để trống để tắt hẳn cơ chế fallback.

Khi dịch NHIỀU tiêu đề cùng lúc (`translate_batch_with_gemini`), thay vì
xử lý tuần tự từng lô, các lô được chạy SONG SONG (số luồng tối đa cấu
hình được qua `max_workers` / mục Cài đặt) để tăng tốc đáng kể khi danh
sách dài, đồng thời báo kết quả từng tiêu đề ngay khi có (qua callback
`on_item`) để GUI cập nhật theo thời gian thực.

Xử lý lỗi & retry (áp dụng cho MỖI lô, chạy độc lập với các lô khác):
  - 503 (server quá tải)         -> retry với backoff; hết retry thì
                                     chuyển sang model dự phòng (nếu có)
  - 429 "RPM" (giới hạn/phút)    -> retry với backoff (ưu tiên header
                                     Retry-After nếu Gemini trả về)
  - 429 "RPD" (giới hạn/ngày)    -> KHÔNG retry ở model này; chuyển thẳng
                                     sang model dự phòng (nếu có). Nếu
                                     model dự phòng cũng hết quota ngày (hoặc
                                     không có model dự phòng) -> coi là lỗi
                                     FATAL: các lô CHƯA bắt đầu xử lý sẽ tự
                                     bỏ qua (không tốn thêm quota), nhưng
                                     các lô đã dịch xong trước đó vẫn được
                                     giữ nguyên kết quả, không bị hủy.
  - 400 (request sai định dạng)  -> dừng ngay lô này, không retry, không đổi
                                     model. Coi là lỗi FATAL (thường do lỗi
                                     cấu hình, không có ý nghĩa thử lại).
  - 401 / 403 (key sai/không có quyền) -> dừng ngay, không retry, không
                                     đổi model. Coi là lỗi FATAL.
  - Lỗi kết nối / timeout        -> retry với backoff như lỗi tạm thời,
                                     KHÔNG coi là fatal (chỉ lô đó thất bại,
                                     các lô khác không bị ảnh hưởng).
  - Các mã lỗi khác              -> dừng lô này, báo lỗi nguyên văn, KHÔNG
                                     coi là fatal.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .config import (
    DEFAULT_GEMINI_BATCH_WORKERS,
    DEFAULT_TRANSLATE_STYLE,
    TRANSLATE_STYLE_FACEBOOK,
    TRANSLATE_STYLE_LITERAL,
    TRANSLATE_STYLE_CUSTOM,
)

# Phần gọi Gemini (retry, fallback model, exception...) nằm ở gemini_client.py.
# Re-export lại các tên cũ để code đang `from .gemini_translator import ...` vẫn chạy.
from .gemini_client import (  # noqa: F401
    GEMINI_MAX_RETRIES,
    GEMINI_RETRY_BASE_DELAY,
    GeminiAuthError,
    GeminiBadRequestError,
    GeminiQuotaExceededError,
    generate_content,
    list_available_models,
)


def _translate_raw_with_gemini(
    prompt: str,
    api_key: str,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
) -> str:
    """Gửi `prompt` bất kỳ lên Gemini, trả về text thô (dịch 1 hoặc gộp nhiều tiêu đề)."""
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    return generate_content(payload, api_key, model=model, fallback_model=fallback_model)


def _render_custom_prompt(custom_prompt: str, target_lang: str) -> str:
    """Chuẩn hoá prompt do người dùng tự điền: bỏ khoảng trắng thừa và thay
    placeholder `{lang}` bằng ngôn ngữ đích. Dùng replace() thay vì
    str.format() để người dùng gõ dấu { } khác cũng không gây lỗi."""
    return (custom_prompt or "").strip().replace("{lang}", target_lang)


def _build_single_prompt(
    text: str, target_lang: str, style: str, custom_prompt: str = ""
) -> str:
    """Dựng prompt dịch 1 tiêu đề, theo đúng `style` (xem
    `config.TRANSLATE_STYLE_OPTIONS`)."""
    if style == TRANSLATE_STYLE_CUSTOM and _render_custom_prompt(custom_prompt, target_lang):
        instruction = _render_custom_prompt(custom_prompt, target_lang)
        return (
            "Bạn nhận 1 tiêu đề video Douyin. Hãy xử lý nó theo HƯỚNG DẪN của "
            "người dùng dưới đây:\n"
            f"<huong_dan>\n{instruction}\n</huong_dan>\n\n"
            "ĐỊNH DẠNG TRẢ VỀ BẮT BUỘC: chỉ trả về DUY NHẤT kết quả cuối cùng "
            "(đúng 1 tiêu đề), không thêm giải thích, không thêm tiền tố, không "
            "bọc trong dấu ngoặc kép.\n\n"
            f"Tiêu đề gốc:\n{text}"
        )
    if style == TRANSLATE_STYLE_FACEBOOK:
        return (
            f"Dịch đoạn văn bản (tiêu đề video Douyin) sau sang {target_lang}, sau đó "
            "VIẾT LẠI thành 1 TIÊU ĐỀ NGẮN GỌN khoảng 10 từ, phù hợp để đăng lên "
            "Facebook — tự nhiên, thu hút, như người Việt thật sự viết, không sáo "
            "rỗng, không rập khuôn. BỎ HẾT hashtag (các từ bắt đầu bằng #), không "
            "thêm emoji nếu bản gốc không có, không thêm dấu ngoặc kép. Chỉ trả về "
            "DUY NHẤT tiêu đề đã viết lại, không thêm giải thích hay tiền tố nào "
            f"khác:\n\n{text}"
        )
    return (
        f"Dịch đoạn văn bản sau sang {target_lang}. "
        "Chỉ trả về duy nhất bản dịch, "
        "không thêm giải thích, "
        "không thêm dấu ngoặc kép, "
        "BỎ HẾT các hashtag (những từ bắt đầu bằng #), "
        "giữ nguyên emoji nếu có:\n\n"
        f"{text}"
    )


def translate_with_gemini(
    text: str,
    api_key: str,
    target_lang: str = "Tiếng Việt",
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    style: str = DEFAULT_TRANSLATE_STYLE,
    custom_prompt: str = "",
) -> str:
    """Dịch `text` sang `target_lang` bằng Gemini API (1 request cho 1 văn
    bản). Vẫn giữ lại để dùng khi chỉ cần dịch lẻ 1 đoạn (VD: sau khi sửa
    tay 1 tiêu đề). Khi cần dịch nhiều tiêu đề cùng lúc, dùng
    `translate_batch_with_gemini` bên dưới.

    `style`: "literal" (mặc định, dịch nguyên văn, bỏ hashtag) hoặc
    "facebook" (dịch rồi viết lại thành tiêu đề ngắn ~10 từ cho Facebook) —
    xem `config.TRANSLATE_STYLE_OPTIONS`."""
    text = (text or "").strip()
    if not text:
        return text
    if not api_key:
        raise RuntimeError(
            "Chưa có Gemini API Key. Vào mục Cài đặt để nhập API Key trước."
        )

    prompt = _build_single_prompt(text, target_lang, style, custom_prompt)
    translated = _translate_raw_with_gemini(prompt, api_key, model=model, fallback_model=fallback_model)
    return translated or text


# Số tiêu đề tối đa gộp trong 1 request dịch (1 lô = 1 request). Danh sách
# được chọn sẽ được chia thành nhiều lô, và các lô chạy SONG SONG với nhau
# (xem `max_workers`) thay vì tuần tự, để tăng tốc khi có nhiều tiêu đề.
GEMINI_BATCH_SIZE = 40

_BATCH_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.]\s*(.*)$")


class _BatchRunState:
    """Trạng thái dùng chung giữa các lô chạy song song: khi 1 lô gặp lỗi
    FATAL (Auth/BadRequest/QuotaExceeded), các lô CHƯA bắt đầu gọi API sẽ
    tự bỏ qua (không tốn thêm quota/request), nhưng các lô đã hoàn thành
    trước đó vẫn giữ nguyên kết quả — không bị hủy theo."""

    def __init__(self):
        self._lock = threading.Lock()
        self.fatal_error: Optional[Exception] = None

    def set_fatal(self, exc: Exception):
        with self._lock:
            if self.fatal_error is None:
                self.fatal_error = exc

    def get_fatal(self) -> Optional[Exception]:
        with self._lock:
            return self.fatal_error


def _build_batch_prompt(
    numbered_lines: str, n: int, target_lang: str, style: str, custom_prompt: str = ""
) -> str:
    """Dựng prompt dịch GỘP nhiều tiêu đề (đánh số từng dòng), theo đúng
    `style` (xem `config.TRANSLATE_STYLE_OPTIONS`)."""
    if style == TRANSLATE_STYLE_CUSTOM and _render_custom_prompt(custom_prompt, target_lang):
        instruction = _render_custom_prompt(custom_prompt, target_lang)
        return (
            f"Bạn nhận {n} đoạn văn bản (tiêu đề video Douyin) được đánh số bên "
            "dưới. Với MỖI đoạn, hãy xử lý theo HƯỚNG DẪN của người dùng:\n"
            f"<huong_dan>\n{instruction}\n</huong_dan>\n\n"
            "ĐỊNH DẠNG TRẢ VỀ BẮT BUỘC (luôn ưu tiên hơn mọi yêu cầu về định dạng "
            "nếu có trong hướng dẫn trên): mỗi đoạn tương ứng với đúng 1 dòng kết "
            "quả, giữ NGUYÊN số thứ tự và định dạng \"số: kết quả\" (ví dụ dòng "
            "đầu vào là \"1: ...\" thì dòng trả lời cũng phải bắt đầu bằng "
            "\"1: \"). Không thêm giải thích, không bọc dấu ngoặc kép, không gộp "
            "nhiều dòng vào 1, không tách 1 dòng thành nhiều dòng. Trả về đúng và "
            f"chỉ đúng {n} dòng tương ứng, không thêm dòng nào khác:\n\n{numbered_lines}"
        )
    if style == TRANSLATE_STYLE_FACEBOOK:
        return (
            f"Với MỖI đoạn trong {n} đoạn văn bản (tiêu đề video Douyin) được đánh số "
            f"dưới đây, hãy: (1) dịch sang {target_lang}, (2) VIẾT LẠI thành 1 TIÊU ĐỀ "
            "NGẮN GỌN khoảng 10 từ, phù hợp để đăng lên Facebook — tự nhiên, thu hút, "
            "như người Việt thật sự viết. BỎ HẾT hashtag (từ bắt đầu bằng #), không "
            "thêm emoji nếu bản gốc không có, không thêm dấu ngoặc kép.\n"
            f"QUAN TRỌNG: {n} tiêu đề PHẢI khác nhau về cách viết, cách mở đầu và "
            "giọng điệu — TUYỆT ĐỐI không dùng chung 1 công thức/cụm mở đầu lặp lại "
            "cho nhiều dòng (ví dụ KHÔNG được để nhiều dòng cùng bắt đầu bằng "
            "\"Khoảnh khắc...\", \"Video...\", \"Khi...\", \"Cảnh...\"...) — hãy viết như "
            "thể mỗi tiêu đề do 1 người khác nhau nghĩ ra, đa dạng cấu trúc câu.\n"
            "Mỗi đoạn tương ứng với đúng 1 dòng kết quả, giữ NGUYÊN số thứ tự và định "
            "dạng \"số: tiêu đề\" (ví dụ dòng đầu vào là \"1: ...\" thì dòng trả lời "
            "cũng phải bắt đầu bằng \"1: \"). Không thêm giải thích, không gộp nhiều "
            f"dòng vào 1, không tách 1 dòng thành nhiều dòng. Trả về đúng và chỉ đúng "
            f"{n} dòng tương ứng, không thêm dòng nào khác:\n\n{numbered_lines}"
        )
    return (
        f"Dịch TẤT CẢ {n} đoạn văn bản được đánh số dưới "
        f"đây sang {target_lang}. Mỗi đoạn tương ứng với đúng 1 dòng kết "
        "quả, giữ NGUYÊN số thứ tự và định dạng \"số: bản dịch\" (ví dụ "
        "dòng đầu vào là \"1: ...\" thì dòng trả lời cũng phải bắt đầu "
        "bằng \"1: \"). Không thêm giải thích, không thêm dấu ngoặc kép, "
        "không gộp nhiều dòng vào 1, không tách 1 dòng thành nhiều dòng, "
        "BỎ HẾT hashtag (những từ bắt đầu bằng #), giữ nguyên emoji nếu có. "
        f"Trả về đúng và chỉ đúng {n} dòng tương ứng, không thêm dòng nào khác:"
        f"\n\n{numbered_lines}"
    )


def _process_one_batch(
    chunk_indices: list[int],
    texts: list[str],
    api_key: str,
    target_lang: str,
    model: Optional[str],
    fallback_model: Optional[str],
    state: _BatchRunState,
    stop_flag: Optional[Callable[[], bool]],
    style: str = DEFAULT_TRANSLATE_STYLE,
    custom_prompt: str = "",
) -> tuple[list[int], Optional[dict[int, str]], Optional[str]]:
    """Xử lý 1 lô (1 request gộp nhiều tiêu đề). Trả về
    (chunk_indices, {pos: bản_dịch} | None, thông_báo_lỗi | None).
    `parsed` là None nếu cả lô bị bỏ qua/lỗi (không dịch được phần tử nào
    trong lô này) — nơi gọi sẽ coi mọi phần tử trong lô là "failed"."""
    if state.get_fatal() is not None:
        return chunk_indices, None, "Đã dừng vì lỗi nghiêm trọng xảy ra ở lô khác."
    if stop_flag and stop_flag():
        return chunk_indices, None, "Đã hủy."

    numbered_lines = "\n".join(
        f"{pos + 1}: {texts[idx].strip()}" for pos, idx in enumerate(chunk_indices)
    )
    prompt = _build_batch_prompt(
        numbered_lines, len(chunk_indices), target_lang, style, custom_prompt
    )

    try:
        translated_text = _translate_raw_with_gemini(
            prompt, api_key, model=model, fallback_model=fallback_model
        )
    except (GeminiAuthError, GeminiBadRequestError, GeminiQuotaExceededError) as exc:
        state.set_fatal(exc)
        return chunk_indices, None, str(exc)
    except RuntimeError as exc:
        # Lỗi tạm thời (mạng, 503, 429 RPM hết retry...) -> chỉ lô này thất
        # bại, KHÔNG coi là fatal, các lô khác vẫn tiếp tục bình thường.
        return chunk_indices, None, str(exc)

    parsed: dict[int, str] = {}
    for line in translated_text.splitlines():
        m = _BATCH_LINE_RE.match(line)
        if not m:
            continue
        pos = int(m.group(1)) - 1
        if 0 <= pos < len(chunk_indices):
            translated_line = m.group(2).strip()
            if translated_line:
                parsed[pos] = translated_line

    return chunk_indices, parsed, None


def translate_batch_with_gemini(
    texts: list[str],
    api_key: str,
    target_lang: str = "Tiếng Việt",
    batch_size: int = GEMINI_BATCH_SIZE,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    max_workers: int = DEFAULT_GEMINI_BATCH_WORKERS,
    stop_flag: Optional[Callable[[], bool]] = None,
    on_item: Optional[Callable[[int, str, Optional[str]], None]] = None,
    style: str = DEFAULT_TRANSLATE_STYLE,
    custom_prompt: str = "",
) -> tuple[list[str], list[int], Optional[str]]:
    """Dịch NHIỀU đoạn văn bản (tiêu đề) cùng lúc, gộp thành nhiều lô (mỗi
    lô 1 request, đánh số từng dòng) và chạy các lô SONG SONG với nhau
    (tối đa `max_workers` lô cùng lúc — cấu hình được trong Cài đặt) thay
    vì tuần tự như trước, giúp dịch nhanh hơn nhiều khi danh sách dài.

    - `style`: "literal" (dịch nguyên văn, bỏ hashtag) hoặc "facebook"
      (dịch rồi viết lại thành tiêu đề ngắn ~10 từ cho Facebook, mỗi tiêu
      đề trong lô được yêu cầu viết khác giọng nhau) — xem
      `config.TRANSLATE_STYLE_OPTIONS`.
    - `custom_prompt`: hướng dẫn do người dùng tự điền, chỉ dùng khi
      `style` = "custom" (hỗ trợ placeholder `{lang}`). Nếu để trống thì tự
      quay về kiểu dịch nguyên văn.
    - `stop_flag`: callable trả True nếu người dùng muốn hủy giữa chừng.
    - `on_item(idx, translated_or_original, error_message_or_None)`: được
      gọi ngay khi TỪNG tiêu đề có kết quả (thành công hay thất bại), để
      GUI cập nhật theo thời gian thực thay vì đợi dịch xong hết.

    Trả về tuple (translated_texts, failed_indices, fatal_error_message):
      - translated_texts: list CÙNG độ dài và thứ tự với `texts`; phần tử
        nào dịch thành công thì thay bằng bản dịch, thất bại thì giữ
        nguyên bản gốc.
      - failed_indices: index (trong `texts`) của các phần tử không dịch
        được (giữ nguyên bản gốc ở `translated_texts`).
      - fatal_error_message: None nếu không có lỗi nghiêm trọng; ngược lại
        là thông báo lỗi (API Key sai / hết quota ngày / request sai định
        dạng) khiến các lô CHƯA xử lý bị bỏ qua. Các lô ĐÃ dịch xong trước
        khi lỗi này xảy ra vẫn được giữ nguyên trong kết quả trả về (không
        bị mất, khác với hành vi raise-toàn-bộ trước đây)."""
    n = len(texts)
    result = list(texts)
    failed: list[int] = []
    if n == 0:
        return result, failed, None

    indices_to_translate = [i for i, t in enumerate(texts) if (t or "").strip()]
    if not indices_to_translate:
        return result, failed, None

    if not api_key:
        raise RuntimeError(
            "Chưa có Gemini API Key. Vào mục Cài đặt để nhập API Key trước."
        )

    chunks = [
        indices_to_translate[i:i + batch_size]
        for i in range(0, len(indices_to_translate), batch_size)
    ]

    state = _BatchRunState()
    workers = max(1, min(max_workers, len(chunks)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _process_one_batch,
                chunk, texts, api_key, target_lang, model, fallback_model, state, stop_flag,
                style=style, custom_prompt=custom_prompt,
            )
            for chunk in chunks
        ]
        for future in as_completed(futures):
            chunk_indices, parsed, err_msg = future.result()
            if parsed is None:
                for idx in chunk_indices:
                    failed.append(idx)
                    if on_item:
                        on_item(idx, result[idx], err_msg)
                continue

            for pos, idx in enumerate(chunk_indices):
                if pos in parsed:
                    result[idx] = parsed[pos]
                    if on_item:
                        on_item(idx, result[idx], None)
                else:
                    failed.append(idx)
                    if on_item:
                        on_item(
                            idx, result[idx],
                            "Không nhận được bản dịch khớp từ phản hồi gộp, đã giữ nguyên tiêu đề gốc.",
                        )

    fatal = state.get_fatal()
    return result, failed, (str(fatal) if fatal else None)