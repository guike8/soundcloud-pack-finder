"""Anonymous Telegram mode must work without account credentials or login."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from packfinder import cli
from packfinder.pipeline import download_assets, scan_channels
from packfinder.state import State


ZIP_BYTES = b"PK\x03\x04public file fixture" * 8
SEED = "https://soundcloud.com/fixture-seed"
CHANNEL = "free_music_fixtures"


class FakePublicClient:
    supports_attachment_download = False

    def __init__(self, messages=None):
        self.messages = messages or []
        self.connect = AsyncMock()
        self.close = AsyncMock()
        self.download_attachment = AsyncMock(side_effect=AssertionError("public mode attempted Telegram file API"))
        self.scan_channel = AsyncMock(return_value=self.messages)


class FakeAccountClient(FakePublicClient):
    supports_attachment_download = True


class FakeSoundCloud:
    profile = {"id": "fixture-root", "username": "Fixture seed", "permalink_url": SEED,
        "description": f"Music https://t.me/{CHANNEL}"}

    def resolve_profile(self, _):
        return dict(self.profile)

    def get_profile(self, _):
        return dict(self.profile)

    def iter_followers(self, user_id, limit):
        return iter(())


class HTTPResponse:
    status = 200

    def __init__(self):
        self.body = io.BytesIO(ZIP_BYTES)

    def getheader(self, name):
        return "application/octet-stream" if name.lower() == "content-type" else None

    def read(self, size):
        return self.body.read(size)

    def close(self):
        pass


class HTTPConnection:
    def close(self):
        pass


class PublicModePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.dbpath = self.directory / "crawl.sqlite3"
        self.state = State(self.dbpath)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def register_channel(self):
        self.state.add_profile(FakeSoundCloud.profile, 0)
        self.state.finish_profile(FakeSoundCloud.profile, [(CHANNEL, f"https://t.me/{CHANNEL}")])

    def scan(self, client):
        return asyncio.run(scan_channels(client, self.state, delay=0, log=lambda _: None))

    def add_legacy_telegram_and_http(self, state):
        attachment_id = state.add_asset(kind="telegram", channel=CHANNEL, message_id=1,
            filename="legacy-sample-pack.zip", text="Free sample pack", eligible=True,
            reason="Earlier account scan advertised a free offer")
        http_id = state.add_asset(kind="http", channel=CHANNEL, message_id=2,
            filename="public-sample-pack.zip", text="Free sample pack", eligible=True,
            reason="Public creator free offer", url="https://files.example/public-sample-pack.zip")
        return attachment_id, http_id

    def fake_download(self, url, directory, max_bytes, **kwargs):
        filename = kwargs.get("filename_hint") or url.rsplit("/", 1)[-1]
        path = Path(directory) / filename
        path.write_bytes(ZIP_BYTES)
        return {"path": str(path), "filename": filename, "bytes": len(ZIP_BYTES),
            "sha256": hashlib.sha256(ZIP_BYTES).hexdigest(), "source_url": url, "final_url": url}

    def test_parser_defaults_to_web_and_account_is_explicit(self):
        for command in ("run", "scan-telegram", "download"):
            with self.subTest(command=command):
                self.assertEqual(cli.parser().parse_args([command]).telegram_mode, "web")
                self.assertEqual(cli.parser().parse_args([command, "--telegram-mode", "account"]).telegram_mode, "account")

    def test_default_run_uses_public_client_without_constructing_account_client(self):
        public = FakePublicClient()
        args = ["run", SEED, "--db", str(self.dbpath), "--depth", "0", "--delay", "0", "--report", str(self.directory / "report")]
        with patch("packfinder.soundcloud.SoundCloudClient", return_value=FakeSoundCloud()), \
             patch("packfinder.telegram_web.PublicTelegramClient", return_value=public) as web_factory, \
             patch("packfinder.telegram.TelegramClient", side_effect=AssertionError("default run attempted account login")) as account_factory, \
             patch("asyncio.sleep", new=AsyncMock()), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        web_factory.assert_called_once_with()
        account_factory.assert_not_called()
        public.connect.assert_awaited_once()
        public.close.assert_awaited_once()
        public.scan_channel.assert_awaited_once_with(CHANNEL, limit=100)

    def test_account_mode_remains_opt_in(self):
        self.register_channel()
        account = FakeAccountClient()
        args = ["scan-telegram", "--db", str(self.dbpath), "--telegram-mode", "account", "--session", str(self.directory / "session")]
        with patch("packfinder.telegram.TelegramClient", return_value=account) as account_factory, \
             patch("packfinder.telegram_web.PublicTelegramClient", side_effect=AssertionError("explicit account mode used web client")) as web_factory, \
             patch("asyncio.sleep", new=AsyncMock()), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        account_factory.assert_called_once_with(session=str(self.directory / "session"))
        web_factory.assert_not_called()
        account.connect.assert_awaited_once()
        account.close.assert_awaited_once()

    def test_public_attachment_without_bytes_stays_queued_while_external_file_is_eligible(self):
        self.register_channel()
        public = FakePublicClient([{"message_id": 1, "text": "Free sample pack released by creator",
            "attachment": {"file_name": "sample-pack.zip", "size": 100},
            "links": ["https://files.example/free-sample-pack.zip"]},
            {"message_id": 2, "text": "Free VST download", "unsupported_media": True, "links": []}])
        self.scan(public)
        assets = self.state.rows("assets")
        attachments = [row for row in assets if row["kind"] == "telegram"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["eligible"], 0)
        self.assertEqual(attachments[0]["status"], "queued")
        self.assertIn("public preview", attachments[0]["reason"].lower())
        eligible = [row for row in assets if row["eligible"]]
        self.assertEqual([row["url"] for row in eligible], ["https://files.example/free-sample-pack.zip"])
        unsupported = [row for row in assets if row["url"] == f"https://t.me/{CHANNEL}/2"]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["eligible"], 0)
        self.assertEqual(self.state.stats()["errors"], 0)

    def test_public_file_url_retains_attachment_filename_even_if_listed_among_links(self):
        self.register_channel()
        public_url = "https://cdn.example/file?asset=public"
        public = FakePublicClient([{"message_id": 1, "text": "Free sample pack — CC0",
            "attachment": {"file_name": "creator-sample-pack.zip", "download_url": public_url},
            "links": [public_url]},
            {"message_id": 2, "text": "Free sample pack", "attachment": {
                "file_name": "bad-sample-pack.zip", "download_url": "https://[malformed"}, "links": []}])
        self.scan(public)
        rows = [row for row in self.state.rows("assets") if row["url"] == public_url]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "http")
        self.assertEqual(rows[0]["filename"], "creator-sample-pack.zip")
        self.assertEqual(rows[0]["eligible"], 1)
        self.assertFalse(any(row["url"] == "https://[malformed" for row in self.state.rows("assets")))
        self.assertEqual(self.state.stats()["errors"], 0)

    def test_public_generic_attachment_url_downloads_using_validated_filename_hint(self):
        self.register_channel()
        public = FakePublicClient([{"message_id": 1, "text": "Free sample pack — CC0",
            "attachment": {"file_name": "public-sample-pack.zip", "download_url": "https://cdn.example/file?asset=public"}, "links": []}])
        self.scan(public)
        with patch("packfinder.downloads._resolve_public_addresses", return_value=("8.8.8.8",)), \
             patch("packfinder.downloads._open_response", return_value=(HTTPResponse(), HTTPConnection())):
            asyncio.run(download_assets(self.state, self.directory / "downloads", telegram=public, log=lambda _: None))
        row = self.state.rows("assets")[0]
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(Path(row["path"]).name, "public-sample-pack.zip")
        self.assertEqual(Path(row["path"]).read_bytes(), ZIP_BYTES)
        public.download_attachment.assert_not_awaited()

    def test_unavailable_legacy_telegram_rows_do_not_consume_http_file_budget(self):
        for label, client in (("web", FakePublicClient()), ("none", None)):
            with self.subTest(mode=label):
                state = State(self.directory / f"{label}.sqlite3")
                try:
                    attachment_id, http_id = self.add_legacy_telegram_and_http(state)
                    with patch("packfinder.downloads.download_url", side_effect=self.fake_download) as download:
                        asyncio.run(download_assets(state, self.directory / label, telegram=client, max_files=1, log=lambda _: None))
                    download.assert_called_once()
                    statuses = {row["id"]: (row["status"], row["error"]) for row in state.rows("assets")}
                    self.assertEqual(statuses[attachment_id], ("queued", None))
                    self.assertEqual(statuses[http_id], ("downloaded", None))
                    if client:
                        client.download_attachment.assert_not_awaited()
                finally:
                    state.close()

    def test_default_download_command_never_requests_account_auth_for_legacy_queue(self):
        attachment_id, http_id = self.add_legacy_telegram_and_http(self.state)
        public = FakePublicClient()
        args = ["download", "--db", str(self.dbpath), "--dest", str(self.directory / "downloads"), "--max-files", "1"]
        with patch("packfinder.telegram_web.PublicTelegramClient", return_value=public), \
             patch("packfinder.telegram.TelegramClient", side_effect=AssertionError("download attempted implicit account login")) as account_factory, \
             patch("packfinder.downloads.download_url", side_effect=self.fake_download), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        account_factory.assert_not_called()
        public.download_attachment.assert_not_awaited()
        statuses = {row["id"]: row["status"] for row in self.state.rows("assets")}
        self.assertEqual(statuses[attachment_id], "queued")
        self.assertEqual(statuses[http_id], "downloaded")


if __name__ == "__main__":
    unittest.main()
