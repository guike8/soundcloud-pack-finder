"""Optional, read-only Telegram channel access through a user's own session.

The core package can be imported without Telethon. Install the ``telegram``
extra and supply your own API credentials before creating a live adapter.
Neither scanning nor downloading joins channels or sends messages.
"""

from __future__ import annotations

import contextlib
import getpass
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit

from .downloads import ALLOWED_SUFFIXES as ALLOWED_DOWNLOAD_SUFFIXES

class TelegramError(RuntimeError):
    """Base class for actionable Telegram adapter errors."""


class TelegramSetupError(TelegramError):
    """The optional dependency, credentials, or user session needs setup."""


class TelegramAccessError(TelegramError):
    """The requested public broadcast channel cannot be read."""


class TelegramUnsupportedChannelError(TelegramAccessError):
    """A discovered link names a personal account or another unsupported peer."""

    skip_channel = True


class TelegramDownloadError(TelegramError):
    """A file could not be downloaded within the requested constraints."""


def credentials_path(session: str | Path) -> Path:
    """Locate this session's local API configuration, without exposing values."""
    path = Path(session).expanduser()
    if path.suffix == ".session":
        path = path.with_suffix("")
    return Path(str(path) + ".credentials.json")


def _validated_credentials(api_id: Any, api_hash: Any) -> tuple[int, str]:
    try:
        numeric_id = int(api_id) if not isinstance(api_id, bool) else 0
    except (TypeError, ValueError, OverflowError):
        numeric_id = 0
    if not isinstance(api_hash, str):
        api_hash = ""
    api_hash = api_hash.strip()
    if numeric_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise TelegramSetupError("Telegram API credentials are missing or invalid. Use the numeric API ID and API hash from https://my.telegram.org/apps.") from None
    return numeric_id, api_hash


def _no_symlinks(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise TelegramSetupError("Telegram credentials and sessions require a local path without symbolic links.")
    return path


def load_local_credentials(session: str | Path) -> tuple[int, str] | None:
    """Load a regular private JSON configuration, or return None when absent."""
    path = _no_symlinks(credentials_path(session))
    directory_fd = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise TelegramSetupError("The local Telegram credentials path must be a regular file.")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise TelegramSetupError("The local Telegram credentials file must be private; use file permissions 0600.")
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise TelegramSetupError("The local Telegram credentials file is invalid.")
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise TelegramSetupError("The local Telegram credentials file is invalid.")
        return _validated_credentials(values.get("api_id"), values.get("api_hash"))
    except FileNotFoundError:
        return None
    except TelegramSetupError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise TelegramSetupError("Could not read the local Telegram API credentials file.") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def save_local_credentials(session: str | Path, api_id: Any, api_hash: Any) -> Path:
    """Atomically save only API credentials as a regular owner-only JSON file."""
    numeric_id, api_hash = _validated_credentials(api_id, api_hash)
    path = _no_symlinks(credentials_path(session))
    directory_fd = None
    temporary_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _no_symlinks(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            previous = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None and not stat.S_ISREG(previous.st_mode):
            raise TelegramSetupError("The local Telegram credentials path must be a regular file.")
        temporary_name = ".telegram-credentials-" + secrets.token_hex(12) + ".tmp"
        descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"api_id": numeric_id, "api_hash": api_hash}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_name = None
        os.fsync(directory_fd)
        return path
    except TelegramSetupError:
        raise
    except OSError:
        raise TelegramSetupError("Could not securely save the local Telegram API credentials file.") from None
    finally:
        if directory_fd is not None:
            if temporary_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_name, dir_fd=directory_fd)
            os.close(directory_fd)


def _credential_sources(session: str | Path, api_id: Any = None, api_hash: Any = None) -> tuple[Any, Any]:
    raw_id = api_id if api_id is not None else os.environ.get("TELEGRAM_API_ID")
    raw_hash = api_hash if api_hash is not None else os.environ.get("TELEGRAM_API_HASH")
    if raw_id is None or raw_hash is None:
        local = load_local_credentials(session)
        if local is not None:
            if raw_id is None:
                raw_id = local[0]
            if raw_hash is None:
                raw_hash = local[1]
    return raw_id, raw_hash


_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")
_URL = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_RESERVED = {
    "addemoji", "addlist", "addstickers", "bg", "boost", "c", "confirmphone",
    "contact", "invoice", "iv", "joinchat", "login", "m", "proxy", "setlanguage",
    "share", "socks", "stars", "web", "z",
}


def normalize_public_channel(value: str) -> str:
    """Return a public username, rejecting invitations and unrelated URLs.

    A post link or a ``t.me/s/`` preview link is reduced to its public channel.
    Telegram itself decides whether a syntactically valid username exists.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A public Telegram channel username or t.me link is required.")
    value = value.strip()
    if value.startswith("@"):
        value = value[1:]
    if value.lower().startswith(("t.me/", "telegram.me/", "www.t.me/", "www.telegram.me/")):
        value = "https://" + value
    if "://" in value:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.hostname not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise ValueError("Use a public t.me channel link, not an external URL or invitation.")
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() == "s":
            parts.pop(0)
        if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
            raise ValueError("Use a public channel link such as https://t.me/example.")
        value = parts[0]
    if not _USERNAME.fullmatch(value) or value.lower() in _RESERVED:
        raise ValueError("Private invitations and Telegram service links are not public channels.")
    return value.lower()


def _message_links(message: Any, text: str) -> list[str]:
    links = [match.group(0).rstrip(".,;:!?)") for match in _URL.finditer(text)]
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if isinstance(url, str):
            links.append(url)
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if isinstance(url, str):
                links.append(url)
    # Discovery preserves public URLs; the download layer separately validates
    # the destination and network address before fetching external links.
    valid = []
    for url in links:
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                valid.append(url)
        except ValueError:
            continue
    return list(dict.fromkeys(valid))


def _attachment(message: Any) -> dict[str, Any] | None:
    document = getattr(message, "document", None)
    if document is None:
        return None
    file = getattr(message, "file", None)
    name = getattr(file, "name", None)
    if not name:
        for attribute in getattr(document, "attributes", None) or []:
            name = getattr(attribute, "file_name", None)
            if name:
                break
    size = getattr(file, "size", None)
    if size is None:
        size = getattr(document, "size", None)
    return {
        "file_name": str(name or f"telegram-{message.id}.bin"),
        "size": int(size) if size is not None else None,
        "mime_type": getattr(file, "mime_type", None) or getattr(document, "mime_type", None),
    }


def _access_error(action: str, exc: Exception) -> TelegramAccessError:
    if type(exc).__name__ in {"FloodWaitError", "FloodPremiumWaitError"}:
        seconds = getattr(exc, "seconds", None)
        return TelegramAccessError(f"Telegram rate limit while {action}; retry after {seconds or 'the requested'} seconds.")
    return TelegramAccessError(f"Telegram could not {action} ({type(exc).__name__}). No channel was joined.")


class TelegramClient:
    """Read public broadcast channels and stream selected documents to disk.

    ``login()`` is deliberately separate from ``connect()``: ordinary scans
    never prompt for a phone number or initiate account authorization.
    ``destination`` in ``download_attachment`` is an explicit final file path.
    Existing files are never overwritten, and failed downloads leave no part.
    """

    def __init__(
        self,
        api_id: int | str | None = None,
        api_hash: str | None = None,
        session: str | Path = "telegram",
        *,
        client: Any | None = None,
    ) -> None:
        self._connected = False
        self.session_path = Path(session).expanduser()
        if self.session_path.suffix != ".session":
            self.session_path = Path(str(self.session_path) + ".session")
        if client is not None:
            self._client = client
            return
        raw_id, raw_hash = _credential_sources(session, api_id, api_hash)
        numeric_id, api_hash = _validated_credentials(raw_id, raw_hash)
        try:
            from telethon import TelegramClient as TelethonClient
        except ImportError as exc:
            raise TelegramSetupError("Telegram support is optional. Install it with: pip install '.[telegram]'") from exc
        self.session_path = _no_symlinks(self.session_path)
        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._client = TelethonClient(
            str(self.session_path), numeric_id, api_hash,
            request_retries=2, connection_retries=2,
            flood_sleep_threshold=30, receive_updates=False,
        )
        # Telethon creates the SQLite session in its constructor. Restrict it
        # before connect/login can write an authorization key into the file.
        self._protect_session()

    def _protect_session(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.session_path) + suffix)
            if path.is_symlink():
                raise TelegramSetupError("Telegram session files must not be symbolic links.")
            if path.is_file():
                path.chmod(0o600)

    async def _verify_user(self) -> None:
        me = await self._client.get_me()
        if me is None or getattr(me, "bot", False):
            raise TelegramSetupError("Channel history requires your own Telegram user session, not a bot token.")

    async def login(self, *, hide_input: bool = False) -> None:
        """Start Telethon's interactive phone/code/2FA login when run by the user."""
        try:
            if hide_input:
                # These callbacks execute only in the user's terminal. Codes
                # and passwords are never persisted or passed on a command line.
                with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                    await self._client.start(
                        phone=lambda: getpass.getpass("Telegram phone number, including country code (hidden): ").strip(),
                        code_callback=lambda: getpass.getpass("Telegram login code (hidden): ").strip(),
                        password=lambda: getpass.getpass("Telegram 2FA password, if enabled (hidden): "),
                    )
            else:
                await self._client.start()
            await self._verify_user()
            self._connected = True
            self._protect_session()
        except TelegramError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise TelegramSetupError(f"Telegram login failed ({type(exc).__name__}).") from exc

    async def connect(self) -> None:
        """Connect using an existing session, without initiating a login."""
        if self._connected:
            return
        try:
            await self._client.connect()
            self._protect_session()
            if not await self._client.is_user_authorized():
                raise TelegramSetupError("No authorized Telegram session. Run the telegram-login command first.")
            await self._verify_user()
            self._connected = True
        except TelegramError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise _access_error("connect with the saved session", exc) from exc

    async def close(self) -> None:
        try:
            result = self._client.disconnect()
            if inspect.isawaitable(result):
                await result
        finally:
            self._connected = False
            self._protect_session()

    async def __aenter__(self) -> "TelegramClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _public_entity(self, channel: str) -> tuple[str, Any]:
        username = normalize_public_channel(channel)
        await self.connect()
        try:
            entity = await self._client.get_entity(username)
        except Exception as exc:
            raise _access_error(f"resolve @{username}", exc) from exc
        aliases = [getattr(entity, "username", None)]
        aliases.extend(
            getattr(alias, "username", None)
            for alias in getattr(entity, "usernames", None) or []
            if getattr(alias, "active", False)
        )
        if not getattr(entity, "broadcast", False) or not any(aliases):
            raise TelegramUnsupportedChannelError(f"@{username} is not a public broadcast channel. Personal accounts, bots, groups and private invitations are skipped.")
        return username, entity

    async def scan_channel(self, channel: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return up to ``limit`` recent public posts, newest first."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 10_000:
            raise ValueError("The message limit must be between 0 and 10000.")
        if limit == 0:
            return []
        username, entity = await self._public_entity(channel)
        records = []
        try:
            async for message in self._client.iter_messages(entity, limit=limit, wait_time=1):
                text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
                date = getattr(message, "date", None)
                records.append({
                    "channel": username,
                    "message_id": int(message.id),
                    "date": date.isoformat() if hasattr(date, "isoformat") else None,
                    "text": str(text),
                    "links": _message_links(message, str(text)),
                    "attachment": _attachment(message),
                    "protected": bool(getattr(entity, "noforwards", False) or getattr(message, "noforwards", False)),
                })
                if len(records) >= limit:
                    break
        except Exception as exc:
            raise _access_error(f"read @{username}", exc) from exc
        return records

    async def download_attachment(
        self, channel: str, message_id: int, destination: Path, max_bytes: int,
    ) -> Path:
        """Stream a document to an explicit new path, enforcing a byte ceiling."""
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise ValueError("A positive Telegram message ID is required.")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive byte limit.")
        username, entity = await self._public_entity(channel)
        if getattr(entity, "noforwards", False):
            raise TelegramDownloadError("This channel protects its content; downloading is skipped.")
        try:
            message = await self._client.get_messages(entity, ids=message_id)
        except Exception as exc:
            raise _access_error(f"read message {message_id} in @{username}", exc) from exc
        if message is None or _attachment(message) is None:
            raise TelegramDownloadError("That Telegram post has no downloadable document.")
        if getattr(message, "noforwards", False):
            raise TelegramDownloadError("This post protects its content; downloading is skipped.")
        attachment = _attachment(message)
        source_name = attachment["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
        if Path(source_name).suffix.lower() not in ALLOWED_DOWNLOAD_SUFFIXES:
            raise TelegramDownloadError("Only archives, audio, presets and VST installer packages can be downloaded; scripts are skipped.")
        size = attachment["size"]
        if size is not None and (size < 0 or size > max_bytes):
            raise TelegramDownloadError(f"Attachment size {size} exceeds the {max_bytes}-byte download limit.")
        target = Path(destination).expanduser()
        if target.suffix.lower() not in ALLOWED_DOWNLOAD_SUFFIXES:
            raise TelegramDownloadError("The destination must use an archive, audio, preset or installer package extension.")
        if target.exists() or target.is_symlink():
            raise TelegramDownloadError(f"Destination already exists: {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, part_name = tempfile.mkstemp(prefix=".packfinder-", suffix=".part", dir=target.parent)
        part = Path(part_name)
        stream = None
        try:
            total = 0
            with os.fdopen(descriptor, "wb") as output:
                stream = self._client.iter_download(message.document, file_size=size, request_size=512 * 1024)
                async for chunk in stream:
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramDownloadError(f"Download exceeded the {max_bytes}-byte limit.")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size is not None and total != size:
                raise TelegramDownloadError(f"Incomplete Telegram download: expected {size} bytes, received {total}.")
            from .downloads import DownloadError, validate_download_file
            try:
                validate_download_file(part, max_bytes=max_bytes, filename=target.name)
            except DownloadError as exc:
                raise TelegramDownloadError(str(exc)) from exc
            # Hard-linking a temporary file in the same directory commits the
            # completed download without a check/rename race overwriting data.
            os.link(part, target)
            return target
        except FileExistsError as exc:
            raise TelegramDownloadError(f"Destination already exists: {target.name}") from exc
        except TelegramError:
            raise
        except Exception as exc:
            raise TelegramDownloadError(f"Telegram download failed ({type(exc).__name__}).") from exc
        finally:
            try:
                if stream is not None:
                    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                    if close is not None:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
            finally:
                part.unlink(missing_ok=True)


async def interactive_setup(session: Path) -> None:
    """Configure and authorize Telegram through an interactive local terminal."""
    if not sys.stdin.isatty():
        raise TelegramSetupError("Telegram setup requires an interactive local terminal. Run the setup command yourself; do not paste credentials into chat.")
    print("Telegram API credentials: https://my.telegram.org/apps")
    raw_id, raw_hash = _credential_sources(session)
    if raw_id is None:
        raw_id = input("Telegram API ID: ").strip()
    if raw_hash is None:
        raw_hash = getpass.getpass("Telegram API hash (hidden): ").strip()
    numeric_id, api_hash = _validated_credentials(raw_id, raw_hash)
    save_local_credentials(session, numeric_id, api_hash)
    client = TelegramClient(api_id=numeric_id, api_hash=api_hash, session=session)
    try:
        await client.login(hide_input=True)
    finally:
        await client.close()
    print("Telegram connected. API credentials and session are saved locally.")
