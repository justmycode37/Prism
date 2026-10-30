from concurrent.futures import ThreadPoolExecutor
import json
import io
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from prism_runtime.ingestion import CollectionError, SourceCollector, _Budget


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def document(body, content_type="text/html", status=200, **headers):
    return {"status": status, "headers": {"content-type": content_type, **headers},
            "body": body.encode() if isinstance(body, str) else body}


class FixtureCollector(SourceCollector):
    def __init__(self, routes, settings=None):
        super().__init__({"sources": [], "ingestion": {"crawl_articles": False, **(settings or {})}})
        self.host_delay = 0
        self.routes, self.calls = routes, []
        self.call_lock = threading.Lock()

    def _wire_get(self, url, host, port, address, timeout):
        with self.call_lock:
            self.calls.append((url, address))
        parsed = urlsplit(url)
        key = parsed.path + ("?" + parsed.query if parsed.query else "")
        value = self.routes.get(url, self.routes.get(key, document("", status=404)))
        if isinstance(value, Exception):
            raise value
        return {**value, "headers": dict(value["headers"])}


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.dns = patch("prism_runtime.ingestion.socket.getaddrinfo", public_dns)
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def test_rss_deduplicates_trackers_strips_scripts_and_ranks_topic(self):
        rss = """<rss><channel>
        <item><title>Unrelated release</title><link>https://public.test/other</link><description>Other.</description></item>
        <item><title>NASDAQ: MRVL AI liquidity rises</title><link>https://public.test/a?utm_source=x</link>
        <description>&lt;p&gt;MRVL AI infrastructure volume up.&lt;/p&gt;&lt;script&gt;evil()&lt;/script&gt;</description>
        <pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate></item>
        <item><title>NASDAQ: MRVL AI liquidity rises</title><link>https://public.test/a?utm_source=y</link><description>MRVL AI infrastructure volume up.</description></item>
        </channel></rss>"""
        collector = FixtureCollector({"/robots.txt": document("User-agent: *\nAllow: /", "text/plain"),
                                      "/rss": document(rss, "application/rss+xml")})
        result = collector.collect({"id": "c", "topic": "liquidity", "sources": ["https://public.test/rss"]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["evidence"]), 2)
        item = result["evidence"][0]
        self.assertEqual(item["source_url"], "https://public.test/a")
        self.assertNotIn("evil", item["content"])
        self.assertEqual(item["symbols"], ["MRVL"])
        self.assertEqual(item["published_at"], "2026-10-27T12:00:00Z")
        self.assertTrue(all(address == "93.184.216.34" for _, address in collector.calls))

    def test_bing_tracking_wrappers_preserve_canonical_publisher_provenance(self):
        rss = '<rss><channel><item><title>AI data centers</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fpublisher.test%2Freport%3Futm_source%3Dfeed</link><description>AI capacity expansion</description></item></channel></rss>'
        collector = FixtureCollector({'/rss': document(rss, 'application/rss+xml')})
        result = collector.collect({'sources': ['https://public.test/rss']})
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['evidence'][0]['source_url'], 'https://publisher.test/report')

    def test_youtube_channel_feed_keeps_video_provenance_without_guessing_transcript(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
          <entry><id>yt:video:abcdefghijk</id><yt:videoId>abcdefghijk</yt:videoId>
            <title>AI data center power outlook</title>
            <link rel="alternate" href="https://www.youtube.com/watch?v=abcdefghijk&amp;feature=feed"/>
            <published>2026-10-28T10:30:00-04:00</published><summary>Channel description only.</summary>
          </entry></feed>'''
        collector = FixtureCollector({"/videos.xml": document(feed, "application/atom+xml")},
                                     {"crawl_articles": True})
        result = collector.collect({"sources": [{"url": "https://public.test/videos.xml", "kind": "atom"}]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["evidence"]), 1)
        item = result["evidence"][0]
        self.assertEqual(item["source_kind"], "youtube")
        self.assertEqual(item["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(item["published_at"], "2026-10-28T14:30:00Z")
        self.assertEqual(item["discovered_from"], ["https://public.test/videos.xml"])
        self.assertEqual(item["transcript_status"], "not_in_public_feed")
        self.assertFalse(any("youtube.com/watch" in url for url, _ in collector.calls))

    def test_feed_private_source_links_are_skipped_without_fetching_them(self):
        feed = '''<rss><channel>
          <item><title>Private destination</title><link>https://127.0.0.1/private</link><description>bad</description></item>
          <item><title>Public report</title><link>https://publisher.test/report</link><description>good</description></item>
        </channel></rss>'''
        collector = FixtureCollector({"/feed": document(feed, "application/rss+xml")})
        result = collector.collect({"sources": ["https://public.test/feed"]})
        self.assertEqual([item["source_url"] for item in result["evidence"]], ["https://publisher.test/report"])
        self.assertTrue(any("Feed entry skipped" in error for error in result["errors"]))
        self.assertFalse(any("127.0.0.1" in url for url, _ in collector.calls))

    def test_news_dedup_merges_feed_provenance_and_html_uses_article_metadata(self):
        duplicate = '''<rss><channel><item><title>MRVL AI capacity update</title>
          <link>https://publisher.test/story?utm_source=feed</link><description>Capacity expands.</description>
          <pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'''
        collector = FixtureCollector({"/one": document(duplicate, "application/rss+xml"),
                                     "/two": document(duplicate, "application/rss+xml"),
                                     "/story": document('<html><head><title>MRVL capacity</title>'
                                         '<meta property="article:published_time" content="2026-10-28T11:00:00Z">'
                                         '<meta name="description" content="Long-form public article."></head>'
                                         '<body><nav>Navigation boilerplate</nav><main>MRVL adds power and AI capacity.</main></body></html>')},
                                    {"crawl_articles": True, "articles_per_feed": 1})
        result = collector.collect({"symbols": ["MRVL"], "sources": [
            {"url": "https://public.test/one", "kind": "rss"},
            {"url": "https://public.test/two", "kind": "rss"},
        ]})
        public_story = [item for item in result["evidence"] if item["source_url"] == "https://publisher.test/story"]
        self.assertEqual(len(public_story), 1)
        item = public_story[0]
        self.assertEqual(item["source_kind"], "article")
        self.assertEqual(set(item["discovered_from"]), {"https://public.test/one", "https://public.test/two"})
        self.assertEqual(item["published_at"], "2026-10-28T11:00:00Z")
        self.assertNotIn("Navigation boilerplate", item["content"])

    def test_normalized_imports_are_attached_to_collect_with_provenance(self):
        collector = FixtureCollector({})
        result = collector.collect({"sources": [], "imported_source_provider": "adapter-fixture",
                                    "imported_sources": [{
                                        "source_kind": "youtube_transcript",
                                        "source_url": "https://youtu.be/abcdefghijk",
                                        "title": "Transcript",
                                        "content": "Imported transcript text.",
                                        "published_at": "2026-10-29T09:00:00+02:00",
                                        "import_reference": "fixture-run-1",
                                    }]})
        self.assertEqual(result["errors"], [])
        item = result["evidence"][0]
        self.assertEqual(item["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(item["source_kind"], "youtube_transcript")
        self.assertEqual(item["source_origin"], "imported")
        self.assertEqual(item["import_provider"], "adapter-fixture")
        self.assertEqual(item["import_reference"], "fixture-run-1")
        self.assertEqual(item["published_at"], "2026-10-29T07:00:00Z")

    def test_evidence_identity_versions_source_content_without_observation_clock(self):
        collector = FixtureCollector({})
        first = collector._evidence("https://public.test/article", "article", "Title", "Original", {})
        unchanged = collector._evidence("https://public.test/article?utm_source=x", "article", "Title", "Original", {})
        changed = collector._evidence("https://public.test/article", "article", "Title", "Revised", {})
        self.assertEqual(first["id"], unchanged["id"])
        self.assertNotEqual(first["id"], changed["id"])

    def test_atom_relative_links_and_article_expansion(self):
        atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Ethereum release</title>
        <link href="/release"/><updated>2026-10-28T12:00:00Z</updated><summary>Initial summary</summary></entry></feed>"""
        collector = FixtureCollector({"/feed": document(atom, "application/atom+xml"),
                                      "/release": document("<title>Protocol release</title><main>Ethereum protocol details.</main>")},
                                     {"crawl_articles": True})
        result = collector.collect({"sources": [{"url": "https://public.test/feed", "kind": "atom"}]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["evidence"][0]["source_kind"], "article")
        self.assertIn("Ethereum protocol details", result["evidence"][0]["content"])

    def test_robots_blocked_html_never_requested_and_failure_isolated(self):
        collector = FixtureCollector({"/robots.txt": document("User-agent: *\nDisallow: /private", "text/plain"),
                                      "/good": document("<title>Allowed</title>Public content")})
        result = collector.collect({"sources": [{"url": "https://public.test/private", "kind": "article"},
                                                {"url": "https://public.test/good", "kind": "article"}]})
        self.assertEqual(len(result["evidence"]), 1)
        self.assertTrue(any("robots.txt" in error for error in result["errors"]))
        self.assertNotIn("https://public.test/private", [url for url, _ in collector.calls])

    def test_cross_protocol_and_private_redirects_blocked_before_fetch(self):
        for destination in ("http://public.test/next", "https://127.0.0.1/next"):
            with self.subTest(destination=destination):
                collector = FixtureCollector({"/start": document("", status=302, location=destination)})
                def dns(host, port, **kwargs):
                    address = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
                with patch("prism_runtime.ingestion.socket.getaddrinfo", dns):
                    with self.assertRaises(CollectionError):
                        collector._fetch("https://public.test/start", _Budget(5, 10, 2))
                self.assertEqual(len(collector.calls), 1)

    def test_dns_answer_with_any_private_address_rejected(self):
        collector = FixtureCollector({})
        answers = public_dns("public.test", 443) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch("prism_runtime.ingestion.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(CollectionError):
                collector._fetch("https://public.test/start", _Budget(5, 10, 2))
        self.assertEqual(collector.calls, [])

    def test_cache_singleflight_shared_across_parallel_collectors(self):
        collector = FixtureCollector({"/article": document("<title>One</title>Only one network request")})
        agent = {"sources": [{"url": "https://public.test/article", "kind": "article"}]}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: collector.collect(agent), range(6)))
        self.assertTrue(all(len(result["evidence"]) == 1 for result in results))
        self.assertEqual([url for url, _ in collector.calls].count("https://public.test/article"), 1)
        self.assertEqual([url for url, _ in collector.calls].count("https://public.test/robots.txt"), 1)

    def test_crawl_is_same_host_and_page_budget_bounded(self):
        collector = FixtureCollector({"/root": document('<a href="/a">A</a><a href="/b">B</a><a href="https://outside.test/x">X</a>'),
                                      "/a": document("<title>A</title>Details"), "/b": document("B")},
                                     {"max_pages": 2, "pages_per_site": 8})
        result = collector.collect({"sources": [{"url": "https://public.test/root", "kind": "article"}]})
        self.assertEqual(len(result["evidence"]), 2)
        self.assertFalse(any("outside.test" in url for url, _ in collector.calls))
        self.assertTrue(any("pages budget" in error for error in result["errors"]))

    def test_market_closed_chronological_candles_and_quote_freshness(self):
        now = int(time.time())
        ticker = {'chart': {'result': [{'meta': {
            'symbol': 'MRVL', 'quoteType': 'EQUITY', 'currency': 'USD',
            'exchangeTimezoneName': 'America/New_York', 'regularMarketTime': now - 90,
            'regularMarketPrice': 102, 'previousClose': 100},
            'timestamp': [now - 3 * 86400, now - 2 * 86400],
            'indicators': {'quote': [{'open': [100, 101], 'high': [103, 104],
                'low': [99, 100], 'close': [102, 103], 'volume': [20, 30]}]}}]}}
        collector = FixtureCollector({
            '/v8/finance/chart/MRVL?range=5y&interval=1d&events=div%2Csplits': document(json.dumps(ticker), 'application/json'),
            '/v8/finance/chart/MRVL?range=1d&interval=1d&events=div%2Csplits': document(json.dumps(ticker), 'application/json')})
        result = collector.collect({'symbol': 'MRVL', 'sources': []})
        self.assertEqual(result['errors'], [])
        market = result['markets'][0]
        self.assertEqual(market['as_of_ms'], (now - 90) * 1000)
        self.assertAlmostEqual(market['change_24h'], .02)
        self.assertIsNone(market['funding_rate'])
        self.assertEqual(len(market['candles']), 2)
        self.assertEqual({row['source_kind'] for row in result['evidence']}, {'market', 'chart'})
        self.assertEqual(len(collector.collect_markets(['MRVL'])), 1)
        self.assertEqual(len(collector.calls), 2)
        refreshed = collector.collect_markets(['MRVL'])[0]
        self.assertTrue(refreshed['quote_only'])
        self.assertNotIn('candles', refreshed)
        self.assertEqual(len(collector.calls), 2)  # Both source URLs retain single-flight caches.

    def test_collector_shards_cover_the_full_discovered_universe(self):
        from prism_runtime.config import default_config
        from prism_runtime.domain import roster
        config = default_config()
        config['symbols'] = ['S' + str(i) for i in range(80)]
        config['sources'] = []
        collector = SourceCollector(config)
        seen = set()
        def market(symbol, *_args):
            seen.add(symbol)
            return {'markets': [], 'evidence': []}
        with patch.object(collector, '_market', side_effect=market):
            for agent in roster(config):
                if agent['layer'] == 'collector':
                    collector.collect(agent)
        self.assertEqual(seen, set(config['symbols']))

    def test_discovery_keeps_observable_seed_fallback_when_catalogues_fail(self):
        from prism_runtime.config import default_config
        config = default_config()
        config['sources'] = []
        collector = SourceCollector(config)
        with patch.object(collector, '_fetch', side_effect=CollectionError('Unavailable')):
            result = collector.discover_universe()
        self.assertTrue(result['errors'])
        self.assertTrue(set(config['sector']['seed_symbols']).issubset({row['symbol'] for row in result['companies']}))

    def test_feed_entity_declarations_rejected(self):
        collector = FixtureCollector({"/rss": document('<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>', "application/rss+xml")})
        result = collector.collect({"sources": ["https://public.test/rss"]})
        self.assertEqual(result["evidence"], [])
        self.assertTrue(any("entity declarations" in error for error in result["errors"]))

    def test_wire_transport_pins_address_and_honors_connection_close(self):
        class FakeSocket:
            def __init__(self, wire):
                self.wire, self.sent, self.timeouts = wire, b"", []
                self.file = None
            def makefile(self, *args, **kwargs):
                self.file = io.BytesIO(self.wire)
                return self.file
            def sendall(self, value):
                self.sent += value
            def settimeout(self, value):
                if self.file is not None and self.file.closed:
                    raise OSError(9, "Bad file descriptor")
                self.timeouts.append(value)
            def close(self):
                pass
        wire = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
        sock = FakeSocket(wire)
        collector = SourceCollector({})
        with patch("prism_runtime.ingestion.socket.create_connection", return_value=sock) as connect:
            result = collector._wire_get("http://public.test/article?q=1", "public.test", 80, "93.184.216.34", 2)
        connect.assert_called_once_with(("93.184.216.34", 80), timeout=2)
        self.assertEqual(result["body"], b"hello")
        self.assertIn(b"Host: public.test", sock.sent)
        self.assertIn(b"GET /article?q=1 HTTP/1.1", sock.sent)
        self.assertTrue(all(0 < value <= 2 for value in sock.timeouts))

    def test_wire_transport_rejects_streamed_oversize_and_compressed_bodies(self):
        class FakeSocket:
            def __init__(self, wire):
                self.wire = wire
            def makefile(self, *args, **kwargs):
                return io.BytesIO(self.wire)
            def sendall(self, value):
                pass
            def settimeout(self, value):
                pass
            def close(self):
                pass
        collector = SourceCollector({"ingestion": {"max_response_bytes": 1024}})
        for wire in (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + b"x" * 2048,
                     b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 1\r\n\r\nx"):
            with self.subTest(wire=wire[:80]):
                with patch("prism_runtime.ingestion.socket.create_connection", return_value=FakeSocket(wire)):
                    with self.assertRaises(CollectionError):
                        collector._wire_get("http://public.test/", "public.test", 80, "93.184.216.34", 2)


if __name__ == "__main__":
    unittest.main()
