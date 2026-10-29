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
        <item><title>Bitcoin liquidity rises</title><link>https://public.test/a?utm_source=x</link>
        <description>&lt;p&gt;Bitcoin volume up.&lt;/p&gt;&lt;script&gt;evil()&lt;/script&gt;</description>
        <pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate></item>
        <item><title>Bitcoin liquidity rises</title><link>https://public.test/a?utm_source=y</link><description>Bitcoin volume up.</description></item>
        </channel></rss>"""
        collector = FixtureCollector({"/robots.txt": document("User-agent: *\nAllow: /", "text/plain"),
                                      "/rss": document(rss, "application/rss+xml")})
        result = collector.collect({"id": "c", "topic": "liquidity", "sources": ["https://public.test/rss"]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["evidence"]), 2)
        item = result["evidence"][0]
        self.assertEqual(item["source_url"], "https://public.test/a")
        self.assertNotIn("evil", item["content"])
        self.assertEqual(item["symbols"], ["BTCUSDT"])
        self.assertEqual(item["published_at"], "2026-10-27T12:00:00Z")
        self.assertTrue(all(address == "93.184.216.34" for _, address in collector.calls))

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
        now = int(time.time() * 1000)
        hour = 3_600_000
        start = now // hour * hour
        rows = [[str(at), "100", "105", "99", "102", "20", "2000"]
                for at in (start, start - hour, start - 2 * hour)]
        ticker = {"retCode": 0, "time": now - 90_000, "result": {"list": [
            {"symbol": "BTCUSDT", "lastPrice": "102", "price24hPcnt": ".02", "fundingRate": ".0001"}]}}
        candles = {"retCode": 0, "time": now, "result": {"list": rows}}
        collector = FixtureCollector({
            "/v5/market/tickers?category=spot&symbol=BTCUSDT": document(json.dumps(ticker), "application/json"),
            "/v5/market/kline?category=spot&symbol=BTCUSDT&interval=60&limit=1000": document(json.dumps(candles), "application/json"),
        })
        result = collector.collect({"symbol": "BTCUSDT", "sources": []})
        self.assertEqual(result["errors"], [])
        market = result["markets"][0]
        self.assertEqual(market["as_of_ms"], now - 90_000)
        self.assertEqual(market["change_24h"], .02)
        self.assertIsNone(market["funding_rate"])
        self.assertEqual([row["start_ms"] for row in market["candles"]], [start - 2 * hour, start - hour])
        self.assertTrue(all(row["closed"] and row["end_ms"] <= now for row in market["candles"]))
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(len(collector.collect_markets(["BTCUSDT"])), 1)
        self.assertEqual(len(collector.calls), 2)

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
