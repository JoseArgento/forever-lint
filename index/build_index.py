#!/usr/bin/env python3
"""Builds the WoW Forever compatibility index: runs forever-lint over public addon repos and writes a static site.

    python index/build_index.py --cache .index-cache --out site

The site shows, per addon: which TOC Forever would load, how many problems the
static analysis finds, the most used missing APIs with their replacements, and
every finding as file:line + message. It never copies the addons' code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forever_lint import __version__, cli  # noqa: E402
from forever_lint import db as dbmod  # noqa: E402

MAX_FINDINGS = 300


def slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "credential.helper=", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=600
    ).stdout


def fetch(repo: str, cache: Path, offline: bool) -> Path:
    dest = cache / repo.replace("/", "__")
    if dest.exists():
        if not offline:
            git("fetch", "-q", "--depth", "1", "origin", "HEAD", cwd=dest)
            git("reset", "-q", "--hard", "FETCH_HEAD", cwd=dest)
        return dest
    if offline:
        raise FileNotFoundError(f"{repo} is not in the cache and --offline was given")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--depth", "1", f"https://github.com/{repo}.git", str(dest))
    return dest


def status_of(primary: dict, counts: Counter) -> str:
    if any(p["rule"] in ("FL008", "FL010") for p in primary["problems"]):
        return "no-load"
    if counts.get("error"):
        return "errors"
    if counts.get("warning"):
        return "warnings"
    return "clean"


def summarize(entry: dict, path: Path, report: dict) -> dict:
    addons = report["addons"]
    # The main addon is the one with the most Lua files that Forever would load.
    primary = max(addons, key=lambda a: (a["toc"] is not None, a["lua_files"])) if addons else None
    own = [f for f in report["findings"] if not f["library"]]
    libs = [f for f in report["findings"] if f["library"]]
    # Secondary modules that only exist for other clients (BigWigs_Classic, a _Vanilla-only plugin) are listed,
    # not counted: not loading in Forever is what their authors intended.
    not_loading = [a["name"] for a in addons if a is not primary and any(p["rule"] in ("FL008", "FL010") for p in a["problems"])]

    def counted(a: dict, p: dict) -> bool:
        return a is primary or p["rule"] not in ("FL008", "FL010")

    counts = Counter(f["severity"] for f in own)
    for a in addons:
        for p in a["problems"]:
            if counted(a, p):
                counts[p["severity"]] += 1
    missing = Counter(f["name"] for f in own if f["rule"] == "FL001" and f["severity"] == "error")
    use = {}
    for f in own:
        if f["rule"] == "FL001" and f.get("extra", {}).get("use"):
            use.setdefault(f["name"], f["extra"]["use"][:2])
    toc = primary["toc"] if primary else None
    head = git("log", "-1", "--format=%H%n%cI", cwd=path).split()
    return {
        "name": entry["name"],
        "slug": slug(entry["name"]),
        "repo": entry["repo"],
        "commit": head[0],
        "commit_date": head[1],
        "status": status_of(primary, counts) if primary else "no-load",
        "toc": toc,
        "forever_toc": bool(toc and any(toc.lower().endswith(s) for s in ("_camelot.toc", "_forever.toc"))),
        "modules": len(addons),
        "lua_files": sum(a["lua_files"] for a in addons),
        "counts": {s: counts.get(s, 0) for s in ("error", "warning", "note")},
        "library_findings": len(libs),
        "top_missing": [{"name": n, "count": c, "use": use.get(n, [])} for n, c in missing.most_common(8)],
        "problems": [{"module": a["name"], "toc": a["toc"], **p} for a in addons for p in a["problems"] if counted(a, p)],
        "modules_not_loading": not_loading,
        "findings": [
            {k: v for k, v in f.items() if k in ("rule", "severity", "file", "line", "name", "message")}
            for f in sorted(own, key=lambda f: ({"error": 0, "warning": 1, "note": 2}[f["severity"]], f["file"], f["line"]))[:MAX_FINDINGS]
        ],
        "findings_total": len(own),
    }


BADGES = {
    "clean": ("ready", "brightgreen"),
    "warnings": ("warnings", "yellow"),
    "errors": ("errors", "orange"),
    "no-load": ("won't load", "red"),
}


def badge(result: dict) -> dict:
    """A shields.io endpoint badge (https://shields.io/badges/endpoint-badge) for one addon's status."""
    message, color = BADGES[result["status"]]
    c = result["counts"]
    if result["status"] in ("errors", "warnings"):
        n = c["error"] if result["status"] == "errors" else c["warning"]
        message = f"{n} {message}" if n else message
    return {"schemaVersion": 1, "label": "WoW Forever", "message": message, "color": color}


def write_badges(results: list[dict], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for r in results:
        (folder / f"{r['slug']}.json").write_text(json.dumps(badge(r)) + "\n", encoding="utf-8")


def lint(path: Path) -> dict:
    """Runs forever-lint from inside the repo so every path in the report is relative to it."""
    args = cli.build_parser().parse_args([".", "--format", "json", "--fail-on", "never"])
    here = os.getcwd()
    try:
        os.chdir(path)
        out, _ = cli.run(args)
    finally:
        os.chdir(here)
    return json.loads(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", type=Path, default=ROOT / "index" / "addons.json")
    ap.add_argument("--cache", type=Path, default=ROOT / ".index-cache")
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--offline", action="store_true", help="use the cache only, never touch the network")
    ap.add_argument("--only", default="", help="comma-separated addon names to (re)check")
    args = ap.parse_args()

    entries = json.loads(args.list.read_text(encoding="utf-8"))["addons"]
    only = {o.strip().lower() for o in args.only.split(",") if o.strip()}
    db = dbmod.load()
    results, failures = [], []
    for entry in entries:
        if only and entry["name"].lower() not in only:
            continue
        try:
            path = fetch(entry["repo"], args.cache, args.offline)
            results.append(summarize(entry, path, lint(path)))
            r = results[-1]
            print(f"{r['status']:9} {r['name']:28} {r['counts']}", file=sys.stderr)
        except Exception as e:  # one broken repo must not stop the index
            failures.append({"name": entry["name"], "repo": entry["repo"], "error": str(e).splitlines()[0][:200]})
            print(f"FAILED    {entry['name']:28} {failures[-1]['error']}", file=sys.stderr)

    order = {"clean": 0, "warnings": 1, "errors": 2, "no-load": 3}
    results.sort(key=lambda r: (order[r["status"]], r["name"].lower()))
    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "linter": __version__,
        "target": {"version": db.version, "build": db.build, "interface": db.interface, "ui_source": db.ui_source},
        "addons": results,
        "failures": failures,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_badges(results, args.out / "badge")
    page = (ROOT / "index" / "template.html").read_text(encoding="utf-8")
    page = page.replace("/*__DATA__*/", "var INDEX_DATA = " + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";")
    (args.out / "index.html").write_text(page, encoding="utf-8")
    print(f"wrote {args.out / 'index.html'} ({len(results)} addons, {len(failures)} failed)", file=sys.stderr)
    return 1 if failures and not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
