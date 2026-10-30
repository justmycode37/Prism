"""Bounded host-side client for Monid discovery and explicit data runs.

The current API contract is documented at https://monid.ai/openapi.json.
This client never accepts an arbitrary provider/endpoint for execution: paid
runs are limited to the verified Search1API news and single-video transcript
contracts below. The CLI guide is at https://monid.ai/.

Discovery and inspection require an API bearer key but do not start a paid run.
Paid runs are opt-in per batch, and this client never retries or polls them.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import io
import json
import re
import socket
import ssl
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit


API_HOST = "api.monid.ai"
API_BASE_URL = f"https://{API_HOST}"
OPENAPI_URL = "https://monid.ai/openapi.json"
DISCOVER_PATH = "/v1/discover/endpoints"
INSPECT_PATH = "/v1/inspect"
RUN_PATH = "/v1/run"

NEWS_PROVIDER = "search1api"
NEWS_ENDPOINT = "/news"
YOUTUBE_PROVIDER = "apify"
YOUTUBE_ENDPOINT = "/starvibe/youtube-video-transcript"

TERMINAL_RUN_STATUSES = frozenset({
    "COMPLETED", "FAILED", "BLOCKED", "STOPPED", "TIMED_OUT", "TIME_OUT",
})
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TIME_RANGES = frozenset({"", "day", "week", "month", "year"})
_ALLOWED_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
})


class MonidError(RuntimeError):
    """A scrubbed Monid client or API error."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MonidHTTPResponse:
    """Small response contract returned by an injected HTTP transport."""

    status_code: int
    body: bytes
    headers: Mapping[str, str] | None = None


HTTPTransport = Callable[
    [str, str, Mapping[str, str], bytes, float, int], MonidHTTPResponse
]
KeyLoader = Callable[[], str | None]


class _DeadlineIO(io.RawIOBase):
    """Apply one wall-clock deadline to each socket read, including headers."""

    def __init__(self, sock: socket.socket, deadline: float):
        super().__init__()
        self.sock = sock
        self.deadline = deadline
        self.file = sock.makefile("rb", buffering=0)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int | None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Monid response timed out")
        self.sock.settimeout(remaining)
        return self.file.readinto(buffer)

    def close(self) -> None:
        if not self.closed:
            self.file.close()
        super().close()


class _DeadlineSocket:
    """Socket facade that enforces a deadline during HTTP writes and reads."""

    def __init__(self, sock: socket.socket, deadline: float):
        self.sock = sock
        self.deadline = deadline

    def makefile(self, mode: str, buffering: int | None = None):
        return io.BufferedReader(_DeadlineIO(self.sock, self.deadline))

    def sendall(self, data: bytes) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Monid request timed out")
        self.sock.settimeout(remaining)
        self.sock.sendall(data)

    def close(self) -> None:
        self.sock.close()


def _bounded_int(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _valid_youtube_url(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        return False
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() != "https" or parts.hostname is None
                or parts.hostname.lower() not in _ALLOWED_YOUTUBE_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            return False
    except ValueError:
        return False

    host = parts.hostname.lower()
    path = [part for part in parts.path.split("/") if part]
    if host == "youtu.be":
        return len(path) == 1 and _YOUTUBE_VIDEO_ID.fullmatch(path[0]) is not None
    if parts.path == "/watch":
        video_ids = parse_qs(parts.query, keep_blank_values=True).get("v", [])
        return len(video_ids) == 1 and _YOUTUBE_VIDEO_ID.fullmatch(video_ids[0]) is not None
    return (len(path) == 2 and path[0] in {"shorts", "embed", "live"}
            and _YOUTUBE_VIDEO_ID.fullmatch(path[1]) is not None)


@dataclass(frozen=True)
class MonidNewsRequest:
    """One bounded request for the verified Search1API `/news` endpoint."""

    query: str
    max_results: int = 5
    crawl_results: int = 0
    time_range: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 1000:
            raise MonidError("News query must contain 1 to 1000 characters")
        try:
            _bounded_int("max_results", self.max_results, 1, 50)
            _bounded_int("crawl_results", self.crawl_results, 0, self.max_results)
        except ValueError as error:
            raise MonidError(str(error)) from None
        if not isinstance(self.time_range, str) or self.time_range not in _TIME_RANGES:
            raise MonidError("Unsupported news time range")

    @property
    def item_budget(self) -> int:
        # Count both returned results and any requested crawls toward the batch cap.
        return self.max_results + self.crawl_results

    def body(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "max_results": self.max_results,
            "crawl_results": self.crawl_results,
            "time_range": self.time_range,
        }


@dataclass(frozen=True)
class MonidYouTubeTranscriptRequest:
    """One video for the verified Apify transcript endpoint."""

    youtube_url: str

    def __post_init__(self) -> None:
        if not _valid_youtube_url(self.youtube_url):
            raise MonidError("A single HTTPS YouTube video URL is required")

    @property
    def item_budget(self) -> int:
        return 1

    def body(self) -> dict[str, Any]:
        return {
            "youtube_url": self.youtube_url,
            "language": "en",
            "include_transcript_text": True,
        }


@dataclass(frozen=True)
class MonidRunReceipt:
    """Full provider response plus the exact non-secret run request provenance."""

    response: Mapping[str, Any]
    request: Mapping[str, Any] | None = None

    @property
    def run_id(self) -> str:
        return str(self.response["runId"])

    @property
    def status(self) -> str:
        return str(self.response["status"])

    @property
    def cost(self) -> Any:
        # Preserve Monid's actual cost as supplied; price/billing remain in response.
        return self.response.get("cost")

    @property
    def is_terminal(self) -> bool:
        return is_terminal_run_status(self.status)

    @property
    def provenance(self) -> dict[str, Any]:
        data = {
            name: self.response[name]
            for name in ("runId", "provider", "providerName", "endpoint", "input",
                         "providerResponse", "createdAt", "startedAt", "completedAt")
            if name in self.response
        }
        if "input" not in data and self.request is not None and "input" in self.request:
            data["input"] = self.request["input"]
        return data


def is_terminal_run_status(status: str) -> bool:
    """Recognize terminal statuses, including both timeout spellings."""
    return isinstance(status, str) and status.upper() in TERMINAL_RUN_STATUSES


def _is_run_response(value: Any) -> bool:
    return (isinstance(value, dict)
            and isinstance(value.get("runId"), str) and bool(value["runId"])
            and isinstance(value.get("status"), str)
            and isinstance(value.get("provider"), str)
            and isinstance(value.get("endpoint"), str))


class MonidClient:
    """Bounded Monid API client using an injected credential loader and transport.

    `transport(method, url, headers, body, timeout_seconds, max_response_bytes)`
    must make one request and return a `MonidHTTPResponse`; it must not follow
    redirects or retry. The default transport uses fixed-host HTTPS directly.

    Free catalog calls still need a Monid API key according to the current
    OpenAPI security declaration. No key lookup occurs until a method is called.
    """

    def __init__(
        self,
        key_loader: KeyLoader,
        transport: HTTPTransport | None = None,
        *,
        timeout_seconds: float = 8.0,
        max_request_bytes: int = 64_000,
        max_response_bytes: int = 1_000_000,
        max_batch_requests: int = 4,
        max_batch_items: int = 50,
    ):
        if not callable(key_loader):
            raise TypeError("key_loader must be callable")
        if transport is not None and not callable(transport):
            raise TypeError("transport must be callable")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)):
            raise ValueError("timeout_seconds must be a number from 0.1 to 30")
        if not 0.1 <= float(timeout_seconds) <= 30.0:
            raise ValueError("timeout_seconds must be a number from 0.1 to 30")
        self.key_loader = key_loader
        self.transport = transport if transport is not None else self._https_transport
        self.timeout_seconds = float(timeout_seconds)
        self.max_request_bytes = _bounded_int("max_request_bytes", max_request_bytes, 1, 256_000)
        self.max_response_bytes = _bounded_int("max_response_bytes", max_response_bytes, 1, 2_000_000)
        self.max_batch_requests = _bounded_int("max_batch_requests", max_batch_requests, 1, 8)
        self.max_batch_items = _bounded_int("max_batch_items", max_batch_items, 1, 100)

    def discover(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        """Search the endpoint catalog without starting a paid provider run."""
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise MonidError("Discovery query must contain 1 to 1000 characters")
        try:
            _bounded_int("limit", limit, 1, 20)
        except ValueError as error:
            raise MonidError(str(error)) from None
        return self._request("POST", DISCOVER_PATH, {"q": query, "limit": limit})

    def inspect(self, provider: str, endpoint: str) -> dict[str, Any]:
        """Read a provider endpoint's schema and pricing without running it."""
        if (not isinstance(provider, str) or not provider.strip() or len(provider) > 100
                or not isinstance(endpoint, str) or not endpoint.strip() or len(endpoint) > 300):
            raise MonidError("Provider and endpoint are required for inspection")
        return self._request("POST", INSPECT_PATH,
                             {"provider": provider, "endpoint": endpoint})

    def run_batch(
        self,
        requests: Sequence[MonidNewsRequest | MonidYouTubeTranscriptRequest],
        *,
        enabled: bool,
    ) -> list[MonidRunReceipt]:
        """Run only fixed, verified plans after explicit enablement.

        All requests, request byte sizes, and aggregate item budgets are checked
        before the first paid call. Each POST happens once; a transport failure
        never retries the request, and accepted async runs are returned pending.
        """
        if enabled is not True:
            raise MonidError("Monid paid runs are disabled")
        if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes, bytearray)):
            raise MonidError("Run batch must be a finite sequence")
        if len(requests) > self.max_batch_requests:
            raise MonidError("Run batch exceeds request limit")
        if not requests:
            return []

        prepared: list[tuple[dict[str, Any], bytes, int]] = []
        for item in requests:
            if isinstance(item, MonidNewsRequest):
                provider, endpoint, body = NEWS_PROVIDER, NEWS_ENDPOINT, item.body()
            elif isinstance(item, MonidYouTubeTranscriptRequest):
                provider, endpoint, body = YOUTUBE_PROVIDER, YOUTUBE_ENDPOINT, item.body()
            else:
                raise MonidError("Unsupported Monid run plan")
            request = {"provider": provider, "endpoint": endpoint, "input": {"body": body}}
            encoded = self._encode(request)
            if len(encoded) > self.max_request_bytes:
                raise MonidError("Monid request exceeds byte limit")
            prepared.append((request, encoded, item.item_budget))

        if sum(item_count for _, _, item_count in prepared) > self.max_batch_items:
            raise MonidError("Run batch exceeds item limit")

        # Load once before the batch so missing credentials cannot cause partial runs.
        authorization = self._authorization()
        receipts: list[MonidRunReceipt] = []
        for request, encoded, _ in prepared:
            response = self._request_encoded(
                "POST", RUN_PATH, encoded, authorization,
                allow_run_response=True,
            )
            receipts.append(MonidRunReceipt(response=response, request=request))
        return receipts

    def get_run(self, run_id: str) -> MonidRunReceipt:
        """Fetch one run status/result snapshot; this does not poll or retry."""
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise MonidError("Invalid Monid run identifier")
        response = self._request("GET", f"/v1/runs/{quote(run_id, safe='')}")
        if not _is_run_response(response) or response["runId"] != run_id:
            raise MonidError("Monid returned an invalid run record")
        return MonidRunReceipt(response=response)

    def _encode(self, payload: Mapping[str, Any]) -> bytes:
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise MonidError("Monid request could not be encoded") from None
        if len(body) > self.max_request_bytes:
            raise MonidError("Monid request exceeds byte limit")
        return body

    def _authorization(self) -> str:
        try:
            key = self.key_loader()
        except Exception:
            raise MonidError("Monid API key is unavailable") from None
        if (not isinstance(key, str) or not key.strip() or len(key.strip()) > 4096
                or "\r" in key or "\n" in key):
            raise MonidError("Monid API key is not configured")
        return "Bearer " + key.strip()

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = self._encode(payload) if payload is not None else b""
        authorization = self._authorization()
        return self._request_encoded(method, path, body, authorization)

    def _request_encoded(
        self,
        method: str,
        path: str,
        body: bytes,
        authorization: str,
        *,
        allow_run_response: bool = False,
    ) -> dict[str, Any]:
        url = API_BASE_URL + path
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
        }
        if method != "GET":
            headers["Content-Type"] = "application/json"
        try:
            response = self.transport(
                method, url, headers, body, self.timeout_seconds, self.max_response_bytes,
            )
        except Exception:
            # Transport exceptions may include credentials, URLs, or request data.
            raise MonidError("Monid transport failed") from None

        if (not isinstance(response, MonidHTTPResponse)
                or isinstance(response.status_code, bool)
                or not isinstance(response.status_code, int)
                or not 100 <= response.status_code <= 599
                or not isinstance(response.body, bytes)):
            raise MonidError("Monid transport returned an invalid response")
        if len(response.body) > self.max_response_bytes:
            raise MonidError("Monid response exceeds byte limit", response.status_code)
        if 300 <= response.status_code < 400:
            raise MonidError("Monid redirects are refused", response.status_code)

        data: Any = None
        if 200 <= response.status_code < 300 or allow_run_response:
            try:
                data = json.loads(response.body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                data = None

        if 200 <= response.status_code < 300:
            if not isinstance(data, dict):
                raise MonidError("Monid returned an invalid JSON response", response.status_code)
            return data
        # Monid deliberately mirrors provider errors for synchronous runs and
        # returns a run record for request timeouts. Preserve those records.
        if allow_run_response and _is_run_response(data):
            return data
        raise MonidError(f"Monid request failed (HTTP {response.status_code})", response.status_code)

    def _https_transport(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> MonidHTTPResponse:
        """Make one direct TLS request to the pinned host; never follow redirects."""
        try:
            parts = urlsplit(url)
            if (parts.scheme != "https" or parts.hostname != API_HOST
                    or parts.port not in (None, 443) or parts.username or parts.password
                    or parts.query or parts.fragment or not parts.path.startswith("/v1/")):
                raise MonidError("Monid request target is invalid")
        except ValueError:
            raise MonidError("Monid request target is invalid") from None

        deadline = time.monotonic() + timeout_seconds
        connection = http.client.HTTPSConnection(
            API_HOST, port=443, timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.connect()
            if connection.sock is None or time.monotonic() >= deadline:
                raise TimeoutError("Monid connection timed out")
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sock = _DeadlineSocket(connection.sock, deadline)
            connection.request(method, parts.path, body=body, headers=dict(headers))
            result = connection.getresponse()
            response_body = result.read(max_response_bytes + 1)
            if time.monotonic() > deadline:
                raise TimeoutError("Monid response timed out")
            if len(response_body) > max_response_bytes:
                raise MonidError("Monid response exceeds byte limit", result.status)
            return MonidHTTPResponse(
                status_code=result.status,
                body=response_body,
                headers={name.lower(): value for name, value in result.getheaders()},
            )
        except MonidError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError):
            raise MonidError("Monid transport failed") from None
        finally:
            connection.close()


__all__ = [
    "API_HOST", "API_BASE_URL", "OPENAPI_URL", "DISCOVER_PATH", "INSPECT_PATH",
    "RUN_PATH", "NEWS_PROVIDER", "NEWS_ENDPOINT", "YOUTUBE_PROVIDER",
    "YOUTUBE_ENDPOINT", "TERMINAL_RUN_STATUSES", "MonidClient", "MonidError",
    "MonidHTTPResponse", "MonidNewsRequest", "MonidRunReceipt",
    "MonidYouTubeTranscriptRequest", "is_terminal_run_status",
]
