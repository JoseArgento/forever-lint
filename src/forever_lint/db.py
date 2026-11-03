"""Loads the bundled API database (see tools/build_db.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class ApiDb:
    available: frozenset[str]
    namespaces: frozenset[str]
    removed_namespaces: frozenset[str]
    deprecated: dict[str, dict]
    removed: dict[str, dict]
    secret: dict[str, dict]
    events: frozenset[str]
    events_removed: frozenset[str]
    events_restricted: dict[str, list[str]]
    version: str
    build: int
    interface: int
    project_id: int
    project_name: str
    ui_source: str
    generated: str

    @property
    def label(self) -> str:
        return f"WoW Forever {self.version} ({self.build}), interface {self.interface}"


def load(path: str | Path | None = None) -> ApiDb:
    if path is None:
        return _bundled()
    return _from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@cache
def _bundled() -> ApiDb:
    text = resources.files("forever_lint").joinpath("data/api.json").read_text(encoding="utf-8")
    return _from_dict(json.loads(text))


def _from_dict(d: dict) -> ApiDb:
    if d.get("schema") != 1:
        raise ValueError(f"unsupported database schema {d.get('schema')!r}")
    namespaces = frozenset(d["namespaces"])
    removed_ns = frozenset(n.split(".", 1)[0] for n in d["removed"] if "." in n and n.startswith("C_")) - namespaces
    fv = d["forever"]
    return ApiDb(
        available=frozenset(d["available"]),
        namespaces=namespaces,
        removed_namespaces=removed_ns,
        deprecated=d["deprecated"],
        removed=d["removed"],
        secret=d["secret"],
        events=frozenset(d["events"]),
        events_removed=frozenset(d["events_removed"]),
        events_restricted=d["events_restricted"],
        version=fv["version"],
        build=fv["build"],
        interface=fv["interface"],
        project_id=fv["project"]["id"],
        project_name=fv["project"]["name"],
        ui_source=fv.get("ui_source", ""),
        generated=d["generated"],
    )
