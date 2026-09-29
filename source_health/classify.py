"""Turn one HTTP result into an error class, and decide whether to retry it."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

STANDARD_5XX = {500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511}
REDIRECT_CODES = {301, 302, 303, 307, 308}
BLOCKED_CODES = {401, 403, 429, 451}

BLOCKED_PATTERNS = [r"/login", r"/signin", r"sign[-_ ]?in", r"log[-_ ]?in", r"captcha", r"are you a (human|robot)"]
NOT_FOUND_PATTERNS = [
    r"page not found", r"(agent|profile|user|listing) not found", r"no longer (available|active|listed|with)",
    r"(profile|page|listing|account) (is|has been) (removed|unavailable|deleted|deactivated)",
    r"we couldn.?t find", r"doesn.?t exist",
]


@dataclass
class FetchObservation:
    """What one HTTP request produced. `network_error` set means no response at all."""

    url: str
    http_status: Optional[int]
    final_status: Optional[int] = None
    final_url: Optional[str] = None
    network_error: Optional[str] = None  # dns | timeout | tls | reset | proxy_connect | proxy_auth
    bytes_expected: Optional[int] = None
    bytes_received: Optional[int] = None
    body: Optional[str] = None
    retry_after_sec: Optional[int] = None
    duration_ms: Optional[int] = None


@dataclass
class Classification:
    error_class: str  # ok | url_changed | gone | blocked | server_error | network | truncated | unknown
    reason: str
    new_url: Optional[str] = None


@dataclass
class RetryDecision:
    retry: bool
    after_sec: float
    reason: str


def _matches_any(value: str, patterns: list[str]) -> bool:
    return any(re.search(p, value, re.IGNORECASE) for p in patterns)


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _looks_like_a_profile_path(url: str) -> bool:
    """
    No per-site config exists to say what a profile URL looks like (one CSV
    "source" can span many real, unrelated websites), so this is a generic
    heuristic: a specific profile page has more than one path segment
    (/agent/jane-doe), while a listing, search or home page usually has zero
    or one (/agents, /). Good enough across ordinary sites without per-site setup.
    """
    from urllib.parse import urlsplit

    return len([s for s in urlsplit(url).path.split("/") if s]) >= 2


def _classify_redirect(original_url: str, final_url: Optional[str]) -> Classification:
    """A redirect's meaning depends on where it lands: another profile page, a login wall, or away (soft removal)."""
    if not final_url:
        return Classification("unknown", "redirect_without_location")
    if _matches_any(final_url, BLOCKED_PATTERNS):
        return Classification("blocked", "redirect_to_login_or_captcha")
    if _host(final_url) != _host(original_url):
        return Classification("gone", "redirect_to_different_host")
    if _looks_like_a_profile_path(final_url):
        return Classification("url_changed", "redirect_to_profile", new_url=final_url)
    return Classification("gone", "redirect_away_from_profile")


def classify(obs: FetchObservation) -> Classification:
    if obs.network_error:
        return Classification("network", f"network_{obs.network_error}")
    status = obs.http_status
    if status is None:
        return Classification("network", "no_response")

    if status in REDIRECT_CODES:
        from .util import normalize_url

        if obs.final_url and obs.final_status is not None and normalize_url(obs.final_url) == normalize_url(obs.url):
            return classify(replace(obs, http_status=obs.final_status, final_url=None, final_status=None))
        r = _classify_redirect(obs.url, obs.final_url)
        if r.error_class != "url_changed" or obs.final_status is None:
            return r
        landed = classify(replace(obs, url=obs.final_url, http_status=obs.final_status, final_url=None, final_status=None))
        return r if landed.error_class == "ok" else landed

    if 200 <= status < 300:
        from .util import normalize_url

        if obs.final_url and normalize_url(obs.final_url) != normalize_url(obs.url):
            r = _classify_redirect(obs.url, obs.final_url)
            if r.error_class != "url_changed":
                return r
            content = _classify_content(obs)
            return r if content.error_class == "ok" else content
        return _classify_content(obs)

    if status in (404, 410):
        return Classification("gone", f"http_{status}")
    if status in BLOCKED_CODES:
        return Classification("blocked", f"http_{status}")
    if status == 408:
        return Classification("network", "http_408")
    if status in STANDARD_5XX:
        return Classification("server_error", f"http_{status}")
    return Classification("unknown", f"http_{status}")  # 550, Oxylabs' own 524/612/613, anything nonstandard


def _classify_content(obs: FetchObservation) -> Classification:
    if obs.http_status != 200:
        return Classification("unknown", f"http_{obs.http_status}")
    if obs.bytes_expected is not None and obs.bytes_received is not None and obs.bytes_received < obs.bytes_expected:
        return Classification("truncated", "body_shorter_than_content_length")
    if obs.body and _matches_any(obs.body, NOT_FOUND_PATTERNS):
        return Classification("gone", "soft_404_pattern")
    return Classification("ok", "http_200")


def retry_decision(c: Classification, obs: FetchObservation, retry_no: int, max_retries: int = 3) -> RetryDecision:
    """Blocks are surfaced and respected, never evaded. Gone is not retried; the next run confirms it."""
    if c.error_class == "network" and obs.network_error == "proxy_auth":
        # Wrong credentials won't fix themselves on a backoff timer — surface it immediately.
        return RetryDecision(False, 0, "proxy_auth_needs_review")
    backoff = min(300, 5 * 2**retry_no)
    retry_after = obs.retry_after_sec if obs.retry_after_sec is not None else backoff
    by_class = {
        "ok": RetryDecision(False, 0, "success"),
        "url_changed": RetryDecision(False, 0, "success_new_url"),
        "gone": RetryDecision(False, 0, "confirm_on_next_run"),
        "blocked": RetryDecision(True, retry_after, "rate_limited_honor_retry_after")
        if obs.http_status == 429
        else RetryDecision(False, 0, "blocked_needs_review"),
        "server_error": RetryDecision(True, retry_after, "transient_server_error"),
        "network": RetryDecision(True, backoff, "transient_network_error"),
        "truncated": RetryDecision(True, 0, "incomplete_body"),
        "unknown": RetryDecision(True, backoff, "unmapped_code_single_retry"),
    }
    d = by_class[c.error_class]
    limit = 1 if c.error_class == "unknown" else max_retries
    return RetryDecision(False, 0, "retries_exhausted") if d.retry and retry_no >= limit else d


def severity_tier(role: Optional[str]) -> str:
    return "truth_at_risk" if role == "primary" else "completeness_at_risk"
