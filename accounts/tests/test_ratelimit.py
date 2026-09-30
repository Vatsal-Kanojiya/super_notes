"""The address-keyed counter behind the Google sign-in cap (accounts/ratelimit.py).

The runner swaps in a dummy cache (config/test_runner.py), so every test
that counts turns a real one back on.
"""

from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from accounts import ratelimit

with_cache = override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ratelimit-tests",
        }
    }
)


def request_from(addr="198.51.100.1", forwarded=None):
    extra = {"REMOTE_ADDR": addr}
    if forwarded is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().post("/", **extra)


class ClientIpTests(SimpleTestCase):
    def test_without_a_proxy_the_forwarded_header_is_ignored(self):
        request = request_from("198.51.100.1", forwarded="203.0.113.9")
        self.assertEqual(ratelimit.client_ip(request), "198.51.100.1")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_behind_one_proxy_the_right_most_entry_is_the_client(self):
        # The client wrote "1.1.1.1"; the proxy appended what it really saw.
        request = request_from("10.0.0.2", forwarded="1.1.1.1, 203.0.113.9")
        self.assertEqual(ratelimit.client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_COUNT=2)
    def test_behind_two_proxies_the_second_from_the_right_is_the_client(self):
        request = request_from("10.0.0.2", forwarded="1.1.1.1, 203.0.113.9, 10.0.0.1")
        self.assertEqual(ratelimit.client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_COUNT=3)
    def test_fewer_hops_than_proxies_takes_the_left_most(self):
        request = request_from("10.0.0.2", forwarded="203.0.113.9, 10.0.0.1")
        self.assertEqual(ratelimit.client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_an_empty_forwarded_header_falls_back_to_the_socket(self):
        self.assertEqual(ratelimit.client_ip(request_from(forwarded=" , ")), "198.51.100.1")

    def test_no_address_at_all_is_unknown(self):
        request = request_from()
        del request.META["REMOTE_ADDR"]
        self.assertEqual(ratelimit.client_ip(request), "unknown")


@with_cache
class GoogleLoginCapTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_it_blocks_after_the_limit_and_only_that_address(self):
        request = request_from("198.51.100.1")
        for _ in range(ratelimit.GOOGLE_LOGIN_IP_LIMIT - 1):
            ratelimit.record_google_login_failure(request)
        self.assertFalse(ratelimit.google_login_blocked(request))

        self.assertEqual(
            ratelimit.record_google_login_failure(request), ratelimit.GOOGLE_LOGIN_IP_LIMIT
        )

        self.assertTrue(ratelimit.google_login_blocked(request))
        self.assertFalse(ratelimit.google_login_blocked(request_from("198.51.100.2")))

    def test_asking_does_not_spend_an_attempt(self):
        request = request_from()
        for _ in range(5):
            ratelimit.google_login_blocked(request)
        self.assertEqual(ratelimit.record_google_login_failure(request), 1)

    def test_a_key_that_expires_between_add_and_incr_starts_a_new_window(self):
        with mock.patch.object(cache, "incr", side_effect=ValueError):
            self.assertEqual(ratelimit.record_google_login_failure(request_from()), 1)
