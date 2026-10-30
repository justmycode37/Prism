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
import copy
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
from zoneinfo import ZoneInfo


DEFAULT_SOURCES = []
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
        self.workers = min(16, max(1, int(self.settings.get("workers", 4))))
        self.timeout = min(20.0, max(1.0, float(self.settings.get("request_timeout_seconds", 8))))
        self.max_bytes = min(2_000_000, max(1024, int(self.settings.get("max_response_bytes", 1_000_000))))
        self.max_content = min(48_000, max(256, int(self.settings.get("max_content_chars", 24_000))))
        self.host_delay = min(10.0, max(.25, float(self.settings.get("host_delay_seconds", .5))))
        self._slots = threading.BoundedSemaphore(self.workers)
        self._lock = threading.Lock()
        self._host_locks, self._host_next, self._host_delays, self._url_locks = {}, {}, {}, {}
        self._cache, self._robots, self._failures = {}, {}, {}
        self._universe_symbols = list(config.get('symbols', []))

    def discover_universe(self) -> dict:
        """Discover listed sector peers before collector assignments are frozen."""
        from .sector import discover_candidates, sector_sources
        budget = _Budget(90, min(512, max(16, int(self.settings.get('max_requests', 160)))), 96)
        evidence, errors, listings = [], [], []
        try:
            listings = self._json('https://www.sec.gov/files/company_tickers.json', budget)
        except Exception as error:
            errors.append('SEC listing catalogue unavailable: ' + type(error).__name__)
            # The public exchange directories also identify ETFs/test issues,
            # so fallback discovery need not guess listings from prose.
            for filename, ticker in (('nasdaqlisted.txt', 'Symbol'), ('otherlisted.txt', 'ACT Symbol')):
                try:
                    url = 'https://www.nasdaqtrader.com/dynamic/SymDir/' + filename
                    body = self._fetch(url, budget, ttl=3600)['body'].decode('utf-8', errors='replace')
                    lines = body.splitlines()
                    headers = lines[0].split('|')
                    for line in lines[1:]:
                        row = dict(zip(headers, line.split('|')))
                        if row.get('Test Issue') == 'N' and row.get('ETF') == 'N' and row.get(ticker):
                            name = row.get('Security Name', '').split(' - ', 1)[0]
                            name = re.sub(r'\s+(?:Class [A-Z] )?(?:Common Stock|Ordinary Shares).*$', '', name, flags=re.I)
                            listings.append({'symbol': row[ticker], 'name': name})
                except Exception as fallback_error:
                    errors.append('Exchange listing catalogue unavailable: ' + type(fallback_error).__name__)
        sources = self.config.get('sources', sector_sources(self.config))[:24]
        assignment = {'topic': 'AI infrastructure equities', 'symbols': self._universe_symbols}
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='prism-discovery') as pool:
            jobs = [pool.submit(self._source, source, budget, assignment) for source in sources]
            for job in as_completed(jobs):
                try:
                    result = job.result()
                    evidence.extend(result.get('evidence', []))
                    errors.extend(result.get('errors', []))
                except Exception as error:
                    errors.append('Sector discovery source unavailable: ' + type(error).__name__)
        unique = {item['id']: item for item in evidence if item.get('content')}
        companies = discover_candidates(list(unique.values()), listings, self.config)
        self._universe_symbols = [row['symbol'] for row in companies]
        if not companies:
            errors.append('No verified sector companies discovered.')
        return {'companies': companies, 'evidence': list(unique.values()), 'errors': list(dict.fromkeys(errors))}

    def collect_markets(self, symbols: list[str]) -> list[dict]:
        """Refresh quotes/charts independently; cache and pacing are shared.

        Individual unavailable symbols are omitted. collect() also reports their
        errors to the evidence log; the market clock treats absence as stale data.
        """
        budget = _Budget(min(90, max(5, float(self.settings.get("max_seconds", 90)))),
                         min(512, max(1, int(self.settings.get("max_requests", 512)))), 0)
        result = []
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="prism-quotes") as pool:
            futures = [pool.submit(self._quote_market, str(symbol).upper(), budget, {"symbol": str(symbol).upper()})
                       for symbol in list(dict.fromkeys(symbols))[:256]]
            for future in as_completed(futures):
                try:
                    result.extend(future.result().get("markets", []))
                except Exception:
                    continue
        return result

    def collect(self, agent: dict) -> dict:
        budget = _Budget(min(90, max(5, float(self.settings.get("max_seconds", 90)))),
                         min(512, max(4, int(self.settings.get("max_requests", 160)))),
                         min(96, max(1, int(self.settings.get("max_pages", 48)))))
        evidence, markets, errors = [], [], []
        explicit_agent_sources = agent.get("sources") is not None
        sources = agent.get("sources")
        if sources is None:
            explicit = self.settings.get("sources", self.config.get("sources"))
            if explicit is not None:
                sources = explicit
            else:
                from .sector import sector_sources
                sources = sector_sources(self.config)
        if isinstance(sources, str):
            sources = [sources]
        sources = list(sources) if isinstance(sources, list) else []
        shard_count = max(1, min(96, int(agent.get("source_shard_count", 24))))
        shard = int(agent.get("source_shard", 0)) % shard_count
        if explicit_agent_sources:
            pass  # Explicit assignments are already scoped by the caller.
        elif len(sources) > shard_count:
            sources = sources[shard::shard_count]
        elif sources:
            # Rotate bounded chunks across rounds while keeping each shard distinct.
            width = max(1, math.ceil(len(sources) / shard_count))
            start = (shard * width) % len(sources)
            sources = [sources[(start + offset) % len(sources)] for offset in range(min(width, len(sources)))]
        universe = list(dict.fromkeys(str(s).upper() for s in agent.get("symbols", []) if s))[:256]
        assigned = universe[shard::shard_count]
        primary = str(agent.get("symbol", "")).upper()
        symbols = list(dict.fromkeys(([primary] if primary else []) + assigned))[:256]
        for symbol in assigned if not explicit_agent_sources and sources else []:
            query = f'"{symbol}" AI infrastructure stock news'
            sources.append({"name": f"Bing News: {symbol}",
                            "url": "https://www.bing.com/news/search?" + urlencode({"q": query, "format": "rss"}),
                            "kind": "rss", "keywords": [symbol]})
        sources = sources[:128]
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
            key = (item["source_url"], item.get("source_kind"))
            existing = unique.get(key)
            if existing is None or (len(item["content"]), bool(item.get("published_at"))) > (len(existing["content"]), bool(existing.get("published_at"))):
                unique[key] = item
        max_evidence = min(256, max(1, int(self.settings.get("max_evidence", 128))))
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
                article_url = urljoin(response["url"], link)
                wrapper = urlsplit(article_url)
                if wrapper.hostname == 'www.bing.com' and wrapper.path == '/news/apiclick.aspx':
                    # RSS advertises the publisher URL in its tracking wrapper.
                    # Publisher acquisition still passes the normal public-HTTPS,
                    # robots, redirect and DNS checks below.
                    publisher = dict(parse_qsl(wrapper.query)).get('url', '')
                    if urlsplit(publisher).scheme == 'https':
                        article_url = publisher
                ranked.append((score, self._evidence(article_url, "news", title, plain, agent, published)))
            ranked.sort(key=lambda row: row[0], reverse=True)
            count = min(32, max(1, int(self.settings.get("items_per_feed", 12))))
            evidence = [item for _, item in ranked[:count]]
            errors = []
            article_count = min(8, max(0, int(self.settings.get("articles_per_feed", 3))))
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
        crawl_count = min(16, max(0, int(self.settings.get("pages_per_site", 6))))
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
        text = title + " " + content
        known = set(agent.get('symbols', [])) | set(getattr(self, '_universe_symbols', self.config.get('symbols', [])))
        tagged = re.findall(r'\b(?:NASDAQ|NYSE|NYSEAMERICAN)\s*:\s*([A-Z][A-Z0-9.-]{0,9})\b|\$([A-Z][A-Z0-9.-]{0,9})\b', text)
        symbols.extend(first or second for first, second in tagged)
        symbols.extend(symbol for symbol in known if re.search(r'(?<![A-Za-z0-9])' + re.escape(symbol) + r'(?![A-Za-z0-9])', text)
                       and symbol not in {'AI', 'ON', 'IT', 'A', 'I'})
        item = {"source_url": url, "source_kind": kind, "title": title[:400],
                "content": content[:self.max_content], "observed_at": _iso(),
                "symbols": list(dict.fromkeys(symbols))[:16]}
        publication = _published(published)
        item['published_at'] = publication or None
        item['event_time'] = publication or None
        item['first_observed_at'] = item['observed_at']
        item['available_at'] = item['observed_at']
        item['content_hash'] = sha256(re.sub(r'\s+', ' ', item['content']).strip().encode()).hexdigest()
        item['event_identity'] = item['content_hash']
        item['source_family'] = urlsplit(url).hostname or ''
        item['primary_source'] = item['source_family'] == 'sec.gov' or item['source_family'].endswith('.sec.gov') or 'investor' in item['source_family']
        snapshot = {key: value for key, value in item.items() if key not in {'observed_at', 'first_observed_at', 'available_at'}}
        item["id"] = "ev_" + sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        return item

    def _market(self, symbol: str, budget: _Budget, agent: dict) -> dict:
        from .equities import EquityMarketData
        with self._lock:
            cache = getattr(self, '_daily_markets', {}).get(symbol)
        if cache:
            zone = ZoneInfo(cache['markets'][0].get('exchange_timezone', 'America/New_York'))
            if cache['_history_day'] == datetime.now(zone).date().isoformat():
                return copy.deepcopy({key:value for key,value in cache.items() if key != '_history_day'})
        settings = {**self.settings, 'benchmark_symbols': self.config.get('research', {}).get('benchmark_symbols', ['SPY', 'SMH'])}
        result = EquityMarketData(self._json, self._evidence, settings).collect(
            symbol, budget, {**agent, 'symbol': symbol})
        zone = ZoneInfo(result['markets'][0].get('exchange_timezone', 'America/New_York'))
        with self._lock:
            if not hasattr(self, '_daily_markets'):
                self._daily_markets = {}
            self._daily_markets[symbol] = {**copy.deepcopy(result), '_history_day':datetime.now(zone).date().isoformat()}
        return result

    def _quote_market(self, symbol, budget, agent):
        from .equities import EquityMarketData
        with self._lock:
            cache = getattr(self, '_daily_markets', {}).get(symbol)
        zone = ZoneInfo(cache['markets'][0].get('exchange_timezone', 'America/New_York')) if cache else ZoneInfo('America/New_York')
        if cache is None or cache['_history_day'] != datetime.now(zone).date().isoformat():
            try:
                return self._market(symbol, budget, agent)
            except Exception:
                pass  # A chart outage does not prevent an independently observed fresh quote.
        settings = {**self.settings, 'quote_only':True,
            'benchmark_symbols':self.config.get('research',{}).get('benchmark_symbols',['SPY','SMH'])}
        return EquityMarketData(self._json,self._evidence,settings).collect(symbol,budget,agent)

    def _json(self, url: str, budget: _Budget) -> dict:
        response = self._fetch(url, budget, ttl=60)
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
