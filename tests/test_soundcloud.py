"""Contracts from observed anonymous website hydration/API responses."""
import json
import unittest
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from packfinder.soundcloud import SoundCloudClient, SoundCloudError, SoundCloudStopError, _PublicRedirects

PUBLIC_CLIENT = "AnonymousPublicWebsiteClient123456"


def user(number, **fields):
    return {"urn": f"soundcloud:users:{number}", "kind": "user", "username": f"artist{number}", **fields}


def page(profile=None):
    data = [{"hydratable": "apiClient", "data": {"id": PUBLIC_CLIENT, "isExpiring": True}}]
    if profile:
        data.append({"hydratable": "user", "data": profile})
    return '<script>window.__sc_hydration = ' + json.dumps(data) + ';</script>'


class SoundCloudTests(unittest.TestCase):
    def test_seed_discovers_public_client_and_separate_profile_links(self):
        calls = []
        def request(url, headers):
            calls.append((url, headers))
            if url.startswith("https://soundcloud.com/"):
                return page(user(1, description="Free packs at t.me/artistpacks", website=None))
            self.assertEqual(parse_qs(urlsplit(url).query)["client_id"], [PUBLIC_CLIENT])
            return [{"url": "https://t.me/artistpacks", "network": "telegram"}]
        client = SoundCloudClient(request=request)
        profile = client.resolve_profile("https://soundcloud.com/artist-one")
        self.assertEqual(profile["id"], "soundcloud:users:1")
        self.assertEqual(profile["web_profiles"][0]["url"], "https://t.me/artistpacks")
        self.assertEqual(profile["website"], "")
        self.assertEqual(client.get_profile(profile["id"]), profile)
        self.assertEqual(len(calls), 2)
        for _, headers in calls:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            self.assertEqual(headers["Origin"], "https://soundcloud.com")

    def test_pagination_appends_public_id_omitted_by_site_next_href(self):
        calls = []
        pages = [{"collection": [user(1), user(2)], "next_href": "https://api-v2.soundcloud.com/users/9/followers?offset=1793573270644&limit=2"}, {"collection": [user(2), user(3), user(4)], "next_href": "https://api-v2.soundcloud.com/users/9/followers?offset=unused"}]
        def request(url, headers):
            calls.append(url)
            self.assertEqual(parse_qs(urlsplit(url).query)["client_id"], [PUBLIC_CLIENT])
            return pages.pop(0)
        result = list(SoundCloudClient(PUBLIC_CLIENT, request).iter_followers("soundcloud:users:9", 3))
        self.assertEqual([p["id"] for p in result], ["soundcloud:users:1", "soundcloud:users:2", "soundcloud:users:3"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(parse_qs(urlsplit(calls[1]).query)["offset"], ["1793573270644"])

    def test_cross_origin_next_page_is_rejected_before_request(self):
        calls = []
        def request(url, headers):
            calls.append(url)
            return {"collection": [user(1)], "next_href": "https://untrusted.example/steal"}
        with self.assertRaisesRegex(SoundCloudError, "outside its public website"):
            list(SoundCloudClient(PUBLIC_CLIENT, request).iter_followers("9", 3))
        self.assertEqual(len(calls), 1)

    def test_cross_origin_redirect_is_rejected(self):
        with self.assertRaises(SoundCloudError):
            _PublicRedirects().redirect_request(Request("https://soundcloud.com/a"), None, 302, "Found", {}, "https://untrusted.example")

    def test_repeated_pagination_cursor_does_not_loop(self):
        url = "https://api-v2.soundcloud.com/users/9/followers?offset=repeat"
        calls = 0
        def request(*_):
            nonlocal calls
            calls += 1
            return {"collection": [], "next_href": url}
        with self.assertRaisesRegex(SoundCloudError, "repeated"):
            list(SoundCloudClient(PUBLIC_CLIENT, request).iter_followers("9", 3))
        self.assertEqual(calls, 2)

    def test_empty_page_can_have_next_page(self):
        pages = [{"collection": [], "next_href": "/users/9/followers?offset=x"}, {"collection": [user(7)], "next_href": None}]
        self.assertEqual(len(list(SoundCloudClient(PUBLIC_CLIENT, lambda *_: pages.pop(0)).iter_followers("9", 2))), 1)

    def test_legacy_id_and_plain_list_response(self):
        client = SoundCloudClient(PUBLIC_CLIENT, lambda *_: [{"id": 123, "username": "legacy", "description": None}])
        self.assertEqual(list(client.iter_followers("9", 2))[0]["id"], "soundcloud:users:123")

    def test_track_seed_is_rejected_without_network(self):
        client = SoundCloudClient(request=lambda *_: self.fail("Unexpected network request"))
        with self.assertRaisesRegex(ValueError, "profile URL"):
            client.resolve_profile("https://soundcloud.com/artist/track")

    def test_zero_limit_does_not_make_requests(self):
        client = SoundCloudClient(request=lambda *_: self.fail("Unnecessary request"))
        self.assertEqual(list(client.iter_followers("9", 0)), [])

    def test_older_site_discovers_id_from_explicit_public_bootstrap_asset(self):
        calls = []
        def request(url, headers):
            calls.append(url)
            if url == "https://soundcloud.com/artist":
                return '<script src="https://a-v2.sndcdn.com/assets/bootstrap.js"></script><script>window.__sc_hydration = ' + json.dumps([{"hydratable": "user", "data": user(1)}]) + ';</script>'
            if url == "https://a-v2.sndcdn.com/assets/bootstrap.js":
                return 'var publicConfig={client_id:"' + PUBLIC_CLIENT + '"};'
            return []
        self.assertEqual(SoundCloudClient(request=request).resolve_profile("https://soundcloud.com/artist")["id"], "soundcloud:users:1")
        self.assertEqual(len(calls), 3)

    def test_get_profile_bootstraps_without_prior_seed(self):
        def request(url, headers):
            if url == "https://soundcloud.com/":
                return page()
            if "/web-profiles?" in url:
                return []
            self.assertIn("soundcloud%3Ausers%3A9", url)
            return user(9)
        self.assertEqual(SoundCloudClient(request=request).get_profile("9")["id"], "soundcloud:users:9")

    def test_site_change_produces_concrete_error(self):
        client = SoundCloudClient(request=lambda *_: "<html>Site changed</html>")
        with self.assertRaisesRegex(SoundCloudError, "public website client configuration"):
            client.resolve_profile("https://soundcloud.com/artist")

    def test_untrusted_seed_is_rejected(self):
        client = SoundCloudClient(request=lambda *_: self.fail("Untrusted request"))
        for seed in ("https://soundcloud.com.evil.test/profile", "http://soundcloud.com/a", "https://evil.test/a"):
            with self.assertRaises((ValueError, SoundCloudError)):
                client.resolve_profile(seed)
        with self.assertRaises(ValueError):
            SoundCloudClient("bad\r\nInjected: header")

    def test_sitewide_http_blocks_signal_crawler_to_stop(self):
        for code in (401, 403, 429):
            with self.subTest(status=code):
                client = SoundCloudClient(PUBLIC_CLIENT, min_interval=0, max_retries=0)
                client._opener = MagicMock()
                client._opener.open.side_effect = HTTPError("https://api-v2.soundcloud.com/users/9", code, "Blocked", None, None)
                with self.assertRaises(SoundCloudStopError) as caught:
                    client.get_profile("9")
                self.assertTrue(caught.exception.stop_crawl)

    def test_missing_profile_remains_a_per_profile_error(self):
        client = SoundCloudClient(PUBLIC_CLIENT, min_interval=0, max_retries=0)
        client._opener = MagicMock()
        client._opener.open.side_effect = HTTPError("https://api-v2.soundcloud.com/users/9", 404, "Not found", None, None)
        with self.assertRaises(SoundCloudError) as caught:
            client.get_profile("9")
        self.assertFalse(getattr(caught.exception, "stop_crawl", False))

    def test_long_retry_after_stops_without_blocking_sleep(self):
        headers = Message()
        headers["Retry-After"] = "120"
        client = SoundCloudClient(PUBLIC_CLIENT, min_interval=0, max_retries=1)
        client._opener = MagicMock()
        client._opener.open.side_effect = HTTPError("https://api-v2.soundcloud.com/users/9", 429, "Too many requests", headers, None)
        with patch("packfinder.soundcloud.time.sleep") as sleep:
            with self.assertRaises(SoundCloudStopError) as caught:
                client.get_profile("9")
        self.assertTrue(caught.exception.stop_crawl)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
