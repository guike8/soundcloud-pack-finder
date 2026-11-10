"""Read public SoundCloud website data without app registration or login.

Anonymous profile HTML supplies an expiring apiClient ID. The site's shipped
JavaScript declares api-v2 user/follower/web-profile routes. Discover that
public configuration at runtime; never embed OAuth tokens or account cookies.
The injectable request(url, headers) returns HTML text/bytes or decoded JSON.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_BASE = "https://api-v2.soundcloud.com"
WEBSITE_BASE = "https://soundcloud.com/"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PUBLIC_HOSTS = {"soundcloud.com", "www.soundcloud.com", "on.soundcloud.com", "api-v2.soundcloud.com", "a-v2.sndcdn.com"}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
JsonRequest = Callable[[str, dict[str, str]], Any]


class SoundCloudError(RuntimeError):
    """Public website/API failure, with no credential-bearing URL in its text."""


class SoundCloudStopError(SoundCloudError):
    """A site-wide pause/block: persist progress and stop the current crawl."""

    stop_crawl = True


def _public_url(url: str, *, api_only: bool = False) -> str:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise SoundCloudError("SoundCloud returned an invalid URL.") from None
    hosts = {"api-v2.soundcloud.com"} if api_only else PUBLIC_HOSTS
    if parts.scheme != "https" or parts.hostname not in hosts or port not in (None, 443) or parts.username is not None or parts.password is not None or parts.fragment:
        raise SoundCloudError("SoundCloud returned a URL outside its public website services.")
    return url


class _PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _user_urn(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdecimal():
        return "soundcloud:users:" + text
    if re.fullmatch(r"soundcloud:users:[A-Za-z0-9_-]+", text):
        return text
    raise SoundCloudError("Expected a SoundCloud user URN or numeric user ID.")


def _normalize_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind", "user") != "user":
        raise SoundCloudError("Use a SoundCloud user profile, rather than a track or playlist.")
    urn = _user_urn(value.get("urn") or value.get("id"))
    result = dict(value)
    result.update(id=urn, urn=urn, username=str(value.get("username") or ""), permalink_url=str(value.get("permalink_url") or ""), description=str(value.get("description") or ""), website=str(value.get("website") or ""), website_title=str(value.get("website_title") or ""), web_profiles=value.get("web_profiles") or [])
    return result


def _hydration(html: str) -> list[dict[str, Any]]:
    marker = re.search(r"window\.__sc_hydration\s*=\s*", html)
    if marker is None:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html, marker.end())
    except json.JSONDecodeError:
        raise SoundCloudError("SoundCloud website hydration data changed or is incomplete.") from None
    if not isinstance(data, list):
        raise SoundCloudError("SoundCloud website hydration data has an unsupported shape.")
    return [entry for entry in data if isinstance(entry, dict)]


class SoundCloudClient:
    """Read public bios/followers using anonymous website requests.

    No API registration, OAuth token, or SoundCloud account is required.
    client_id is an optional public website configuration override; the normal
    path discovers it from the seed HTML. This website interface may change.
    """

    def __init__(self, client_id: str | None = None, request: JsonRequest | None = None, *, timeout: float = 30.0, min_interval: float = 0.5, max_retries: int = 3):
        if client_id is not None and not re.fullmatch(r"[A-Za-z0-9]{16,128}", client_id):
            raise ValueError("The optional public website client ID must be alphanumeric.")
        if timeout <= 0 or not 0 <= min_interval <= 60 or not 0 <= max_retries <= 8:
            raise ValueError("Invalid SoundCloud timeout, request interval, or retry count.")
        self._client_id = client_id
        self._request = request
        self._timeout = timeout
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_request = 0.0
        self._opener = build_opener(_PublicRedirects())
        self._profiles: dict[str, dict[str, Any]] = {}
        self._bootstrap_url = WEBSITE_BASE
        self._headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/javascript, */*; q=0.01", "Accept-Language": "en-US,en;q=0.9", "Origin": "https://soundcloud.com", "Referer": WEBSITE_BASE}

    def _fetch(self, url: str) -> Any:
        _public_url(url)
        headers = dict(self._headers)
        if urlsplit(url).hostname != "api-v2.soundcloud.com":
            headers["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.8"
        if self._request is not None:
            return self._request(url, headers)
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                with self._opener.open(Request(url, headers=headers), timeout=self._timeout) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SoundCloudError("SoundCloud response exceeded 8 MiB.")
                    return body
            except HTTPError as exc:
                exc.close()
                if exc.code in (401, 403):
                    raise SoundCloudStopError("SoundCloud blocked this anonymous request or its public client ID expired. Try again later; this tool does not bypass login or access checks.") from None
                if exc.code == 404:
                    raise SoundCloudError("SoundCloud profile/resource was not found or is unavailable.") from None
                if exc.code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                    delay = min(2 ** attempt, 30)
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    if retry_after:
                        try:
                            delay = max(0.0, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                            except (TypeError, ValueError, OverflowError):
                                pass
                    if delay > 60:
                        raise SoundCloudStopError("SoundCloud requested a longer pause. Resume the crawl later.") from None
                    time.sleep(delay)
                    continue
                if exc.code == 429:
                    raise SoundCloudStopError("SoundCloud rate limit reached. Resume the crawl later.") from None
                raise SoundCloudError(f"SoundCloud returned HTTP {exc.code}.") from None
            except (URLError, TimeoutError, OSError):
                if attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise SoundCloudError("Could not reach SoundCloud. Check your connection and resume later.") from None
        raise SoundCloudError("SoundCloud request failed.")

    def _text(self, url: str) -> str:
        raw = self._fetch(url)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        if isinstance(raw, str):
            return raw
        raise SoundCloudError("SoundCloud returned an invalid website response.")

    def _discover_client_id(self, html: str) -> None:
        for entry in _hydration(html):
            if entry.get("hydratable") == "apiClient" and isinstance(entry.get("data"), dict):
                candidate = entry["data"].get("id")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9]{16,128}", candidate):
                    self._client_id = candidate
                    return
        # Older website versions ship the public ID in a bootstrap asset.
        sources = re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I)
        trusted = [unescape(src) for src in sources if urlsplit(unescape(src)).hostname == "a-v2.sndcdn.com"]
        for src in reversed(trusted[-8:]):
            script = self._text(_public_url(src))
            match = re.search(r'\bclient_id\s*:\s*["\']([A-Za-z0-9]{16,128})["\']', script)
            if match:
                self._client_id = match.group(1)
                return
        raise SoundCloudError("Could not discover SoundCloud's public website client configuration. Its website may have changed.")

    def _with_client_id(self, url: str) -> str:
        _public_url(url, api_only=True)
        if self._client_id is None:
            self._discover_client_id(self._text(self._bootstrap_url))
        parts = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "client_id"]
        query.append(("client_id", self._client_id or ""))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def _json(self, url: str) -> Any:
        raw = self._fetch(self._with_client_id(url))
        if isinstance(raw, (bytes, str)):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise SoundCloudError("SoundCloud returned an invalid JSON response.") from None
        return raw

    def _hydrate(self, profile: dict[str, Any]) -> dict[str, Any]:
        raw = self._json(f"{API_BASE}/users/{quote(profile['id'], safe='')}/web-profiles")
        if isinstance(raw, dict):
            raw = raw.get("collection")
        if not isinstance(raw, list):
            raise SoundCloudError("SoundCloud returned an invalid web-profile list.")
        profile["web_profiles"] = [item for item in raw if isinstance(item, dict)]
        self._profiles[profile["id"]] = profile
        return dict(profile)

    def resolve_profile(self, url: str) -> dict[str, Any]:
        url = str(url).strip()
        parts = urlsplit(url)
        if parts.hostname not in {"soundcloud.com", "www.soundcloud.com", "on.soundcloud.com"} or len(parts.path.strip("/").split("/")) != 1 or not parts.path.strip("/"):
            raise ValueError("Use a SoundCloud profile URL, such as https://soundcloud.com/artist.")
        _public_url(url)
        self._bootstrap_url = url
        html = self._text(url)
        self._discover_client_id(html)
        # A short link may resolve to a track: do not silently crawl its artist.
        canonical = re.search(r'<link\b[^>]*\brel=["\']canonical["\'][^>]*\bhref=["\']([^"\']+)', html, re.I)
        if canonical and len(urlsplit(unescape(canonical.group(1))).path.strip("/").split("/")) != 1:
            raise SoundCloudError("Use a SoundCloud user profile, rather than a track or playlist.")
        users = [entry.get("data") for entry in _hydration(html) if entry.get("hydratable") == "user"]
        if not users:
            raise SoundCloudError("The public page did not contain a SoundCloud user profile. It may be unavailable or the website may have changed.")
        return self._hydrate(_normalize_profile(users[0]))

    def get_profile(self, user_id: str) -> dict[str, Any]:
        urn = _user_urn(user_id)
        if urn in self._profiles:
            return dict(self._profiles[urn])
        return self._hydrate(_normalize_profile(self._json(f"{API_BASE}/users/{quote(urn, safe='')}")))

    def iter_followers(self, user_id: str, limit: int = 200) -> Iterator[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("Follower limit must be a non-negative integer.")
        if limit == 0:
            return
        urn = _user_urn(user_id)
        # Shipped website follower routes use the numeric user id; the other
        # observed user routes accept the complete user URN.
        identifier = urn.rsplit(":", 1)[-1]
        params = urlencode({"limit": min(limit, 200), "linked_partitioning": 1})
        url: str | None = f"{API_BASE}/users/{quote(identifier, safe='')}/followers?{params}"
        seen_users: set[str] = set()
        seen_pages: set[str] = set()
        while url and len(seen_pages) < max(10, limit * 2):
            _public_url(url, api_only=True)
            if url in seen_pages:
                raise SoundCloudError("SoundCloud repeated a follower pagination cursor.")
            seen_pages.add(url)
            raw = self._json(url)
            if isinstance(raw, list):
                collection, next_href = raw, None
            elif isinstance(raw, dict) and isinstance(raw.get("collection"), list):
                collection, next_href = raw["collection"], raw.get("next_href")
            else:
                raise SoundCloudError("SoundCloud returned an invalid follower page.")
            for item in collection:
                profile = _normalize_profile(item)
                if profile["id"] in seen_users:
                    continue
                seen_users.add(profile["id"])
                yield profile
                if len(seen_users) >= limit:
                    return
            url = _public_url(urljoin(API_BASE, str(next_href)), api_only=True) if next_href else None
        if url:
            raise SoundCloudError("SoundCloud follower pagination exceeded its bounded page limit.")
