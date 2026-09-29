"""Small, shared helpers: URL matching keys, domain allow-listing, person-name matching."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAM = re.compile(r"^(utm_[a-z0-9_]*|gclid|fbclid|msclkid|mc_cid|mc_eid|ref|ref_src)$", re.IGNORECASE)
_NAME_NOISE = {"jr", "sr", "ii", "iii", "iv", "realtor", "broker", "agent"}


def normalize_url(url: str) -> str:
    """
    Matching key for a URL: http/https, www., host case, default ports, trailing
    slashes, #fragments and tracking params (utm_*, gclid, ...) don't make the
    same page look like two. Path case is kept — some sites treat it as meaningful.
    Links are still fetched exactly as stored; this key is only for matching.
    """
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme.lower() == "http" else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAM.match(k)])
    return urlunsplit((scheme, netloc, path, query, ""))


def url_allowed(url: str, allowed_hosts: list[str]) -> bool:
    """True only for http(s) URLs on an allowed host or a subdomain of one. Empty list allows nothing."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in (h.lower().lstrip(".") for h in allowed_hosts))


def normalize_person_name(name: Optional[str]) -> str:
    """'John Smith, REALTOR (r)' -> 'john smith'."""
    if not name:
        return ""
    tokens = re.sub(r"[^a-z]+", " ", name.lower()).split()
    return " ".join(t for t in tokens if t not in _NAME_NOISE)


def same_person_name(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = normalize_person_name(a), normalize_person_name(b)
    return bool(na) and na == nb
