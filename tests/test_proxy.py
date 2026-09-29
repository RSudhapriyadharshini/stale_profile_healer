import os
import unittest
from unittest import mock

import requests

from source_health.proxy import (
    DisallowedUrl,
    ProxyConfig,
    fetch_with_retries,
    make_direct_fetch_impl,
    make_fetch_impl,
    observe,
    proxy_config_from_env,
    require_proxy_config,
    url_allowed,
)

ENV_KEYS = ("PROXY_ENDPOINT", "PROXY_USERNAME", "PROXY_PASSWORD", "PROXY_SCHEME", "PROXY_VERIFY_TLS", "PROXY_GEO_LOCATION")


class ProxyConfigTest(unittest.TestCase):
    def setUp(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def test_no_endpoint_is_not_configured(self):
        self.assertIsNone(proxy_config_from_env())
        with self.assertRaises(Exception):
            require_proxy_config()

    def test_oxylabs_defaults(self):
        os.environ.update(PROXY_ENDPOINT="realtime.oxylabs.io:60000", PROXY_USERNAME="bob", PROXY_PASSWORD="s3cr3t")
        cfg = proxy_config_from_env()
        self.assertEqual((cfg.scheme, cfg.verify_tls), ("https", False))
        self.assertTrue(cfg.url.startswith("https://bob:s3cr3t@realtime.oxylabs.io:60000"))

    def test_display_hides_credentials(self):
        cfg = ProxyConfig(endpoint="realtime.oxylabs.io:60000", username="bob", password="s3cr3t")
        self.assertNotIn("bob", cfg.display)
        self.assertNotIn("s3cr3t", cfg.display)

    def test_job_header_from_env(self):
        os.environ.update(PROXY_ENDPOINT="realtime.oxylabs.io:60000", PROXY_GEO_LOCATION="Germany")
        self.assertEqual(proxy_config_from_env().extra_headers, {"x-oxylabs-geo-location": "Germany"})


class FetchWithRetriesTest(unittest.TestCase):
    def test_refuses_disallowed_url_before_any_request(self):
        calls = []

        def fetch(url, timeout, headers):
            calls.append(url)
            return 200, {}, b"ok"

        with self.assertRaises(DisallowedUrl):
            fetch_with_retries("https://evil.example/x", fetch, ["good.example"])
        self.assertEqual(calls, [])

    def test_ok(self):
        def fetch(url, timeout, headers):
            return 200, {}, b"hello"

        r = fetch_with_retries("https://good.example/x", fetch, ["good.example"])
        self.assertEqual((r.classification.error_class, r.observation.http_status), ("ok", 200))

    def test_retries_network_error_then_succeeds(self):
        calls = {"n": 0}

        def fetch(url, timeout, headers):
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ConnectTimeout("slow")
            return 200, {}, b"ok"

        r = fetch_with_retries("https://good.example/x", fetch, ["good.example"], sleep=lambda s: None)
        self.assertEqual(r.classification.error_class, "ok")
        self.assertEqual(calls["n"], 3)

    def test_407_is_proxy_auth_not_site_error(self):
        def fetch(url, timeout, headers):
            return 407, {}, b""

        r = fetch_with_retries("https://good.example/x", fetch, ["good.example"], sleep=lambda s: None)
        self.assertEqual((r.classification.error_class, r.classification.reason), ("network", "network_proxy_auth"))

    def test_401_is_also_proxy_auth(self):
        """Oxylabs' Proxy Endpoint rejects bad credentials with plain 401, not the RFC 7235 407 — verified live."""

        def fetch(url, timeout, headers):
            return 401, {}, b""

        r = fetch_with_retries("https://good.example/x", fetch, ["good.example"], sleep=lambda s: None)
        self.assertEqual((r.classification.error_class, r.classification.reason), ("network", "network_proxy_auth"))

    def test_proxy_error_carrying_401_is_proxy_auth_not_proxy_connect(self):
        """An HTTPS target tunnels via CONNECT; Oxylabs' 401 then surfaces inside a ProxyError, not as a plain status."""

        def fetch(url, timeout, headers):
            raise requests.exceptions.ProxyError("Unable to connect to proxy: Tunnel connection failed: 401 Unauthorized")

        r = fetch_with_retries("https://good.example/x", fetch, ["good.example"], sleep=lambda s: None)
        self.assertEqual((r.classification.error_class, r.classification.reason), ("network", "network_proxy_auth"))

    def test_403_never_retried(self):
        calls = {"n": 0}

        def fetch(url, timeout, headers):
            calls["n"] += 1
            return 403, {}, b""

        fetch_with_retries("https://good.example/x", fetch, ["good.example"], sleep=lambda s: None)
        self.assertEqual(calls["n"], 1)


class RedirectSafetyTest(unittest.TestCase):
    def test_off_domain_redirect_is_recorded_but_never_followed(self):
        calls = []

        def fetch(url, timeout, headers):
            calls.append(url)
            if url == "https://good.example/a":
                return 302, {"location": "https://evil.example/steal"}, b""
            return 200, {}, b"should never be requested"

        obs = observe("https://good.example/a", fetch, is_allowed=lambda u: url_allowed(u, ["good.example"]))
        self.assertEqual(calls, ["https://good.example/a"])  # the off-domain target was never requested
        self.assertEqual(obs.final_url, "https://evil.example/steal")
        self.assertIsNone(obs.final_status)  # not followed, so no status from it

    def test_same_domain_redirect_is_followed(self):
        def fetch(url, timeout, headers):
            if url == "https://good.example/a":
                return 302, {"location": "https://good.example/b"}, b""
            return 200, {}, b"landed"

        obs = observe("https://good.example/a", fetch, is_allowed=lambda u: url_allowed(u, ["good.example"]))
        self.assertEqual((obs.final_url, obs.final_status, obs.body), ("https://good.example/b", 200, "landed"))


class MakeFetchImplTest(unittest.TestCase):
    """Checks the real requests.Session wiring, with Session.get mocked (no real network)."""

    def test_https_scheme_verify_off_and_headers_merged(self):
        captured = {}

        def fake_get(self, url, **kw):
            captured["proxies"], captured["trust_env"] = self.proxies, self.trust_env
            captured["verify"], captured["allow_redirects"], captured["headers"] = kw["verify"], kw["allow_redirects"], kw["headers"]
            r = requests.Response()
            r.status_code = 200
            r._content = b"ok"
            return r

        cfg = ProxyConfig(endpoint="realtime.oxylabs.io:60000", username="bob", password="s3cr3t", extra_headers={"x-oxylabs-geo-location": "US"})
        with mock.patch.object(requests.Session, "get", fake_get):
            status, headers, body = make_fetch_impl(cfg)("https://example.com/x", 10, {"user-agent": "demo"})
        self.assertEqual((status, body), (200, b"ok"))
        self.assertTrue(captured["proxies"]["https"].startswith("https://bob:s3cr3t@realtime.oxylabs.io:60000"))
        self.assertFalse(captured["trust_env"])
        self.assertFalse(captured["verify"])
        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(captured["headers"], {"user-agent": "demo", "x-oxylabs-geo-location": "US"})



class MakeDirectFetchImplTest(unittest.TestCase):
    """--no-proxy: a genuinely direct connection, no proxy involved at all."""

    def test_no_proxies_set_and_ambient_env_ignored(self):
        captured = {}

        def fake_get(self, url, **kw):
            captured["proxies"], captured["trust_env"] = self.proxies, self.trust_env
            captured["verify"] = kw["verify"]
            r = requests.Response()
            r.status_code = 200
            r._content = b"direct"
            return r

        with mock.patch.object(requests.Session, "get", fake_get):
            status, headers, body = make_direct_fetch_impl()("https://example.com/x", 10, {})
        self.assertEqual((status, body), (200, b"direct"))
        self.assertEqual(captured["proxies"], {})
        self.assertFalse(captured["trust_env"])
        self.assertTrue(captured["verify"])  # verify_tls=True by default for direct connections

    def test_verify_tls_false_is_honored(self):
        captured = {}

        def fake_get(self, url, **kw):
            captured["verify"] = kw["verify"]
            r = requests.Response()
            r.status_code = 200
            return r

        with mock.patch.object(requests.Session, "get", fake_get):
            make_direct_fetch_impl(verify_tls=False)("https://example.com/x", 10, {})
        self.assertFalse(captured["verify"])

if __name__ == "__main__":
    unittest.main()
