#!/usr/bin/env python3
"""Builds site/api.html and site/api.json: the Classic -> Forever API migration table.

Everything comes from forever-lint's bundled database (src/forever_lint/data/api.json):
functions that existed in a Classic client and are missing, moved or restricted in Forever,
the deprecation fallbacks that still work for now, secret-value functions and events.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forever_lint import __version__  # noqa: E402

CLASSIC = {"classic_era": "era", "classic_era:deprecated": "era", "classic_anniversary": "anniversary", "classic": "classic"}


def build(db: dict) -> dict:
    moved = []
    for name, e in sorted(db["removed"].items(), key=lambda kv: kv[0].lower()):
        seen = sorted({CLASSIC[i] for i in e["in"] if i in CLASSIC})
        if not seen:
            continue  # retail-only or history-only names: not part of a Classic port
        kind = "restricted" if e.get("restricted") else "ui" if e.get("ui") else "removed"
        row = {"n": name, "k": kind, "in": seen}
        if e.get("use"):
            row["use"] = e["use"][:3]
            if e.get("guess"):
                row["guess"] = 1
        if e.get("returns"):
            row["ret"] = e["returns"]
        if e.get("restricted"):
            row["to"] = e["restricted"]
        moved.append(row)
    fallbacks = [
        {"n": n, "to": d.get("to"), "file": d["file"], **({"ret": d["returns"]} if d.get("returns") else {})}
        for n, d in sorted(db["deprecated"].items(), key=lambda kv: kv[0].lower())
    ]
    secret = [
        {"n": n, "always": bool(s["always"]), "when": s["when"], "num": bool(s["numeric"])}
        for n, s in sorted(db["secret"].items(), key=lambda kv: kv[0].lower())
    ]
    events = [{"n": e, "k": "removed"} for e in db["events_removed"]] + [
        {"n": e, "k": "restricted", "why": why} for e, why in db["events_restricted"].items()
    ]
    events.sort(key=lambda e: e["n"])
    fv = db["forever"]
    return {
        "generated": db["generated"],
        "linter": __version__,
        "target": {
            "version": fv["version"],
            "build": fv["build"],
            "interface": fv["interface"],
            "project": fv["project"],
            "ui_source": fv.get("ui_source", ""),
        },
        "era": db.get("classic_era"),
        "moved": moved,
        "fallbacks": fallbacks,
        "secret": secret,
        "events": events,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    args = ap.parse_args()
    db = json.loads((ROOT / "src" / "forever_lint" / "data" / "api.json").read_text(encoding="utf-8"))
    data = build(db)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "api.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    page = (ROOT / "index" / "api_template.html").read_text(encoding="utf-8")
    page = page.replace(
        "/*__DATA__*/", "var API_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";"
    )
    (args.out / "api.html").write_text(page, encoding="utf-8")
    print(
        f"wrote {args.out / 'api.html'}: {len(data['moved'])} moved/removed, {len(data['fallbacks'])} fallbacks, "
        f"{len(data['secret'])} secret, {len(data['events'])} events",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
