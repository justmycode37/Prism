import unittest

from prism_runtime.research_sources import normalize_imported_records


class ResearchSourceImportTests(unittest.TestCase):
    def test_supported_source_types_dates_and_canonical_duplicates(self):
        records = [
            {"source_kind": "news", "source_url": "https://news.test/item?utm_source=one",
             "title": "News", "content": "Brief report", "published_at": "Wed, 28 Oct 2026 10:00:00 GMT",
             "import_reference": "fixture-run-1"},
            {"source_kind": "news", "source_url": "https://news.test/item?utm_source=two",
             "title": "News update", "content": "A longer brief report", "published_at": "2026-10-28T12:00:00+02:00",
             "import_reference": "fixture-run-2"},
            {"source_kind": "article", "source_url": "https://publisher.test/article",
             "title": "Article", "content": "Full article", "published_at": "2026-10-28T10:30:00Z"},
            {"source_kind": "youtube_transcript", "source_url": "https://youtu.be/abcdefghijk",
             "title": "Video", "content": "Transcript text", "published_at": "2026-10-28T10:00:00-04:00"},
        ]
        result = normalize_imported_records(records, provider="fixture")

        self.assertEqual(result["errors"], [])
        self.assertEqual([item["source_kind"] for item in result["evidence"]],
                         ["news", "article", "youtube_transcript"])
        news = result["evidence"][0]
        self.assertEqual(news["source_url"], "https://news.test/item")
        self.assertEqual(news["content"], "A longer brief report")
        self.assertEqual(news["published_at"], "2026-10-28T10:00:00Z")
        self.assertEqual(news["import_references"], ["fixture-run-1", "fixture-run-2"])
        transcript = result["evidence"][2]
        self.assertEqual(transcript["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(transcript["published_at"], "2026-10-28T14:00:00Z")
        self.assertEqual(transcript["source_origin"], "imported")

    def test_content_truncation_is_explicit(self):
        result = normalize_imported_records([{
            "source_kind": "article", "source_url": "https://publisher.test/article",
            "title": "Long", "content": "0123456789abcdef",
        }], provider="fixture", max_content_chars=6)

        item = result["evidence"][0]
        self.assertEqual(item["content"], "012345")
        self.assertTrue(item["content_truncated"])
        self.assertEqual(item["original_content_chars"], 16)

    def test_item_and_total_byte_budgets_are_bounded(self):
        first = {"source_kind": "news", "source_url": "https://news.test/a",
                 "title": "A", "content": "one"}
        second = {"source_kind": "news", "source_url": "https://news.test/b",
                  "title": "B", "content": "two"}
        result = normalize_imported_records([first, second], provider="fixture", max_total_bytes=45)
        self.assertEqual(len(result["evidence"]), 1)
        self.assertTrue(any("byte budget" in error for error in result["errors"]))

        capped = normalize_imported_records([first, second], provider="fixture", max_items=1)
        self.assertEqual(len(capped["evidence"]), 1)
        self.assertTrue(any("item budget" in error for error in capped["errors"]))

    def test_invalid_kind_url_and_date_do_not_become_unattributed_evidence(self):
        result = normalize_imported_records([
            {"source_kind": "transcript", "source_url": "https://youtube.com/watch?v=abcdefghijk",
             "title": "Guess", "content": "Not normalized"},
            {"source_kind": "youtube_transcript", "source_url": "https://youtube.com/playlist?list=x",
             "title": "Playlist", "content": "Not a single video"},
            {"source_kind": "news", "source_url": "https://news.test/item", "title": "Dated",
             "content": "Content", "published_at": "not a date"},
        ], provider="fixture")

        self.assertEqual(len(result["evidence"]), 1)
        self.assertIsNone(result["evidence"][0]["published_at"])
        self.assertGreaterEqual(len(result["errors"]), 3)


if __name__ == "__main__":
    unittest.main()
