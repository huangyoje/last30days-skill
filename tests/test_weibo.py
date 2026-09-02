"""Tests for the weibo module."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from lib import weibo


class TestStripHtml(unittest.TestCase):
    def test_tags_removed(self):
        self.assertEqual(weibo._strip_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_entities(self):
        self.assertEqual(weibo._strip_html("A &amp; B &quot;c&quot;"), 'A & B "c"')

    def test_empty(self):
        self.assertEqual(weibo._strip_html(None), "")


class TestParseCreatedAt(unittest.TestCase):
    def test_rfc2822_like(self):
        # Weibo's mobile search API returns this shape most of the time.
        result = weibo._parse_created_at("Mon Sep 01 12:34:56 +0800 2025")
        self.assertEqual(result, "2025-09-01")

    def test_iso_8601(self):
        result = weibo._parse_created_at("2025-09-01T12:34:56+08:00")
        self.assertEqual(result, "2025-09-01")

    def test_absolute_ymd(self):
        self.assertEqual(weibo._parse_created_at("2024-06-15"), "2024-06-15")

    def test_relative_minutes(self):
        with patch.object(weibo, "_today", return_value=datetime(2025, 9, 1, 12, 0, tzinfo=weibo._CST)):
            self.assertEqual(weibo._parse_created_at("30分钟前"), "2025-09-01")

    def test_relative_hours(self):
        with patch.object(weibo, "_today", return_value=datetime(2025, 9, 1, 12, 0, tzinfo=weibo._CST)):
            self.assertEqual(weibo._parse_created_at("2小时前"), "2025-09-01")

    def test_yesterday(self):
        with patch.object(weibo, "_today", return_value=datetime(2025, 9, 2, 12, 0, tzinfo=weibo._CST)):
            self.assertEqual(weibo._parse_created_at("昨天 08:30"), "2025-09-01")

    def test_just_now(self):
        with patch.object(weibo, "_today", return_value=datetime(2025, 9, 1, 12, 0, tzinfo=weibo._CST)):
            self.assertEqual(weibo._parse_created_at("刚刚"), "2025-09-01")

    def test_month_day_implicit_year(self):
        with patch.object(weibo, "_today", return_value=datetime(2025, 9, 15, 12, 0, tzinfo=weibo._CST)):
            self.assertEqual(weibo._parse_created_at("08-20"), "2025-08-20")

    def test_month_day_rolls_back_when_future(self):
        # A post seen in Feb showing "12-30" is from last year.
        with patch.object(weibo, "_today", return_value=datetime(2025, 2, 1, 12, 0, tzinfo=weibo._CST)):
            self.assertEqual(weibo._parse_created_at("12-30"), "2024-12-30")

    def test_empty_and_garbage(self):
        self.assertIsNone(weibo._parse_created_at(""))
        self.assertIsNone(weibo._parse_created_at(None))
        self.assertIsNone(weibo._parse_created_at("not a date"))


class TestInWindow(unittest.TestCase):
    def test_within(self):
        self.assertTrue(weibo._in_window("2024-06-15", "2024-06-01", "2024-06-30"))

    def test_outside(self):
        self.assertFalse(weibo._in_window("2024-05-01", "2024-06-01", "2024-06-30"))

    def test_unknown_kept(self):
        self.assertTrue(weibo._in_window(None, "2024-06-01", "2024-06-30"))


class TestIterMblogs(unittest.TestCase):
    def test_extracts_from_top_and_group(self):
        cards = [
            {"card_type": 9, "mblog": {"id": "1", "text": "top-level"}},
            {
                "card_type": 11,
                "card_group": [
                    {"card_type": 9, "mblog": {"id": "2", "text": "in-group"}},
                    {"card_type": 21, "mblog": {"id": "ignore"}},  # not a post
                ],
            },
            {"card_type": 5},  # no mblog
        ]
        out = weibo._iter_mblogs(cards)
        ids = [m["id"] for m in out]
        self.assertEqual(ids, ["1", "2"])


class TestNormalizeMblog(unittest.TestCase):
    def test_basic(self):
        raw = {
            "id": "4123456789",
            "text": '今日 <a href="/n/xxx">@某人</a> 讨论 <b>AI</b>',
            "user": {"screen_name": "tester", "id": 999},
            "attitudes_count": 42,
            "comments_count": 5,
            "reposts_count": 2,
            "created_at": "Mon Sep 01 12:34:56 +0800 2025",
        }
        item = weibo._normalize_mblog(raw)
        self.assertIsNotNone(item)
        self.assertEqual(item["id"], "4123456789")
        self.assertEqual(item["url"], "https://m.weibo.cn/detail/4123456789")
        self.assertEqual(item["author"], "tester")
        self.assertEqual(item["author_id"], "999")
        self.assertEqual(item["date"], "2025-09-01")
        self.assertEqual(item["engagement"]["likes"], 42)
        self.assertEqual(item["engagement"]["comments"], 5)
        self.assertEqual(item["engagement"]["reposts"], 2)
        self.assertIn("讨论", item["body"])
        # HTML must be stripped
        self.assertNotIn("<b>", item["body"])

    def test_long_text_preferred(self):
        raw = {
            "id": "1",
            "text": "short preview...",
            "longText": {"longTextContent": "This is the full long-text body content."},
            "user": {"screen_name": "a"},
        }
        item = weibo._normalize_mblog(raw)
        self.assertIn("full long-text body", item["body"])

    def test_missing_id_returns_none(self):
        self.assertIsNone(weibo._normalize_mblog({"text": "no id"}))

    def test_non_dict_returns_none(self):
        self.assertIsNone(weibo._normalize_mblog(None))
        self.assertIsNone(weibo._normalize_mblog("string"))


class TestSearchWeibo(unittest.TestCase):
    def _make_mblog(self, mid: str, text: str, likes: int = 10) -> dict:
        return {
            "id": mid,
            "text": text,
            "user": {"screen_name": f"user{mid}"},
            "attitudes_count": likes,
            "comments_count": 0,
            "reposts_count": 0,
            "created_at": "Mon Jun 15 12:34:56 +0800 2024",
        }

    @patch("lib.weibo._fetch_search")
    def test_relevant_only(self, mock_search):
        mock_search.side_effect = [
            # 综合 page 1
            [{"card_type": 9, "mblog": self._make_mblog("1", "AI 讨论 大模型")}],
            # 综合 page 2 (empty terminates)
            [],
            # 实时 page 1
            [{"card_type": 9, "mblog": self._make_mblog("2", "我家猫在睡觉")}],
            # 实时 page 2
            [],
        ]
        result = weibo.search_weibo(
            "AI", "2024-06-01", "2024-06-30",
            depth="quick", config={"WEIBO_COOKIE": "x=1"},
        )
        results = result["results"]
        # only mblog 1 mentions AI; mblog 2 (cat) should be filtered by relevance
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "1")

    @patch("lib.weibo._fetch_search")
    @patch("lib.weibo._try_visitor_pass")
    def test_no_cookie_falls_back_to_visitor_pass(self, mock_visitor, mock_search):
        # Reset module state so this test path is exercised.
        weibo._cookies_loaded = False
        weibo._visitor_attempted = False
        # Empty the jar to simulate a fresh process.
        weibo._cookie_jar.clear()
        # Visitor pass returns True and populates the jar for us.
        def _install(*_a, **_kw):
            weibo._set_cookie("SUB", "visitor-value", ".weibo.cn")
            return True
        mock_visitor.side_effect = _install
        mock_search.side_effect = [
            [{"card_type": 9, "mblog": self._make_mblog("1", "AI 讨论")}],
            [], [], [],
        ]
        result = weibo.search_weibo(
            "AI", "2024-06-01", "2024-06-30",
            depth="quick", config={},
        )
        # Visitor pass was tried; search then executed and produced a hit.
        mock_visitor.assert_called_once()
        self.assertEqual(len(result["results"]), 1)

    @patch("lib.weibo._fetch_search")
    @patch("lib.weibo._try_visitor_pass")
    def test_visitor_pass_failure_is_error(self, mock_visitor, mock_search):
        weibo._cookies_loaded = False
        weibo._visitor_attempted = False
        weibo._cookie_jar.clear()
        mock_visitor.return_value = False
        mock_search.return_value = []
        result = weibo.search_weibo("AI", "2024-06-01", "2024-06-30", config={})
        self.assertEqual(result["results"], [])
        self.assertIn("visitor pass", result["error"])
        # must not have hit the search endpoint when credentials unavailable
        mock_search.assert_not_called()

    @patch("lib.weibo._fetch_search")
    @patch("lib.weibo._try_visitor_pass")
    def test_user_cookie_skips_visitor_pass(self, mock_visitor, mock_search):
        weibo._cookies_loaded = False
        weibo._visitor_attempted = False
        weibo._cookie_jar.clear()
        mock_search.side_effect = [
            [{"card_type": 9, "mblog": self._make_mblog("1", "AI 讨论")}],
            [], [], [],
        ]
        result = weibo.search_weibo(
            "AI", "2024-06-01", "2024-06-30",
            depth="quick", config={"WEIBO_COOKIE": "SUB=real-user-cookie"},
        )
        # User cookie was loaded; visitor pass never invoked.
        mock_visitor.assert_not_called()
        self.assertEqual(len(result["results"]), 1)

    @patch("lib.weibo._fetch_search")
    def test_transport_error_envelope(self, mock_search):
        mock_search.side_effect = weibo.http.HTTPError("network down")
        result = weibo.search_weibo(
            "AI", "2024-06-01", "2024-06-30",
            config={"WEIBO_COOKIE": "x=1"},
        )
        self.assertEqual(result["results"], [])
        self.assertIn("network down", result["error"])

    def test_empty_topic(self):
        result = weibo.search_weibo("", "2024-06-01", "2024-06-30")
        self.assertEqual(result, {"results": []})

    @patch("lib.weibo._fetch_search")
    def test_date_window_filter(self, mock_search):
        # Mblog dated 2024-05-15 is outside the June window.
        mock_search.side_effect = [
            [{"card_type": 9, "mblog": {
                "id": "1",
                "text": "AI 讨论",
                "user": {"screen_name": "u"},
                "attitudes_count": 10,
                "created_at": "Wed May 15 12:00:00 +0800 2024",
            }}],
            [],
            [],
            [],
        ]
        result = weibo.search_weibo(
            "AI", "2024-06-01", "2024-06-30",
            depth="quick", config={"WEIBO_COOKIE": "x=1"},
        )
        self.assertEqual(result["results"], [])

    @patch("lib.weibo._fetch_search")
    def test_dedupe_across_tabs(self, mock_search):
        # Same mblog id in both 综合 and 实时: must appear once.
        mock_search.side_effect = [
            [{"card_type": 9, "mblog": self._make_mblog("1", "AI 讨论")}],
            [],
            [{"card_type": 9, "mblog": self._make_mblog("1", "AI 讨论")}],
            [],
        ]
        result = weibo.search_weibo(
            "AI", "2024-06-01", "2024-06-30",
            depth="quick", config={"WEIBO_COOKIE": "x=1"},
        )
        self.assertEqual(len(result["results"]), 1)


class TestParseWeiboResponse(unittest.TestCase):
    def test_parses_items(self):
        result = {
            "results": [
                {
                    "id": "1",
                    "title": "T",
                    "url": "https://m.weibo.cn/detail/1",
                    "author": "a",
                    "snippet": "s",
                    "body": "b",
                    "date": "2024-06-15",
                    "date_confidence": "high",
                    "relevance": 0.8,
                    "why_relevant": "overlap",
                    "engagement": {"likes": 10, "reposts": 2, "comments": 3},
                    "mblog_id": "1",
                    "author_id": "42",
                }
            ]
        }
        items = weibo.parse_weibo_response(result)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["container"], "微博")
        self.assertEqual(items[0]["metadata"]["mblog_id"], "1")
        self.assertEqual(items[0]["metadata"]["author_id"], "42")

    def test_empty(self):
        self.assertEqual(weibo.parse_weibo_response({"results": []}), [])
        self.assertEqual(weibo.parse_weibo_response(None), [])


if __name__ == "__main__":
    unittest.main()
