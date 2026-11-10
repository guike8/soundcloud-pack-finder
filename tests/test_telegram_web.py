"""Sanitized fictional fixtures follow real Telegram /s/ preview structure."""
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from packfinder.telegram_web import (
    MAX_RESPONSE_BYTES, PublicTelegramClient, TelegramUnsupportedChannelError,
    TelegramWebError, _PublicRedirects,
)


def post(number, text="Free sample pack", extra="", channel="artistpacks"):
    return f'''<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{channel}/{number}">
<div class="tgme_widget_message_user"><a href="https://unrelated.example/avatar">Artist</a></div>
{extra}<div class="tgme_widget_message_text js-message_text">{text}</div>
<div class="tgme_widget_message_footer"><a href="https://unrelated.example/footer">Footer</a><time datetime="2026-01-01T12:00:00+00:00">12:00</time></div>
</div></div>'''


def document(name="Free sample pack.zip", href="https://t.me/artistpacks/42", size="1.5 MB"):
    return f'''<a class="tgme_widget_message_document_wrap" href="{href}">
<div class="tgme_widget_message_document"><div class="tgme_widget_message_document_title">{name}</div>
<div class="tgme_widget_message_document_extra">{size}</div></div></a>'''


class TelegramWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_posts_extract_caption_links_metadata_not_navigation(self):
        html = '<nav><a href="https://bad.example/nav">Nav</a></nav>' + post(42, 'Free &amp; original sample pack<br><a href="https://creator.example/free.zip?x=1&amp;y=2">Download</a>', document())
        requests = []
        def request(url, headers):
            requests.append((url, headers))
            return html
        client = PublicTelegramClient(request, min_interval=0)
        result = await client.scan_channel("@ArtistPacks", limit=1)
        self.assertEqual(result[0]["text"], "Free & original sample pack\nDownload")
        self.assertEqual(result[0]["links"], ["https://creator.example/free.zip?x=1&y=2"])
        self.assertEqual(result[0]["attachment"]["file_name"], "Free sample pack.zip")
        self.assertEqual(result[0]["attachment"]["size"], 1572864)
        self.assertIsNone(result[0]["attachment"]["download_url"])
        self.assertTrue(result[0]["unsupported_media"])
        self.assertEqual(result[0]["date"], "2026-01-01T12:00:00+00:00")
        self.assertEqual(requests[0][0], "https://t.me/s/artistpacks")
        self.assertNotIn("Cookie", requests[0][1])
        self.assertNotIn("Authorization", requests[0][1])
        self.assertFalse(client.supports_attachment_download)

    async def test_pagination_is_bounded_deduplicated_and_newest_first(self):
        calls = []
        pages = [post(4) + post(5), post(3) + post(4), post(1) + post(2)]
        def request(url, _headers):
            calls.append(url)
            return pages.pop(0)
        result = await PublicTelegramClient(request, min_interval=0).scan_channel("artistpacks", limit=4)
        self.assertEqual([r["message_id"] for r in result], [5, 4, 3, 2])
        self.assertEqual(calls, ["https://t.me/s/artistpacks", "https://t.me/s/artistpacks?before=4", "https://t.me/s/artistpacks?before=3"])

    async def test_repeated_oldest_cursor_stops_without_loop(self):
        request = Mock(return_value=post(4) + post(5))
        result = await PublicTelegramClient(request, min_interval=0).scan_channel("artistpacks", limit=100)
        self.assertEqual(len(result), 2)
        self.assertEqual(request.call_count, 2)

    async def test_page_limit_prevents_unbounded_history_requests(self):
        request = Mock(side_effect=[post(5), post(4), post(3)])
        result = await PublicTelegramClient(request, min_interval=0, max_pages=2).scan_channel("artistpacks", limit=100)
        self.assertEqual([r["message_id"] for r in result], [5, 4])
        self.assertEqual(request.call_count, 2)

    async def test_document_post_link_never_claims_downloadable_bytes(self):
        for href in ("https://t.me/artistpacks/42", "https://t.me/artistpacks/42?single", "tg://resolve?domain=artistpacks&post=42"):
            with self.subTest(href=href):
                rows = await PublicTelegramClient(lambda *_: post(42, extra=document(href=href)), min_interval=0).scan_channel("artistpacks", 1)
                self.assertIsNone(rows[0]["attachment"]["download_url"])
                self.assertEqual(rows[0]["links"], [])

    async def test_album_uses_supported_asset_as_primary_instead_of_preview_image(self):
        html = post(42, extra=document(name="cover.png") + document(name="Free sample pack.zip"))
        row = (await PublicTelegramClient(lambda *_: html, min_interval=0).scan_channel("artistpacks", 1))[0]
        self.assertEqual(row["attachment"]["file_name"], "Free sample pack.zip")
        self.assertEqual(len(row["attachments"]), 2)

    async def test_exposed_supported_cdn_document_and_audio_are_recorded(self):
        extra = document(href="https://cdn1.telesco.pe/file/free.zip") + '<audio src="https://cdn4.telegram-cdn.org/file/preview.mp3" type="audio/mpeg"></audio>'
        rows = await PublicTelegramClient(lambda *_: post(42, extra=extra), min_interval=0).scan_channel("artistpacks", 1)
        self.assertEqual(rows[0]["attachment"]["download_url"], "https://cdn1.telesco.pe/file/free.zip")
        self.assertEqual(len(rows[0]["attachments"]), 2)
        self.assertEqual(rows[0]["links"], ["https://cdn1.telesco.pe/file/free.zip", "https://cdn4.telegram-cdn.org/file/preview.mp3"])
        self.assertFalse(rows[0]["unsupported_media"])

    async def test_video_or_opaque_media_is_not_mislabeled_as_pack_download(self):
        html = post(42, extra='<video src="https://cdn1.telesco.pe/file/video.mp4"></video><audio src="https://cdn1.telesco.pe/file/opaque"></audio>')
        row = (await PublicTelegramClient(lambda *_: html, min_interval=0).scan_channel("artistpacks", 1))[0]
        self.assertIsNone(row["attachment"])
        self.assertEqual(row["links"], [])
        self.assertTrue(row["unsupported_media"])

    async def test_malformed_links_and_foreign_post_ids_are_ignored(self):
        html = post(42, '<a href="https://[broken">Bad</a><a href="javascript:alert(1)">JS</a><a href="https://user:secret@bad.example/file.zip">Credentials</a>') + post(99, channel="anotherchannel")
        rows = await PublicTelegramClient(lambda *_: html, min_interval=0).scan_channel("artistpacks", 1)
        self.assertEqual([r["message_id"] for r in rows], [42])
        self.assertEqual(rows[0]["links"], [])

    async def test_private_personal_and_unavailable_links_skip_without_login(self):
        client = PublicTelegramClient(lambda *_: ("<html>Contact user</html>", "https://t.me/artistpacks"), min_interval=0)
        with self.assertRaises(TelegramUnsupportedChannelError) as caught:
            await client.scan_channel("artistpacks")
        self.assertTrue(caught.exception.skip_channel)

    async def test_invalid_input_never_fetches(self):
        request = Mock()
        client = PublicTelegramClient(request, min_interval=0)
        for link in ("https://evil.example/channel", "https://t.me/+private", "tg://join?invite=private"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                await client.scan_channel(link)
        request.assert_not_called()

    async def test_cross_host_redirect_is_rejected(self):
        client = PublicTelegramClient(lambda *_: (post(42), "https://evil.example/capture"), min_interval=0)
        with self.assertRaises(TelegramWebError):
            await client.scan_channel("artistpacks", 1)
        with self.assertRaises(TelegramWebError):
            _PublicRedirects().redirect_request(Request("https://t.me/s/artistpacks"), None, 302, "Found", {}, "http://t.me/artistpacks")

    async def test_response_size_limit_and_unexpected_response_type(self):
        for body in (b"x" * (MAX_RESPONSE_BYTES + 1), {"not": "html"}):
            with self.subTest(type=type(body)), self.assertRaises(TelegramWebError):
                await PublicTelegramClient(lambda *_: body, min_interval=0).scan_channel("artistpacks", 1)

    async def test_fetch_failures_have_concrete_errors_without_authentication(self):
        errors = [URLError("offline"), HTTPError("https://t.me/s/artistpacks", 429, "limited", {}, None), HTTPError("https://t.me/s/artistpacks", 404, "missing", {}, None)]
        for error in errors:
            with self.subTest(error=type(error)):
                request = Mock(side_effect=error)
                with self.assertRaises(TelegramWebError):
                    await PublicTelegramClient(request, min_interval=0).scan_channel("artistpacks", 1)

    async def test_empty_later_page_ends_history(self):
        request = Mock(side_effect=[post(5), "<html>No more posts</html>"])
        rows = await PublicTelegramClient(request, min_interval=0).scan_channel("artistpacks", 20)
        self.assertEqual([row["message_id"] for row in rows], [5])
        self.assertEqual(request.call_count, 2)

    async def test_no_telethon_or_credentials_needed(self):
        with patch.dict(sys.modules, {"telethon": None}):
            client = PublicTelegramClient(lambda *_: post(42), min_interval=0)
            await client.connect()
            self.assertEqual(len(await client.scan_channel("artistpacks", 1)), 1)
            await client.close()

    async def test_zero_limit_and_unsupported_attachment_download_never_fetch(self):
        request = Mock()
        client = PublicTelegramClient(request, min_interval=0)
        self.assertEqual(await client.scan_channel("artistpacks", 0), [])
        with self.assertRaisesRegex(TelegramWebError, "no public download URL"):
            await client.download_attachment("artistpacks", 42, None, 100)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
