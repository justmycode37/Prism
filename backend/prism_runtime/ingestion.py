"""Public market data and bounded RSS/Atom/HTML collection for the cascade.

The fetcher pins validated public DNS addresses, avoids proxies, honors robots,
and shares caches/rate limits across every collector in this process. Nothing
here authenticates to an exchange or accepts instructions from page content.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import io
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET


DEFAULT_SOURCES = [
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "kind": "rss"},
    {"name": "ECB", "url": "https://www.ecb.europa.eu/rss/press.html", "kind": "rss"},
    {"name": "Ethereum Foundation", "url": "https://blog.ethereum.org/en/feed.xml", "kind": "rss"},
    {"name": "Bitcoin Core", "url": "https://bitcoincore.org/en/rss.xml", "kind": "rss"},
    {"name": "Glassnode Insights", "url": "https://insights.glassnode.com/rss/", "kind": "rss"},
]
_UA = "PrismCascade/1.0"
_TRACKERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class CollectionError(RuntimeError):
    pass


class _DeadlineIO(io.RawIOBase):
    """Bound every recv, including trickled status lines and response headers."""
    def __init__(self, sock, deadline: float):
        super().__init__()
        self.sock, self.deadline = sock, deadline
        self.file = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("Source response exceeded time limit")
        self.sock.settimeout(remaining)
        return self.file.readinto(buffer)

    def close(self):
        if not self.closed:
            self.file.close()
        super().close()


class _DeadlineSocket:
    def __init__(self, sock, deadline: float):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode, buffering=None):
        return io.BufferedReader(_DeadlineIO(self.sock, self.deadline))

    def sendall(self, data):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("Source request exceeded time limit")
        self.sock.settimeout(remaining)
        self.sock.sendall(data)

    def close(self):
        self.sock.close()


def _iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in _TRACKERS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def _number(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.title = [], [], []
        self.hidden = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)


def _plain(value: str, limit: int = 8000) -> str:
    parser = _HTML()
    parser.feed(value)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def _published(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Budget:
    def __init__(self, seconds: float, requests: int, pages: int):
        self.deadline = time.monotonic() + seconds
        self.requests, self.pages = requests, pages
        self.lock = threading.Lock()

    def take(self, kind: str) -> None:
        with self.lock:
            if time.monotonic() >= self.deadline:
                raise CollectionError("Collection time budget exhausted")
            if getattr(self, kind) <= 0:
                raise CollectionError(f"Collection {kind} budget exhausted")
            setattr(self, kind, getattr(self, kind) - 1)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CollectionError("Collection time budget exhausted")
        return left


class SourceCollector:
    """collect(agent)->{evidence, markets, errors}; configurable public sources.

    Settings live under config['ingestion']; top-level sources is also accepted.
    Sources may be URL strings or {url,kind:'rss'|'atom'|'article',name,keywords}.
    Agent sources override configured sources; agent keywords rank feed entries.
    """

    def __init__(self, config: dict):
        self.config = config
        self.settings = dict(config.get("ingestion", {}))
        self.workers = min(8, max(1, int(self.settings.get("workers", 4))))
        self.timeout = min(20.0, max(1.0, float(self.settings.get("request_timeout_seconds", 8))))
        self.max_bytes = min(2_000_000, max(1024, int(self.settings.get("max_response_bytes", 1_000_000))))
        self.max_content = min(12_000, max(256, int(self.settings.get("max_content_chars", 6000))))
        self.host_delay = min(10.0, max(.25, float(self.settings.get("host_delay_seconds", .5))))
        self._slots = threading.BoundedSemaphore(self.workers)
        self._lock = threading.Lock()
        self._host_locks, self._host_next, self._host_delays, self._url_locks = {}, {}, {}, {}
        self._cache, self._robots, self._failures = {}, {}, {}

    def collect_markets(self, symbols: list[str]) -> list[dict]:
        """Refresh quotes/charts independently; cache and pacing are shared.

        Individual unavailable symbols are omitted. collect() also reports their
        errors to the evidence log; the market clock treats absence as stale data.
        """
        budget = _Budget(30, 32, 0)
        result = []
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="prism-quotes") as pool:
            futures = [pool.submit(self._market, str(symbol).upper(), budget, {"symbol": str(symbol).upper()})
                       for symbol in list(dict.fromkeys(symbols))[:8]]
            for future in as_completed(futures):
                try:
                    result.extend(future.result().get("markets", []))
                except Exception:
                    continue
        return result

    def collect(self, agent: dict) -> dict:
        budget = _Budget(min(90, max(5, float(self.settings.get("max_seconds", 30)))),
                         min(96, max(4, int(self.settings.get("max_requests", 48)))),
                         min(40, max(1, int(self.settings.get("max_pages", 16)))))
        evidence, markets, errors = [], [], []
        sources = agent.get("sources")
        if sources is None:
            sources = self.settings.get("sources", self.config.get("sources", DEFAULT_SOURCES))
        if isinstance(sources, str):
            sources = [sources]
        sources = sources[:16] if isinstance(sources, list) else []
        symbols = [agent["symbol"]] if agent.get("symbol") else agent.get("symbols", [])
        symbols = list(dict.fromkeys(str(s).upper() for s in symbols))[:8]
        jobs = [(self._source, source) for source in sources]
        jobs.extend((self._market, symbol) for symbol in symbols)
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="prism-source") as pool:
            futures = {pool.submit(fn, value, budget, agent): value for fn, value in jobs}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    evidence.extend(result.get("evidence", []))
                    markets.extend(result.get("markets", []))
                    errors.extend(result.get("errors", []))
                except Exception as exc:
                    source = futures[future]
                    label = source.get("url", "source") if isinstance(source, dict) else str(source)
                    errors.append(f"{label[:120]}: {type(exc).__name__}: {str(exc)[:200]}")
        unique = {}
        for item in evidence:
            key = item["source_url"]
            existing = unique.get(key)
            if existing is None or (len(item["content"]), bool(item.get("published_at"))) > (len(existing["content"]), bool(existing.get("published_at"))):
                unique[key] = item
        max_evidence = min(96, max(1, int(self.settings.get("max_evidence", 32))))
        return {"evidence": list(unique.values())[:max_evidence], "markets": markets,
                "errors": list(dict.fromkeys(errors))[:32]}

    def _source(self, source, budget: _Budget, agent: dict) -> dict:
        source = {"url": source} if isinstance(source, str) else dict(source)
        url = _canonical(str(source.get("url", "")))
        budget.take("pages")
        response = self._fetch(url, budget, respect_robots=True)
        body = response["body"].decode("utf-8", errors="replace")
        kind = str(source.get("kind", "rss"))
        if kind in {"rss", "atom", "feed"}:
            if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
                raise CollectionError("Feed entity declarations are not supported")
            root = ET.fromstring(body)
            entries = [node for node in root.iter() if node.tag.split("}")[-1] in {"item", "entry"}][:100]
            keywords = source.get("keywords", agent.get("keywords"))
            if keywords is None:
                keywords = [word for word in re.findall(r"[a-z]{3,}", str(agent.get("topic", "")).lower())
                            if word not in {"and", "the", "for", "with", "from"}]
            if isinstance(keywords, str):
                keywords = keywords.split()
            ranked = []
            for node in entries:
                fields, link = {}, ""
                for child in node:
                    tag = child.tag.split("}")[-1]
                    fields[tag] = "".join(child.itertext())
                    if tag == "link" and child.attrib.get("rel", "alternate") == "alternate":
                        link = child.attrib.get("href") or fields[tag]
                link = link or fields.get("link", "")
                if not link:
                    continue
                content = fields.get("encoded") or fields.get("content") or fields.get("description") or fields.get("summary", "")
                title = _plain(fields.get("title", ""), 400)
                plain = _plain(content, self.max_content)
                score = sum(str(word).lower() in (title + " " + plain).lower() for word in keywords)
                published = fields.get("published") or fields.get("pubDate") or fields.get("updated", "")
                ranked.append((score, self._evidence(urljoin(response["url"], link), "news", title, plain, agent, published)))
            ranked.sort(key=lambda row: row[0], reverse=True)
            count = min(12, max(1, int(self.settings.get("items_per_feed", 4))))
            evidence = [item for _, item in ranked[:count]]
            errors = []
            article_count = min(4, max(0, int(self.settings.get("articles_per_feed", 1))))
            if self.settings.get("crawl_articles", True):
                for index, item in enumerate(evidence[:article_count]):
                    try:
                        budget.take("pages")
                        article = self._fetch(item["source_url"], budget, respect_robots=True)
                        parser = self._parse_html(article)
                        if parser["content"]:
                            evidence[index] = self._evidence(article["url"], "article", parser["title"] or item["title"],
                                                              parser["content"], agent, item.get("published_at", ""))
                    except Exception as exc:
                        errors.append(f"Article {item['source_url']}: {str(exc)[:160]}")
            return {"evidence": evidence, "errors": errors}
        parser = self._parse_html(response)
        evidence = [self._evidence(response["url"], "article", parser["title"], parser["content"], agent)]
        seen = {response["url"]}
        depth = min(2, max(0, int(self.settings.get("crawl_depth", 1))))
        frontier = [(urljoin(response["url"], link), 1) for link in parser["links"]]
        errors = []
        crawl_count = min(8, max(0, int(self.settings.get("pages_per_site", 3))))
        while frontier and len(evidence) < crawl_count + 1:
            candidate, level = frontier.pop(0)
            candidate = _canonical(candidate)
            if level > depth or candidate in seen or urlsplit(candidate).netloc != urlsplit(url).netloc:
                continue
            seen.add(candidate)
            if re.search(r"\.(pdf|zip|png|jpg|gif|mp4|css|js)$", urlsplit(candidate).path, re.I):
                continue
            try:
                budget.take("pages")
                page = self._fetch(candidate, budget, respect_robots=True)
                child = self._parse_html(page)
                evidence.append(self._evidence(page["url"], "article", child["title"], child["content"], agent))
                frontier.extend((urljoin(page["url"], link), level + 1) for link in child["links"][:32])
            except Exception as exc:
                errors.append(f"Page {candidate}: {str(exc)[:160]}")
        return {"evidence": evidence, "errors": errors}

    def _parse_html(self, response: dict) -> dict:
        content_type = response["headers"].get("content-type", "text/html")
        if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
            raise CollectionError("Article is not HTML or plain text")
        parser = _HTML()
        parser.feed(response["body"].decode("utf-8", errors="replace"))
        return {"title": re.sub(r"\s+", " ", " ".join(parser.title)).strip()[:400],
                "content": re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:self.max_content],
                "links": parser.links[:100]}

    def _evidence(self, url: str, kind: str, title: str, content: str, agent: dict, published: str = "") -> dict:
        url = _canonical(url)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise CollectionError("Evidence link is not HTTP(S)")
        symbols = [agent["symbol"]] if kind in {"market", "chart"} and agent.get("symbol") else []
        symbols.extend(re.findall(r"\b[A-Z0-9]{2,12}USDT\b", title + " " + content))
        aliases = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
                   "xrp": "XRPUSDT", "dogecoin": "DOGEUSDT", "cardano": "ADAUSDT"}
        words = set(re.findall(r"[a-z]+", (title + " " + content).lower()))
        symbols.extend(symbol for name, symbol in aliases.items() if name in words)
        item = {"source_url": url, "source_kind": kind, "title": title[:400],
                "content": content[:self.max_content], "observed_at": _iso(),
                "symbols": list(dict.fromkeys(symbols))[:16]}
        publication = _published(published)
        if publication:
            item["published_at"] = publication
        snapshot = {key: value for key, value in item.items() if key != "observed_at"}
        item["id"] = "ev_" + sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        return item

    def _market(self, symbol: str, budget: _Budget, agent: dict) -> dict:
        if not re.fullmatch(r"[A-Z0-9]{3,24}", symbol):
            raise CollectionError("Invalid market symbol")
        market = self.settings.get("market", self.config.get("market", {}))
        market = market if isinstance(market, dict) else {}
        interval = str(market.get("interval", self.settings.get("candle_interval", "60")))
        if interval not in {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W"}:
            raise CollectionError("Unsupported candle interval")
        duration = {"D": 86_400_000, "W": 604_800_000}.get(interval, int(interval) * 60_000 if interval.isdigit() else 0)
        limit = min(1000, max(50, int(market.get("history_bars", self.settings.get("candle_limit", 1000)))))
        base = "https://api.bybit.com"
        category = str(market.get("category", "spot"))
        if category not in {"spot", "linear"}:
            raise CollectionError("Unsupported public market category")
        query = {"category": category, "symbol": symbol}
        ticker_url = base + "/v5/market/tickers?" + urlencode(query)
        candle_url = base + "/v5/market/kline?" + urlencode({**query, "interval": interval, "limit": limit})
        ticker = self._json(ticker_url, budget)
        data = self._json(candle_url, budget)
        wall_ms = int(time.time() * 1000)
        now_ms = min(wall_ms, int(data.get("time") or data["_fetched_at_ms"]), data["_fetched_at_ms"])
        quote_ms = min(wall_ms, int(ticker.get("time") or ticker["_fetched_at_ms"]), ticker["_fetched_at_ms"])
        rows = ticker.get("result", {}).get("list", [])
        quote = next((row for row in rows if row.get("symbol") == symbol), None)
        if quote is None or not _number(quote.get("lastPrice"), 0) > 0:
            raise CollectionError(f"No valid public quote for {symbol}")
        candles = {}
        for row in data.get("result", {}).get("list", []):
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                start = int(row[0])
            except (TypeError, ValueError):
                continue
            if start + duration > now_ms:
                continue
            values = [_number(value) for value in row[1:6]]
            if any(value is None for value in values) or min(values[:4]) <= 0 or values[4] < 0:
                continue
            opening, high, low, close, volume = values
            if high < max(opening, close) or low > min(opening, close) or high < low:
                continue
            candles[start] = {"timestamp": _iso(start / 1000), "start_ms": start,
                              "end_ms": start + duration, "closed": True,
                              "open": opening, "high": high, "low": low,
                              "close": close, "volume": volume}
        observed = _iso(quote_ms / 1000)
        result = {"symbol": symbol, "price": _number(quote["lastPrice"]),
                  "change_24h": _number(quote.get("price24hPcnt"), 0),
                  "funding_rate": _number(quote.get("fundingRate")) if category == "linear" else None,
                  "timestamp": observed, "as_of_ms": quote_ms, "updated_at": observed,
                  "candles": [candles[key] for key in sorted(candles)]}
        quote_evidence = self._evidence(ticker_url, "market", symbol + " public market quote",
                                         json.dumps({k: v for k, v in result.items() if k != "candles"}), agent)
        quote_evidence["numeric"] = {"price": result["price"], "change_24h": result["change_24h"], "funding_rate": result["funding_rate"]}
        quote_evidence["observed_at"] = observed
        chart_hash = sha256(json.dumps(result["candles"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        chart_evidence = self._evidence(candle_url, "chart", symbol + " closed " + interval + " interval candles",
                                         f"{len(candles)} chronological closed candles; incomplete candles excluded. Series SHA256: {chart_hash}.", agent)
        chart_evidence["numeric"] = {"closed_candles": len(candles), "interval_ms": duration}
        chart_evidence["observed_at"] = _iso(now_ms / 1000)
        return {"markets": [result], "evidence": [quote_evidence, chart_evidence]}

    def _json(self, url: str, budget: _Budget) -> dict:
        response = self._fetch(url, budget, ttl=30)
        data = json.loads(response["body"])
        if not isinstance(data, dict) or data.get("retCode", 0) != 0:
            raise CollectionError("Public market API rejected the request")
        data["_fetched_at_ms"] = response["fetched_at_ms"]
        return data

    def _key_lock(self, mapping: dict, key: str):
        with self._lock:
            if mapping is self._url_locks and len(mapping) > 4096:
                stale = [name for name, lock in mapping.items() if not lock.locked()][:2048]
                for name in stale:
                    del mapping[name]
            return mapping.setdefault(key, threading.Lock())

    @contextmanager
    def _acquire(self, lock, budget: _Budget):
        if not lock.acquire(timeout=budget.remaining()):
            raise CollectionError("Collection time budget exhausted while waiting for a source")
        try:
            budget.remaining()
            yield
        finally:
            lock.release()

    def _safe_target(self, url: str) -> tuple:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise CollectionError("Only public, unauthenticated HTTP(S) URLs are allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise CollectionError("Only standard HTTP(S) ports are allowed")
        host = parsed.hostname.encode("idna").decode("ascii")
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CollectionError("Public host DNS lookup failed") from exc
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise CollectionError("Private, local, or reserved network addresses are blocked")
        return parsed, host, port, addresses[0]

    def _fetch(self, url: str, budget: _Budget, respect_robots: bool = False, ttl: int = 60) -> dict:
        url = _canonical(url)
        if respect_robots and not self._allowed(url, budget):
            raise CollectionError("robots.txt disallows this URL")
        with self._acquire(self._key_lock(self._url_locks, url), budget):
            with self._lock:
                cached = self._cache.get(url)
                failed = self._failures.get(url)
            if failed and failed[0] > time.monotonic():
                raise failed[1]
            if cached and cached[0] > time.monotonic():
                if respect_robots and cached[1]["url"] != url and not self._allowed(cached[1]["url"], budget):
                    raise CollectionError("robots.txt disallows the redirected URL")
                return cached[1]
            original_scheme = urlsplit(url).scheme
            current = url
            for _ in range(4):
                parsed, host, port, address = self._safe_target(current)
                if parsed.scheme != original_scheme:
                    raise CollectionError("Cross-protocol redirects are blocked")
                if respect_robots and current != url and not self._allowed(current, budget):
                    raise CollectionError("robots.txt disallows this URL")
                budget.take("requests")
                host_key = f"{host}:{port}"
                with self._acquire(self._key_lock(self._host_locks, host_key), budget):
                    delay = self._host_next.get(host_key, 0) - time.monotonic()
                    if delay > 0:
                        if delay >= budget.remaining():
                            raise CollectionError("Host pacing exceeds collection time budget")
                        time.sleep(delay)
                    with self._acquire(self._slots, budget):
                        timeout = min(self.timeout, budget.remaining())
                        result = self._wire_get(current, host, port, address, timeout)
                    self._host_next[host_key] = time.monotonic() + self._host_delays.get(host_key, self.host_delay)
                status = result["status"]
                if status in {301, 302, 303, 307, 308}:
                    location = result["headers"].get("location")
                    if not location:
                        raise CollectionError("Redirect has no destination")
                    current = _canonical(urljoin(current, location))
                    continue
                result["url"] = current
                result["fetched_at_ms"] = int(time.time() * 1000)
                if status >= 400:
                    error = CollectionError(f"Public source returned HTTP {status}")
                    error.status = status
                    with self._lock:
                        self._failures[url] = (time.monotonic() + 30, error)
                    raise error
                if status != 200:
                    raise CollectionError(f"Unsupported source response HTTP {status}")
                with self._lock:
                    if len(self._cache) > 512:
                        self._cache = {key: value for key, value in self._cache.items() if value[0] > time.monotonic()}
                        if len(self._cache) > 512:
                            self._cache.clear()
                    self._cache[url] = (time.monotonic() + ttl, result)
                return result
            raise CollectionError("Too many source redirects")

    def _allowed(self, url: str, budget: _Budget) -> bool:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        with self._acquire(self._key_lock(self._url_locks, "robots:" + origin), budget):
            cached = self._robots.get(origin)
            if cached and cached[0] > time.monotonic():
                return cached[1].can_fetch(_UA, url)
            parser = RobotFileParser()
            try:
                response = self._fetch(origin + "/robots.txt", budget, ttl=3600)
                parser.parse(response["body"].decode("utf-8", errors="replace").splitlines())
            except CollectionError as exc:
                # A missing robots file allows crawling. Other failures fail closed.
                parser.parse(["User-agent: *", "Allow: /"] if getattr(exc, "status", None) in {404, 410}
                             else ["User-agent: *", "Disallow: /"])
            self._robots[origin] = (time.monotonic() + 3600, parser)
            crawl_delay = parser.crawl_delay(_UA)
            request_rate = parser.request_rate(_UA)
            rate_delay = request_rate.seconds / request_rate.requests if request_rate and request_rate.requests else 0
            if crawl_delay is not None or rate_delay:
                host_key = f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
                delay = max(self.host_delay, crawl_delay or 0, rate_delay)
                self._host_delays[host_key] = delay
                self._host_next[host_key] = max(self._host_next.get(host_key, 0), time.monotonic() + delay)
            return parser.can_fetch(_UA, url)

    def _wire_get(self, url: str, host: str, port: int, address: str, timeout: float) -> dict:
        parsed = urlsplit(url)
        deadline = time.monotonic() + timeout
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        raw_socket = socket.create_connection((address, port), timeout=timeout)
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectionError("Source connection exceeded time limit")
            raw_socket.settimeout(remaining)
            if parsed.scheme == "https":
                raw_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
            connection.sock = _DeadlineSocket(raw_socket, deadline)
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request("GET", path, headers={"Host": host, "User-Agent": _UA,
                                                    "Accept": "application/json, application/atom+xml, application/rss+xml, text/html, text/plain;q=0.8",
                                                    "Accept-Encoding": "identity"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                raise CollectionError("Compressed source bodies are not accepted")
            if int(headers.get("content-length", "0")) > self.max_bytes:
                raise CollectionError("Source response exceeds size limit")
            # A wall-clock deadline also bounds a server that trickles body bytes.
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionError("Source response exceeded time limit")
                chunk = response.read1(min(65_536, self.max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_bytes:
                    raise CollectionError("Source response exceeds size limit")
            return {"status": response.status, "headers": headers, "body": b"".join(chunks)}
        finally:
            if response is not None:
                response.close()
            connection.close()
            raw_socket.close()
