import unittest

from naver_map_crawler import (
    BLOG_REVIEW_RE,
    VISITOR_REVIEW_RE,
    extract_place_id,
    parse_count,
    parse_menu_lines,
)


class NaverMapParserTests(unittest.TestCase):
    def test_extract_place_id(self):
        self.assertEqual(extract_place_id("https://map.naver.com/p/entry/place/123456"), "123456")
        self.assertEqual(extract_place_id("https://pcmap.place.naver.com/restaurant/998877/home"), "998877")

    def test_parse_review_counts(self):
        text = "방문자 리뷰 1,234 블로그 리뷰 567"
        self.assertEqual(parse_count(VISITOR_REVIEW_RE, text), 1234)
        self.assertEqual(parse_count(BLOG_REVIEW_RE, text), 567)

    def test_parse_menu_lines(self):
        text = """
        메뉴
        아메리카노
        4,500원
        카페라떼
        5,500원
        방문자 리뷰 120
        """
        self.assertEqual(
            parse_menu_lines(text),
            [
                {"name": "아메리카노", "price": "4,500원"},
                {"name": "카페라떼", "price": "5,500원"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
