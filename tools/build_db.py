#!/usr/bin/env python3
"""Builds forever_lint/data/api.json from the game's own data.

Inputs (all public, see README "Where the data comes from"):

  --bir DIR        clone root holding Ketho/BlizzardInterfaceResources branches as
                   bir-forever/, bir-classic_era/, bir-classic/, bir-classic_anniversary/, bir-live/
                   (dumped from the running client with deprecation fallbacks off)
  --ui-forever DIR checkout of Gethe/wow-ui-source, branch "forever" (Interface/AddOns at least
                   Blizzard_Deprecated*, Deprecated_*, Blizzard_APIDocumentationGenerated)
  --ui-era DIR     same, branch "classic_era" (deprecation addons only)

Only names and flags go into the output. No Blizzard code is copied.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from forever_lint.lexer import COMMENT, tokenize  # noqa: E402
from forever_lint.luatable import parse_tables  # noqa: E402

FLAVORS = ["forever", "classic_era", "classic_anniversary", "classic", "live"]
DOTTED = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
ALIAS_RE = re.compile(rf"^\s*({DOTTED})\s*=\s*({DOTTED})\s*;?\s*$")
FUNC_ASSIGN_RE = re.compile(rf"^\s*({DOTTED})\s*=\s*function\b")
FUNC_DEF_RE = re.compile(rf"^\s*function\s+({DOTTED})\s*\(")
GUIDE_RE = re.compile(rf"^\s*({DOTTED})\s*\([^)]*\)\s*=\s*({DOTTED})\s*\(")


# --------------------------------------------------------------------------- dumps


def read_bir(root: Path, flavor: str) -> dict:
    res = root / f"bir-{flavor}" / "Resources"
    text = (res / "GlobalAPI.lua").read_text(encoding="utf-8")
    api_part, _, lua_part = text.partition("local LuaAPI")
    readme = (root / f"bir-{flavor}" / "README.md").read_text(encoding="utf-8")
    build = re.search(r'GetBuildInfo\(\) => "([^"]+)", "(\d+)", "[^"]*", (\d+)', readme)
    project = re.search(r"WOW_PROJECT_ID => (\w+) \((\d+)\)", readme)
    return {
        "api": set(re.findall(r'"([^"]+)"', api_part)),
        "lua": set(re.findall(r'"([^"]+)"', lua_part)),
        "framexml": set(re.findall(r'"([^"]+)"', (res / "FrameXML.lua").read_text(encoding="utf-8"))),
        "events": set(re.findall(r'"([A-Z0-9_]+)"', (res / "Events.lua").read_text(encoding="utf-8"))),
        "build": {"version": build.group(1), "build": int(build.group(2)), "interface": int(build.group(3))} if build else None,
        "project": {"name": project.group(1), "id": int(project.group(2))} if project else None,
    }


# --------------------------------------------------------------------------- shims


def strip_comments(source: str) -> str:
    chars = list(source)
    for tok in tokenize(source, keep_comments=True):
        if tok.kind != COMMENT:
            continue
        end = tok.pos + len(tok.value)
        for k in range(tok.pos, min(end, len(chars))):
            if chars[k] != "\n":
                chars[k] = " "
    return "".join(chars)


def toc_loads(toc: Path, game_types: set[str], placeholders: dict[str, str]) -> list[Path] | None:
    """Files a Blizzard TOC loads for the given game types, or None if the addon is excluded."""
    allow: set[str] | None = None
    exclude: set[str] = set()
    files: list[Path] = []
    for raw in toc.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("##"):
            key, _, value = line[2:].partition(":")
            values = {v.strip().lower() for v in value.split(",") if v.strip()}
            if key.strip() == "AllowLoadGameType":
                allow = (allow or set()) | values
            elif key.strip() == "ExcludeLoadGameType":
                exclude |= values
            continue
        if line.startswith("#"):
            continue
        m = re.match(r"^(.*?)\s*(?:\[(AllowLoadGameType|ExcludeLoadGameType)\s+([^\]]+)\])?\s*$", line)
        assert m
        path, kind, values = m.group(1), m.group(2), m.group(3)
        if kind:
            vals = {v.strip().lower() for v in values.split(",")}
            if kind == "AllowLoadGameType" and not (vals & game_types):
                continue
            if kind == "ExcludeLoadGameType" and vals & game_types:
                continue
        for key, sub in placeholders.items():
            path = path.replace(f"[{key}]", sub)
        files.append(toc.parent / path.replace("\\", "/"))
    if allow is not None and not (allow & game_types):
        return None
    if exclude & game_types:
        return None
    return [f for f in files if f.suffix == ".lua" and f.exists()]


def deprecation_files(addons: Path, game_types: set[str], placeholders: dict[str, str]) -> list[Path]:
    loaded: list[Path] = []
    for folder in sorted(addons.iterdir()):
        if (
            not folder.is_dir()
            or "eprecat" not in folder.name
            and not folder.name.startswith("Blizzard_Settings_Shared")
            and folder.name != "Blizzard_FrameXML"
        ):
            continue
        tocs = sorted(folder.glob("*.toc"))
        if tocs and "eprecat" in folder.name:
            files = toc_loads(tocs[0], game_types, placeholders)
            if files:
                loaded += files
        else:
            # FrameXML and Settings hold a couple of deprecation files among hundreds of others. Only those count,
            # whether the checkout is sparse (the workflow) or complete (a full clone has a TOC that loads everything,
            # and `OnAccept = function` fields of StaticPopup tables would be taken for shims).
            loaded += [f for f in folder.rglob("*.lua") if "eprecat" in f.name]
    return loaded


def parse_shims(files: list[Path]) -> tuple[dict[str, str | None], dict[str, str], dict[str, list[str]]]:
    """Returns ({name: alias_target_or_None_for_function}, {name: file}, {name: C_ calls in its body})."""
    shims: dict[str, str | None] = {}
    origin: dict[str, str] = {}
    calls: dict[str, list[str]] = {}
    for f in files:
        lines = strip_comments(f.read_text(encoding="utf-8-sig")).splitlines()
        for idx, line in enumerate(lines):
            for rx, has_target in ((FUNC_DEF_RE, False), (FUNC_ASSIGN_RE, False), (ALIAS_RE, True)):
                m = rx.match(line)
                if m:
                    name = m.group(1)
                    # API globals are PascalCase or C_Namespace.Function; lowercase names are locals of shim bodies.
                    if name.startswith("Enum.") or name.isupper() or not re.match(r"^(C_\w+\.\w+|[A-Z]\w*)$", name):
                        break
                    target = m.group(2) if has_target else None
                    if target and (target.startswith("Enum.") or target in ("nil", "true", "false")):
                        break
                    shims[name] = target
                    origin[name] = f.name
                    if not has_target:
                        indent = len(line) - len(line.lstrip())
                        body = []
                        for nxt in lines[idx + 1 : idx + 60]:
                            if nxt.strip().startswith("end") and len(nxt) - len(nxt.lstrip()) <= indent:
                                break
                            body.append(nxt)
                        calls[name] = list(dict.fromkeys(re.findall(r"\b((?:C_\w+|\w+Util)\.\w+)\s*\(", "\n".join(body))))
                    break
    return shims, origin, calls


def transition_guide(addons: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in addons.glob("Blizzard_Deprecated/*TransitionGuide*.lua"):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = GUIDE_RE.match(line)
            if m:
                out.setdefault(m.group(1), [])
                if m.group(2) not in out[m.group(1)]:
                    out[m.group(1)].append(m.group(2))
    return out


# --------------------------------------------------------------------------- secret values

SECRET_FUNCTION_KEYS = re.compile(r"^(SecretReturns|SecretWhen\w+|SecretInChatMessagingLockdown)$")


def struct_returns(doc_dir: Path) -> dict[str, str]:
    """Functions whose only return value is a structure (e.g. C_Spell.GetSpellInfo -> SpellInfo)."""
    docs = []
    structs: set[str] = set()
    for f in sorted(doc_dir.glob("*.lua")):
        try:
            tables = parse_tables(f.read_text(encoding="utf-8-sig"))
        except ValueError:
            continue
        for doc in tables.values():
            if isinstance(doc, dict) and doc.get("Type") == "System":
                docs.append(doc)
                structs |= {t["Name"] for t in doc.get("Tables") or [] if isinstance(t, dict) and t.get("Type") == "Structure"}
    out: dict[str, str] = {}
    for doc in docs:
        ns = doc.get("Namespace")
        for fn in doc.get("Functions") or []:
            returns = [r for r in (fn.get("Returns") or []) if isinstance(r, dict)] if isinstance(fn, dict) else []
            if len(returns) == 1 and returns[0].get("Type") in structs:
                out[f"{ns}.{fn['Name']}" if ns else fn["Name"]] = returns[0]["Type"]
    return out


def secret_info(doc_dir: Path) -> tuple[dict[str, dict], dict[str, list[str]]]:
    functions: dict[str, dict] = {}
    events: dict[str, list[str]] = {}
    for f in sorted(doc_dir.glob("*.lua")):
        try:
            tables = parse_tables(f.read_text(encoding="utf-8-sig"))
        except ValueError as e:  # pragma: no cover - report and keep going
            print(f"warning: {f.name}: {e}", file=sys.stderr)
            continue
        for doc in tables.values():
            if not isinstance(doc, dict) or doc.get("Type") != "System":
                continue
            ns = doc.get("Namespace")
            for fn in doc.get("Functions") or []:
                if not isinstance(fn, dict):
                    continue
                flags = sorted(k for k, v in fn.items() if v is True and SECRET_FUNCTION_KEYS.match(k))
                returns = [r for r in fn.get("Returns") or [] if isinstance(r, dict)]
                secret_returns = [
                    r for r in returns if not r.get("NeverSecret") and (flags or r.get("ConditionalSecret") or r.get("Secret"))
                ]
                if not secret_returns:
                    continue
                name = f"{ns}.{fn['Name']}" if ns else fn["Name"]
                functions[name] = {
                    "always": "SecretReturns" in flags,
                    "when": [k for k in flags if k != "SecretReturns"],
                    "numeric": any(
                        r.get("Type") in ("number", "uiUnit", "fileID", "BigInteger", "WOWMONEY", "time_t") for r in secret_returns
                    ),
                }
            for ev in doc.get("Events") or []:
                if isinstance(ev, dict) and ev.get("LiteralName"):
                    reasons = [k for k in ("HasRestrictions", "SecretPayloads") if ev.get(k) is True]
                    if reasons:
                        events[ev["LiteralName"]] = reasons
    return functions, events


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bir", type=Path, required=True)
    ap.add_argument("--ui-forever", type=Path, required=True)
    ap.add_argument("--ui-era", type=Path, required=True)
    ap.add_argument("--history", type=Path, help="file with every function name that ever appeared in the dumps")
    ap.add_argument("--curated", type=Path, default=Path(__file__).with_name("curated.json"))
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "src/forever_lint/data/api.json")
    ap.add_argument("--ui-forever-build", default="", help="label of the wow-ui-source commit, e.g. '1.60.1 (70755)'")
    args = ap.parse_args()

    dumps = {fl: read_bir(args.bir, fl) for fl in FLAVORS}
    fv = dumps["forever"]
    native = fv["api"] | fv["lua"] | fv["framexml"]

    fv_addons = args.ui_forever / "Interface/AddOns"
    era_addons = args.ui_era / "Interface/AddOns"
    fv_shims, fv_origin, _ = parse_shims(
        deprecation_files(fv_addons, {"camelot", "forever", "mainline"}, {"Family": "Mainline", "Game": "Camelot"})
    )
    all_fv_shims, _, fv_calls = parse_shims([p for p in fv_addons.rglob("*.lua") if "eprecat" in str(p)])
    era_shims, _, era_calls = parse_shims(deprecation_files(era_addons, {"vanilla", "classic"}, {"Family": "Classic", "Game": "Vanilla"}))
    guide = transition_guide(fv_addons)

    # A shim only helps if what it points to exists (CombatLogGetCurrentEventInfo -> nil in Forever).
    deprecated: dict[str, dict] = {}
    dead_shims: dict[str, str] = {}
    changed = True
    available = set(native)
    while changed:  # aliases can point at other aliases
        changed = False
        for name, target in fv_shims.items():
            if name in available:
                continue
            if target is None or target in available:
                available.add(name)
                deprecated[name] = {"to": target, "file": fv_origin[name]}
                changed = True
    for name, target in fv_shims.items():
        if name not in available and target:
            dead_shims[name] = target

    known: set[str] = set()
    for d in dumps.values():
        known |= d["api"] | d["lua"] | d["framexml"]
    known |= set(all_fv_shims) | set(era_shims) | set(guide)
    history: set[str] = set()
    if args.history:
        history = {line.strip() for line in args.history.read_text(encoding="utf-8").splitlines() if line.strip()}
        known |= history
    curated = json.loads(args.curated.read_text(encoding="utf-8"))
    known |= set(curated.get("known", []))
    # A name that has members in Forever is a table that exists (C_SpellBook, math, bit), not a missing function.
    tables = {n.split(".", 1)[0] for n in available if "." in n}
    missing = sorted(n for n in known - available - tables if not n.startswith("__") and re.match(r"^[A-Za-z_][\w.]*$", n))

    by_short: dict[str, list[str]] = {}
    for n in native:
        if n.startswith("C_") and "." in n:
            by_short.setdefault(n.split(".", 1)[1], []).append(n)

    def suggestions(name: str) -> tuple[list[str], bool]:
        """Replacements that exist in Forever, and whether they are only a guess by name."""
        out: list[str] = []

        def add(items):
            for t in items or []:
                if t and t in available and t not in out and t != name:
                    out.append(t)

        add(curated.get("use", {}).get(name))
        add([era_shims.get(name), all_fv_shims.get(name), dead_shims.get(name)])
        add(guide.get(name))
        add(era_calls.get(name))
        add(fv_calls.get(name))
        if out or "." in name:
            return out, False
        candidates = [c for c in by_short.get(name, []) if c in available]
        return (candidates, True) if len(candidates) == 1 else ([], False)

    removed = {}
    for n in missing:
        entry: dict = {}
        use, guess = suggestions(n)
        if use:
            entry["use"] = use
            if guess:
                entry["guess"] = True
        if n in dead_shims:
            entry["restricted"] = dead_shims[n]
        src = [fl for fl in FLAVORS[1:] if n in dumps[fl]["api"] | dumps[fl]["lua"] | dumps[fl]["framexml"]]
        if n in era_shims:
            src.append("classic_era:deprecated")
        entry["in"] = src or (["history"] if n in history else ["notes"] if n in curated.get("known", []) else ["deprecated"])
        if n in dumps["classic_era"]["framexml"] and n not in dumps["classic_era"]["api"]:
            entry["ui"] = True
        removed[n] = entry

    for n, info in deprecated.items():
        if info.get("to") is None and fv_calls.get(n):
            info["to"] = next((c for c in fv_calls[n] if c in available), None)

    structs = struct_returns(fv_addons / "Blizzard_APIDocumentationGenerated")
    for entry in removed.values():
        shapes = {u: structs[u] for u in entry.get("use", []) if u in structs}
        if shapes:
            entry["returns"] = shapes
    for info in deprecated.values():
        if info.get("to") in structs:
            info["returns"] = structs[info["to"]]
    secret_fns, restricted_events = secret_info(fv_addons / "Blizzard_APIDocumentationGenerated")
    secret_fns = {k: v for k, v in secret_fns.items() if k in available}

    known_events = set()
    for d in dumps.values():
        known_events |= d["events"]

    namespaces = sorted({n.split(".")[0] for n in native if n.startswith("C_") and "." in n})
    db = {
        "schema": 1,
        "generated": date.today().isoformat(),
        "forever": {**(fv["build"] or {}), "project": fv["project"], "ui_source": args.ui_forever_build},
        "classic_era": dumps["classic_era"]["build"],
        "available": sorted(available),
        "namespaces": namespaces,
        "deprecated": dict(sorted(deprecated.items())),
        "removed": removed,
        "secret": dict(sorted(secret_fns.items())),
        "events": sorted(fv["events"]),
        "events_removed": sorted(known_events - fv["events"]),
        "events_restricted": dict(sorted(restricted_events.items())),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(db, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(
        f"forever {db['forever'].get('version')} ({db['forever'].get('build')}): "
        f"{len(available)} available, {len(deprecated)} via deprecation fallbacks, {len(removed)} removed/absent, "
        f"{len(secret_fns)} secret-returning, {len(db['events_removed'])} events absent, "
        f"{len(restricted_events)} restricted events -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
