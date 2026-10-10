import unittest

from douyin_manager import browser_sniffer as bs


class FakePage:
    def __init__(self, text="", fail=False, click_ok=True):
        self.text, self.fail, self.click_ok = text, fail, click_ok
        self.clicked = []

    def inner_text(self, sel):
        if self.fail:
            raise RuntimeError("closed")
        return self.text

    def get_by_text(self, label, exact=False):
        page = self

        class Loc:
            @property
            def first(self_inner):
                return self_inner

            def click(self_inner, timeout=0):
                if not page.click_ok:
                    raise RuntimeError("không thấy")
                page.clicked.append(label)

        return Loc()


class ServiceErrorTests(unittest.TestCase):
    def test_detects_douyin_service_error(self):
        self.assertTrue(bs._service_error_now(FakePage("作品 19 | 服务异常，重新刷新拉取数据")))

    def test_normal_page_not_flagged(self):
        self.assertFalse(bs._service_error_now(FakePage("作品 19 推荐 喜欢")))
        self.assertFalse(bs._service_error_now(FakePage(fail=True)))

    def test_click_refresh(self):
        page = FakePage()
        self.assertTrue(bs._click_refresh(page))
        self.assertEqual(page.clicked, ["刷新"])
        self.assertFalse(bs._click_refresh(FakePage(click_ok=False)))


if __name__ == "__main__":
    unittest.main()
