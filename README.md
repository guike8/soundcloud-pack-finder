# SoundCloud Pack Finder

Start at a SoundCloud profile, traverse followers and their followers, collect public Telegram links from bios and profile links, then find and download music-production freebies.

**SoundCloud works anonymously. No registered app, API token, account cookies, or login is required.** It reads the public site's hydration data and uses the same anonymous website requests with a dynamically discovered public client identifier. Telegram scanning uses your own locally authenticated Telegram session.

## Install

Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[telegram]'
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

## Connect Telegram once

Obtain your own `api_id` and `api_hash` from [Telegram API development tools](https://my.telegram.org/apps). Keep them in your local environment; do not commit them or paste them into a public issue.

```sh
export TELEGRAM_API_ID='your_numeric_id'
export TELEGRAM_API_HASH='your_api_hash'
packfinder telegram-login
```

Login asks for your phone number, Telegram login code, and two-step password if enabled. Credentials are not sent to SoundCloud. The local Telegram session is stored under `data/` and excluded from Git. No channels are joined and no messages are sent. Public broadcast-channel histories are read; personal accounts, private invites, and protected files are skipped.

```sh
packfinder scan-telegram --messages 100 --max-channels 50
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

Only direct HTTPS downloads and Telegram attachments are fetched. Vendor landing pages, shortened links, Google Drive/MEGA pages, and shopping/checkout flows remain links in the report; the tool does not pretend those pages are download files. Archives, audio, preset files, and conventional VST installers can be saved. Nothing is opened, installed, run, or extracted. Files get size limits, format checks, SHA-256 hashes, and non-executable permissions. HTTP redirects and destinations are checked, and existing files are never overwritten.

Re-scan recent channel posts with `scan-telegram --refresh-channels`. Repeated messages and downloads are deduplicated. Telegram history reads are limited to the most recent `--messages` posts, not the channel's entire history. Refresh can miss older posts outside that window.

## Verify

```sh
python -m unittest discover -s tests -v
packfinder demo
packfinder doctor --live
```

The demo uses fictional profiles and messages, performs no network requests, and never downloads anything. `doctor --live` resolves the configured real SoundCloud seed and reads one follower. Telegram integration tests use injected clients and do not log into your account. A complete live Telegram run requires your local credentials and login.

The public-site SoundCloud adapter is based on observed website behavior and may need updating when the site changes. A rate limit or access denial stops that request with a saved error; the tool does not rotate proxies or bypass challenges. See [the implementation notes](REVERSE_ENGINEERING.md).

## Files and privacy

The public repository contains source, tests, fictional fixtures, and documentation. Local crawls, bios, graph exports, downloads, API credentials, and Telegram sessions are excluded from Git. Reports contain the public profile/channel data you collect, so share those deliberately.

MIT license covers this tool. Third-party plugins, packs, and samples retain their own licenses.
