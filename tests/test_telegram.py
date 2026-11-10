import datetime
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace as Obj
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from packfinder.telegram import (
    TelegramAccessError,
    TelegramClient,
    TelegramDownloadError,
    TelegramSetupError,
    credentials_path,
    interactive_setup,
    load_local_credentials,
    normalize_public_channel,
    save_local_credentials,
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


class LocalCredentialTests(unittest.TestCase):
    def test_session_suffix_has_same_credentials_path(self):
        self.assertEqual(credentials_path(Path("data/telegram")), Path("data/telegram.credentials.json"))
        self.assertEqual(credentials_path(Path("data/telegram.session")), Path("data/telegram.credentials.json"))

    def test_save_load_private_file_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "private" / "telegram"
            self.assertIsNone(load_local_credentials(session))
            path = save_local_credentials(session, "12345", "a" * 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_local_credentials(session), (12345, "a" * 32))
            save_local_credentials(session, 67890, "b" * 32)
            self.assertEqual(load_local_credentials(session), (67890, "b" * 32))
            self.assertEqual([p.name for p in path.parent.iterdir()], ["telegram.credentials.json"])

    def test_refuses_credential_file_symlinks_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "another-file"
            target.write_text("unchanged")
            session = root / "telegram"
            credentials_path(session).symlink_to(target)
            for operation in (lambda: load_local_credentials(session), lambda: save_local_credentials(session, 12345, "a" * 32)):
                with self.assertRaises(TelegramSetupError):
                    operation()
            self.assertEqual(target.read_text(), "unchanged")

    def test_refuses_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            for operation in (lambda: load_local_credentials(alias / "telegram"), lambda: save_local_credentials(alias / "telegram", 12345, "a" * 32)):
                with self.assertRaises(TelegramSetupError):
                    operation()
            self.assertEqual(list(real.iterdir()), [])

    def test_refuses_readable_by_others(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "telegram"
            path = save_local_credentials(session, 12345, "a" * 32)
            path.chmod(0o644)
            with self.assertRaises(TelegramSetupError):
                load_local_credentials(session)

    def test_invalid_values_are_not_disclosed_in_errors(self):
        marker = "do-not-disclose-this-test-value"
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "telegram"
            with self.assertRaises(TelegramSetupError) as caught:
                save_local_credentials(session, marker, marker)
            self.assertNotIn(marker, str(caught.exception))
            path = credentials_path(session)
            path.write_text(json.dumps({"api_id": marker, "api_hash": marker}))
            path.chmod(0o600)
            with self.assertRaises(TelegramSetupError) as caught:
                load_local_credentials(session)
            self.assertNotIn(marker, str(caught.exception))

    def test_constructor_precedence_explicit_then_environment_then_local(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "telegram"
            save_local_credentials(session, 100, "a" * 32)
            factory = Mock(return_value=FakeClient())
            with patch.dict("sys.modules", {"telethon": Obj(TelegramClient=factory)}), patch.dict(os.environ, {"TELEGRAM_API_ID": "200", "TELEGRAM_API_HASH": "b" * 32}, clear=True):
                TelegramClient(300, "c" * 32, session)
                self.assertEqual(factory.call_args.args[1:], (300, "c" * 32))
                TelegramClient(session=session)
                self.assertEqual(factory.call_args.args[1:], (200, "b" * 32))
            with patch.dict("sys.modules", {"telethon": Obj(TelegramClient=factory)}), patch.dict(os.environ, {}, clear=True):
                TelegramClient(session=session)
                self.assertEqual(factory.call_args.args[1:], (100, "a" * 32))

    def test_partial_environment_uses_missing_local_field(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "telegram"
            save_local_credentials(session, 100, "a" * 32)
            factory = Mock(return_value=FakeClient())
            with patch.dict("sys.modules", {"telethon": Obj(TelegramClient=factory)}), patch.dict(os.environ, {"TELEGRAM_API_ID": "200"}, clear=True):
                TelegramClient(session=session)
            self.assertEqual(factory.call_args.args[1:], (200, "a" * 32))


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_is_private_before_any_network_connection(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "login.session"
            fake = FakeClient()
            def construct(path,*args,**kwargs):
                Path(path).write_bytes(b"empty unauthenticated fixture")
                Path(path).chmod(0o644)
                return fake
            with patch.dict("sys.modules",{"telethon":Obj(TelegramClient=construct)}):
                TelegramClient(api_id=123,api_hash="a"*32,session=session)
            self.assertEqual(session.stat().st_mode & 0o777,0o600)

    async def test_hidden_login_uses_callbacks_without_disclosing_their_values(self):
        fake = FakeClient()
        supplied = ["+15550001111", "54321", "test-two-factor-password"]
        observed = []
        async def start(**callbacks):
            observed.extend(callbacks[key]() for key in ("phone", "code_callback", "password"))
            print("Signed in successfully as Test Account")
        fake.start = start
        output = io.StringIO()
        with patch("packfinder.telegram.getpass.getpass", side_effect=supplied), contextlib.redirect_stdout(output):
            await TelegramClient(client=fake).login(hide_input=True)
        self.assertEqual(observed, supplied)
        self.assertEqual(output.getvalue(), "")

    async def test_interactive_setup_refuses_non_tty_before_prompting(self):
        with patch("packfinder.telegram.sys.stdin.isatty", return_value=False), patch("builtins.input") as prompt, patch("packfinder.telegram.getpass.getpass") as hidden:
            with self.assertRaises(TelegramSetupError):
                await interactive_setup(Path("data/test-session"))
            prompt.assert_not_called()
            hidden.assert_not_called()

    async def test_interactive_setup_saves_only_api_credentials_and_hides_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "telegram"
            client = Obj(login=AsyncMock(), close=AsyncMock())
            output = io.StringIO()
            with patch("packfinder.telegram.sys.stdin.isatty", return_value=True), patch.dict(os.environ, {}, clear=True), patch("builtins.input", return_value="12345"), patch("packfinder.telegram.getpass.getpass", return_value="a" * 32), patch("packfinder.telegram.TelegramClient", return_value=client), contextlib.redirect_stdout(output):
                await interactive_setup(session)
            client.login.assert_awaited_once_with(hide_input=True)
            client.close.assert_awaited_once()
            self.assertEqual(load_local_credentials(session), (12345, "a" * 32))
            self.assertEqual(set(json.loads(credentials_path(session).read_text())), {"api_id", "api_hash"})
            self.assertNotIn("a" * 32, output.getvalue())
            self.assertIn("Telegram connected", output.getvalue())

    async def test_interactive_setup_reuses_configured_values_without_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory).resolve() / "telegram"
            save_local_credentials(session, 12345, "a" * 32)
            client = Obj(login=AsyncMock(), close=AsyncMock())
            with patch("packfinder.telegram.sys.stdin.isatty", return_value=True), patch.dict(os.environ, {}, clear=True), patch("builtins.input") as prompt, patch("packfinder.telegram.getpass.getpass") as hidden, patch("packfinder.telegram.TelegramClient", return_value=client), contextlib.redirect_stdout(io.StringIO()):
                await interactive_setup(session)
            prompt.assert_not_called()
            hidden.assert_not_called()

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
