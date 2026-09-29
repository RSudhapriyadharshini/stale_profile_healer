import unittest

from source_health.classify import FetchObservation, classify, retry_decision


def obs(**kw) -> FetchObservation:
    kw.setdefault("http_status", 200)
    kw.setdefault("url", "https://www.redfin.com/agents/jane")
    return FetchObservation(**kw)


class ClassifyTest(unittest.TestCase):
    def test_status_codes(self):
        cases = [
            (404, "gone"), (410, "gone"),
            (401, "blocked"), (403, "blocked"), (429, "blocked"), (451, "blocked"),
            (500, "server_error"), (503, "server_error"),
            (408, "network"),
            (550, "unknown"),   # nonstandard: never treated as removal
            (524, "unknown"),  # Oxylabs' own timeout code
            (612, "unknown"),  # Oxylabs' own "faulted job" code
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(classify(obs(http_status=status)).error_class, expected)

    def test_valid_page_is_ok(self):
        self.assertEqual(classify(obs(body="hello")).error_class, "ok")

    def test_soft_404_text(self):
        c = classify(obs(body="Sorry, this agent no longer with us."))
        self.assertEqual((c.error_class, c.reason), ("gone", "soft_404_pattern"))

    def test_truncated_body(self):
        self.assertEqual(classify(obs(bytes_expected=1000, bytes_received=10)).error_class, "truncated")

    def test_network_error(self):
        self.assertEqual(classify(obs(http_status=None, network_error="dns")).reason, "network_dns")

    def test_redirect_to_another_profile_is_url_changed(self):
        c = classify(obs(http_status=301, final_url="https://www.redfin.com/agents/jane-2", final_status=200))
        self.assertEqual((c.error_class, c.new_url), ("url_changed", "https://www.redfin.com/agents/jane-2"))

    def test_redirect_to_shallow_path_same_host_is_gone(self):
        """Redirected to the general listing/directory on the same site (not a specific profile path) -> soft removal."""
        c = classify(obs(http_status=302, final_url="https://www.redfin.com/agents", final_status=200))
        self.assertEqual(c.error_class, "gone")

    def test_redirect_to_different_host_is_gone(self):
        c = classify(obs(http_status=302, final_url="https://www.zillow.com/agents/jane", final_status=200))
        self.assertEqual(c.error_class, "gone")

    def test_redirect_to_login_is_blocked(self):
        c = classify(obs(http_status=302, final_url="https://www.redfin.com/login", final_status=200))
        self.assertEqual(c.error_class, "blocked")

    def test_canonical_redirect_same_page_is_not_url_changed(self):
        c = classify(obs(http_status=301, final_url="https://redfin.com/agents/jane/", final_status=200))
        self.assertEqual(c.error_class, "ok")


class RetryDecisionTest(unittest.TestCase):
    def test_never_retries_403(self):
        c = classify(obs(http_status=403))
        self.assertFalse(retry_decision(c, obs(http_status=403), 0).retry)

    def test_no_retry_for_gone(self):
        c = classify(obs(http_status=404))
        self.assertFalse(retry_decision(c, obs(http_status=404), 0).retry)

    def test_retries_429_honoring_retry_after(self):
        c = classify(obs(http_status=429))
        d = retry_decision(c, obs(http_status=429, retry_after_sec=30), 0)
        self.assertEqual((d.retry, d.after_sec), (True, 30))

    def test_retries_exhausted(self):
        c = classify(obs(http_status=500))
        self.assertFalse(retry_decision(c, obs(http_status=500), 3).retry)

    def test_unknown_retried_once_only(self):
        c = classify(obs(http_status=550))
        self.assertTrue(retry_decision(c, obs(http_status=550), 0).retry)
        self.assertFalse(retry_decision(c, obs(http_status=550), 1).retry)

    def test_proxy_auth_never_retried(self):
        """Wrong credentials aren't a transient network glitch: don't waste time retrying them."""
        o = obs(http_status=None, network_error="proxy_auth")
        d = retry_decision(classify(o), o, 0)
        self.assertEqual((d.retry, d.reason), (False, "proxy_auth_needs_review"))


if __name__ == "__main__":
    unittest.main()
