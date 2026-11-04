"""The "now works on Forever" feed built from two weeks of the index."""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_feed", ROOT / "index" / "build_feed.py")
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)

NOW = datetime(2026, 11, 9, 7, 41, tzinfo=timezone.utc)


def index(**statuses):
    return {"addons": [{"slug": k, "name": k.title(), "repo": f"x/{k}", "status": v} for k, v in statuses.items()]}


def test_only_improvements_are_news():
    old = index(bagnon="errors", dbm="warnings", weakauras="no-load", plater="errors")
    new = index(bagnon="clean", dbm="errors", weakauras="errors", plater="errors", newone="clean")
    got = feed.changes(old, new)
    assert [(c["slug"], c["from"], c["to"]) for c in got] == [("bagnon", "errors", "clean"), ("weakauras", "no-load", "errors")]


def test_first_run_has_no_news():
    assert feed.changes(None, index(bagnon="clean")) == []


def test_rss_is_valid_and_keeps_older_items(tmp_path):
    xml1, lines1 = feed.build(index(bagnon="clean"), index(bagnon="errors"), None, "https://x/", NOW)
    assert lines1 == ["Bagnon now works on WoW Forever: https://x/#bagnon"]
    p = tmp_path / "feed.xml"
    p.write_text(xml1, encoding="utf-8")
    later = datetime(2026, 11, 16, 7, 41, tzinfo=timezone.utc)
    xml2, lines2 = feed.build(index(bagnon="clean", dbm="clean"), index(bagnon="clean", dbm="warnings"), p, "https://x/", later)
    root = ET.fromstring(xml2)
    titles = [i.findtext("title") for i in root.findall("./channel/item")]
    assert titles == ["Dbm now works on WoW Forever", "Bagnon now works on WoW Forever"]
    assert lines2 == ["Dbm now works on WoW Forever: https://x/#dbm"]


def test_partial_progress_says_from_and_to():
    xml, _ = feed.build(index(wa="errors"), index(wa="no-load"), None, "https://x/", NOW)
    item = ET.fromstring(xml).find("./channel/item")
    assert item.findtext("title") == "Wa: does not load → loads, with errors"
    assert "github.com/x/wa" in item.findtext("description")


def test_a_broken_old_feed_is_ignored(tmp_path):
    p = tmp_path / "feed.xml"
    p.write_text("<rss><channel><item>", encoding="utf-8")
    xml, _ = feed.build(index(a="clean"), index(a="errors"), p, "https://x/", NOW)
    assert len(ET.fromstring(xml).findall("./channel/item")) == 1


def test_at_most_50_items(tmp_path):
    p = tmp_path / "feed.xml"
    for week in range(60):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(day=1 + week % 28, month=1 + week // 28)
        xml, _ = feed.build(index(**{f"a{week}": "clean"}), index(**{f"a{week}": "errors"}), p if p.exists() else None, "https://x/", when)
        p.write_text(xml, encoding="utf-8")
    assert len(ET.parse(p).getroot().findall("./channel/item")) == 50
