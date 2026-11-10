"""Parse public links and identify explicitly advertised music freebies."""

import re
from urllib.parse import parse_qs, unquote, urlsplit

URL = re.compile(r"(?:https?://|tg://)[^\s<>\"']+|(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']+", re.I)
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")
MUSIC = re.compile(r"\b(?:vsts?|plugins?|sample\s*packs?|sound\s*packs?|drum\s*(?:kits?|packs?)|loop\s*packs?|one[ -]?shots?|presets?|soundfonts?|kontakt|serum|vital\s*bank)\b", re.I)
FREE = re.compile(r"\bfree\b|\bcc0\b|\bpublic domain\b|\bpay what you want\b|(?:\$|€|£)\s*0(?:[.,]00)?\b", re.I)
BLOCKED = re.compile(r"\b(?:crack(?:ed)?|keygens?|warez|nulled|pirated|license bypass|trials?|demos?|preview|free shipping|with (?:a )?purchase)\b|\bnot(?:\s+\w+){0,3}\s+free\b|\b(?:isn't|isnt|no)\s+free\b", re.I)
PRICE = re.compile(r"(?:\$|€|£)\s*(\d+(?:[.,]\d{1,2})?)")


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(m.group().rstrip(".,;:!?)]}") for m in URL.finditer(text or "")))


def telegram_channel(url: str) -> str | None:
    """Accept public usernames/post links; never private invites or tg login links."""
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlsplit(url)
        if p.username or p.password:
            return None
        if p.scheme == "tg" and p.netloc == "resolve":
            name = parse_qs(p.query).get("domain", [""])[0]
        elif p.scheme in ("http", "https") and p.hostname in ("t.me", "telegram.me", "telegram.dog", "www.t.me"):
            parts = [unquote(x) for x in p.path.split("/") if x]
            if parts and parts[0] == "s":
                parts = parts[1:]
            if not parts:
                return None
            name = parts[0]
            if name.lower() in {"joinchat", "c", "share", "proxy", "socks", "login", "addstickers", "addemoji", "boost", "iv"}:
                return None
        else:
            return None
        return name.lower() if USERNAME.fullmatch(name) else None
    except (ValueError, TypeError):
        return None


def profile_telegram_links(profile: dict) -> list[tuple[str, str]]:
    pieces = [str(profile.get(k) or "") for k in ("description", "website", "website_title")]
    for item in profile.get("web_profiles") or []:
        if isinstance(item, dict):
            pieces.extend(str(item.get(k) or "") for k in ("url", "title", "network"))
    channels = {}
    for url in extract_urls("\n".join(pieces)):
        name = telegram_channel(url)
        if name:
            channels[name] = "https://t.me/" + name
    return sorted(channels.items())


def classify_freebie(text: str, filename: str = "") -> tuple[bool, str]:
    combined = f"{text or ''}\n{filename or ''}"
    if BLOCKED.search(combined):
        return False, "Excluded wording (trial, paid exception, or unauthorized copy)"
    if not MUSIC.search(combined):
        return False, "No music-asset wording"
    # A filename or 'royalty-free' alone is not a statement of a zero price.
    caption = re.sub(r"(?:royalty|ad|copyright|license)[ -]free", "price_unspecified", text or "", flags=re.I)
    if any(float(m.group(1).replace(",",".")) > 0 for m in PRICE.finditer(caption)):
        return False,"Music asset; caption includes a nonzero price"
    if FREE.search(caption):
        return True, "Caption advertises a free music asset; inspect its supplied license"
    return False, "Music asset; no explicit free offer in caption"


def soundcloud_profile_url(value: str) -> str:
    p = urlsplit(value if "://" in value else "https://soundcloud.com/" + value.strip("/"))
    parts = [x for x in p.path.split("/") if x]
    if p.scheme != "https" or p.hostname not in {"soundcloud.com", "www.soundcloud.com"} or p.username or p.password or len(parts) != 1:
        raise ValueError("Use a SoundCloud profile URL, for example https://soundcloud.com/dollsy_blackheart")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
        raise ValueError("Invalid SoundCloud profile name")
    return "https://soundcloud.com/" + parts[0]
