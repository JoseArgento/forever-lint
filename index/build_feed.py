#!/usr/bin/env python3
"""The "now works on Forever" feed: compares this week's index with last week's and writes an RSS feed of the
addons that got better (and a plain-text list for the optional Telegram step).

  python index/build_feed.py --new site/data.json --old prev/data.json --old-feed prev/feed.xml \
      --out site/feed.xml --text site/feed-new.txt --site https://joseargento.github.io/forever-lint/

Without --old (the first run) nothing is "new": the feed starts empty. The feed keeps the last 50 items.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

RANK = {"no-load": 0, "errors": 1, "warnings": 2, "clean": 3}
WORDS = {
    "no-load": "does not load",
    "errors": "loads, with errors",
    "warnings": "works, with warnings",
    "clean": "works, no problems found",
}
KEEP = 50


def changes(old: dict | None, new: dict) -> list[dict]:
    """Addons whose status improved since the last index, best news first."""
    if not old:
        return []
    before = {a["slug"]: a for a in old.get("addons", [])}
    out = []
    for a in new.get("addons", []):
        prev = before.get(a["slug"])
        if not prev or a["status"] not in RANK or prev["status"] not in RANK:
            continue
        if RANK[a["status"]] > RANK[prev["status"]]:
            out.append({"slug": a["slug"], "name": a["name"], "repo": a.get("repo"), "from": prev["status"], "to": a["status"]})
    out.sort(key=lambda c: (-RANK[c["to"]], c["name"].lower()))
    return out


def title(c: dict) -> str:
    if c["to"] == "clean":
        return f"{c['name']} now works on WoW Forever"
    return f"{c['name']}: {WORDS[c['from']]} → {WORDS[c['to']]}"


def old_items(path: Path | None) -> list[ET.Element]:
    if not path or not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    return root.findall("./channel/item")


def build(new: dict, old: dict | None, old_feed: Path | None, site: str, now: datetime) -> tuple[str, list[str]]:
    news = changes(old, new)
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Now works on WoW Forever"
    ET.SubElement(ch, "link").text = site
    ET.SubElement(ch, "description").text = "Addons of the Forever compatibility index that got better since last week (forever-lint)."
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now)
    lines = []
    week = now.strftime("%Y-%m-%d")
    for c in news:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = title(c)
        ET.SubElement(it, "link").text = f"{site}#{c['slug']}"
        ET.SubElement(it, "guid", isPermaLink="false").text = f"{c['slug']}-{c['to']}-{week}"
        ET.SubElement(it, "pubDate").text = format_datetime(now)
        ET.SubElement(it, "description").text = (
            f'{c["name"]} went from "{WORDS[c["from"]]}" to "{WORDS[c["to"]]}" in this week\'s index.'
            + (f" Source: https://github.com/{c['repo']}" if c.get("repo") else "")
        )
        lines.append(f"{title(c)}: {site}#{c['slug']}")
    seen = {c["slug"] + "-" + c["to"] + "-" + week for c in news}
    for item in old_items(old_feed)[: max(0, KEEP - len(news))]:
        guid = item.findtext("guid")
        if guid not in seen:
            ch.append(item)
    ET.indent(rss)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n", lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--new", type=Path, required=True)
    ap.add_argument("--old", type=Path)
    ap.add_argument("--old-feed", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--text", type=Path, help="one line per news item, for the Telegram step")
    ap.add_argument("--site", default="https://joseargento.github.io/forever-lint/")
    args = ap.parse_args()
    new = json.loads(args.new.read_text(encoding="utf-8"))
    old = None
    if args.old and args.old.is_file():
        try:
            old = json.loads(args.old.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = None
    xml, lines = build(new, old, args.old_feed, args.site, datetime.now(timezone.utc))
    args.out.write_text(xml, encoding="utf-8")
    if args.text:
        args.text.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"{len(lines)} new items, wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
