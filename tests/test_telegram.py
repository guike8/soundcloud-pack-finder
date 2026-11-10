import datetime
from pathlib import Path
from types import SimpleNamespace as Obj
import tempfile
import unittest

from packfinder.telegram import (
    TelegramAccessError,
    TelegramClient,
    TelegramDownloadError,
    TelegramSetupError,
    normalize_public_channel,
)


class FakeClient:
    def __init__(self, *, authorized=True, broadcast=True, public=True, protected=False, size=4, chunks=None):
        self.authorized = authorized
        self.entity = Obj(broadcast=broadcast, username="musicpacks" if public else None, noforwards=protected)
        self.message = Obj(
            id=42,
            raw_text="Free sample pack https://example.org/samples.zip.",
            date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            entities=[Obj(url="https://example.org/hidden.zip")],
            reply_markup=Obj(rows=[Obj(buttons=[Obj(url="https://example.org/button.zip")])]),
            document=Obj(size=size, mime_type="application/zip"),
            file=Obj(name="samples.zip", size=size, mime_type="application/zip"),
            noforwards=False,
        )
        self.chunks = [b"PK\x03\x04"] if chunks is None else chunks
        self.download_started = False
        self.disconnected = False
        self.iter_limit = None

    async def connect(self):
        pass

    async def start(self):
        self.authorized = True

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return Obj(bot=False)

    async def disconnect(self):
        self.disconnected = True

    async def get_entity(self, username):
        return self.entity

    async def iter_messages(self, entity, limit, wait_time):
        self.iter_limit = limit
        yield self.message

    async def get_messages(self, entity, ids):
        return self.message

    async def iter_download(self, document, **kwargs):
        self.download_started = True
        for chunk in self.chunks:
            yield chunk


class ChannelParsingTests(unittest.TestCase):
    def test_canonicalizes_public_post_and_preview_links(self):
        for value in ("@MusicPacks", "https://t.me/s/MusicPacks/42", "telegram.me/MusicPacks"):
            self.assertEqual(normalize_public_channel(value), "musicpacks")

    def test_rejects_invites_and_non_channel_urls(self):
        for value in ("https://t.me/+private", "https://t.me/joinchat/abc", "https://t.me/c/123/4", "https://example.org/musicpacks", "tg://join?invite=abc", "https://t.me/share/url", "https://user:secret@t.me/musicpacks"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_public_channel(value)


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_preserves_attachment_and_hidden_links(self):
        fake = FakeClient()
        records = await TelegramClient(client=fake).scan_channel("musicpacks", limit=7)
        self.assertEqual(fake.iter_limit, 7)
        self.assertEqual(records[0]["message_id"], 42)
        self.assertEqual(records[0]["attachment"]["size"], 4)
        self.assertEqual(records[0]["links"], ["https://example.org/samples.zip", "https://example.org/hidden.zip", "https://example.org/button.zip"])
        self.assertFalse(records[0]["protected"])

    async def test_missing_session_never_starts_interactive_login(self):
        fake = FakeClient(authorized=False)
        with self.assertRaises(TelegramSetupError):
            await TelegramClient(client=fake).scan_channel("musicpacks")
        self.assertFalse(fake.authorized)
        self.assertTrue(fake.disconnected)

    async def test_personal_accounts_and_private_entities_are_skipped(self):
        for fake in (FakeClient(broadcast=False), FakeClient(public=False)):
            with self.assertRaises(TelegramAccessError):
                await TelegramClient(client=fake).scan_channel("musicpacks")

    async def test_success_streams_document_to_explicit_path(self):
        fake = FakeClient(chunks=[b"PK", memoryview(b"\x03\x04")])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "pack.zip"
            result = await TelegramClient(client=fake).download_attachment("musicpacks", 42, target, 100)
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), b"PK\x03\x04")
            self.assertEqual([p.name for p in target.parent.iterdir()], ["pack.zip"])

    async def test_declared_size_is_checked_before_downloading(self):
        fake = FakeClient(size=101)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, Path(directory) / "pack.zip", 100)
            self.assertFalse(fake.download_started)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_streamed_size_ceiling_removes_partial_file(self):
        fake = FakeClient(size=None, chunks=[b"1234", b"5678"])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, Path(directory) / "pack.zip", 5)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_incomplete_download_is_not_committed(self):
        fake = FakeClient(size=100)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, Path(directory) / "pack.zip", 100)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_existing_destination_is_never_overwritten(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "pack.zip"
            target.write_bytes(b"existing")
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, target, 100)
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertFalse(fake.download_started)

    async def test_protected_content_is_not_downloaded(self):
        fake = FakeClient(protected=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, Path(directory) / "pack.zip", 100)
            self.assertFalse(fake.download_started)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_script_source_is_skipped_even_when_target_is_zip(self):
        fake = FakeClient()
        fake.message.file.name = "installer.sh"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, Path(directory) / "pack.zip", 100)
            self.assertFalse(fake.download_started)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_vst_installer_is_saved_without_executable_permissions(self):
        payload = b"MZ" + bytes(62)
        fake = FakeClient(size=len(payload), chunks=[payload])
        fake.message.file.name = "free-vst.exe"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "free-vst.exe"
            result = await TelegramClient(client=fake).download_attachment("musicpacks", 42, target, 100)
            self.assertEqual(result.read_bytes(), payload)
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    async def test_mislabeled_content_is_rejected_before_commit(self):
        fake = FakeClient(chunks=[b"oops"])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TelegramDownloadError):
                await TelegramClient(client=fake).download_attachment("musicpacks", 42, Path(directory) / "pack.zip", 100)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
