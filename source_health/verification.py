"""
Turn one real fetch's Classification into the three fields ReRun writes:
status, updated_status_code, verification_error.

One-shot, no history: a single confirmed 404 is inactive immediately; a
network-level failure never invents an HTTP code. This reuses classify.py's
ErrorClass unchanged (including its redirect handling — a same-host redirect
to a shallow listing page, or to a different host entirely, is already
"gone", the same as a plain 404; a redirect to another specific-looking page
on the same host is already "url_changed") rather than a second classifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .classify import Classification, FetchObservation

# obs.network_error -> the label stored in verification_error.
NETWORK_ERROR_LABELS = {
    "dns": "DNS_ERROR",
    "timeout": "TIMEOUT",
    "tls": "TLS_ERROR",
    "reset": "CONNECTION_RESET",
    "proxy_connect": "PROXY_CONNECT_ERROR",
    "proxy_auth": "PROXY_AUTH_ERROR",
}


@dataclass
class Verification:
    status: str  # active | inactive | suspect
    updated_status_code: Optional[int]
    verification_error: Optional[str]
    comment: str  # human-readable, so a console/log can show *why* without reading code


# classify.py's Classification.reason -> a plain-English explanation. Every reason
# classify() can produce is listed here (checked by test_verification.py), so a
# reason it doesn't recognize falls back to something honest rather than blank.
_REASON_COMMENTS = {
    "http_200": "Live page (200) — profile exists.",
    "redirect_to_profile": "Redirected to another specific profile page on the same site — still active.",
    "redirect_away_from_profile": "Redirected to a generic/listing page on the same site (not an individual profile) — profile no longer exists.",
    "redirect_to_different_host": "Redirected to a different website entirely — profile no longer exists.",
    "redirect_to_login_or_captcha": "Redirected to a login/captcha page — blocked, could not verify.",
    "redirect_without_location": "Redirect with no destination given — could not verify.",
    "soft_404_pattern": "Page loaded (200) but its text says the profile is gone.",
    "body_shorter_than_content_length": "Page body was incomplete/cut short — could not verify.",
    "no_response": "No response received — could not verify.",
    "http_408": "Request timed out (408) — could not verify.",
}


def _describe(c: Classification, obs: FetchObservation) -> str:
    if c.error_class == "network":
        label = NETWORK_ERROR_LABELS.get(obs.network_error, "NETWORK_ERROR")
        return f"Network error ({label}) — could not reach the source."
    if c.reason in _REASON_COMMENTS:
        return _REASON_COMMENTS[c.reason]
    if c.reason.startswith("http_"):
        code = c.reason[len("http_") :]
        by_class = {
            "gone": f"HTTP {code} — confirmed not found.",
            "blocked": f"HTTP {code} — blocked/forbidden, could not verify.",
            "server_error": f"HTTP {code} — source server error, could not verify.",
            "unknown": f"HTTP {code} — unrecognized/nonstandard response, could not verify.",
        }
        if c.error_class in by_class:
            return by_class[c.error_class]
    return f"{c.error_class}: {c.reason}"  # fallback — never blank, even for an unforeseen reason


def verify(c: Classification, obs: FetchObservation) -> Verification:
    comment = _describe(c, obs)

    if c.error_class == "network":
        label = NETWORK_ERROR_LABELS.get(obs.network_error, "NETWORK_ERROR")
        return Verification("suspect", None, label, comment)

    if c.error_class == "url_changed":
        # The profile moved to a specific-looking page on the same host, and that
        # page is confirmed valid: active, and the code shown is what that new
        # page actually returned.
        return Verification("active", obs.final_status, None, comment)

    if c.error_class == "ok":
        return Verification("active", obs.http_status, None, comment)

    if c.error_class == "gone":
        # 404/410, a "not found" page, or a redirect away from the profile
        # (different host, or a shallow same-host listing page) — all confirmed gone.
        # Store the code the URL itself actually returned (a 404, or a 301/302 that
        # led nowhere), not a code we invented.
        return Verification("inactive", obs.http_status, None, comment)

    # blocked (401/403/429/451/login-wall), server_error (5xx), truncated (200 but
    # incomplete), unknown (550, Oxylabs' own 612/613/524, anything unmapped):
    # a real response came back, but it neither confirms the profile is there nor
    # that it's gone.
    return Verification("suspect", obs.http_status, None, comment)
