"""Anonymous Telegram public-preview discovery, without an API app or session.

Only public ``https://t.me/s/<username>`` pages are requested. Document cards
usually link back to a Telegram post rather than exposing file bytes; those
stay metadata. Public external links and genuinely exposed media URLs remain
available to the separate bounded HTTPS download layer.

The optional request(url, headers) fixture returns HTML str/bytes, or a
(body, final_url) pair for redirect tests. It never receives credentials.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .downloads import is_supported_filename
from .telegram import normalize_public_channel

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_POST = re.compile(r"([A-Za-z][A-Za-z0-9_]{0,31})/([1-9][0-9]*)\Z")
_CDN = re.compile(r"(?:cdn[0-9]+\.)?(?:telesco\.pe|telegram-cdn\.org)\Z", re.I)


class TelegramWebError(RuntimeError):
    """A public preview could not be safely read."""


class TelegramUnsupportedChannelError(TelegramWebError):
    """The link does not expose a public channel preview; skip it normally."""

    skip_channel = True
    skip = True


def _public_page_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "t.me" or parts.port not in (None, 443) or parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError
    except (ValueError, TypeError):
        raise TelegramWebError("Telegram redirected outside its public HTTPS preview host.") from None
    return url


def _http_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None or any(ord(c) < 32 for c in value):
            return None
        parts.port  # reject malformed ports without contacting the URL
        return value
    except (ValueError, TypeError):
        return None


def _direct_media(value: str, filename: str = "") -> str | None:
    url = _http_url(value)
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not _CDN.fullmatch(parts.hostname or "") or parts.port not in (None, 443):
        return None
    name = filename or unquote(parts.path.rsplit("/", 1)[-1])
    return url if is_supported_filename(name) else None


def _size_bytes(value: str) -> int | None:
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)\b", value, re.I)
    if not match:
        return None
    magnitude = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4, "KIB": 1, "MIB": 2, "GIB": 3, "TIB": 4}[match[2].upper()]
    # Preview sizes are rounded display metadata, never a trusted byte length.
    return int(float(match[1].replace(",", ".")) * 1024 ** magnitude)


class _PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_page_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PreviewParser(HTMLParser):
    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.records: list[dict[str, Any]] = []
        self.stack: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.root_depth = 0

    def _capturing(self, field: str) -> bool:
        return any(frame.get("field") == field for frame in self.stack[self.root_depth - 1:]) if self.current else False

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = dict(attributes)
        classes = set((attrs.get("class") or "").split())
        post = _POST.fullmatch(attrs.get("data-post") or "")
        if post and "tgme_widget_message" in classes and post[1].lower() == self.channel:
            if self.current is not None:
                self._finish()
            self.current = {
                "channel": self.channel, "message_id": int(post[2]), "date": None,
                "text_parts": [], "links": [], "documents": [], "media": [],
                "unsupported_media": False,
            }
            self.root_depth = len(self.stack) + 1
        field = None
        if "tgme_widget_message_text" in classes:
            field = "text_parts"
        elif "tgme_widget_message_document_title" in classes:
            field = "filename_parts"
        elif "tgme_widget_message_document_extra" in classes:
            field = "size_parts"
        frame = {"tag": tag, "field": field}
        if tag not in _VOID:
            self.stack.append(frame)
        if self.current is None:
            return
        if "tgme_widget_message_document_wrap" in classes:
            self.current["documents"].append({"filename_parts": [], "size_parts": [], "href": attrs.get("href") or ""})
        if tag == "br" and self._capturing("text_parts"):
            self.current["text_parts"].append("\n")
        if tag == "time" and attrs.get("datetime"):
            self.current["date"] = attrs["datetime"]
        if tag == "a":
            in_caption = self._capturing("text_parts")
            inline_button = "tgme_widget_message_inline_button" in classes
            preview_link = "tgme_widget_message_link_preview" in classes
            if in_caption or inline_button or preview_link:
                value = attrs.get("href") or ""
                url = _http_url(value)
                if url:
                    self.current["links"].append(url)
        if tag in {"audio", "video", "source"} and attrs.get("src"):
            media_type = attrs.get("type") or ""
            self.current["media"].append({"url": attrs["src"], "type": media_type, "tag": tag})
        if classes.intersection({"tgme_widget_message_unsupported", "message_media_not_supported", "tgme_widget_message_sticker_wrap", "text_not_supported_wrap"}):
            self.current["unsupported_media"] = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i]["tag"] == tag), None)
        if index is None:
            return
        closing_current = self.current is not None and index < self.root_depth
        del self.stack[index:]
        if closing_current:
            self._finish()

    def handle_data(self, value: str) -> None:
        if self.current is None:
            return
        if self._capturing("text_parts"):
            self.current["text_parts"].append(value)
        documents = self.current["documents"]
        if documents:
            for field in ("filename_parts", "size_parts"):
                if self._capturing(field):
                    documents[-1][field].append(value)

    def _finish(self) -> None:
        if self.current is None:
            return
        record = self.current
        self.current = None
        record["text"] = "".join(record.pop("text_parts")).strip()
        record["links"] = list(dict.fromkeys(record["links"]))
        attachments = []
        for document in record.pop("documents"):
            filename = "".join(document["filename_parts"]).strip()
            if not filename:
                record["unsupported_media"] = True
                continue
            download_url = _direct_media(document["href"], filename)
            attachments.append({
                "file_name": filename, "size": _size_bytes("".join(document["size_parts"])),
                "mime_type": None, "download_url": download_url,
            })
            if download_url:
                record["links"].append(download_url)
            else:
                record["unsupported_media"] = True
        for media in record.pop("media"):
            try:
                filename = unquote(urlsplit(media["url"]).path.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                record["unsupported_media"] = True
                continue
            download_url = _direct_media(media["url"], filename)
            if download_url:
                attachments.append({"file_name": filename, "size": None, "mime_type": media["type"] or None, "download_url": download_url})
                record["links"].append(download_url)
            else:
                record["unsupported_media"] = True
        record["links"] = list(dict.fromkeys(record["links"]))
        record["attachment"] = (
            next((item for item in attachments if item.get("download_url")), None)
            or next((item for item in attachments if is_supported_filename(item["file_name"])), None)
            or (attachments[0] if attachments else None)
        )
        record["attachments"] = attachments
        record["protected"] = False
        record["source_url"] = f"https://t.me/{self.channel}/{record['message_id']}"
        record["public_preview"] = True
        self.records.append(record)

    def finish(self) -> list[dict[str, Any]]:
        self.close()
        self._finish()
        return self.records


class PublicTelegramClient:
    """Scan public channel pages; no Telegram login, app registration or cookies."""

    supports_attachment_download = False

    def __init__(self, request=None, timeout=30, min_interval=1, max_pages=100):
        if not isinstance(timeout, (float, int)) or timeout <= 0 or not isinstance(min_interval, (float, int)) or not 0 <= min_interval <= 60 or not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 1000:
            raise ValueError("Invalid Telegram public-preview timeout, request interval or page limit.")
        self._request = request
        self._timeout = timeout
        self._min_interval = min_interval
        self._max_pages = max_pages
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._opener = build_opener(_PublicRedirects())

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _fetch(self, url: str) -> str:
        _public_page_url(url)
        headers = {"User-Agent": "SoundCloudPackFinder/0.1 (public Telegram previews)", "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity"}
        try:
            with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()
                if self._request is not None:
                    result = self._request(url, headers)
                    if isinstance(result, tuple) and len(result) == 2:
                        body, final_url = result
                        _public_page_url(final_url)
                    else:
                        body = result
                else:
                    with self._opener.open(Request(url, headers=headers), timeout=self._timeout) as response:
                        _public_page_url(response.geturl())
                        body = response.read(MAX_RESPONSE_BYTES + 1)
            if not isinstance(body, (str, bytes)):
                raise TelegramWebError("Telegram public preview did not return HTML.")
            if len(body.encode("utf-8") if isinstance(body, str) else body) > MAX_RESPONSE_BYTES:
                raise TelegramWebError("Telegram public preview exceeded the 2 MiB response limit.")
            return body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {404, 410}:
                raise TelegramUnsupportedChannelError("This Telegram link has no available public channel preview.") from None
            if status == 429:
                raise TelegramWebError("Telegram public previews are rate-limited. Resume later; no login or access check is bypassed.") from None
            raise TelegramWebError(f"Telegram public preview returned HTTP {status}.") from None
        except (URLError, TimeoutError, OSError):
            raise TelegramWebError("Could not reach the Telegram public preview. Resume when the service is available.") from None

    async def scan_channel(self, channel: str, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 10000:
            raise ValueError("The message limit must be between 0 and 10000.")
        username = normalize_public_channel(channel)
        if limit == 0:
            return []
        records: dict[int, dict[str, Any]] = {}
        before = None
        seen_cursors = set()
        for _ in range(self._max_pages):
            url = f"https://t.me/s/{username}" + (f"?before={before}" if before is not None else "")
            html = await asyncio.to_thread(self._fetch, url)
            parser = _PreviewParser(username)
            try:
                parser.feed(html)
                page = parser.finish()
            except (ValueError, TypeError, RecursionError):
                raise TelegramWebError("The Telegram public-preview HTML could not be parsed.") from None
            if not page:
                if not records:
                    raise TelegramUnsupportedChannelError("This link does not expose public channel posts. Personal accounts, private links and unavailable previews are skipped.")
                break
            for record in page:
                records.setdefault(record["message_id"], record)
            if len(records) >= limit:
                break
            oldest = min(record["message_id"] for record in page)
            if oldest in seen_cursors or (before is not None and oldest >= before):
                break
            seen_cursors.add(oldest)
            before = oldest
        return sorted(records.values(), key=lambda record: record["message_id"], reverse=True)[:limit]

    async def download_attachment(self, channel: str, message_id: int, destination: Path, max_bytes: int) -> Path:
        raise TelegramWebError("This Telegram document has no public download URL. Use an exposed external link, open the post in Telegram, or optionally enable the authenticated adapter.")
