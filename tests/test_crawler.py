import unittest

from crawler import item_doc_id, parse_items


class CrawlerParserTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "key": "demo",
            "url": "https://example.com/base/",
            "item_selector": "article",
            "fields": {
                "title": {"selector": "h2", "type": "text", "required": True},
                "url": {
                    "selector": "a",
                    "type": "attr",
                    "attr": "href",
                    "required": True,
                    "absolute_url": True,
                },
                "imageUrl": {
                    "selector": "img",
                    "type": "attr",
                    "attr": "src",
                    "absolute_url": True,
                },
            },
        }

    def test_parse_items_and_absolute_urls(self):
        html = """
        <main>
          <article><h2>One</h2><a href="/one">Open</a><img src="img/a.png"></article>
          <article><h2>Two</h2><a href="two">Open</a></article>
        </main>
        """
        items = parse_items(self.source, html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "One")
        self.assertEqual(items[0]["url"], "https://example.com/one")
        self.assertEqual(items[0]["imageUrl"], "https://example.com/base/img/a.png")
        self.assertEqual(items[1]["url"], "https://example.com/base/two")

    def test_required_field_drops_invalid_item(self):
        html = "<article><h2>Missing URL</h2></article>"
        self.assertEqual(parse_items(self.source, html), [])

    def test_doc_id_is_stable(self):
        item = {"title": "One", "url": "https://example.com/one"}
        self.assertEqual(item_doc_id("demo", item), item_doc_id("demo", dict(item)))
        self.assertEqual(len(item_doc_id("demo", item)), 40)


if __name__ == "__main__":
    unittest.main()
