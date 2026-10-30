import json
import unittest

from prism_runtime.monid import (
    API_BASE_URL,
    DISCOVER_PATH,
    INSPECT_PATH,
    NEWS_ENDPOINT,
    NEWS_PROVIDER,
    RUN_PATH,
    YOUTUBE_ENDPOINT,
    YOUTUBE_PROVIDER,
    MonidClient,
    MonidError,
    MonidHTTPResponse,
    MonidNewsRequest,
    MonidYouTubeTranscriptRequest,
    is_terminal_run_status,
)


def json_response(value, status=200, headers=None):
    return MonidHTTPResponse(status, json.dumps(value).encode(), headers or {})


class FixtureTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout, max_response_bytes):
        self.calls.append((method, url, dict(headers), body, timeout, max_response_bytes))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MonidClientTests(unittest.TestCase):
    def test_discover_and_inspect_use_current_free_routes_with_auth_isolated(self):
        key = "monid_live_private_fixture"
        transport = FixtureTransport(json_response({"items": [], "total": 0}),
                                     json_response({"provider": "apify", "endpoint": "/demo"}))
        client = MonidClient(lambda: key, transport)

        self.assertEqual(client.discover("youtube transcript"), {"items": [], "total": 0})
        self.assertEqual(client.inspect("apify", "/demo")["endpoint"], "/demo")
        self.assertEqual([call[0:2] for call in transport.calls], [
            ("POST", API_BASE_URL + DISCOVER_PATH),
            ("POST", API_BASE_URL + INSPECT_PATH),
        ])
        self.assertEqual(json.loads(transport.calls[0][3]), {"q": "youtube transcript", "limit": 5})
        self.assertEqual(json.loads(transport.calls[1][3]), {"provider": "apify", "endpoint": "/demo"})
        for _, url, headers, body, _, _ in transport.calls:
            self.assertEqual(headers["Authorization"], "Bearer " + key)
            self.assertNotIn(key, url)
            self.assertNotIn(key, body.decode())
            self.assertEqual(set(headers), {"Authorization", "Accept", "Content-Type"})

    def test_discovery_and_inspection_are_bounded_before_auth_or_transport(self):
        calls = []
        key_calls = []
        client = MonidClient(lambda: key_calls.append(True) or "key", lambda *args: calls.append(args),
                             max_request_bytes=256)
        with self.assertRaises(MonidError):
            client.discover("x" * 1000)
        with self.assertRaises(MonidError):
            client.discover("news", limit=21)
        with self.assertRaises(MonidError):
            client.inspect("", "/demo")
        self.assertEqual(calls, [])
        self.assertEqual(key_calls, [])

    def test_paid_batch_requires_enablement_and_rejects_arbitrary_plans(self):
        calls = []
        key_calls = []
        client = MonidClient(lambda: key_calls.append(True) or "key", lambda *args: calls.append(args))
        news = MonidNewsRequest("markets")
        with self.assertRaisesRegex(MonidError, "disabled"):
            client.run_batch([news], enabled=False)
        with self.assertRaisesRegex(MonidError, "Unsupported"):
            client.run_batch([{"provider": "any", "endpoint": "/paid-tool"}], enabled=True)
        self.assertEqual(calls, [])
        self.assertEqual(key_calls, [])

    def test_verified_news_and_single_video_requests_match_runrequest_schema(self):
        transcript_data = {"transcript": "fixture text", "source_url": "https://youtube.com/watch?v=abcdefghijk"}
        transport = FixtureTransport(
            json_response({"runId": "01NEWS", "provider": NEWS_PROVIDER, "endpoint": NEWS_ENDPOINT,
                           "status": "RUNNING", "price": {"type": "PER_RESULT"}, "createdAt": "now"}, 202),
            json_response({"runId": "01VIDEO", "provider": YOUTUBE_PROVIDER, "endpoint": YOUTUBE_ENDPOINT,
                           "status": "COMPLETED", "cost": {"value": 0.12, "currency": "USD"},
                           "price": {"type": "PER_CALL"}, "output": transcript_data,
                           "providerResponse": {"httpStatus": 200}, "createdAt": "now"}),
        )
        client = MonidClient(lambda: "test-key", transport)
        receipts = client.run_batch([
            MonidNewsRequest("semiconductor supply", max_results=5, crawl_results=2, time_range="week"),
            MonidYouTubeTranscriptRequest("https://www.youtube.com/watch?v=abcdefghijk"),
        ], enabled=True)

        self.assertEqual(len(receipts), 2)
        news_payload = json.loads(transport.calls[0][3])
        self.assertEqual(news_payload, {
            "provider": "search1api",
            "endpoint": "/news",
            "input": {"body": {
                "query": "semiconductor supply", "max_results": 5,
                "crawl_results": 2, "time_range": "week",
            }},
        })
        youtube_payload = json.loads(transport.calls[1][3])
        self.assertEqual(youtube_payload, {
            "provider": "apify",
            "endpoint": "/starvibe/youtube-video-transcript",
            "input": {"body": {
                "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "language": "en", "include_transcript_text": True,
            }},
        })
        self.assertTrue(all(call[0] == "POST" and call[1] == API_BASE_URL + RUN_PATH
                            for call in transport.calls))
        self.assertEqual(receipts[0].run_id, "01NEWS")
        self.assertEqual(receipts[0].status, "RUNNING")
        self.assertFalse(receipts[0].is_terminal)
        self.assertEqual(receipts[0].provenance["input"], news_payload["input"])
        self.assertEqual(receipts[1].run_id, "01VIDEO")
        self.assertEqual(receipts[1].cost, {"value": 0.12, "currency": "USD"})
        self.assertEqual(receipts[1].response["price"], {"type": "PER_CALL"})
        self.assertEqual(receipts[1].response["providerResponse"], {"httpStatus": 200})
        self.assertEqual(receipts[1].response["output"], transcript_data)
        self.assertEqual(receipts[1].provenance["provider"], "apify")

    def test_batch_request_and_item_limits_are_checked_before_any_run_or_key_load(self):
        calls = []
        key_calls = []
        client = MonidClient(lambda: key_calls.append(True) or "key", lambda *args: calls.append(args),
                             max_batch_requests=1, max_batch_items=5)
        news = MonidNewsRequest("news", max_results=5)
        video = MonidYouTubeTranscriptRequest("https://youtu.be/abcdefghijk")
        with self.assertRaisesRegex(MonidError, "request limit"):
            client.run_batch([news, video], enabled=True)
        with self.assertRaisesRegex(MonidError, "item limit"):
            client.run_batch([MonidNewsRequest("news", max_results=5, crawl_results=1)], enabled=True)
        self.assertEqual(calls, [])
        self.assertEqual(key_calls, [])

    def test_request_and_response_bytes_and_transport_timeout_are_bounded(self):
        calls = []
        transport = FixtureTransport(json_response({"runId": "unused"}))
        client = MonidClient(lambda: "key", transport, max_request_bytes=256,
                             max_response_bytes=128, timeout_seconds=2.5)
        with self.assertRaisesRegex(MonidError, "request exceeds byte limit"):
            client.run_batch([MonidNewsRequest("x" * 1000)], enabled=True)
        self.assertEqual(transport.calls, [])

        oversized = MonidHTTPResponse(200, b"{" + b" " * 129 + b"}")
        response_transport = FixtureTransport(oversized)
        response_client = MonidClient(lambda: "key", response_transport, max_response_bytes=128,
                                      timeout_seconds=2.5)
        with self.assertRaisesRegex(MonidError, "response exceeds byte limit"):
            response_client.discover("news")
        self.assertEqual(response_transport.calls[0][4:], (2.5, 128))

    def test_redirect_is_refused_without_following_location(self):
        transport = FixtureTransport(MonidHTTPResponse(
            302, b"", {"location": "https://elsewhere.invalid/steal"}))
        client = MonidClient(lambda: "key", transport)
        with self.assertRaisesRegex(MonidError, "redirects are refused") as caught:
            client.discover("news")
        self.assertEqual(caught.exception.status_code, 302)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], API_BASE_URL + DISCOVER_PATH)

    def test_run_status_lookup_is_one_shot_and_preserves_timeout_cost_and_input(self):
        run_id = "01JABCDEF0123456789ABCDEFG"
        raw = {
            "runId": run_id, "provider": "apify", "endpoint": YOUTUBE_ENDPOINT,
            "status": "TIMED_OUT", "cost": {"value": 0, "currency": "USD"},
            "input": {"body": {"youtube_url": "https://youtu.be/abcdefghijk"}},
            "providerResponse": {"httpStatus": 408}, "createdAt": "now",
        }
        transport = FixtureTransport(json_response(raw))
        receipt = MonidClient(lambda: "key", transport).get_run(run_id)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][0], "GET")
        self.assertEqual(transport.calls[0][1], API_BASE_URL + "/v1/runs/" + run_id)
        self.assertEqual(receipt.run_id, run_id)
        self.assertEqual(receipt.cost, {"value": 0, "currency": "USD"})
        self.assertTrue(receipt.is_terminal)
        self.assertEqual(receipt.provenance["input"], raw["input"])
        self.assertEqual(receipt.response, raw)

    def test_terminal_failure_block_and_both_timeout_spellings_are_terminal(self):
        for status in ("BLOCKED", "FAILED", "TIME_OUT", "TIMED_OUT", "COMPLETED", "STOPPED"):
            with self.subTest(status=status):
                self.assertTrue(is_terminal_run_status(status))
        for status in ("READY", "RUNNING", "STOPPING", "unknown"):
            with self.subTest(status=status):
                self.assertFalse(is_terminal_run_status(status))

    def test_non_success_run_record_is_preserved_and_transport_errors_are_scrubbed(self):
        blocked = {"runId": "01BLOCKED", "provider": "search1api", "endpoint": NEWS_ENDPOINT,
                   "status": "BLOCKED", "reason": "workspace control", "controls": [],
                   "price": {"type": "PER_CALL"}}
        receipt = MonidClient(lambda: "key", FixtureTransport(json_response(blocked, 200))).run_batch(
            [MonidNewsRequest("news")], enabled=True)[0]
        self.assertEqual(receipt.response, blocked)
        self.assertTrue(receipt.is_terminal)

        secret = "monid_live_should_not_leak"
        transport = FixtureTransport(RuntimeError("credential=" + secret))
        with self.assertRaises(MonidError) as caught:
            MonidClient(lambda: secret, transport).discover("news")
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_paid_run_transport_failure_is_never_retried(self):
        transport = FixtureTransport(TimeoutError("fixture timeout"))
        client = MonidClient(lambda: "key", transport)
        with self.assertRaisesRegex(MonidError, "transport failed"):
            client.run_batch([MonidNewsRequest("one request")], enabled=True)
        self.assertEqual(len(transport.calls), 1)

    def test_single_youtube_url_validation_rejects_arbitrary_or_multiple_videos(self):
        for value in (
            "http://youtu.be/abcdefghijk",
            "https://evil.example/watch?v=abcdefghijk",
            "https://youtube.com/watch?v=abcdefghijk&v=zzzzzzzzzzz",
            "https://youtube.com/playlist?list=abcdefghijk",
            "https://user@youtube.com/watch?v=abcdefghijk",
        ):
            with self.subTest(value=value), self.assertRaises(MonidError):
                MonidYouTubeTranscriptRequest(value)
        self.assertEqual(MonidYouTubeTranscriptRequest(
            "https://youtu.be/abcdefghijk").body()["youtube_url"], "https://youtu.be/abcdefghijk")


if __name__ == "__main__":
    unittest.main()
