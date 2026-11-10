"""Bounded downloads of public HTTPS archives, audio, presets, and installers.

Files are never opened, installed, or extracted. DNS answers are checked and the
connection is pinned to a checked address while TLS verifies the original host.
Redirects go through the same checks and receive no cookies or credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
import hashlib
import hmac
import http.client
import ipaddress
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import time
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_TIMEOUT = 30
MAX_REDIRECTS = 5
MAX_DOWNLOAD_SECONDS = 600
INSTALLER_SUFFIXES = frozenset({".exe", ".msi", ".pkg", ".dmg"})
ALLOWED_SUFFIXES = frozenset({
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
    ".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg",
    ".sf2", ".sfz", ".fxp", ".fxb", ".vstpreset", ".nki", ".adg", ".als",
    ".nkm", ".nksf", ".fst", ".adv", ".amxd",
}) | INSTALLER_SUFFIXES
BLOCKED_SUFFIXES = frozenset({
    ".msp", ".com", ".bat", ".cmd", ".ps1", ".sh",
    ".bash", ".zsh", ".fish", ".py", ".js", ".vbs", ".jar", ".app",
    ".deb", ".rpm", ".apk", ".dll", ".so", ".dylib",
    ".scr", ".lnk", ".url", ".desktop", ".workflow", ".command",
})


class DownloadError(RuntimeError):
    """The URL or returned file cannot be downloaded under the configured limits."""


def is_supported_filename(name: str) -> bool:
    """Whether a bare filename names a supported music artifact or installer."""
    if not isinstance(name, str) or not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or any(unicodedata.category(char).startswith("C") for char in name):
        return False
    return Path(name).suffix.lower() in ALLOWED_SUFFIXES


def _parse_url(url: str):
    if not isinstance(url, str) or not url or len(url) > 16_384:
        raise DownloadError("A nonempty HTTPS URL is required.")
    if any(ord(char) < 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise DownloadError("The URL contains unsupported control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port or 443
        host = parts.hostname
    except ValueError as exc:
        raise DownloadError("The download URL is malformed.") from exc
    if parts.scheme.lower() != "https" or not host:
        raise DownloadError("Downloads require a public HTTPS URL.")
    if parts.username is not None or parts.password is not None:
        raise DownloadError("URLs containing credentials are not supported.")
    if port != 443:
        raise DownloadError("Downloads support the standard HTTPS port (443) only.")
    if "%" in host:
        raise DownloadError("Encoded or scoped host addresses are not supported.")
    try:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DownloadError("The URL hostname is invalid.") from exc
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise DownloadError("Local and internal download hosts are not supported.")
    if Path(unquote(parts.path)).suffix.lower() in BLOCKED_SUFFIXES:
        raise DownloadError("Scripts and unsupported executable files cannot be downloaded.")
    authority = f"[{host}]" if ":" in host else host
    # Retain existing percent escapes and signed query strings without decoding.
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="%=&;/:@!$'()*+,-._~?")
    return urlsplit(urlunsplit(("https", authority, path, query, "")))


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_global and not ip.is_multicast and not ip.is_reserved)


def _resolve_public_addresses(host: str, port: int = 443) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadError(f"Could not resolve the download host: {host}.") from exc
    addresses = tuple(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise DownloadError("The download host resolves to a non-public address.")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: int):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._checked_address = address

    def connect(self):
        # Connecting to a numeric address prevents a second DNS lookup from
        # changing the destination after the public-address validation.
        sock = socket.create_connection((self._checked_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _open_response(parts, addresses: tuple[str, ...], timeout: int):
    last_error = None
    request_target = parts.path + (f"?{parts.query}" if parts.query else "")
    for address in addresses:
        connection = _PinnedHTTPSConnection(parts.hostname, address, timeout)
        try:
            connection.request("GET", request_target, headers={
                "User-Agent": "SoundCloudPackFinder/0.1 (bounded artifact download)",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            return connection.getresponse(), connection
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            last_error = exc
    raise DownloadError("Could not establish a verified HTTPS download connection.") from last_error


def _safe_filename(response, parts) -> str:
    header = response.getheader("Content-Disposition")
    filename = None
    if header:
        message = Message()
        message["content-disposition"] = header
        filename = message.get_filename()
    if not filename:
        filename = unquote(parts.path.rsplit("/", 1)[-1])
    filename = unicodedata.normalize("NFKC", str(filename))
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if not unicodedata.category(char).startswith("C"))
    filename = re.sub(r"[^\w .()+-]", "_", filename).strip(" .")
    if not filename or filename in {".", ".."}:
        raise DownloadError("The server did not provide a supported artifact filename.")
    suffix = Path(filename).suffix.lower()
    if suffix in BLOCKED_SUFFIXES:
        raise DownloadError("Scripts and unsupported executable files cannot be downloaded.")
    if suffix not in ALLOWED_SUFFIXES:
        raise DownloadError("The download must be an archive, audio file, supported preset, or installer.")
    stem = filename[:-len(suffix)].strip(" .") or "download"
    if stem.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        stem = f"download-{stem}"
    # Keep UTF-8 names within ordinary filesystem component length limits.
    while len((stem + suffix).encode("utf-8")) > 180:
        stem = stem[:-1]
    return stem + suffix


def _check_content(prefix: bytes, suffix: str, content_type: str):
    if not prefix:
        raise DownloadError("The server returned an empty file.")
    if content_type in {"text/html", "application/xhtml+xml", "application/json"}:
        raise DownloadError("The URL returned a webpage or API response rather than a file.")
    text_start = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text_start.startswith((b"<!doctype html", b"<html", b"<script", b"<?xml")):
        raise DownloadError("The URL returned a webpage rather than a file.")
    if prefix.startswith((b"\x7fELF", b"#!", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")):
        raise DownloadError("The server returned unsupported executable content.")
    if prefix.startswith(b"MZ") and suffix != ".exe":
        raise DownloadError("The server returned executable content with a misleading extension.")
    signatures = {
        ".exe": lambda p: p.startswith(b"MZ") and len(p) >= 64,
        ".msi": lambda p: p.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ".pkg": lambda p: p.startswith(b"xar!"),
        ".zip": lambda p: p.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
        ".7z": lambda p: p.startswith(b"7z\xbc\xaf\x27\x1c"),
        ".rar": lambda p: p.startswith(b"Rar!\x1a\x07"),
        ".gz": lambda p: p.startswith(b"\x1f\x8b"),
        ".bz2": lambda p: p.startswith(b"BZh"),
        ".xz": lambda p: p.startswith(b"\xfd7zXZ\x00"),
        ".tar": lambda p: len(p) >= 262 and p[257:262] == b"ustar",
        ".wav": lambda p: p.startswith((b"RIFF", b"RF64")) and p[8:12] == b"WAVE",
        ".aif": lambda p: p.startswith(b"FORM") and p[8:12] in {b"AIFF", b"AIFC"},
        ".aiff": lambda p: p.startswith(b"FORM") and p[8:12] in {b"AIFF", b"AIFC"},
        ".flac": lambda p: p.startswith(b"fLaC"),
        ".mp3": lambda p: p.startswith(b"ID3") or (len(p) > 1 and p[0] == 255 and p[1] & 224 == 224),
        ".ogg": lambda p: p.startswith(b"OggS"),
        ".sf2": lambda p: p.startswith(b"RIFF") and p[8:12] == b"sfbk",
        ".fxp": lambda p: p.startswith(b"CcnK"),
        ".fxb": lambda p: p.startswith(b"CcnK"),
        ".vstpreset": lambda p: p.startswith(b"VST3"),
        ".adg": lambda p: p.startswith(b"\x1f\x8b"),
        ".als": lambda p: p.startswith(b"\x1f\x8b"),
    }
    if suffix in signatures and not signatures[suffix](prefix):
        raise DownloadError(f"The file contents do not match the {suffix} extension.")


def _check_complete_content(trailer: bytes, suffix: str, total_bytes: int):
    if suffix == ".dmg" and (total_bytes < 512 or trailer[-512:-508] != b"koly"):
        raise DownloadError("The file contents do not match the .dmg extension (missing UDIF trailer).")


def _publish_file(temporary: Path, directory: Path, filename: str) -> Path:
    """Atomically publish without replacing any existing file, even during a race."""
    name = Path(filename)
    for index in range(1000):
        candidate = directory / (filename if index == 0 else f"{name.stem}-{index}{name.suffix}")
        try:
            os.link(temporary, candidate)
        except FileExistsError:
            continue
        temporary.unlink()
        return candidate
    raise DownloadError("Too many files share this download name.")


def validate_download_file(
    path: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    filename: str | None = None,
) -> dict:
    """Validate and hash a local artifact, including one received from Telegram.

    ``filename`` may supply the intended filename while ``path`` is a temporary
    file. This does not extract, execute, rename, or delete the file.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise DownloadError("max_bytes must be a positive integer.")
    path = Path(path)
    artifact_name = filename or path.name
    if not is_supported_filename(artifact_name):
        raise DownloadError("The download must be an archive, audio file, supported preset, or installer.")
    if path.is_symlink() or not path.is_file():
        raise DownloadError("The downloaded artifact must be a regular file.")
    try:
        if path.stat().st_size > max_bytes:
            raise DownloadError("The file exceeds the configured download size limit.")
        digest = hashlib.sha256()
        total = 0
        trailer = b""
        with path.open("rb") as stream:
            prefix = stream.read(min(1024, max_bytes + 1))
            _check_content(prefix, Path(artifact_name).suffix.lower(), "application/octet-stream")
            chunk = prefix
            while chunk:
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError("The file exceeds the configured download size limit.")
                digest.update(chunk)
                trailer = (trailer + chunk)[-512:]
                chunk = stream.read(64 * 1024)
        suffix = Path(artifact_name).suffix.lower()
        _check_complete_content(trailer, suffix, total)
        return {"path": str(path), "filename": artifact_name, "bytes": total, "sha256": digest.hexdigest(), "installer": suffix in INSTALLER_SUFFIXES}
    except OSError as exc:
        raise DownloadError(f"Could not read the downloaded artifact: {exc}.") from exc


def download_url(
    url: str,
    destination_dir: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    sha256_expected: str | None = None,
) -> dict:
    """Download a public HTTPS artifact and return its path and SHA-256 metadata.

    Redirects, file size, filename, and recognizable formats are checked. Files,
    including installers, remain unopened with non-executable file permissions.
    This is format validation, not a malware scanner or a license determination.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise DownloadError("max_bytes must be a positive integer.")
    if sha256_expected is not None and (not isinstance(sha256_expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256_expected)):
        raise DownloadError("The expected SHA-256 must contain 64 hexadecimal characters.")
    initial = _parse_url(url)
    current = initial
    response = None
    connection = None
    temporary = None
    started = time.monotonic()
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            addresses = _resolve_public_addresses(current.hostname)
            response, connection = _open_response(current, addresses, DEFAULT_TIMEOUT)
            if response.status not in {301, 302, 303, 307, 308}:
                break
            location = response.getheader("Location")
            response.close()
            connection.close()
            response = connection = None
            if not location:
                raise DownloadError("The download server returned a redirect without a destination.")
            if redirect_count == MAX_REDIRECTS:
                raise DownloadError("The download exceeded the redirect limit.")
            current = _parse_url(urljoin(current.geturl(), location))
        if response.status != 200:
            raise DownloadError(f"The download server returned HTTP {response.status}.")
        content_encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
        if content_encoding != "identity":
            raise DownloadError("Unexpected HTTP content encoding; use a direct file URL.")
        length = response.getheader("Content-Length")
        expected_length = None
        if length is not None:
            try:
                expected_length = int(length)
            except (ValueError, TypeError) as exc:
                raise DownloadError("The server returned an invalid Content-Length.") from exc
            if expected_length < 0 or expected_length > max_bytes:
                raise DownloadError("The file exceeds the configured download size limit.")
        filename = _safe_filename(response, current)
        content_type = (response.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        first = response.read(min(1024, max_bytes + 1))
        if len(first) > max_bytes:
            raise DownloadError("The file exceeds the configured download size limit.")
        _check_content(first, Path(filename).suffix.lower(), content_type)
        directory = Path(destination_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        trailer = b""
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".packfinder-", suffix=".part", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            chunk = first
            while chunk:
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError("The file exceeds the configured download size limit.")
                if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                    raise DownloadError("The download exceeded the overall time limit.")
                digest.update(chunk)
                trailer = (trailer + chunk)[-512:]
                stream.write(chunk)
                chunk = response.read(64 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_length is not None and total != expected_length:
            raise DownloadError("The server returned an incomplete file.")
        suffix = Path(filename).suffix.lower()
        _check_complete_content(trailer, suffix, total)
        actual_hash = digest.hexdigest()
        if sha256_expected is not None and not hmac.compare_digest(actual_hash, sha256_expected.lower()):
            raise DownloadError("The downloaded file did not match the expected SHA-256.")
        final_path = _publish_file(temporary, directory, filename)
        temporary = None
        return {
            "path": str(final_path),
            "filename": final_path.name,
            "bytes": total,
            "sha256": actual_hash,
            "source_url": initial.geturl(),
            "final_url": current.geturl(),
            "content_type": content_type,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "installer": suffix in INSTALLER_SUFFIXES,
        }
    except DownloadError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise DownloadError(f"The download could not be completed: {exc}.") from exc
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
