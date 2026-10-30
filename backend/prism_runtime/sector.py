"""Offline discovery helpers for public AI infrastructure equity research."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

MAX_SYMBOLS = 80
THEMES = ('semiconductors', 'memory', 'networking', 'cloud_compute', 'data_centers', 'power', 'cooling', 'manufacturing')
RELEVANCE = {
    'semiconductors': ('semiconductor', 'chip', 'foundry', 'wafer', 'silicon', 'gpu', 'accelerator'),
    'memory': ('high bandwidth memory', 'hbm', 'memory', 'dram', 'nand'),
    'networking': ('optical', 'photonics', 'transceiver', 'ethernet switch', 'networking', 'interconnect'),
    'cloud_compute': ('cloud infrastructure', 'cloud platform', 'cloud computing', 'accelerated computing'),
    'data_centers': ('data center', 'datacenter', 'server rack', 'hyperscaler'),
    'power': ('power infrastructure', 'data center power', 'grid capacity'),
    'cooling': ('liquid cooling', 'thermal management', 'data center cooling'),
    'manufacturing': ('semiconductor manufacturing', 'chip manufacturing', 'advanced packaging'),
}
_SYMBOL = re.compile(r'^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?$')
_SUFFIX = re.compile(r'\b(incorporated|inc|corporation|corp|limited|ltd|company|co|plc|holdings|holding|technologies|technology|systems|group|international|intl)\b', re.I)


def normalize_symbol(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper().removeprefix('$')
    if len(value) > 10 or not _SYMBOL.fullmatch(value):
        return None
    if value.endswith(('USDT', 'USD')):
        return None
    return value


def _listing_rows(listings):
    if isinstance(listings, dict):
        rows = listings.get('data', listings)
        if isinstance(rows, dict):
            rows = [{'symbol': v.get('ticker'), 'name': v.get('title'), 'cik': v.get('cik_str')}
                    for v in rows.values() if isinstance(v, dict) and not str(v.get('ticker', '')).isdigit()]
    else:
        rows = listings
    return rows if isinstance(rows, (list, tuple)) else []


def _normalize_name(text):
    return re.sub(r'\s+', ' ', _SUFFIX.sub('', re.sub(r'[^\w ]', ' ', str(text).lower()))).strip()


# These common nouns and publishing/venue names require an explicit ticker tag.
_AMBIGUOUS_NAMES = {'bandwidth', 'nasdaq', 'morningstar', 'memory', 'power', 'global', 'arm', 'meta'}
_AI_CONTEXT = re.compile(r'\b(?:ai|artificial intelligence|gpu|accelerat(?:ed|ors?)|hbm\d*|hyperscalers?|data[ -]?cent(?:er|re)s?|high bandwidth memory)\b', re.I)


def _themes(text, keywords):
    low = text.lower()
    found = {theme for theme, phrases in RELEVANCE.items() if any(p in low for p in phrases)}
    for keyword in keywords:
        if isinstance(keyword, str) and keyword.strip() and keyword.lower() in low:
            # Custom keywords establish topical context, mapped to the canonical theme.
            found.add('semiconductors')
    return sorted(found)


def discover_candidates(evidence, listings, config) -> list[dict]:
    config = config if isinstance(config, dict) else {}
    sector = config.get('sector') if isinstance(config.get('sector'), dict) else {}
    rows = _listing_rows(listings)
    catalog = {}
    non_equity = re.compile(r'\b(etf|fund|warrant|preferred shares?)\b', re.I)
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get('symbol') or row.get('ticker'))
        name = row.get('name') or row.get('title')
        if symbol and isinstance(name, str) and name.strip() and not non_equity.search(name):
            catalog[symbol] = {'symbol': symbol, 'name': name.strip()[:160], 'cik': row.get('cik') or row.get('cik_str'), 'match_name': _normalize_name(name)}
    seeds = sector.get('seed_symbols', config.get('seed_symbols', config.get('symbols', [])))
    if isinstance(seeds, str): seeds = [seeds]
    limit = sector.get('max_symbols', MAX_SYMBOLS)
    limit = max(12, min(256, limit)) if isinstance(limit, int) and not isinstance(limit, bool) else MAX_SYMBOLS
    output = {}
    for raw in seeds if isinstance(seeds, (list, tuple)) else []:
        symbol = normalize_symbol(raw)
        if symbol and symbol not in output and len(output) < limit:
            row = catalog.get(symbol, {'symbol': symbol, 'name': symbol})
            output[symbol] = {'symbol': symbol, 'name': row['name'], 'themes': [], 'source_urls': [], 'evidence_ids': [], 'catalogue_verified': symbol in catalog, 'discovery_reason': ('Explicitly configured sector seed; listing verified, sector membership is operator supplied.' if symbol in catalog else 'Explicitly configured sector seed; listing not verified.')}
    evidence = evidence if isinstance(evidence, (list, tuple)) else []
    keywords = sector.get('keywords', [])
    for item in evidence:
        if not isinstance(item, dict): continue
        url = item.get('url') or item.get('source_url')
        try:
            parts = urlsplit(url) if isinstance(url, str) else None
            valid_url = parts and parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password
        except ValueError:
            valid_url = False
        eid = item.get('id') or item.get('evidence_id')
        if not valid_url or not isinstance(eid, str) or not eid.strip(): continue
        text = ' '.join(str(item.get(k, '')) for k in ('title', 'summary', 'text', 'content', 'source_name'))
        matched = {}
        contexts = []
        for match in re.finditer(r'\b(?:NASDAQ(?:\s+GS)?|NYSE(?:\s+AMERICAN)?)\s*:\s*([A-Z][A-Z0-9.-]{0,9})\b|\$([A-Z][A-Z0-9.-]{0,9})', text):
            symbol = normalize_symbol(match.group(1) or match.group(2))
            if symbol in catalog:
                contexts.append((symbol, text, match.start(), match.end()))
        folded = _normalize_name(text)
        for symbol, row in catalog.items():
            norm = row['match_name']
            if len(norm) < 5 or norm in _AMBIGUOUS_NAMES or norm not in folded:
                continue
            for match in re.finditer(r'(?<!\w)' + re.escape(norm) + r'(?!\w)', folded):
                contexts.append((symbol, folded, match.start(), match.end()))
        for symbol, source_text, start, end in contexts:
            local = source_text[max(0, start - 220):end + 220]
            themes = _themes(local, keywords if isinstance(keywords, list) else [])
            custom_context = any(isinstance(word, str) and word.lower() in local.lower() for word in keywords)
            if themes and (_AI_CONTEXT.search(local) or custom_context):
                matched.setdefault(symbol, set()).update(themes)
        for s in matched:
            if s not in output and len(output) < limit:
                row = catalog[s]
                output[s] = {'symbol': s, 'name': row['name'], 'themes': [], 'source_urls': [], 'evidence_ids': [], 'catalogue_verified': True, 'discovery_reason': 'Matched a listed issuer to AI infrastructure themes in source evidence.'}
            candidate = output.get(s)
            if candidate:
                candidate['themes'] = sorted(set(candidate['themes']) | matched[s])
                if url not in candidate['source_urls']: candidate['source_urls'].append(url)
                if eid not in candidate['evidence_ids']: candidate['evidence_ids'].append(eid)
    seeds_order = [normalize_symbol(x) for x in seeds] if isinstance(seeds, (list, tuple)) else []
    observed = datetime.now(timezone.utc).isoformat()
    for company in output.values():
        company.update(first_observed_at=observed, valid_from=observed,
            membership_basis='current_public_evidence_or_operator_seed', historical_membership_verified=False,
            business_exposure_verified=False)
    return sorted(output.values(), key=lambda c: (0, seeds_order.index(c['symbol'])) if c['symbol'] in seeds_order else (1, c['symbol']))


def sector_sources(config, companies=None) -> list[dict]:
    queries = ['AI infrastructure stocks', 'semiconductor AI data center earnings', 'AI optical networking photonics', 'GPU accelerator data center', 'hyperscaler capital expenditure AI', 'high bandwidth memory AI servers', 'data center power cooling AI', 'AI cloud infrastructure companies', 'advanced semiconductor packaging AI', 'data center grid power capacity', 'liquid cooling server infrastructure', 'AI interconnect networking suppliers', 'foundry wafer equipment AI demand', 'DRAM NAND memory AI servers', 'data center construction infrastructure']
    sources = [{'name': 'Bing News: ' + q, 'url': 'https://www.bing.com/news/search?' + urlencode({'q': q, 'format': 'rss'}), 'kind': 'rss', 'keywords': list(dict.fromkeys(q.lower().split() + ['AI infrastructure']))} for q in queries]
    roots = [('SEC Company Filings','https://www.sec.gov/edgar/search/','article'),('NVIDIA Investor Relations','https://investor.nvidia.com/news/','article'),('AMD Investor Relations','https://ir.amd.com/news-events/press-releases','article'),('Micron Investor Relations','https://investors.micron.com/news-releases','article'),('Broadcom Investor Relations','https://investors.broadcom.com/news-releases','article'),('Arista Investor Relations','https://investors.arista.com/news-releases','article'),('Vertiv Investor Relations','https://investors.vertiv.com/news-releases','article'),('TSMC News','https://pr.tsmc.com/english/news','article')]
    sources.extend({'name': name, 'url': url, 'kind': 'rss', 'keywords': ['AI', 'data center', 'semiconductor']} for name, url in [
        ('NVIDIA announcements', 'https://nvidianews.nvidia.com/releases.xml'),
        ('HPCwire', 'https://www.hpcwire.com/feed/'),
        ('Semiconductor Engineering', 'https://semiengineering.com/feed/'),
        ('Data Center Dynamics', 'https://www.datacenterdynamics.com/en/rss/'),
    ])
    sources.extend({'name': n, 'url': u, 'kind': k, 'keywords': ['AI', 'data center', 'infrastructure']} for n,u,k in roots)
    return sources
