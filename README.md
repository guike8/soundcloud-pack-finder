# SoundCloud Pack Finder

Start at a SoundCloud profile, traverse followers and their followers, collect public Telegram links from bios and profile links, then find and download music-production freebies.

**The default pipeline works anonymously, with no registered apps, API keys, account cookies, or login.** SoundCloud uses the public website's current client configuration. Telegram uses public `t.me/s/` channel pages and downloads openly exposed files and external links. A local Telegram account session is an optional mode for attachments whose bytes aren't exposed on public pages.

## Install

Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
packfinder doctor
```

On macOS, `./run.command doctor` also creates the local virtual environment. All commands below can be passed to `./run.command` instead of `packfinder`.

## Start with the SoundCloud graph

The default seed is `https://soundcloud.com/dollsy_blackheart`.

```sh
packfinder crawl --depth 2 --max-profiles 200 --followers 30
packfinder report --out reports/latest
```

Open `reports/latest/index.html`. JSON and CSV exports are beside it. `--depth 0` reads only the starting profile, `1` includes its followers, and `2` includes followers of those followers. Traversal is breadth-first and bounded by the profile count and the followers-per-profile cap. This is a sample of the graph, not a promise to enumerate every follower.

Progress, graph edges, sources, channel status, and file hashes live in `data/crawl.sqlite3`. Repeating the command resumes it. You can increase the depth, profile limit, or per-profile follower limit to widen a crawl. Use `--retry-errors` to retry failed profiles. Use a different `--db data/another.sqlite3` for a different starting profile.

## Scan public Telegram pages — no login

The default `--telegram-mode web` reads [public channel previews](https://telegram.org/tour/channels). It paginates the available posts, extracts captions and links, and checks for music freebies. It does not ask for an API ID, API hash, phone number, or login code. Links with no public post history are recorded as skipped.

```sh
packfinder scan-telegram --messages 1000 --max-channels 50
packfinder queue
packfinder download --max-files 20 --max-file-mb 512 --max-total-mb 1024
packfinder report
```

Or run the full pipeline:

```sh
packfinder run https://soundcloud.com/dollsy_blackheart --depth 2 --max-profiles 200 --followers 30 --download
```

`--download` automatically saves direct files from posts explicitly advertising a free music asset. It looks for VSTs, presets, drum kits, loops, samples, and sound packs; skips trial, cracked-copy, and nonzero-price wording; and queues ambiguous offers and landing pages for review. “Royalty-free” alone does not mean a zero price. Caption matching is a discovery signal; the source's supplied license determines use of each asset.

If you confirm an ambiguous offer is free, record why:

```sh
packfinder approve ASSET_ID --reason 'Creator offers this pack free under the included license'
packfinder download
```

Public mode fetches direct HTTPS files, including an attachment URL if its bytes are actually exposed. A Telegram document card often links back to its post instead of a downloadable file; those attachments stay in the queue with a clear reason and source post link. No API setup is triggered automatically. Vendor landing pages, shortened links, Google Drive/MEGA pages, and shopping/checkout flows remain links in the report. Archives, audio, preset files, and conventional VST installers can be saved. Nothing is opened, installed, run, or extracted. Files get size limits, format checks, SHA-256 hashes, and non-executable permissions. HTTP redirects and destinations are checked, and existing files are never overwritten.

Re-scan recent channel posts with `scan-telegram --refresh-channels`. Repeated messages and downloads are deduplicated. Reads are limited to `--messages` and the public history the website exposes. Refresh can miss older posts outside that window. This unofficial HTML interface can change independently of the tool.

## Optional: Telegram account mode

If an attachment has no public file URL, its source post can still be opened in the normal Telegram app or web client and downloaded there, without registering an API application. For automated headless access, you can explicitly opt into a locally authenticated user session:

```sh
python -m pip install -e '.[telegram]'
packfinder telegram-setup
packfinder scan-telegram --telegram-mode account --refresh-channels --messages 1000
packfinder download --telegram-mode account
```

This optional setup needs your own `api_id` and `api_hash` from [Telegram API development tools](https://my.telegram.org/apps). The interactive helper prompts locally for those credentials and the phone/code/2FA login. On macOS, `connect-telegram.command` runs this setup. Secret inputs are hidden. App credentials are stored in `data/telegram.credentials.json` with owner-only permissions, and future account-mode commands load them automatically. Codes and passwords are never saved. The session stays under `data/` and is excluded from Git.

You can instead supply `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in your environment and run `packfinder telegram-login`. No channels are joined and no messages are sent. Personal accounts, private invites, and protected files are skipped. `--telegram-mode account` must be selected explicitly; normal scans/downloads stay anonymous.

## Verify

```sh
python -m unittest discover -s tests -v
packfinder demo
packfinder doctor --live
```

The demo uses fictional profiles and messages, performs no network requests, and never downloads anything. `doctor --live` resolves the configured real SoundCloud seed and reads one follower. Telegram integration tests use injected clients or HTML fixtures and never log into your account. Public Telegram scans can run live without any credentials.

The public-site SoundCloud adapter is based on observed website behavior and may need updating when the site changes. A rate limit or access denial stops that request with a saved error; the tool does not rotate proxies or bypass challenges. See [the implementation notes](REVERSE_ENGINEERING.md).

## Files and privacy

The public repository contains source, tests, fictional fixtures, and documentation. Local crawls, bios, graph exports, downloads, API credentials, and Telegram sessions are excluded from Git. Reports contain the public profile/channel data you collect, so share those deliberately.

MIT license covers this tool. Third-party plugins, packs, and samples retain their own licenses.
