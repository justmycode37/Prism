"""Bounded normalization boundary for evidence returned by external adapters.

Adapters must translate their response into the documented record shape before
calling :func:`normalize_imported_records`. This module does not make requests,
read credentials, or guess provider response schemas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import ipaddress
import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_ALLOWED_KINDS = frozenset({"news", "article", "youtube_transcript"})
_RECORD_FIELDS = frozenset({"source_url", "source_kind", "title", "content", "published_at", "import_reference"})
_TRACKERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VOLATILE_ID_FIELDS = frozenset({"observed_at", "first_observed_at", "available_at"})


def _canonical_url(value: str, *, youtube: bool = False) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() != "https" or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            return None
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if youtube:
        if host not in _YOUTUBE_HOSTS:
            return None
        video_id = None
        path = [segment for segment in parts.path.split("/") if segment]
        if host == "youtu.be" and len(path) == 1:
            video_id = path[0]
        elif parts.path == "/watch":
            ids = [value for key, value in parse_qsl(parts.query, keep_blank_values=True) if key == "v"]
            if len(ids) == 1:
                video_id = ids[0]
        elif len(path) == 2 and path[0] in {"shorts", "embed", "live"}:
            video_id = path[1]
        if not video_id or not _VIDEO_ID.fullmatch(video_id):
            return None
        return "https://www.youtube.com/watch?v=" + video_id
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return None
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in _TRACKERS]
    return urlunsplit(("https", host, parts.path or "/", urlencode(query), ""))


def _published(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evidence_id(item: Mapping[str, Any]) -> str:
    """Stable evidence identity, independent of local observation timestamps."""
    snapshot = {key: value for key, value in item.items() if key not in _VOLATILE_ID_FIELDS}
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "ev_" + sha256(encoded).hexdigest()[:24]


def _string_size(value: str, limit: int) -> int:
    """Return UTF-8 bytes up to limit + 1 without encoding an unbounded string."""
    size = 0
    for character in value:
        size += len(character.encode("utf-8"))
        if size > limit:
            return size
    return size


def _bounded_limit(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def normalize_imported_records(
    records: Iterable[Mapping[str, Any]] | None,
    *,
    provider: str,
    max_items: int = 32,
    max_total_bytes: int = 256_000,
    max_content_chars: int = 24_000,
) -> dict[str, list[dict[str, Any]]]:
    """Validate and normalize already-imported external evidence.

    Each record uses only this normalized shape: ``source_url``, ``source_kind``
    (``news``, ``article``, or ``youtube_transcript``), ``title``, ``content``,
    optional ``published_at`` and optional ``import_reference``. Transcript
    adapters must provide the YouTube URL and transcript text in these fields.
    Unknown provider fields cause the record to be skipped. This boundary
    performs no API calls.
    """
    max_items = _bounded_limit(max_items, "max_items", 1, 32)
    max_total_bytes = _bounded_limit(max_total_bytes, "max_total_bytes", 1, 256_000)
    max_content_chars = _bounded_limit(max_content_chars, "max_content_chars", 1, 48_000)
    if not isinstance(provider, str) or not provider.strip() or len(provider) > 80:
        raise ValueError("provider must contain 1 to 80 characters")
    provider = re.sub(r"[^A-Za-z0-9_.:-]", "_", provider.strip())[:80]

    evidence: list[dict[str, Any]] = []
    errors: list[str] = []
    if records is None:
        return {"evidence": evidence, "errors": errors}
    if isinstance(records, (str, bytes, bytearray, Mapping)) or not isinstance(records, Iterable):
        return {"evidence": evidence, "errors": ["Imported evidence must be a sequence of normalized records."]}
    if not isinstance(records, (list, tuple)):
        return {"evidence": evidence, "errors": ["Imported evidence must be materialized before normalization."]}
    if len(records) > max_items:
        errors.append("Imported item budget exhausted; excess records were not processed.")
    used_bytes = 0
    seen: dict[tuple[str, str], int] = {}
    for record in records[:max_items]:
        if not isinstance(record, Mapping):
            errors.append("Imported record is not an object.")
            continue
        if any(key not in _RECORD_FIELDS for key in record):
            errors.append("Imported record contains fields outside the normalized shape.")
            continue
        kind = record.get("source_kind")
        if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
            errors.append("Imported record has an unsupported source kind.")
            continue
        raw_url = record.get("source_url")
        source_url = _canonical_url(raw_url, youtube=kind == "youtube_transcript") if isinstance(raw_url, str) else None
        if source_url is None:
            errors.append("Imported record has an invalid public source URL.")
            continue
        title = record.get("title", "")
        content = record.get("content", "")
        published_raw = record.get("published_at", "")
        reference = record.get("import_reference", "")
        if not all(isinstance(value, str) for value in (title, content, published_raw, reference)):
            errors.append("Imported record text fields must be strings.")
            continue
        if not content.strip():
            errors.append("Imported record has no content.")
            continue
        title = title.strip()
        reference = reference.strip()[:128]
        raw_fields = (raw_url, title, content, published_raw, reference, provider)
        record_bytes = 0
        for value in raw_fields:
            record_bytes += _string_size(value, max_total_bytes - used_bytes - record_bytes)
            if used_bytes + record_bytes > max_total_bytes:
                break
        if used_bytes + record_bytes > max_total_bytes:
            errors.append("Imported byte budget exhausted; remaining records were not processed.")
            break
        used_bytes += record_bytes

        truncated = len(content) > max_content_chars
        normalized_content = content[:max_content_chars]
        published = _published(published_raw)
        if published_raw.strip() and published is None:
            errors.append("Imported publication date was invalid and was omitted.")
        content_hash = sha256(re.sub(r"\s+", " ", normalized_content).strip().encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        family = urlsplit(source_url).hostname or ""
        is_primary = (family == "sec.gov" or family.endswith(".sec.gov") or "investor" in family)
        item = {
            "source_url": source_url,
            "source_kind": kind,
            "title": title[:400],
            "content": normalized_content,
            "observed_at": now,
            "symbols": [],
            "published_at": published,
            "event_time": published,
            "first_observed_at": now,
            "available_at": now,
            "content_hash": content_hash,
            "event_identity": content_hash,
            "source_family": family,
            "primary_source": is_primary,
            "source_origin": "imported",
            "acquisition_method": "normalized_import",
            "import_provider": provider,
            "import_reference": reference or None,
        }
        if truncated:
            item["content_truncated"] = True
            item["original_content_chars"] = len(content)
        item["id"] = evidence_id(item)

        key = (kind, source_url)
        previous_index = seen.get(key)
        if previous_index is None:
            seen[key] = len(evidence)
            evidence.append(item)
            continue
        previous = evidence[previous_index]
        previous_score = (len(previous["content"]), bool(previous.get("published_at")))
        item_score = (len(item["content"]), bool(item.get("published_at")))
        selected = item if item_score > previous_score else previous
        selected = dict(selected)
        references = list(dict.fromkeys(
            ([previous.get("import_reference")] if previous.get("import_reference") else [])
            + previous.get("import_references", [])
            + ([item.get("import_reference")] if item.get("import_reference") else [])
            + item.get("import_references", [])
        ))
        providers = list(dict.fromkeys(previous.get("import_providers", [previous["import_provider"]])
                                       + item.get("import_providers", [item["import_provider"]])))
        if references:
            selected["import_references"] = references[:16]
        if len(providers) > 1:
            selected["import_providers"] = providers[:16]
        selected["id"] = evidence_id(selected)
        evidence[previous_index] = selected
    return {"evidence": evidence, "errors": list(dict.fromkeys(errors))[:32]}
