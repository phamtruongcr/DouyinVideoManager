"""Test ghi log + lấy danh sách Douyin (Playwright -> API dự phòng).
Chạy: python -m unittest discover -s tests -v"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from douyin_manager import app_logger, browser_sniffer, config, douyin_client
from douyin_manager.douyin_client import DouyinAPIError, DouyinClient
from douyin_manager.fetch_filters import FetchFilters, ItemCollector


def aweme(i, ts=None, with_url=True):
    d = {
        "aweme_id": str(i), "desc": f"video {i}", "create_time": ts or (2_000_000 - i),
        "video": {"duration": 12000},
        "statistics": {"play_count": 100 + i, "digg_count": i},
    }
    if with_url:
        d["video"]["play_addr"] = {"url_list": [f"https://x/playwm/?id={i}"]}
    return d


class RedactTests(unittest.TestCase):
    def test_masks_cookie_header_and_tokens(self):
        txt = ("GET https://www.douyin.com/aweme/post?msToken=SECRET1&a_bogus=SECRET2&x=1 "
               "Cookie: ttwid=SECRET3; sessionid=SECRET4")
        out = app_logger.redact(txt)
        for s in ("SECRET1", "SECRET2", "SECRET3", "SECRET4"):
            self.assertNotIn(s, out)
        self.assertIn("x=1", out)

    def test_masks_api_key(self):
        out = app_logger.redact("url?key=AIzaSyA1234567890abcdefghijklmnopqrstu and AIzaSyA1234567890abcdefghijklmnopqrstu")
        self.assertNotIn("AIza", out)

    def test_describe_cookie_has_names_not_values(self):
        d = app_logger.describe_cookie("ttwid=SECRETVAL; odin_tt=ANOTHER")
        self.assertIn("ttwid", d)
        self.assertIn("odin_tt", d)
        self.assertNotIn("SECRETVAL", d)
        self.assertEqual(app_logger.describe_cookie(""), "TRỐNG")


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_dir = config.LOG_DIR
        config.LOG_DIR = Path(self._tmp.name) / "logs"
        self.addCleanup(setattr, config, "LOG_DIR", self._old_dir)
        # tách handler cũ để test dùng file tạm
        root = app_logger.get_logger()
        for h in list(root.handlers):
            root.removeHandler(h)
        app_logger._file_handler = None
        self.addCleanup(self._cleanup_handlers)

    def _cleanup_handlers(self):
        root = app_logger.get_logger()
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        app_logger._file_handler = None

    def test_writes_redacted_line_and_traceback(self):
        path = app_logger.setup_logging("INFO")
        log = app_logger.get_logger("t")
        log.info("Cookie: sessionid=ABC123; ttwid=XYZ789 msToken=TOK")
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("boom")
        app_logger.flush_handler()
        text = path.read_text(encoding="utf-8")
        self.assertIn("boom", text)
        self.assertIn("ZeroDivisionError", text)
        for s in ("ABC123", "XYZ789", "TOK"):
            self.assertNotIn(s, text)

    def test_level_switch_and_read_tail_and_clear(self):
        app_logger.setup_logging("INFO")
        log = app_logger.get_logger("t")
        log.debug("debug-hidden")
        app_logger.set_level("DEBUG")
        log.debug("debug-shown")
        app_logger.flush_handler()
        tail = app_logger.read_tail(50)
        self.assertNotIn("debug-hidden", tail)
        self.assertIn("debug-shown", tail)
        self.assertTrue(app_logger.clear_logs())
        self.assertEqual(app_logger.read_tail(50), "")

    def test_setup_twice_does_not_duplicate_handlers(self):
        app_logger.setup_logging("INFO")
        n = len(app_logger.get_logger().handlers)
        app_logger.setup_logging("DEBUG")
        self.assertEqual(len(app_logger.get_logger().handlers), n)


class DouyinParserTests(unittest.TestCase):
    def test_parse_keeps_source_order(self):
        data = {"status_code": 0, "aweme_list": [aweme(1), aweme(2), aweme(3)]}
        ids = [it["id"] for it in browser_sniffer.parse_douyin_json(data)]
        self.assertEqual(ids, ["1", "2", "3"])

    def test_skips_items_without_play_url(self):
        data = {"aweme_list": [aweme(1), aweme(2, with_url=False)]}
        self.assertEqual([i["id"] for i in browser_sniffer.parse_douyin_json(data)], ["1"])

    def test_item_matches_api_format(self):
        item = browser_sniffer.parse_douyin_json({"aweme_list": [aweme(5)]})[0]
        self.assertEqual(item["url"], "https://x/play/?id=5")   # playwm -> play
        self.assertEqual(item["duration_s"], 12)
        self.assertEqual((item["view_count"], item["like_count"]), (105, 5))

    def test_parse_response_text_handles_json_text(self):
        text = json.dumps({"aweme_list": [aweme(7)], "has_more": 0})
        items = browser_sniffer.parse_response_text("douyin", text)
        self.assertEqual([i["id"] for i in items], ["7"])

    def test_wanted_response_for_douyin(self):
        w = browser_sniffer._wanted_response
        self.assertTrue(w("douyin", "https://www.douyin.com/aweme/v1/web/aweme/post/?a=1",
                          "application/json", False))
        self.assertFalse(w("douyin", "https://www.douyin.com/aweme/v1/web/other/",
                           "application/json", False))

    def test_tiktok_and_walk_order_preserved(self):
        mk = lambda i: {"id": str(i), "video": {"duration": 5}, "createTime": 1000 - i, "desc": "d"}
        ids = [x["id"] for x in browser_sniffer.parse_tiktok_json({"itemList": [mk(1), mk(2), mk(3)]})]
        self.assertEqual(ids, ["1", "2", "3"])


class DouyinFallbackTests(unittest.TestCase):
    def _collector(self, **kw):
        return ItemCollector(FetchFilters(**kw), stop_flag=lambda: False)

    def _fake_response(self, status=200, payload=None):
        r = mock.Mock()
        r.status_code = status
        r.content = b"x" * 10
        r.headers = {"Content-Type": "application/json"}
        r.text = json.dumps(payload or {})
        r.json.return_value = payload or {}
        return r

    def test_playwright_success_skips_api(self):
        def fake_sniff(platform, url, stop, collector, **kw):
            self.assertEqual(platform, "douyin")
            for it in browser_sniffer.parse_douyin_json({"aweme_list": [aweme(1), aweme(2)]}):
                collector.feed(it)

        client = DouyinClient(cookie="ttwid=a")
        with mock.patch.object(douyin_client, "sniff_profile_videos", fake_sniff), \
             mock.patch.object(douyin_client.requests, "get") as get:
            items = client.fetch_all_user_posts("SEC", lambda: False, collector=self._collector())
        get.assert_not_called()
        self.assertEqual(len(items), 2)
        self.assertEqual(client.last_warning, "")

    def test_playwright_unavailable_falls_back_to_api(self):
        page = {"aweme_list": [aweme(1)], "has_more": 0, "max_cursor": 0}
        client = DouyinClient()
        with mock.patch.object(douyin_client, "sniff_profile_videos",
                               side_effect=browser_sniffer.SnifferUnavailable("no pw")), \
             mock.patch.object(douyin_client.requests, "get", return_value=self._fake_response(200, page)):
            items = client.fetch_all_user_posts("SEC", lambda: False, collector=self._collector())
        self.assertEqual([i["id"] for i in items], ["1"])
        self.assertIn("API trực tiếp", client.last_warning)

    def test_both_fail_reports_both_reasons(self):
        client = DouyinClient()
        with mock.patch.object(douyin_client, "sniff_profile_videos",
                               side_effect=browser_sniffer.SnifferBlocked("captcha!")), \
             mock.patch.object(douyin_client.requests, "get", return_value=self._fake_response(403)):
            with self.assertRaises(DouyinAPIError) as cm:
                client.fetch_all_user_posts("SEC", lambda: False, collector=self._collector())
        msg = str(cm.exception)
        self.assertIn("403", msg)
        self.assertIn("captcha!", msg)
        self.assertIn("log", msg.lower())

    def test_playwright_disabled_uses_api_only(self):
        page = {"aweme_list": [aweme(3)], "has_more": 0, "max_cursor": 0}
        client = DouyinClient(use_playwright=False)
        with mock.patch.object(douyin_client, "sniff_profile_videos") as sniff, \
             mock.patch.object(douyin_client.requests, "get", return_value=self._fake_response(200, page)):
            items = client.fetch_all_user_posts("SEC", lambda: False, collector=self._collector())
        sniff.assert_not_called()
        self.assertEqual(len(items), 1)

    def test_unexpected_playwright_error_still_falls_back(self):
        page = {"aweme_list": [aweme(4)], "has_more": 0, "max_cursor": 0}
        client = DouyinClient()
        with mock.patch.object(douyin_client, "sniff_profile_videos", side_effect=RuntimeError("weird")), \
             mock.patch.object(douyin_client.requests, "get", return_value=self._fake_response(200, page)):
            items = client.fetch_all_user_posts("SEC", lambda: False, collector=self._collector())
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
