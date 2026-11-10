"""Optional, read-only Telegram channel access through a user's own session.

The core package can be imported without Telethon. Install the ``telegram``
extra and supply your own API credentials before creating a live adapter.
Neither scanning nor downloading joins channels or sends messages.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
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


class TelegramDownloadError(TelegramError):
    """A file could not be downloaded within the requested constraints."""


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
        raw_id = api_id if api_id is not None else os.environ.get("TELEGRAM_API_ID")
        api_hash = api_hash if api_hash is not None else os.environ.get("TELEGRAM_API_HASH")
        try:
            numeric_id = int(raw_id) if raw_id is not None else 0
        except (TypeError, ValueError) as exc:
            raise TelegramSetupError("TELEGRAM_API_ID must be the numeric API ID from my.telegram.org.") from exc
        if numeric_id <= 0 or not api_hash or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise TelegramSetupError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH from https://my.telegram.org/apps.")
        try:
            from telethon import TelegramClient as TelethonClient
        except ImportError as exc:
            raise TelegramSetupError("Telegram support is optional. Install it with: pip install '.[telegram]'") from exc
        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._client = TelethonClient(
            str(self.session_path), numeric_id, api_hash,
            request_retries=2, connection_retries=2,
            flood_sleep_threshold=30, receive_updates=False,
        )

    def _protect_session(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.session_path) + suffix)
            if path.is_file():
                path.chmod(0o600)

    async def _verify_user(self) -> None:
        me = await self._client.get_me()
        if me is None or getattr(me, "bot", False):
            raise TelegramSetupError("Channel history requires your own Telegram user session, not a bot token.")

    async def login(self) -> None:
        """Start Telethon's interactive phone/code/2FA login when run by the user."""
        try:
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
            raise TelegramAccessError(f"@{username} is not a public broadcast channel. Personal accounts, bots, groups and private invitations are skipped.")
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
