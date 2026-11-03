"""Output formats: text for people, JSON for scripts, SARIF for GitHub code scanning."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from . import __version__
from .analyzer import ERROR, NOTE, WARNING, Finding
from .db import ApiDb

REPO = "https://github.com/JoseArgento/forever-lint"

RULES = {
    "FL001": ("removed-api", "API that does not exist in Forever", ERROR),
    "FL002": ("deprecated-api", "API that only exists as a deprecation fallback", WARNING),
    "FL003": ("secret-value", "Arithmetic or comparison on a value that can be secret", WARNING),
    "FL004": ("project-id", "WOW_PROJECT_ID check that Forever (18, WOW_PROJECT_CAMELOT) does not expect", WARNING),
    "FL005": ("removed-event", "Event that does not exist in Forever", ERROR),
    "FL006": ("restricted-event", "Event that is restricted or has secret payloads", WARNING),
    "FL007": ("build-version", "Interface version check that misreads Forever (16001)", WARNING),
    "FL008": ("no-forever-toc", "No TOC file that Forever loads", ERROR),
    "FL009": ("toc-interface", "TOC Interface does not include 16001", WARNING),
    "FL010": ("toc-game-type", "TOC excludes the Forever game type", ERROR),
    "FL011": ("chat-hardware-event", "SAY/YELL/EMOTE sent without a hardware event", NOTE),
    "FL012": ("trial-addon-messages", "Addon messages fail on trial accounts", NOTE),
}

SEVERITY_ORDER = {ERROR: 0, WARNING: 1, NOTE: 2}


def _rel(path: str, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(base).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _color(enabled: bool):
    codes = {"error": "31;1", "warning": "33", "note": "36", "dim": "2", "bold": "1", "ok": "32"}

    def paint(text: str, what: str) -> str:
        return f"\033[{codes[what]}m{text}\033[0m" if enabled else text

    return paint


def text(findings: list[Finding], addons, db: ApiDb, base: Path, show_libs: bool, color: bool | None = None) -> str:
    if color is None:
        color = os.isatty(1) and os.environ.get("NO_COLOR") is None
    c = _color(color)
    lines = [c(f"forever-lint {__version__}", "bold") + c(f" · {db.label}", "dim"), ""]
    main = [f for f in findings if not f.library]
    libs = [f for f in findings if f.library]
    for addon in addons:
        toc = addon.toc.path.name if addon.toc else "no TOC for Forever"
        lines.append(
            c(f"{addon.name}", "bold") + c(f"  ({_rel(str(addon.root), base) or '.'}, {toc}, {len(addon.lua_files)} Lua files)", "dim")
        )
        for rule, sev, msg in addon.problems:
            lines.append(f"  {c(sev, sev)} {rule} {msg}")
        if addon.missing_files:
            hint = (
                " (probably fetched by the packager from .pkgmeta)"
                if (addon.root / ".pkgmeta").exists() or any((p / ".pkgmeta").exists() for p in addon.root.parents)
                else ""
            )
            lines.append(
                c(
                    f"  {len(addon.missing_files)} file(s) listed in the TOC/XML were not found{hint}: {', '.join(addon.missing_files[:4])}",
                    "dim",
                )
            )
    lines.append("")
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in main + (libs if show_libs else []):
        by_file[f.file].append(f)
    for file in sorted(by_file):
        lines.append(c(_rel(file, base), "bold"))
        for f in sorted(by_file[file], key=lambda x: (x.line, x.col)):
            lines.append(f"  {f.line}:{f.col}  {c(f.severity, f.severity)} {f.rule} {f.message}")
        lines.append("")
    if libs and not show_libs:
        per_lib = Counter(_lib_root(_rel(f.file, base)) for f in libs)
        lines.append(c(f"{len(libs)} finding(s) in embedded libraries (use --libs to list them):", "dim"))
        for lib, count in per_lib.most_common():
            lines.append(c(f"  {lib}: {count}", "dim"))
        lines.append("")

    top = Counter(f.name for f in main if f.rule == "FL001" and f.severity == ERROR)
    if top:
        lines.append(c("Most used missing APIs", "bold"))
        for name, count in top.most_common(10):
            use = next((f.extra.get("use") for f in main if f.name == name and f.extra.get("use")), None)
            hint = f"  → {', '.join(use[:2])}" if use else ""
            lines.append(f"  {count:4}  {name}{c(hint, 'dim')}")
        lines.append("")

    toc_problems = [p for a in addons for p in a.problems]
    counts = Counter(f.severity for f in main) + Counter(sev for _, sev, _ in toc_problems)
    if not counts:
        lines.append(c("No problems found for Forever.", "ok"))
    else:
        parts = [c(f"{counts[s]} {s}{'s' if counts[s] != 1 else ''}", s) for s in (ERROR, WARNING, NOTE) if counts[s]]
        lines.append(", ".join(parts) + c(f" in {len(addons)} addon(s)", "dim"))
    return "\n".join(lines) + "\n"


def _lib_root(rel: str) -> str:
    parts = Path(rel).parts
    for i, p in enumerate(parts):
        if p.lower() in ("libs", "lib", "libraries", "embeds", "embed", "externals", "external", "vendor"):
            return Path(*parts[: i + 2]).as_posix()
    return rel


def as_json(findings: list[Finding], addons, db: ApiDb, base: Path) -> str:
    return (
        json.dumps(
            {
                "tool": {"name": "forever-lint", "version": __version__},
                "target": {"version": db.version, "build": db.build, "interface": db.interface, "ui_source": db.ui_source},
                "addons": [
                    {
                        "name": a.name,
                        "path": _rel(str(a.root), base),
                        "toc": a.toc.path.name if a.toc else None,
                        "lua_files": len(a.lua_files),
                        "problems": [{"rule": r, "severity": s, "message": m} for r, s, m in a.problems],
                        "missing_files": a.missing_files,
                    }
                    for a in addons
                ],
                "findings": [
                    {
                        "rule": f.rule,
                        "severity": f.severity,
                        "file": _rel(f.file, base),
                        "line": f.line,
                        "column": f.col,
                        "name": f.name,
                        "message": f.message,
                        "library": f.library,
                        **({"extra": f.extra} if f.extra else {}),
                    }
                    for f in sorted(findings, key=lambda x: (x.file, x.line, x.col))
                ],
            },
            indent=2,
        )
        + "\n"
    )


def sarif(findings: list[Finding], addons, db: ApiDb, base: Path) -> str:
    rule_ids = list(RULES)
    results = []
    for f in findings:
        results.append(
            {
                "ruleId": f.rule,
                "ruleIndex": rule_ids.index(f.rule),
                "level": f.severity,
                "message": {"text": f.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": _rel(f.file, base), "uriBaseId": "SRCROOT"},
                            "region": {"startLine": f.line, "startColumn": f.col},
                        }
                    }
                ],
                "partialFingerprints": {"forever-lint/v1": f"{f.rule}:{_rel(f.file, base)}:{f.name}:{f.line}"},
                "properties": {"library": f.library},
            }
        )
    for a in addons:
        uri = _rel(str(a.toc.path if a.toc else (a.tocs[0] if a.tocs else a.root)), base)
        for rule, sev, msg in a.problems:
            results.append(
                {
                    "ruleId": rule,
                    "ruleIndex": rule_ids.index(rule),
                    "level": sev,
                    "message": {"text": msg},
                    "locations": [
                        {"physicalLocation": {"artifactLocation": {"uri": uri, "uriBaseId": "SRCROOT"}, "region": {"startLine": 1}}}
                    ],
                }
            )
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "forever-lint",
                        "version": __version__,
                        "informationUri": REPO,
                        "rules": [
                            {
                                "id": rid,
                                "name": name,
                                "shortDescription": {"text": short},
                                "helpUri": f"{REPO}#{rid.lower()}-{name}",
                                "defaultConfiguration": {"level": sev},
                            }
                            for rid, (name, short, sev) in RULES.items()
                        ],
                    }
                },
                "originalUriBaseIds": {"SRCROOT": {"uri": base.as_uri() + "/"}},
                "properties": {"target": db.label},
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2) + "\n"
