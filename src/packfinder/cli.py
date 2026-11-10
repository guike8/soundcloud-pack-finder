"""Command-line entry point. Credentials are supplied only through the local environment."""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

from .pipeline import crawl_profiles, scan_channels, download_assets
from .report import export_report
from .state import State

DEFAULT_SEED = "https://soundcloud.com/dollsy_blackheart"


def parser():
    p = argparse.ArgumentParser(prog="packfinder",description="SoundCloud follower graph → public Telegram links → free music-asset downloads.")
    sub = p.add_subparsers(dest="command",required=True)
    def command(name,help):
        s = sub.add_parser(name,help=help)
        s.add_argument("--db",type=Path,default=Path("data/crawl.sqlite3"))
        return s
    def graph(s):
        s.add_argument("seed",nargs="?",default=DEFAULT_SEED)
        s.add_argument("--depth",type=int,default=2,help="0 = seed only, 1 = followers, 2 = followers of followers")
        s.add_argument("--max-profiles",type=int,default=200)
        s.add_argument("--followers",type=int,default=30,help="Maximum followers read from each profile")
        s.add_argument("--delay",type=float,default=0.7,help="Seconds between profile requests")
        s.add_argument("--retry-errors",action="store_true")
    def tg(s):
        s.add_argument("--session",type=Path,default=Path("data/telegram"))
    def scan(s):
        s.add_argument("--messages",type=int,default=100)
        s.add_argument("--max-channels",type=int,default=50)
        s.add_argument("--refresh-channels",action="store_true")
    def downloads(s):
        s.add_argument("--dest",type=Path,default=Path("downloads"))
        s.add_argument("--max-files",type=int,default=20)
        s.add_argument("--max-file-mb",type=int,default=512)
        s.add_argument("--max-total-mb",type=int,default=1024)
    c = command("crawl","Crawl anonymous public SoundCloud pages and follower requests")
    graph(c)
    c = command("telegram-login","Sign into Telegram locally (interactive)")
    tg(c)
    c = command("scan-telegram","Find music assets in discovered public channels")
    tg(c); scan(c)
    c = command("download","Save queued free music assets; never run or extract them")
    tg(c); downloads(c)
    c.add_argument("--retry-errors",action="store_true")
    c = command("run","Crawl SoundCloud, scan Telegram, and optionally download")
    graph(c); tg(c); scan(c); downloads(c)
    c.add_argument("--download",action="store_true",help="Download explicitly advertised free candidates")
    c.add_argument("--report",type=Path,default=Path("reports/latest"))
    c = command("report","Write offline HTML, JSON, and CSV results")
    c.add_argument("--out",type=Path,default=Path("reports/latest"))
    c = command("queue","Show download candidates and their selection reasons")
    c.add_argument("--limit",type=int,default=50)
    c = command("approve","Mark a queued asset as a confirmed free download")
    c.add_argument("asset_id")
    c.add_argument("--reason",required=True,help="Record where the creator offers it free")
    c = command("doctor","Check local setup; optionally test public SoundCloud access")
    c.add_argument("--live",action="store_true")
    c.add_argument("--seed",default=DEFAULT_SEED)
    c = command("demo","Exercise the pipeline with fictional offline data")
    c.set_defaults(db=Path("data/demo.sqlite3"))
    c.add_argument("--out",type=Path,default=Path("reports/demo"))
    return p


async def _telegram(args,login=False):
    from .telegram import TelegramClient
    args.session.parent.mkdir(parents=True,exist_ok=True)
    client = TelegramClient(session=str(args.session))
    try:
        if login:
            await client.login()
        else:
            await client.connect()
        return client
    except BaseException:
        await client.close()
        raise


async def _run(args,state):
    tgclient = None
    try:
        if args.command == "demo":
            from .demo import DemoSoundCloud,DemoTelegram
            crawl_profiles(DemoSoundCloud(),state,"https://soundcloud.com/demo-seed",delay=0)
            await scan_channels(DemoTelegram(),state,delay=0)
            print("FICTIONAL DEMO: no remote profiles or downloads were accessed.")
            print(json.dumps(export_report(state,args.out),indent=2))
        elif args.command in {"crawl","run"}:
            from .soundcloud import SoundCloudClient
            client = SoundCloudClient()
            crawl_profiles(client,state,args.seed,depth=args.depth,max_profiles=args.max_profiles,followers_per_profile=args.followers,delay=args.delay,retry_errors=args.retry_errors)
            if args.command == "run":
                if state.stats()["channels"]:
                    try:
                        tgclient = await _telegram(args)
                        await scan_channels(tgclient,state,messages_per_channel=args.messages,max_channels=args.max_channels,refresh=args.refresh_channels)
                        if args.download:
                            await _download(args,state,tgclient)
                    except (ValueError,RuntimeError) as exc:
                        print(f"Telegram setup needed: {exc}",file=sys.stderr)
                        print(json.dumps(export_report(state,args.report),indent=2))
                        return 2
                print(json.dumps(export_report(state,args.report),indent=2))
        elif args.command == "telegram-login":
            tgclient = await _telegram(args,login=True)
            print("Telegram session saved locally. You can now use scan-telegram or run.")
        elif args.command == "scan-telegram":
            tgclient = await _telegram(args)
            await scan_channels(tgclient,state,messages_per_channel=args.messages,max_channels=args.max_channels,refresh=args.refresh_channels)
        elif args.command == "download":
            needs_tg = state.db.execute("SELECT 1 FROM assets WHERE kind='telegram' AND eligible=1 AND status!='downloaded' LIMIT 1").fetchone()
            if needs_tg:
                tgclient = await _telegram(args)
            await _download(args,state,tgclient)
        elif args.command == "report":
            print(json.dumps(export_report(state,args.out),indent=2))
        elif args.command == "queue":
            rows = state.db.execute("SELECT id,kind,filename,channel,eligible,status,reason,error FROM assets ORDER BY rowid LIMIT ?",(max(1,args.limit),)).fetchall()
            print(json.dumps([dict(r) for r in rows],indent=2,ensure_ascii=False))
        elif args.command == "approve":
            if not args.reason.strip():
                raise ValueError("A selection reason is required")
            with state.db:
                result = state.db.execute("UPDATE assets SET eligible=1,reason=? WHERE id=? AND status!='downloaded'",("User-confirmed free offer: "+args.reason,args.asset_id))
            if not result.rowcount:
                raise ValueError("Unknown asset or already downloaded")
            print("Free offer recorded. Use download to save this asset.")
        elif args.command == "doctor":
            info = {"python":sys.version.split()[0],"soundcloud":"anonymous website mode (no API app/token)","telethon_installed":importlib.util.find_spec("telethon") is not None,
              "telegram_api_id_set":bool(os.environ.get("TELEGRAM_API_ID")),"telegram_api_hash_set":bool(os.environ.get("TELEGRAM_API_HASH"))}
            if args.live:
                from .soundcloud import SoundCloudClient
                client = SoundCloudClient()
                profile = client.resolve_profile(args.seed)
                followers = list(client.iter_followers(profile["id"],limit=1))
                info["live_soundcloud"] = {"username":profile.get("username"),"resolved":True,"followers_read":len(followers)}
            print(json.dumps(info,indent=2))
        print(json.dumps(state.stats(),sort_keys=True))
        return 1 if state.stats()["errors"] else 0
    finally:
        if tgclient:
            await tgclient.close()


async def _download(args,state,tgclient):
    return await download_assets(state,args.dest,telegram=tgclient,max_files=args.max_files,max_file_bytes=args.max_file_mb*1024*1024,max_total_bytes=args.max_total_mb*1024*1024,retry_errors=args.retry_errors)


def main(argv=None):
    args = parser().parse_args(argv)
    state = None
    try:
        state = State(args.db)
        return asyncio.run(_run(args,state))
    except KeyboardInterrupt:
        print("Stopped. Progress is saved; run the same command to resume.",file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}",file=sys.stderr)
        return 2
    finally:
        if state:
            state.close()
