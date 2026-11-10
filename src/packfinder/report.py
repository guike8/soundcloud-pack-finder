"""Portable JSON/CSV and an escaped, offline HTML crawl report."""

import csv
from html import escape
import json
from pathlib import Path
from urllib.parse import urlsplit

from .state import State


def _link(url, label=None):
    url = str(url or "")
    label = escape(str(label if label is not None else url))
    try:
        if urlsplit(url).scheme not in {"https","http"}:
            return label
    except ValueError:
        return label
    return f'<a href="{escape(url,quote=True)}" rel="noreferrer noopener">{label}</a>'


def _asset_source(asset):
    return asset["url"] or f"https://t.me/{asset['channel']}/{asset['message_id']}"


def export_report(state: State, directory: Path) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    data = {"stats":state.stats(), **{table:state.rows(table) for table in ("profiles","edges","channels","channel_sources","assets")}}
    for p in data["profiles"]:
        p["data"] = json.loads(p["data"])
    (directory/"results.json").write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
    for name in ("channels","assets"):
        rows = data[name]
        columns = list(rows[0]) if rows else (["username","url"] if name=="channels" else ["id","kind","url","channel","filename","eligible","status"])
        with (directory/f"{name}.csv").open("w",newline="",encoding="utf-8") as f:
            writer = csv.DictWriter(f,fieldnames=columns)
            writer.writeheader()
            writer.writerows({k:("'"+v if isinstance(v,str) and (v.lstrip()[:1] in {"=","+","-","@"} or v[:1] in {"\t","\r","\n"}) else v) for k,v in row.items()} for row in rows)
    cards = "".join(f'<div class="card"><b>{value}</b><span>{escape(key.replace("_"," "))}</span></div>' for key,value in data["stats"].items())
    channel_rows = "".join(f'<tr><td>{_link(c["url"],"@"+c["username"])}</td><td>{"read" if c["scanned"] else "pending"}</td><td>{escape(c["error"] or "")}</td></tr>' for c in data["channels"])
    asset_rows = "".join(f'<tr><td>{escape(a["filename"])}</td><td>{_link(_asset_source(a),"source")}</td><td>{escape(a["status"])}</td><td>{escape(a["reason"])}</td><td>{escape(a["error"] or "")}</td></tr>' for a in data["assets"])
    profile_rows = "".join(f'<tr><td>{_link(p["data"].get("permalink_url"),p["data"].get("username",p["id"]))}</td><td>{p["depth"]}</td><td>{escape(p["error"] or "")}</td></tr>' for p in data["profiles"])
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'"><title>SoundCloud Pack Finder — results</title>
    <style>body{{background:#10161d;color:#e9edf2;font:15px system-ui;max-width:1200px;margin:40px auto;padding:0 24px}}h1{{font-size:34px}}h2{{margin-top:36px}}a{{color:#9bd9cd}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#1c2732;padding:18px;min-width:110px;border-radius:12px}}b{{display:block;font-size:26px}}span{{color:#a7b6c4}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px 8px;text-align:left;border-bottom:1px solid #30404f;overflow-wrap:anywhere}}p{{color:#a7b6c4}}.scroll{{overflow-x:auto}}</style>
    <h1>SoundCloud Pack Finder</h1><p>Local crawl report. “Free candidate” means the source caption advertises a free music asset. Files are saved without opening, installing, or extracting them.</p><div class="cards">{cards}</div>
    <h2>Download queue</h2><div class="scroll"><table><tr><th>File / page</th><th>Link</th><th>Status</th><th>Selection reason</th><th>Error</th></tr>{asset_rows or '<tr><td colspan="5">No assets collected yet. Scan the discovered Telegram channels to populate this queue.</td></tr>'}</table></div>
    <h2>Telegram links</h2><table><tr><th>Public channel</th><th>Status</th><th>Error</th></tr>{channel_rows}</table>
    <h2>SoundCloud graph</h2><table><tr><th>Profile</th><th>Depth</th><th>Error</th></tr>{profile_rows}</table></html>'''
    (directory/"index.html").write_text(html,encoding="utf-8")
    return {"html":str(directory/"index.html"),"json":str(directory/"results.json"),"channels_csv":str(directory/"channels.csv"),"assets_csv":str(directory/"assets.csv")}
