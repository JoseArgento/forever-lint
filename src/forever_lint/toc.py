"""Which TOC Forever loads for an addon, and which Lua files that TOC pulls in.

Rules (warcraft.wiki.gg, "TOC format"): the client looks for a TOC named after
the addon folder with its game-type suffix first, then the family suffix, then
no suffix. Forever's game type is ``camelot`` (alias ``forever``) in the
``mainline`` family, so the order is _Camelot, _Forever, _Mainline, plain.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

FOREVER_GAME_TYPES = {"camelot", "forever", "mainline"}
FOREVER_SUFFIXES = ["_Camelot", "_Forever", "_Mainline", ""]
PLACEHOLDERS = {"Family": "Mainline", "Game": "Camelot", "TextLocale": "enUS"}
_SUFFIX_RE = re.compile(
    r"[_-](Mainline|Standard|Forever|Camelot|Vanilla|Classic|TBC|BCC|Wrath|WOTLKC|Cata|Mists|Plunderstorm|WoWLabs|WoWHack)$", re.I
)
_LIB_DIR_RE = re.compile(r"^(libs?|libraries|embeds?|externals?|vendor)$", re.I)
_XML_REF_RE = re.compile(r"<\s*(Script|Include)\b[^>]*?\bfile\s*=\s*[\"']([^\"']+)[\"']", re.I)


@dataclass
class Toc:
    path: Path
    interfaces: list[int] = field(default_factory=list)
    allow: set[str] | None = None
    exclude: set[str] = field(default_factory=set)
    entries: list[tuple[str, list[tuple[str, set[str]]]]] = field(default_factory=list)  # (file, [(directive, values)])
    title: str = ""

    def loads_in_forever(self) -> bool:
        if self.allow is not None and not (self.allow & FOREVER_GAME_TYPES):
            return False
        return not (self.exclude & FOREVER_GAME_TYPES)


@dataclass
class Addon:
    root: Path
    name: str
    tocs: list[Path]
    toc: Toc | None = None
    lua_files: list[Path] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    problems: list[tuple[str, str, str]] = field(default_factory=list)  # (rule, severity, message)


def parse_toc(path: Path) -> Toc:
    toc = Toc(path)
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("##"):
            key, _, value = line[2:].partition(":")
            key, value = key.strip(), value.strip()
            if key.lower() == "interface":
                toc.interfaces = [int(v) for v in re.findall(r"\d+", value)]
            elif key == "AllowLoadGameType":
                toc.allow = (toc.allow or set()) | {v.strip().lower() for v in value.split(",") if v.strip()}
            elif key == "ExcludeLoadGameType":
                toc.exclude |= {v.strip().lower() for v in value.split(",") if v.strip()}
            elif key.lower() == "title" and not toc.title:
                toc.title = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", value)
            continue
        if line.startswith("#"):
            continue
        # "file.lua [AllowLoadGameType a, b][AllowLoadTextLocale enUS]": any number of trailing directives.
        entry = line
        conds: list[tuple[str, set[str]]] = []
        while m := re.search(r"\s*\[(\w+)\s+([^\]]*)\]\s*$", entry):
            conds.append((m.group(1), {v.strip().lower() for v in m.group(2).split(",") if v.strip()}))
            entry = entry[: m.start()]
        if entry.strip():
            toc.entries.append((entry.strip(), conds))
    return toc


def is_library(rel_path: str) -> bool:
    return any(_LIB_DIR_RE.match(part) for part in Path(rel_path).parts[:-1])


def find_addons(root: Path) -> list[Addon]:
    """Addon folders under root (a folder with TOC files). Libraries embedded in an addon are not separate addons."""
    root = root.resolve()
    by_dir: dict[Path, list[Path]] = {}
    for toc in sorted(root.rglob("*.toc")):
        if any(p.startswith(".") for p in toc.relative_to(root).parts):
            continue
        by_dir.setdefault(toc.parent, []).append(toc)
    addons: list[Addon] = []
    for folder in sorted(by_dir, key=lambda p: len(p.parts)):
        parent = next((a for a in addons if folder != a.root and a.root in folder.parents), None)
        if parent is not None and is_library(str(folder.relative_to(parent.root) / "x")):
            continue
        addons.append(Addon(folder, _addon_name(folder, by_dir[folder]), by_dir[folder]))
    return addons


def _addon_name(folder: Path, tocs: list[Path]) -> str:
    stems = [t.stem for t in tocs]
    for s in stems:
        if s.lower() == folder.name.lower() or _SUFFIX_RE.sub("", s).lower() == folder.name.lower():
            return folder.name
    bases = Counter(_SUFFIX_RE.sub("", s) for s in stems)
    return bases.most_common(1)[0][0]


def resolve(addon: Addon) -> Addon:
    lower = {t.name.lower(): t for t in addon.tocs}
    chosen = None
    for suffix in FOREVER_SUFFIXES:
        chosen = lower.get(f"{addon.name}{suffix}.toc".lower())
        if chosen:
            break
    if chosen is None:
        names = ", ".join(sorted(t.name for t in addon.tocs))
        addon.problems.append(
            (
                "FL008",
                "error",
                f"No TOC that Forever loads: Forever looks for {addon.name}_Camelot.toc, {addon.name}_Forever.toc, "
                f"{addon.name}_Mainline.toc or {addon.name}.toc, and this addon has {names}. "
                "Forever will not list the addon.",
            )
        )
        return addon
    toc = parse_toc(chosen)
    addon.toc = toc
    if not toc.loads_in_forever():
        addon.problems.append(
            (
                "FL010",
                "note",
                f"{chosen.name} excludes Forever with AllowLoadGameType/ExcludeLoadGameType, so it does not load there. "
                "Fine if that is on purpose (e.g. a retail-only module).",
            )
        )
        return addon
    if toc.interfaces and 16001 not in toc.interfaces:
        listed = ", ".join(str(v) for v in toc.interfaces)
        addon.problems.append(
            (
                "FL009",
                "warning",
                f"{chosen.name} declares Interface {listed} but Forever is 16001: the addon shows as out of date "
                'and only loads with "Load out of date AddOns". Add 16001 to the comma-separated list.',
            )
        )
    seen: set[Path] = set()
    for entry, conds in toc.entries:
        if not all(_passes(kind, vals) for kind, vals in conds):
            continue
        rel = entry
        for key, sub in PLACEHOLDERS.items():
            rel = rel.replace(f"[{key}]", sub)
        _collect(addon, chosen.parent, rel, seen)
    return addon


def _passes(kind: str, values: set[str]) -> bool:
    if kind == "AllowLoadGameType":
        return bool(values & FOREVER_GAME_TYPES)
    if kind == "ExcludeLoadGameType":
        return not values & FOREVER_GAME_TYPES
    return True


def _ci_path(base: Path, rel: str) -> Path | None:
    """Case-insensitive lookup (the game runs on Windows and macOS; addon paths are often miscased)."""
    current = base
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        direct = current / part
        if direct.exists():
            current = direct
            continue
        if not current.is_dir():
            return None
        match = next((c for c in current.iterdir() if c.name.lower() == part.lower()), None)
        if match is None:
            return None
        current = match
    return current


def _collect(addon: Addon, base: Path, rel: str, seen: set[Path]) -> None:
    path = _ci_path(base, rel)
    if path is None or not path.is_file():
        addon.missing_files.append(rel)
        return
    if path in seen:
        return
    seen.add(path)
    suffix = path.suffix.lower()
    if suffix == ".lua":
        addon.lua_files.append(path)
    elif suffix == ".xml":
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        for _, ref in _XML_REF_RE.findall(text):
            _collect(addon, path.parent, ref, seen)
