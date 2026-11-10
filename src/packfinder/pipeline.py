"""Bounded breadth-first discovery and a resumable music-download pipeline."""

import json
from pathlib import Path
import re
import time
from urllib.parse import unquote,urlsplit

from .links import classify_freebie, extract_urls, profile_telegram_links, soundcloud_profile_url
from .state import State


def crawl_profiles(client, state: State, seed: str, *, depth=2, max_profiles=200, followers_per_profile=30, delay=0.5, retry_errors=False, log=print):
    if not 0 <= depth <= 6 or not 1 <= max_profiles <= 10000 or not 1 <= followers_per_profile <= 1000 or delay < 0:
        raise ValueError("depth must be 0..6, profiles 1..10000, followers 1..1000, delay >=0")
    seed = soundcloud_profile_url(seed)
    state.seed(seed)
    if state.count_profiles() == 0:
        state.add_profile(client.resolve_profile(seed), 0)
    state.prepare_resume(retry_errors,followers_limit=followers_per_profile)
    while row := state.next_profile(depth):
        user_id = row["id"]
        try:
            if not row["scanned"]:
                if delay:
                    time.sleep(delay)
                profile = client.get_profile(user_id)
                state.finish_profile(profile,profile_telegram_links(profile))
                log(f"depth {row['depth']}: {profile.get('username',user_id)}")
            if row["depth"] >= depth:
                continue
            if state.count_profiles() >= max_profiles:
                state.expanded(user_id,capped=True)
                continue
            if delay:
                time.sleep(delay)
            capped = False
            for follower in client.iter_followers(user_id,limit=followers_per_profile):
                child = str(follower["id"])
                if not state.has_profile(child) and state.count_profiles() >= max_profiles:
                    capped = True
                    break
                state.add_profile(follower,row["depth"]+1)
                state.add_edge(user_id,child)
            state.expanded(user_id,capped)
        except Exception as exc:
            # KeyboardInterrupt propagates; a partial expansion resumes without duplicates.
            state.profile_error(user_id,str(exc))
            log(f"Could not read profile {user_id}: {exc}")
            if getattr(exc,"stop_crawl",False):
                log("Paused this crawl; use --retry-errors when the service is available again.")
                break
    return state.stats()


async def scan_channels(client, state: State, *, messages_per_channel=100, max_channels=50, refresh=False, delay=1.0, log=print):
    if not 1 <= messages_per_channel <= 1000 or not 1 <= max_channels <= 1000 or delay < 0:
        raise ValueError("messages and channels must be 1..1000; delay >=0")
    channels = state.db.execute("SELECT * FROM channels WHERE scanned=0 OR ? ORDER BY username LIMIT ?",(int(refresh),max_channels)).fetchall()
    from .downloads import is_supported_filename
    import asyncio
    for row in channels:
        channel = row["username"]
        try:
            for message in await client.scan_channel(channel,limit=messages_per_channel):
                state.add_message(channel,message)
                caption = message.get("text") or ""
                attachment = message.get("attachment")
                handled_public_url = None
                if attachment:
                    filename = attachment.get("file_name") or "unnamed"
                    eligible,reason = classify_freebie(caption,filename)
                    if is_supported_filename(filename) and (eligible or "Music asset" in reason):
                        public_url = attachment.get("download_url")
                        try:
                            parsed_public = urlsplit(public_url) if isinstance(public_url,str) else None
                            valid_public = parsed_public is not None and parsed_public.scheme == "https" and parsed_public.hostname and parsed_public.username is None and parsed_public.password is None
                        except ValueError:
                            valid_public = False
                        if valid_public:
                            state.add_asset(kind="http",channel=channel,message_id=message["message_id"],filename=filename,text=caption,eligible=eligible,reason=reason,url=public_url)
                            handled_public_url = public_url
                        else:
                            if getattr(client,"supports_attachment_download",True) is False:
                                eligible = False
                                reason = "Public preview does not expose a file URL; open the post in Telegram or use optional account mode"
                            state.add_asset(kind="telegram",channel=channel,message_id=message["message_id"],filename=filename,text=caption,eligible=eligible,reason=reason)
                for url in list(dict.fromkeys((message.get("links") or []) + extract_urls(caption))):
                    if not isinstance(url,str) or url == handled_public_url:
                        continue
                    try:
                        parsed = urlsplit(url)
                        filename = unquote(parsed.path.rsplit("/",1)[-1])
                    except (ValueError,TypeError):
                        continue
                    eligible,reason = classify_freebie(caption,filename)
                    if parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None and (eligible or "Music asset" in reason):
                        # Landing pages stay in the queue, alongside directly downloadable files.
                        if not is_supported_filename(filename):
                            eligible,reason = False,"Music offer landing page; open it to obtain a direct download URL"
                        state.add_asset(kind="http",channel=channel,message_id=message["message_id"],filename=filename or "landing-page",text=caption,eligible=eligible,reason=reason,url=url)
                if message.get("unsupported_media") and classify_freebie(caption)[0]:
                    state.add_asset(kind="http",channel=channel,message_id=message["message_id"],filename="Telegram post",text=caption,eligible=False,
                        reason="Free music offer; the public preview does not expose its media file URL",url=f"https://t.me/{channel}/{message['message_id']}")
            with state.db:
                state.db.execute("UPDATE channels SET scanned=1,error=NULL,skip_reason=NULL WHERE username=?",(channel,))
            log(f"Scanned @{channel}")
        except Exception as exc:
            with state.db:
                if getattr(exc,"skip_channel",False):
                    state.db.execute("UPDATE channels SET scanned=1,error=NULL,skip_reason=? WHERE username=?",(str(exc),channel))
                else:
                    state.db.execute("UPDATE channels SET error=? WHERE username=?",(str(exc),channel))
            log(f"Skipped @{channel}: {exc}" if getattr(exc,"skip_channel",False) else f"Could not read @{channel}: {exc}")
        if delay:
            await asyncio.sleep(delay)
    return state.stats()


async def download_assets(state: State, directory: Path, *, telegram=None, max_files=20, max_file_bytes=512*1024*1024, max_total_bytes=1024*1024*1024, retry_errors=False, log=print):
    from .downloads import download_url, is_supported_filename, validate_download_file
    if min(max_files,max_file_bytes,max_total_bytes) <= 0:
        raise ValueError("Download budgets must be positive")
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    statuses = ("queued","error") if retry_errors else ("queued",)
    placeholders = ",".join("?" for _ in statuses)
    available_kind = " AND kind='http'" if telegram is None or getattr(telegram,"supports_attachment_download",True) is False else ""
    assets = state.db.execute(f"SELECT * FROM assets WHERE eligible=1 AND status IN ({placeholders}){available_kind} ORDER BY rowid LIMIT ?",(*statuses,max_files)).fetchall()
    total = 0
    for row in assets:
        remaining = max_total_bytes-total
        if remaining <= 0:
            break
        try:
            if row["kind"] == "http":
                options = {"max_bytes":min(max_file_bytes,remaining)}
                url_name = unquote(urlsplit(row["url"]).path.rsplit("/",1)[-1])
                if not is_supported_filename(url_name) and is_supported_filename(row["filename"]):
                    options["filename_hint"] = row["filename"]
                result = download_url(row["url"],directory,**options)
            else:
                if telegram is None or getattr(telegram,"supports_attachment_download",True) is False:
                    log(f"{row['id']}: no public file URL; leaving Telegram attachment queued")
                    continue
                name = re.sub(r"[^A-Za-z0-9._-]","_",Path(row["filename"]).name)[:160]
                if not is_supported_filename(name):
                    raise ValueError("Unsupported attachment file type")
                path = directory / f"{row['id']}-{name}"
                await telegram.download_attachment(row["channel"],row["message_id"],path,max_bytes=min(max_file_bytes,remaining))
                try:
                    result = validate_download_file(path,max_bytes=min(max_file_bytes,remaining))
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
            total += result["bytes"]
            with state.db:
                state.db.execute("UPDATE assets SET status='downloaded',path=?,sha256=?,bytes=?,error=NULL WHERE id=?",(str(result["path"]),result["sha256"],result["bytes"],row["id"]))
            # Include provenance next to each file; never place credentials in this manifest.
            try:
                with (directory / "manifest.jsonl").open("a",encoding="utf-8") as f:
                    f.write(json.dumps({"asset_id":row["id"],"source":row["url"] or f"https://t.me/{row['channel']}/{row['message_id']}","reason":row["reason"],**result},default=str,ensure_ascii=False)+"\n")
            except OSError as exc:
                # SQLite already records the successful file and hash. Do not retry it.
                log(f"File saved; could not append the JSONL manifest: {exc}")
            log(f"Downloaded {result['filename']} ({result['bytes']} bytes)")
        except Exception as exc:
            with state.db:
                state.db.execute("UPDATE assets SET status='error',error=? WHERE id=?",(str(exc),row["id"]))
            log(f"Download {row['id']} failed: {exc}")
    return state.stats()
