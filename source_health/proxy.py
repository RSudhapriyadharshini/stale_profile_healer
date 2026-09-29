"""
The real network layer: every request goes through this, and only this.

Oxylabs' Web Scraper API "Proxy Endpoint":
https://developers.oxylabs.io/products/web-scraper-api/integration-methods/proxy-endpoint

    curl -k -x https://realtime.oxylabs.io:60000 -U 'USER:PASS' 'https://target'

  -x https://...  -> PROXY_SCHEME=https: TLS to the proxy node itself
  -k              -> PROXY_VERIFY_TLS=false: Oxylabs says to ignore certificates
  -U 'USER:PASS'  -> PROXY_USERNAME / PROXY_PASSWORD, embedded in the proxy URL
  job parameters  -> optional x-oxylabs-* headers (geo-location, render, ...)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote, urljoin

import requests
import urllib3

from .classify import Classification, FetchObservation, classify, retry_decision

# Oxylabs' own docs say to ignore certificates for the Proxy Endpoint (curl's -k).
# We do that on purpose (PROXY_VERIFY_TLS=false by default) — silence the resulting warning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_OXYLABS_HEADER_ENV = {
    "PROXY_GEO_LOCATION": "x-oxylabs-geo-location",
    "PROXY_USER_AGENT_TYPE": "x-oxylabs-user-agent-type",
    "PROXY_RENDER": "x-oxylabs-render",
    "PROXY_PARSE": "x-oxylabs-parse",
    "PROXY_PARSER_TYPE": "x-oxylabs-parser-type",
}


class ProxyNotConfigured(Exception):
    """PROXY_ENDPOINT is missing. Requests are refused rather than sent directly."""


class DisallowedUrl(Exception):
    """The URL is not on this source's allowed hosts. Refused before any request."""


@dataclass
class ProxyConfig:
    endpoint: str  # host:port, e.g. "realtime.oxylabs.io:60000"
    username: Optional[str] = None
    password: Optional[str] = None
    scheme: str = "https"
    verify_tls: bool = False
    extra_headers: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        auth = f"{quote(self.username, safe='')}:{quote(self.password or '', safe='')}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.endpoint}"

    @property
    def display(self) -> str:
        """Never includes the credentials."""
        note = "" if self.verify_tls else " (TLS verify off, per Oxylabs)"
        return f"{self.scheme}://{self.endpoint}{note}"


def load_env(path: str = ".env") -> None:
    """Tiny .env reader — KEY=VALUE lines into os.environ, quotes stripped. No extra dependency."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _bool_env(name: str, default: bool) -> bool:
    return {"true": True, "1": True, "false": False, "0": False}.get(os.environ.get(name, "").strip().lower(), default)


def proxy_config_from_env() -> Optional[ProxyConfig]:
    endpoint = os.environ.get("PROXY_ENDPOINT", "").strip()
    if not endpoint:
        return None
    return ProxyConfig(
        endpoint=endpoint,
        username=os.environ.get("PROXY_USERNAME", "").strip() or None,
        password=os.environ.get("PROXY_PASSWORD", "").strip() or None,
        scheme=os.environ.get("PROXY_SCHEME", "https").strip() or "https",
        verify_tls=_bool_env("PROXY_VERIFY_TLS", default=False),
        extra_headers={h: os.environ[e] for e, h in _OXYLABS_HEADER_ENV.items() if os.environ.get(e, "").strip()},
    )


def require_proxy_config() -> ProxyConfig:
    cfg = proxy_config_from_env()
    if cfg is None:
        raise ProxyNotConfigured("Set PROXY_ENDPOINT, PROXY_USERNAME and PROXY_PASSWORD in .env — nothing is ever sent unproxied.")
    return cfg


def url_allowed(url: str, allowed_hosts: list[str]) -> bool:
    from .util import url_allowed as _url_allowed

    return _url_allowed(url, allowed_hosts)


# (url, timeout, headers) -> (status, headers_dict, body_bytes). Must NOT follow redirects.
FetchImpl = Callable[[str, float, dict], tuple[int, dict, bytes]]


def _requests_fetch(config: ProxyConfig) -> FetchImpl:
    session = requests.Session()
    session.trust_env = False  # ignore any HTTP_PROXY/HTTPS_PROXY already on the machine
    session.proxies = {"http": config.url, "https": config.url}

    def fetch(url: str, timeout: float, headers: dict) -> tuple[int, dict, bytes]:
        merged = {**headers, **config.extra_headers}
        res = session.get(url, timeout=timeout, headers=merged, allow_redirects=False, verify=config.verify_tls)
        return res.status_code, {k.lower(): v for k, v in res.headers.items()}, res.content

    return fetch


def make_direct_fetch_impl(verify_tls: bool = True) -> FetchImpl:
    """
    No proxy at all — a plain direct connection. Only for local testing
    (e.g. against httpstat.us or a local server) when explicitly requested
    with --no-proxy; never the default. `trust_env=False` still ignores any
    ambient HTTP_PROXY/HTTPS_PROXY, so "direct" always means genuinely direct.
    """
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}

    def fetch(url: str, timeout: float, headers: dict) -> tuple[int, dict, bytes]:
        res = session.get(url, timeout=timeout, headers=headers, allow_redirects=False, verify=verify_tls)
        return res.status_code, {k.lower(): v for k, v in res.headers.items()}, res.content

    return fetch


def observe(url: str, fetch_impl: FetchImpl, timeout_sec: float = 20.0, max_redirects: int = 5, is_allowed: Optional[Callable[[str], bool]] = None) -> FetchObservation:
    """One request, following redirects by hand so the code the URL itself returned (e.g. 302) is kept separately."""
    started = time.monotonic()
    chain: list[str] = []
    current = url
    first_status: Optional[int] = None

    def elapsed() -> int:
        return round((time.monotonic() - started) * 1000)

    try:
        hop = 0
        while True:
            status, headers, body = fetch_impl(current, timeout_sec, {})
            if status in (401, 407):
                # Standard forward proxies use 407; Oxylabs' own Proxy Endpoint uses plain 401.
                raise _NetworkError("proxy_auth", f"proxy authentication required or rejected ({status})")
            first_status = first_status if first_status is not None else status
            location = headers.get("location")
            if 300 <= status < 400 and location and hop < max_redirects:
                target = urljoin(current, location)
                chain.append(current)
                current = target
                if is_allowed and not is_allowed(target):
                    return FetchObservation(url=url, http_status=first_status, final_url=target, duration_ms=elapsed())
                hop += 1
                continue
            length = headers.get("content-length")
            retry_after = headers.get("retry-after")
            return FetchObservation(
                url=url,
                http_status=first_status,
                final_status=status if chain else None,
                final_url=current if chain else None,
                bytes_expected=int(length) if length else None,
                bytes_received=len(body),
                body=body.decode("utf-8", errors="replace"),
                retry_after_sec=int(retry_after) if retry_after and retry_after.isdigit() else None,
                duration_ms=elapsed(),
            )
    except _NetworkError as err:
        return FetchObservation(url=url, http_status=first_status, network_error=err.kind, final_url=chain[-1] if chain else None, duration_ms=elapsed())
    except Exception as err:  # noqa: BLE001 — any transport failure is "no response"
        return FetchObservation(url=url, http_status=first_status, network_error=_network_error_kind(err), final_url=chain[-1] if chain else None, duration_ms=elapsed())


class _NetworkError(Exception):
    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message)
        self.kind = kind


def _network_error_kind(err: Exception) -> str:
    if isinstance(err, requests.exceptions.ProxyError):
        # An HTTPS target tunnels through the proxy via CONNECT; if the proxy rejects the
        # tunnel because of bad credentials, that shows up as a ProxyError whose message
        # carries the proxy's own status (401 for Oxylabs, 407 for a standard forward proxy)
        # rather than as a plain response status. Same real distinction, different surface.
        if "401" in str(err) or "407" in str(err):
            return "proxy_auth"
        return "proxy_connect"
    if isinstance(err, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timeout"
    if isinstance(err, requests.exceptions.SSLError):
        return "tls"
    if isinstance(err, requests.exceptions.ConnectionError) and "Name or service not known" in str(err):
        return "dns"
    return "reset"


@dataclass
class FetchResult:
    observation: FetchObservation
    classification: Classification
    attempts: int


def fetch_with_retries(
    url: str,
    fetch_impl: FetchImpl,
    allowed_hosts: list[str],
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    on_attempt: Optional[Callable[[int, Classification, float], None]] = None,
) -> FetchResult:
    """Fetch one URL through the proxy, retrying transient failures. Never called for a disallowed URL."""
    if not url_allowed(url, allowed_hosts):
        raise DisallowedUrl(f"{url} is not on the allowed hosts {allowed_hosts}")

    def allowed(u: str) -> bool:
        return url_allowed(u, allowed_hosts)

    retry_no = 0
    while True:
        obs = observe(url, fetch_impl, is_allowed=allowed)
        c = classify(obs)
        d = retry_decision(c, obs, retry_no, max_retries)
        if on_attempt:
            on_attempt(retry_no, c, d.after_sec if d.retry else 0)
        if not d.retry:
            return FetchResult(obs, c, retry_no + 1)
        sleep(d.after_sec)
        retry_no += 1


def make_fetch_impl(config: ProxyConfig) -> FetchImpl:
    return _requests_fetch(config)
