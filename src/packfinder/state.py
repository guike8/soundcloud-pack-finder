"""Durable graph, crawl queue, provenance, and download status."""

import hashlib
import json
from pathlib import Path
import sqlite3


class State:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profiles (
          id TEXT PRIMARY KEY, depth INTEGER NOT NULL, data TEXT NOT NULL,
          scanned INTEGER NOT NULL DEFAULT 0, expanded INTEGER NOT NULL DEFAULT 0,error TEXT);
        CREATE TABLE IF NOT EXISTS edges (parent TEXT, child TEXT, PRIMARY KEY(parent,child));
        CREATE TABLE IF NOT EXISTS channels (
          username TEXT PRIMARY KEY,url TEXT NOT NULL,scanned INTEGER DEFAULT 0,error TEXT,skip_reason TEXT);
        CREATE TABLE IF NOT EXISTS channel_sources (
          username TEXT,profile_id TEXT,PRIMARY KEY(username,profile_id));
        CREATE TABLE IF NOT EXISTS messages (
          channel TEXT,message_id INTEGER,data TEXT NOT NULL,PRIMARY KEY(channel,message_id));
        CREATE TABLE IF NOT EXISTS assets (
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,url TEXT,channel TEXT,message_id INTEGER,
          filename TEXT NOT NULL,text TEXT NOT NULL,eligible INTEGER NOT NULL,reason TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',path TEXT,sha256 TEXT,bytes INTEGER,error TEXT);
        """)
        if "skip_reason" not in {r[1] for r in self.db.execute("PRAGMA table_info(channels)")}:
            with self.db:
                self.db.execute("ALTER TABLE channels ADD COLUMN skip_reason TEXT")

    def close(self):
        self.db.close()

    def seed(self, url: str):
        row = self.db.execute("SELECT value FROM meta WHERE key='seed'").fetchone()
        if row and row[0] != url:
            raise ValueError("This database belongs to another seed. Use a separate --db path.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('seed',?)", (url,))

    def add_profile(self, profile: dict, depth: int):
        with self.db:
            self.db.execute("""INSERT INTO profiles(id,depth,data) VALUES (?,?,?)
              ON CONFLICT(id) DO UPDATE SET depth=min(depth,excluded.depth)""",
              (str(profile["id"]), depth, json.dumps(profile, ensure_ascii=False)))

    def add_edge(self, parent: str, child: str):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO edges VALUES (?,?)", (parent,child))

    def finish_profile(self, profile: dict, channels: list[tuple[str,str]]):
        with self.db:
            self.db.execute("UPDATE profiles SET data=?,scanned=1,error=NULL WHERE id=?", (json.dumps(profile, ensure_ascii=False),str(profile["id"])))
            for name,url in channels:
                self.db.execute("INSERT OR IGNORE INTO channels(username,url) VALUES (?,?)", (name,url))
                self.db.execute("INSERT OR IGNORE INTO channel_sources VALUES (?,?)", (name,str(profile["id"])))

    def next_profile(self, depth: int):
        return self.db.execute("SELECT * FROM profiles WHERE error IS NULL AND depth<=? AND (scanned=0 OR (expanded=0 AND depth<?)) ORDER BY depth,rowid LIMIT 1", (depth,depth)).fetchone()

    def profile_error(self, user_id: str, error: str):
        with self.db:
            self.db.execute("UPDATE profiles SET error=? WHERE id=?", (error,user_id))

    def expanded(self, user_id: str, capped: bool = False):
        with self.db:
            self.db.execute("UPDATE profiles SET expanded=? WHERE id=?", (2 if capped else 1,user_id))

    def count_profiles(self):
        return self.db.execute("SELECT count(*) FROM profiles").fetchone()[0]

    def has_profile(self, user_id: str):
        return self.db.execute("SELECT 1 FROM profiles WHERE id=?", (user_id,)).fetchone() is not None

    def prepare_resume(self, retry_errors: bool = False, followers_limit: int | None = None):
        with self.db:
            self.db.execute("UPDATE profiles SET expanded=0 WHERE expanded=2")
            if followers_limit is not None:
                previous = self.db.execute("SELECT value FROM meta WHERE key='followers_limit'").fetchone()
                if previous and followers_limit > int(previous[0]):
                    self.db.execute("UPDATE profiles SET expanded=0 WHERE expanded=1")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('followers_limit',?)",(str(followers_limit),))
            if retry_errors:
                self.db.execute("UPDATE profiles SET error=NULL")
                self.db.execute("UPDATE channels SET error=NULL WHERE scanned=0")

    def add_message(self, channel: str, message: dict):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?)", (channel,message["message_id"],json.dumps(message,ensure_ascii=False)))

    def add_asset(self, *, kind: str, channel: str, message_id: int, filename: str, text: str, eligible: bool, reason: str, url: str = ""):
        identity = f"http:{url}" if kind == "http" else f"telegram:{channel}:{message_id}"
        key = hashlib.sha256(identity.encode()).hexdigest()[:16]
        with self.db:
            self.db.execute("""INSERT INTO assets(id,kind,url,channel,message_id,filename,text,eligible,reason)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            channel=excluded.channel,message_id=excluded.message_id,filename=excluded.filename,
            text=excluded.text,eligible=excluded.eligible,reason=excluded.reason,
            status=CASE WHEN assets.eligible=0 AND excluded.eligible=1 THEN 'queued' ELSE assets.status END,
            error=CASE WHEN assets.eligible=0 AND excluded.eligible=1 THEN NULL ELSE assets.error END
            WHERE assets.status!='downloaded' AND assets.reason NOT LIKE 'User-confirmed free offer:%'""",
            (key,kind,url,channel,message_id,filename,text,int(eligible),reason))
        return key

    def rows(self, table: str):
        if table not in {"profiles","edges","channels","channel_sources","messages","assets"}:
            raise ValueError("Unknown table")
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table}")]

    def stats(self):
        return {"profiles":self.count_profiles(), "channels":self.db.execute("SELECT count(*) FROM channels").fetchone()[0],
          "assets":self.db.execute("SELECT count(*) FROM assets").fetchone()[0],
          "free_candidates":self.db.execute("SELECT count(*) FROM assets WHERE eligible=1").fetchone()[0],
          "downloaded":self.db.execute("SELECT count(*) FROM assets WHERE status='downloaded'").fetchone()[0],
          "errors":sum(self.db.execute(f"SELECT count(*) FROM {t} WHERE error IS NOT NULL").fetchone()[0] for t in ("profiles","channels","assets"))}
