"""The exact ReRun mapping table from the spec: one HTTP result -> one verification."""

import unittest

from source_health.classify import FetchObservation, classify
from source_health.verification import verify


def obs(**kw) -> FetchObservation:
    kw.setdefault("http_status", 200)
    kw.setdefault("url", "https://example.com/profile/123")
    return FetchObservation(**kw)


def verify_status(status_code=None, network_error=None, **kw) -> str:
    o = obs(http_status=status_code, network_error=network_error, **kw)
    return verify(classify(o), o).status


class HttpCodeMappingTest(unittest.TestCase):
    def test_200_is_active(self):
        self.assertEqual(verify_status(200), "active")

    def test_404_is_inactive(self):
        self.assertEqual(verify_status(404), "inactive")

    def test_other_codes_are_suspect(self):
        for code in (403, 302, 503, 550, 500, 502, 504, 429, 451):
            with self.subTest(code=code):
                self.assertEqual(verify_status(code), "suspect")

    def test_410_is_inactive_same_as_404(self):
        """HTTP 410 Gone is, semantically, the same confirmation as 404 — not a generic error."""
        self.assertEqual(verify_status(410), "inactive")

    def test_updated_status_code_is_the_raw_code(self):
        o = obs(http_status=404)
        v = verify(classify(o), o)
        self.assertEqual((v.status, v.updated_status_code, v.verification_error), ("inactive", 404, None))

        o = obs(http_status=550)
        v = verify(classify(o), o)
        self.assertEqual((v.status, v.updated_status_code, v.verification_error), ("suspect", 550, None))


class NetworkErrorMappingTest(unittest.TestCase):
    def test_never_invents_an_http_code(self):
        for kind, label in [
            ("timeout", "TIMEOUT"),
            ("dns", "DNS_ERROR"),
            ("reset", "CONNECTION_RESET"),
            ("tls", "TLS_ERROR"),
            ("proxy_connect", "PROXY_CONNECT_ERROR"),
            ("proxy_auth", "PROXY_AUTH_ERROR"),
        ]:
            with self.subTest(kind=kind):
                o = obs(http_status=None, network_error=kind)
                v = verify(classify(o), o)
                self.assertEqual((v.status, v.updated_status_code, v.verification_error), ("suspect", None, label))


class RedirectAndSoftRemovalTest(unittest.TestCase):
    """Section 7: a URL can look like 200/301/302 while the individual profile is actually gone."""

    def test_redirect_to_generic_listing_same_host_is_inactive(self):
        """The Compass case: /agents/andrew-sohn/ -> 302 -> /agents/ (a listing page, not a profile)."""
        o = obs(url="https://www.compass.com/agents/andrew-sohn/", http_status=302, final_url="https://www.compass.com/agents/", final_status=200)
        v = verify(classify(o), o)
        self.assertEqual((v.status, v.updated_status_code), ("inactive", 302))
        self.assertIn("generic/listing page", v.comment)

    def test_elliman_404_is_inactive_with_a_comment(self):
        """The Elliman case: Page Not Found."""
        o = obs(url="https://www.elliman.com/agent/ace-lahli/1024440", http_status=404)
        v = verify(classify(o), o)
        self.assertEqual((v.status, v.updated_status_code), ("inactive", 404))
        self.assertIn("confirmed not found", v.comment)

    def test_redirect_to_another_specific_profile_same_host_is_active(self):
        o = obs(url="https://www.compass.com/agents/old-slug/", http_status=301, final_url="https://www.compass.com/agents/new-slug/", final_status=200)
        v = verify(classify(o), o)
        self.assertEqual((v.status, v.updated_status_code), ("active", 200))  # code of the page it actually landed on

    def test_soft_404_text_on_200_is_inactive(self):
        o = obs(http_status=200, body="Sorry, this agent is no longer with us.")
        v = verify(classify(o), o)
        self.assertEqual((v.status, v.updated_status_code), ("inactive", 200))

    def test_truncated_200_is_suspect_not_active(self):
        o = obs(http_status=200, bytes_expected=1000, bytes_received=10)
        v = verify(classify(o), o)
        self.assertEqual(v.status, "suspect")


class CommentCoverageTest(unittest.TestCase):
    """Every classification this system can produce has a non-blank, specific comment — nothing tracked silently."""

    CASES = [
        obs(http_status=200),
        obs(http_status=404),
        obs(http_status=410),
        obs(http_status=403),
        obs(http_status=550),
        obs(http_status=503),
        obs(http_status=408),
        obs(http_status=302, final_url="https://example.com/profile/other-guy", final_status=200),
        obs(http_status=302, final_url="https://example.com/agents", final_status=200),
        obs(http_status=302, final_url="https://other-site.com/x", final_status=200),
        obs(http_status=302, final_url="https://example.com/login", final_status=200),
        obs(http_status=302, final_url=None),
        obs(http_status=200, body="This agent no longer with us."),
        obs(http_status=200, bytes_expected=100, bytes_received=5),
        obs(http_status=None, network_error="timeout"),
        obs(http_status=None, network_error="dns"),
        obs(http_status=None, network_error="reset"),
        obs(http_status=None, network_error="tls"),
        obs(http_status=None, network_error="proxy_connect"),
        obs(http_status=None, network_error="proxy_auth"),
        obs(http_status=None, network_error=None),  # "no_response"
    ]

    def test_every_case_has_a_specific_non_generic_comment(self):
        for o in self.CASES:
            with self.subTest(http_status=o.http_status, final_url=o.final_url, network_error=o.network_error, body=o.body):
                v = verify(classify(o), o)
                self.assertTrue(v.comment)
                self.assertNotIn(":", v.comment.split(" ")[0])  # not the raw "error_class: reason" fallback


if __name__ == "__main__":
    unittest.main()
