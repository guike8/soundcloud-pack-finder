"""Offline end-to-end checks for graph, queue, resume, and export behavior."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packfinder import cli
from packfinder.downloads import DownloadError
from packfinder.pipeline import crawl_profiles, download_assets, scan_channels
from packfinder.report import export_report
from packfinder.state import State


SEED = "https://soundcloud.com/fixture-seed"
ZIP_BYTES = b"PK\x03\x04offline fixture payload"


class FixtureSoundCloud:
    def __init__(self):
        self.profiles = {
            "a": {"id": "a", "username": "Seed", "description": "Sounds https://t.me/music_freebies", "permalink_url": SEED},
            "b": {"id": "b", "username": "First follower", "website": "https://telegram.me/MUSIC_FREEBIES/42", "description": "Private invite https://t.me/+not_public"},
            "c": {"id": "c", "username": "Second follower", "description": "No Telegram here"},
            "d": {"id": "d", "username": "Second hop", "web_profiles": [{"url": "https://t.me/s/synth_presets/7"}]},
            "e": {"id": "e", "username": "Third hop", "description": "Outside a two-hop crawl"},
        }
        self.followers = {"a": ["b", "c", "b"], "b": ["a", "d"], "c": ["d"], "d": ["e"], "e": []}
        self.resolves = []
        self.fetched = []
        self.expansions = []
        self.interrupt_once = False

    def resolve_profile(self, url):
        self.resolves.append(url)
        return dict(self.profiles["a"])

    def get_profile(self, user_id):
        self.fetched.append(user_id)
        return dict(self.profiles[user_id])

    def iter_followers(self, user_id, limit):
        self.expansions.append((user_id, limit))
        for index, key in enumerate(self.followers.get(user_id, [])[:limit]):
            yield dict(self.profiles[key])
            if self.interrupt_once and user_id == "a" and index == 0:
                self.interrupt_once = False
                raise KeyboardInterrupt()


class FixtureTelegram:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def scan_channel(self, channel, limit=100):
        self.calls.append((channel, limit))
        return self.messages.get(channel, [])[:limit]


def message(message_id, text, filename=None, links=None):
    return {
        "message_id": message_id,
        "text": text,
        "links": links or [],
        "attachment": {"file_name": filename, "size": len(ZIP_BYTES)} if filename else None,
    }


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.dbpath = self.directory / "crawl.sqlite3"
        self.state = State(self.dbpath)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def reopen(self):
        self.state.close()
        self.state = State(self.dbpath)

    def crawl(self, client, **options):
        return crawl_profiles(client, self.state, SEED, delay=0, log=lambda _: None, **options)

    def add_channel(self, name="music_freebies"):
        profile = {"id": "seed", "username": "Fixture"}
        self.state.add_profile(profile, 0)
        self.state.finish_profile(profile, [(name, f"https://t.me/{name}")])

    def scan(self, client, **options):
        return asyncio.run(scan_channels(client, self.state, delay=0, log=lambda _: None, **options))

    def asset(self, url, *, eligible=True, filename="free-samples.zip"):
        return self.state.add_asset(kind="http", channel="music_freebies", message_id=1,
            filename=filename, text="Free sample pack", eligible=eligible, reason="Fixture free offer", url=url)

    def fake_download(self, url, directory, max_bytes):
        self.assertGreaterEqual(max_bytes, len(ZIP_BYTES))
        filename = url.rsplit("/", 1)[-1]
        path = Path(directory) / filename
        path.write_bytes(ZIP_BYTES)
        return {"path": str(path), "filename": filename, "bytes": len(ZIP_BYTES),
            "sha256": hashlib.sha256(ZIP_BYTES).hexdigest(), "source_url": url, "final_url": url}

    def test_two_hop_bfs_cycles_dedup_and_channel_provenance(self):
        client = FixtureSoundCloud()
        self.crawl(client, depth=2)
        profiles = {row["id"]: row for row in self.state.rows("profiles")}
        self.assertEqual({key: row["depth"] for key, row in profiles.items()}, {"a": 0, "b": 1, "c": 1, "d": 2})
        self.assertEqual(client.fetched, ["a", "b", "c", "d"])
        self.assertEqual({tuple(row.values()) for row in self.state.rows("edges")}, {("a", "b"), ("a", "c"), ("b", "a"), ("b", "d"), ("c", "d")})
        self.assertEqual({row["username"] for row in self.state.rows("channels")}, {"music_freebies", "synth_presets"})
        self.assertEqual({(row["username"], row["profile_id"]) for row in self.state.rows("channel_sources")}, {("music_freebies", "a"), ("music_freebies", "b"), ("synth_presets", "d")})
        fetched = list(client.fetched)
        self.crawl(client, depth=2)
        self.assertEqual(client.fetched, fetched)
        self.assertEqual(len(self.state.rows("edges")), 5)

    def test_profile_cap_and_raising_cap_after_reopening_database(self):
        client = FixtureSoundCloud()
        self.crawl(client, depth=2, max_profiles=2)
        self.assertEqual(self.state.count_profiles(), 2)
        self.reopen()
        self.crawl(client, depth=2, max_profiles=10)
        self.assertEqual({row["id"] for row in self.state.rows("profiles")}, {"a", "b", "c", "d"})
        self.assertEqual(len(client.resolves), 1)
        self.assertEqual(self.state.stats()["errors"], 0)
        with self.assertRaisesRegex(ValueError, "another seed"):
            crawl_profiles(client, self.state, "https://soundcloud.com/another-seed", delay=0)

    def test_raising_follower_limit_resumes_existing_graph(self):
        client = FixtureSoundCloud()
        self.crawl(client, depth=1, followers_per_profile=1)
        self.assertEqual({row["id"] for row in self.state.rows("profiles")}, {"a", "b"})
        self.reopen()
        self.crawl(client, depth=1, followers_per_profile=3)
        self.assertEqual({row["id"] for row in self.state.rows("profiles")}, {"a", "b", "c"})
        self.assertEqual(len(self.state.rows("edges")), 2)

    def test_interrupted_expansion_resumes_without_duplicates(self):
        client = FixtureSoundCloud()
        client.interrupt_once = True
        with self.assertRaises(KeyboardInterrupt):
            self.crawl(client, depth=2)
        self.assertTrue(self.state.has_profile("b"))
        self.reopen()
        self.crawl(client, depth=2)
        self.assertEqual(self.state.count_profiles(), 4)
        self.assertEqual(len(self.state.rows("edges")), 5)
        self.assertEqual(client.fetched.count("a"), 1)
        self.assertEqual(self.state.stats()["errors"], 0)

    def test_sitewide_denial_stops_without_requesting_each_pending_profile(self):
        from packfinder.soundcloud import SoundCloudStopError
        client = FixtureSoundCloud()
        self.crawl(client,depth=1,followers_per_profile=1)
        self.state.add_profile(client.profiles["c"],1)
        self.state.add_profile(client.profiles["d"],1)
        calls = []
        def denied(user_id):
            calls.append(user_id)
            raise SoundCloudStopError("Access denied; resume later")
        client.get_profile = denied
        self.crawl(client,depth=1)
        self.assertEqual(calls,["c"])
        self.assertEqual(self.state.stats()["errors"],1)

    def test_personal_telegram_links_are_recorded_as_skips_not_crawl_errors(self):
        from packfinder.telegram import TelegramUnsupportedChannelError
        self.add_channel("personal_contact")
        class PersonalLink:
            async def scan_channel(self,channel,limit):
                raise TelegramUnsupportedChannelError("This is a personal Telegram account")
        self.scan(PersonalLink())
        channel = self.state.rows("channels")[0]
        self.assertEqual(channel["scanned"],1)
        self.assertIn("personal",channel["skip_reason"])
        self.assertIsNone(channel["error"])
        self.assertEqual(self.state.stats()["errors"],0)

    def test_scan_handles_bad_links_and_refreshes_queue_with_current_caption(self):
        self.add_channel()
        first = message(1, "Premium sample pack", "samples.zip", ["https://[malformed/pack.zip"])
        telegram = FixtureTelegram({"music_freebies": [first,
            message(2, "Free VST from the developer", links=["https://files.example/free-plugin.zip", "https://files.example/free-plugin.zip"]),
            message(3, "Free VST on our website", links=["https://files.example/synth"])]})
        self.scan(telegram, messages_per_channel=25)
        assets = self.state.rows("assets")
        self.assertEqual(len(assets), 3)
        self.assertEqual(sum(row["eligible"] for row in assets), 1)
        self.assertEqual(self.state.stats()["errors"], 0)
        self.assertEqual(telegram.calls, [("music_freebies", 25)])
        self.scan(telegram)
        self.assertEqual(len(telegram.calls), 1)
        first["text"] = "Free sample pack released by the creator"
        self.scan(telegram, refresh=True)
        attachment = next(row for row in self.state.rows("assets") if row["kind"] == "telegram")
        self.assertEqual(attachment["eligible"], 1)
        self.assertEqual(attachment["text"], first["text"])
        self.assertEqual(attachment["status"], "queued")
        self.assertEqual(len(self.state.rows("assets")), 3)
        with self.state.db:
            self.state.db.execute("UPDATE assets SET status='downloaded',path='saved.zip',sha256='verified' WHERE id=?", (attachment["id"],))
        first["text"] = "Premium sample pack again"
        self.scan(telegram, refresh=True)
        saved = self.state.db.execute("SELECT * FROM assets WHERE id=?", (attachment["id"],)).fetchone()
        self.assertEqual(saved["status"], "downloaded")
        self.assertEqual(saved["sha256"], "verified")

    def test_only_zero_price_music_offers_are_auto_eligible(self):
        self.add_channel()
        captions = [
            "Free sample pack — CC0", "Royalty-free sample pack, $25",
            "Cracked VST free download", "Free VST trial", "This is not a free sound pack",
            "Ad-free sample pack, $29", "Sample pack with free shipping", "Free lunch with a purchase",
        ]
        telegram = FixtureTelegram({"music_freebies": [message(index, caption, "sample-pack.zip") for index, caption in enumerate(captions, 1)]})
        self.scan(telegram)
        eligible = [row["text"] for row in self.state.rows("assets") if row["eligible"]]
        self.assertEqual(eligible, [captions[0]])

    def test_download_budgets_status_manifest_and_rerun_dedup(self):
        self.asset("https://files.example/paid.zip", eligible=False)
        first = self.asset("https://files.example/first.zip")
        second = self.asset("https://files.example/second.zip")
        destination = self.directory / "downloads"
        with patch("packfinder.downloads.download_url", side_effect=self.fake_download) as download:
            asyncio.run(download_assets(self.state, destination, max_total_bytes=len(ZIP_BYTES), log=lambda _: None))
            self.assertEqual(download.call_count, 1)
            statuses = {row["id"]: row["status"] for row in self.state.rows("assets")}
            self.assertEqual(statuses[first], "downloaded")
            self.assertEqual(statuses[second], "queued")
            self.reopen()
            asyncio.run(download_assets(self.state, destination, log=lambda _: None))
            self.assertEqual(download.call_count, 2)
        self.assertEqual(self.state.stats()["downloaded"], 2)
        manifest = [json.loads(line) for line in (destination / "manifest.jsonl").read_text().splitlines()]
        self.assertEqual({row["asset_id"] for row in manifest}, {first, second})
        self.assertEqual({row["source"] for row in manifest}, {"https://files.example/first.zip", "https://files.example/second.zip"})
        self.assertTrue(all(row["sha256"] == hashlib.sha256(ZIP_BYTES).hexdigest() for row in manifest))

    def test_failed_download_requires_explicit_retry_and_then_clears_error(self):
        key = self.asset("https://files.example/retry.zip")
        destination = self.directory / "downloads"
        with patch("packfinder.downloads.download_url", side_effect=DownloadError("temporary failure")) as download:
            asyncio.run(download_assets(self.state, destination, log=lambda _: None))
            asyncio.run(download_assets(self.state, destination, log=lambda _: None))
            self.assertEqual(download.call_count, 1)
        row = self.state.db.execute("SELECT * FROM assets WHERE id=?", (key,)).fetchone()
        self.assertEqual(row["status"], "error")
        self.assertIn("temporary failure", row["error"])
        with patch("packfinder.downloads.download_url", side_effect=self.fake_download):
            asyncio.run(download_assets(self.state, destination, retry_errors=True, log=lambda _: None))
        row = self.state.db.execute("SELECT * FROM assets WHERE id=?", (key,)).fetchone()
        self.assertEqual(row["status"], "downloaded")
        self.assertIsNone(row["error"])

    def test_manifest_failure_does_not_redownload_a_completed_file(self):
        key = self.asset("https://files.example/completed.zip")
        destination = self.directory / "downloads"
        (destination / "manifest.jsonl").mkdir(parents=True)
        with patch("packfinder.downloads.download_url", side_effect=self.fake_download) as download:
            asyncio.run(download_assets(self.state, destination, log=lambda _: None))
            asyncio.run(download_assets(self.state, destination, retry_errors=True, log=lambda _: None))
        self.assertEqual(download.call_count, 1)
        row = self.state.db.execute("SELECT * FROM assets WHERE id=?", (key,)).fetchone()
        self.assertEqual(row["status"], "downloaded")
        self.assertIsNone(row["error"])
        self.assertEqual(Path(row["path"]).read_bytes(), ZIP_BYTES)
        self.assertEqual(row["sha256"], hashlib.sha256(ZIP_BYTES).hexdigest())

    def test_demo_cli_is_fully_offline_and_repeatable(self):
        database = self.directory / "demo.sqlite3"
        report = self.directory / "demo-report"
        args = ["demo", "--db", str(database), "--out", str(report)]
        with patch("socket.create_connection", side_effect=AssertionError("demo attempted network")), \
             patch("packfinder.downloads.download_url", side_effect=AssertionError("demo attempted download")) as download, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(cli.main(args), 0)
        download.assert_not_called()
        results = json.loads((report / "results.json").read_text())
        self.assertEqual(results["stats"]["profiles"], 3)
        self.assertEqual(results["stats"]["channels"], 2)
        self.assertEqual(results["stats"]["assets"], 4)
        self.assertEqual(results["stats"]["free_candidates"], 2)
        self.assertEqual(results["stats"]["downloaded"], 0)
        self.assertTrue((report / "index.html").is_file())
        self.assertFalse((report / "manifest.jsonl").exists())

    def test_report_escapes_untrusted_html_links_and_spreadsheet_formulas(self):
        profile = {"id": "unsafe", "username": '<img src=x onerror="alert(1)">', "permalink_url": "javascript:alert(1)"}
        self.state.add_profile(profile, 0)
        for index, filename in enumerate(('=HYPERLINK("https://evil.example").zip', '\t=1+1.zip'), 1):
            self.state.add_asset(kind="http", channel="music_freebies", message_id=index, filename=filename,
                text="<script>alert(1)</script>", eligible=False, reason='<script>alert(2)</script>', url=f"javascript:alert({index})")
        report = self.directory / "safe-report"
        export_report(self.state, report)
        html = (report / "index.html").read_text()
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Content-Security-Policy", html)
        with (report / "assets.csv").open(newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertTrue(all(row["filename"].startswith("'") for row in rows))
        exported = json.loads((report / "results.json").read_text())
        self.assertEqual(len(exported["assets"]), 2)


if __name__ == "__main__":
    unittest.main()
