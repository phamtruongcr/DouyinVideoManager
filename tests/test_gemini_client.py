"""Test lớp gọi Gemini dùng chung (không gọi mạng, không ngủ thật).
Chạy: python -m unittest discover -s tests -v"""

import unittest
from unittest import mock

import requests

from douyin_manager import gemini_client as gc
from douyin_manager import gemini_translator as gt


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _ok(text):
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _err(status, message="lỗi", headers=None):
    return _Resp(status, {"error": {"message": message}}, headers)


class GenerateContentTests(unittest.TestCase):
    def setUp(self):
        # không ngủ thật khi backoff
        p = mock.patch.object(gc.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def run_gen(self, responses, model="a", fallback="", **kw):
        with mock.patch.object(gc, "_call_gemini", side_effect=responses) as call:
            try:
                out = gc.generate_content({"contents": []}, "KEY", model=model,
                                          fallback_model=fallback, **kw)
            finally:
                self.call = call
        return out

    def test_ok_strips_and_joins_parts(self):
        resp = _Resp(200, {"candidates": [{"content": {"parts": [{"text": " a "}, {"text": "b "}]}}]})
        self.assertEqual(self.run_gen([resp]), "a b")

    def test_timeout_default_and_custom(self):
        self.run_gen([_ok("x")])
        self.assertEqual(self.call.call_args.kwargs["timeout"], gc.REQUEST_TIMEOUT)
        self.run_gen([_ok("x")], timeout=99)
        self.assertEqual(self.call.call_args.kwargs["timeout"], 99)

    def test_block_reason_reported(self):
        resp = _Resp(200, {"promptFeedback": {"blockReason": "SAFETY"}})
        with self.assertRaises(RuntimeError) as ctx:
            self.run_gen([resp])
        self.assertIn("SAFETY", str(ctx.exception))
        self.assertIn("kết quả dịch", str(ctx.exception))      # purpose mặc định giữ chữ cũ

    def test_no_candidates_plain_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_gen([_Resp(200, {})], purpose="viết kịch bản")
        self.assertIn("không trả về candidates", str(ctx.exception))
        self.assertIn("kết quả viết kịch bản", str(ctx.exception))

    def test_400_and_auth_are_fatal_no_retry(self):
        with self.assertRaises(gc.GeminiBadRequestError):
            self.run_gen([_err(400)])
        self.assertEqual(self.call.call_count, 1)
        for code in (401, 403):
            with self.assertRaises(gc.GeminiAuthError):
                self.run_gen([_err(code)])
            self.assertEqual(self.call.call_count, 1)

    def test_auth_error_not_retried_on_fallback_model(self):
        with self.assertRaises(gc.GeminiAuthError):
            self.run_gen([_err(403)], model="a", fallback="b")
        self.assertEqual(self.call.call_count, 1)

    def test_503_retries_then_succeeds(self):
        out = self.run_gen([_err(503), _err(503), _ok("xong")])
        self.assertEqual(out, "xong")
        self.assertEqual(self.call.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_503_exhausted_falls_back_to_second_model(self):
        seq = [_err(503)] * gc.GEMINI_MAX_RETRIES + [_ok("từ dự phòng")]
        out = self.run_gen(seq, model="a", fallback="b")
        self.assertEqual(out, "từ dự phòng")
        models = [c.kwargs["model"] for c in self.call.call_args_list]
        self.assertEqual(models, ["a"] * gc.GEMINI_MAX_RETRIES + ["b"])

    def test_503_exhausted_without_fallback_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_gen([_err(503)] * gc.GEMINI_MAX_RETRIES)
        self.assertIn("503", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, (gc.GeminiAuthError, gc.GeminiQuotaExceededError))

    def test_429_rpm_retries_with_retry_after(self):
        rpm = _err(429, "quota per minute exceeded", headers={"Retry-After": "7"})
        out = self.run_gen([rpm, _ok("ok")])
        self.assertEqual(out, "ok")
        self.assertGreaterEqual(self.sleep.call_args.args[0], 7)

    def test_429_unknown_treated_as_retryable(self):
        out = self.run_gen([_err(429, "slow down"), _ok("ok")])
        self.assertEqual(out, "ok")
        self.assertEqual(self.call.call_count, 2)

    def test_429_rpd_goes_to_fallback_without_retry(self):
        rpd = _err(429, "quota per day exceeded")
        out = self.run_gen([rpd, _ok("dự phòng")], model="a", fallback="b")
        self.assertEqual(out, "dự phòng")
        self.assertEqual([c.kwargs["model"] for c in self.call.call_args_list], ["a", "b"])
        self.assertEqual(self.sleep.call_count, 0)

    def test_429_rpd_on_last_model_is_quota_error(self):
        rpd = _err(429, "quota per day exceeded")
        with self.assertRaises(gc.GeminiQuotaExceededError):
            self.run_gen([rpd, rpd], model="a", fallback="b")
        self.assertEqual(self.call.call_count, 2)

    def test_connection_error_retries(self):
        out = self.run_gen([requests.ConnectionError("down"), _ok("lại ok")])
        self.assertEqual(out, "lại ok")

    def test_connection_error_exhausted_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_gen([requests.Timeout("slow")] * gc.GEMINI_MAX_RETRIES)
        self.assertIn("Lỗi kết nối tới Gemini", str(ctx.exception))

    def test_other_status_raises_plain_runtimeerror(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_gen([_err(500, "boom")])
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertEqual(self.call.call_count, 1)


class HelperTests(unittest.TestCase):
    def test_classify_429(self):
        self.assertEqual(gc._classify_429({}, "Quota exceeded per day"), "RPD")
        self.assertEqual(gc._classify_429({"details": [{"quotaId": "RequestsPerDay"}]}, ""), "RPD")
        self.assertEqual(gc._classify_429({}, "requests per minute"), "RPM")
        self.assertEqual(gc._classify_429({}, "???"), "UNKNOWN")

    def test_resolve_models_dedupes_and_orders(self):
        self.assertEqual(gc._resolve_models("a", "a"), ["a"])
        self.assertEqual(gc._resolve_models(" a ", " b "), ["a", "b"])
        self.assertEqual(gc._resolve_models("a", ""), ["a"])

    def test_resolve_models_empty_message_uses_purpose(self):
        with mock.patch.object(gc, "DEFAULT_GEMINI_MODEL", ""):
            with self.assertRaises(gc.GeminiBadRequestError) as ctx:
                gc._resolve_models("", "")
            self.assertIn("để dịch", str(ctx.exception))
            with self.assertRaises(gc.GeminiBadRequestError) as ctx:
                gc._resolve_models("", "", "viết kịch bản")
            self.assertIn("để viết kịch bản", str(ctx.exception))

    def test_parse_error_body_non_json(self):
        class R:
            text = "plain text" * 100
            def json(self):
                raise ValueError
        msg, err = gc._parse_error_body(R())
        self.assertEqual(err, {})
        self.assertLessEqual(len(msg), 500)


class ListModelsTests(unittest.TestCase):
    def _page(self, names, token=None):
        body = {"models": [{"name": f"models/{n}", "supportedGenerationMethods": m} for n, m in names]}
        if token:
            body["nextPageToken"] = token
        return _Resp(200, body)

    def test_pagination_filter_sort_dedupe(self):
        pages = [
            self._page([("zeta", ["generateContent"]), ("embed", ["embedContent"])], token="t2"),
            self._page([("alpha", ["generateContent"]), ("zeta", ["generateContent"])]),
        ]
        with mock.patch.object(gc.requests, "get", side_effect=pages) as get:
            self.assertEqual(gc.list_available_models(" KEY "), ["alpha", "zeta"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["pageToken"], "t2")
        self.assertEqual(get.call_args.kwargs["headers"]["x-goog-api-key"], "KEY")

    def test_no_key(self):
        with self.assertRaises(RuntimeError):
            gc.list_available_models("  ")

    def test_auth_and_http_errors(self):
        with mock.patch.object(gc.requests, "get", return_value=_err(403, "bad key")):
            with self.assertRaises(RuntimeError) as ctx:
                gc.list_available_models("K")
            self.assertIn("API Key sai", str(ctx.exception))
        with mock.patch.object(gc.requests, "get", return_value=_err(500, "x")):
            with self.assertRaises(RuntimeError) as ctx:
                gc.list_available_models("K")
            self.assertIn("HTTP 500", str(ctx.exception))

    def test_connection_error(self):
        with mock.patch.object(gc.requests, "get", side_effect=requests.ConnectionError("x")):
            with self.assertRaises(RuntimeError) as ctx:
                gc.list_available_models("K")
            self.assertIn("Lỗi kết nối", str(ctx.exception))


class TranslatorCompatTests(unittest.TestCase):
    """Sau khi tách, gemini_translator phải giữ nguyên tên cũ và chữ thông báo cũ."""

    def test_reexports_are_same_objects(self):
        self.assertIs(gt.GeminiAuthError, gc.GeminiAuthError)
        self.assertIs(gt.GeminiBadRequestError, gc.GeminiBadRequestError)
        self.assertIs(gt.GeminiQuotaExceededError, gc.GeminiQuotaExceededError)
        self.assertIs(gt.list_available_models, gc.list_available_models)
        self.assertEqual(gt.GEMINI_MAX_RETRIES, gc.GEMINI_MAX_RETRIES)

    def test_translate_raw_builds_simple_payload(self):
        with mock.patch.object(gc, "_call_gemini", return_value=_ok(" xin chào ")) as call:
            out = gt._translate_raw_with_gemini("hi", "K", model="m", fallback_model="")
        self.assertEqual(out, "xin chào")
        self.assertEqual(call.call_args.kwargs["payload"], {"contents": [{"parts": [{"text": "hi"}]}]})

    def test_translate_single_and_batch_end_to_end(self):
        with mock.patch.object(gc, "_call_gemini", return_value=_ok("Xin chào")):
            self.assertEqual(gt.translate_with_gemini("你好", "K", model="m", fallback_model=""),
                             "Xin chào")
        with mock.patch.object(gc, "_call_gemini", return_value=_ok("1: Một\n2: Hai")):
            res, failed, fatal = gt.translate_batch_with_gemini(
                ["一", "二"], "K", model="m", fallback_model="", max_workers=1)
        self.assertEqual((res, failed, fatal), (["Một", "Hai"], [], None))

    def test_batch_reports_fatal_error(self):
        with mock.patch.object(gc, "_call_gemini", return_value=_err(403, "bad")):
            res, failed, fatal = gt.translate_batch_with_gemini(
                ["一"], "K", model="m", fallback_model="", max_workers=1)
        self.assertEqual(res, ["一"])
        self.assertEqual(failed, [0])
        self.assertIn("API Key", fatal)

    def test_translator_error_text_unchanged(self):
        with mock.patch.object(gc, "DEFAULT_GEMINI_MODEL", ""):
            with self.assertRaises(gc.GeminiBadRequestError) as ctx:
                gt._translate_raw_with_gemini("x", "K", model="", fallback_model="")
        self.assertEqual(str(ctx.exception), "Chưa chọn Model Gemini nào để dịch (mục Cài đặt).")


if __name__ == "__main__":
    unittest.main()
