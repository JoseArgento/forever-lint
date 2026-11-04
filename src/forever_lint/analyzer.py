"""Finds the API an addon uses and checks it against Forever.

The analysis is lexical, not a full parser. It tracks Lua scopes (function,
do/then/else, loops, ``local`` declarations) so that a local called
``GetSpellInfo`` is not reported, and it recognises the patterns addon authors
use to support several clients at once:

* polyfills: the addon assigns the global itself (``GetSpellInfo = GetSpellInfo or ...``)
* guards: the name is tested before use (``if GetSpellInfo then``, ``GetSpellInfo and ...``,
  ``C_Spell and C_Spell.GetSpellInfo or GetSpellInfo``)

Guarded uses are still reported, as notes, because the fallback branch is now the
one that runs in Forever.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .db import ApiDb
from .lexer import COMMENT, KEYWORD, NAME, NUMBER, OP, STRING, Token, tokenize

ERROR, WARNING, NOTE = "error", "warning", "note"

ARITH = {"+", "-", "*", "/", "%", "^", "//", "<", ">", "<=", ">="}
VALUE_END_KW = {"end", "nil", "true", "false", "..."}
STATEMENT_KW = {"local", "if", "for", "while", "repeat", "return", "do", "end", "break", "goto", "else", "elseif", "until", "function"}
GUARD_BEFORE_KW = {"if", "elseif", "while", "and", "or", "not", "until"}
GUARD_AFTER = {"and", "or", "then", "do"}
EVENT_METHODS = {"RegisterEvent", "RegisterUnitEvent", "RegisterEventCallback", "UnregisterEvent", "IsEventRegistered", "RegisterMessage"}
ADDON_MESSAGE_SENDERS = {"C_ChatInfo.SendAddonMessage", "C_ChatInfo.SendAddonMessageLogged", "SendAddonMessage"}
EVENT_REGISTER = {"RegisterEvent", "RegisterUnitEvent", "RegisterEventCallback"}
# Reported by an addon author in Forever's beta feedback thread (Arghness, 18/10/2026, client 1.60.1), not
# confirmed by Blizzard: notes, so authors can guard against them.
BETA_EVENT_QUIRKS = {
    "PLAYER_DEAD": "fires twice for one death; ignore a second one that comes within a few seconds",
    "MERCHANT_CLOSED": "fires twice when you walk away from the vendor",
    "AUCTION_HOUSE_CLOSED": "fires twice when you walk away from the auction house",
    "ZONE_CHANGED_NEW_AREA": "right after a hearthstone, a flight or a portal the zone text can still be the old zone, or empty; "
    "read it a moment later",
}
BETA_SOURCE = "reported in the beta feedback thread on 18/10, not confirmed by Blizzard"

_SUPPRESS_RE = re.compile(r"forever-lint:\s*(ignore-file|ignore-next-line|ignore)(?:\s*=?\s*\[?([A-Z0-9,\s]+)\]?)?", re.I)


@dataclass(slots=True)
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    col: int
    message: str
    name: str = ""
    library: bool = False
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class _Ref:
    name: str  # full dotted name as checked (e.g. "C_Spell.GetSpellInfo" or "GetSpellInfo")
    tok: Token
    index: int  # token index of the first part
    end: int  # token index after the chain
    is_call: bool
    guarded: bool
    alias: bool = False  # a use of "local X = X": name is the global it stands for


@dataclass(slots=True)
class FileScan:
    path: str
    refs: list[_Ref]
    defines: set[str]  # globals the file assigns (polyfills / its own API)
    guarded_names: set[str]
    tokens: list[Token]
    suppress_all: set[str] | None  # None = nothing; empty set = all rules
    suppress_lines: dict[int, set[str]]


class _Scope:
    __slots__ = ("names", "parent", "aliases")

    def __init__(self, parent: _Scope | None):
        self.names: set[str] = set()
        self.parent = parent
        self.aliases: dict[str, str] = {}  # "local UnitHealth = UnitHealth" -> {"UnitHealth": "UnitHealth"}

    def owner(self, name: str) -> _Scope | None:
        s: _Scope | None = self
        while s is not None:
            if name in s.names:
                return s
            s = s.parent
        return None

    def has(self, name: str) -> bool:
        s: _Scope | None = self
        while s is not None:
            if name in s.names:
                return True
            s = s.parent
        return False


def _is_value_end(t: Token) -> bool:
    return t.kind in (NAME, NUMBER, STRING) or t.is_op(")", "]", "}") or (t.kind == KEYWORD and t.value in VALUE_END_KW)


def _suppressions(source: str) -> tuple[set[str] | None, dict[int, set[str]]]:
    file_rules: set[str] | None = None
    lines: dict[int, set[str]] = {}
    if "forever-lint" not in source:
        return None, lines
    for tok in tokenize(source, keep_comments=True):
        if tok.kind != COMMENT:
            continue
        m = _SUPPRESS_RE.search(tok.value)
        if not m:
            continue
        rules = {r.strip().upper() for r in (m.group(2) or "").split(",") if r.strip()}
        kind = m.group(1).lower()
        if kind == "ignore-file":
            file_rules = (file_rules or set()) | rules if rules else set()
        else:
            line = tok.line + 1 if kind == "ignore-next-line" else tok.line
            lines.setdefault(line, set()).update(rules or {"*"})
    return file_rules, lines


def scan(path: str, source: str) -> FileScan:
    toks = tokenize(source)
    n = len(toks)
    refs: list[_Ref] = []
    defines: set[str] = set()
    guarded: set[str] = set()
    reassigned: set[str] = set()  # locals assigned again later (e.g. a guarded replacement)

    root = _Scope(None)
    scope = root
    # Block stack entries: the scope object pushed for that block.
    blocks: list[_Scope] = []
    pending_loop: list[str] = []
    # (names, scope, block depth) declared by "local a, b = ..." and waiting for the statement to end
    pending_locals: list[tuple[list[str], _Scope, int, dict[str, str] | None]] = []
    brackets: list[str] = []  # "(", "[", "{"
    # A function body starts with no open brackets, even inside a call: f(function() local a = 1 ... end)
    fn_brackets: list[tuple[int, list[str]]] = []  # (block depth of the body, brackets outside it)

    def push(names: Iterable[str] = ()) -> None:
        nonlocal scope
        scope = _Scope(scope)
        scope.names.update(names)
        blocks.append(scope)

    def pop() -> None:
        nonlocal scope
        if blocks:
            blocks.pop()
            scope = blocks[-1] if blocks else root

    def flush_locals(force: bool = False) -> None:
        keep = []
        for names, sc, depth, alias in pending_locals:
            if force or depth == len(blocks):
                sc.names.update(names)
                for name in names:
                    if alias and name in alias:
                        sc.aliases[name] = alias[name]
                    else:
                        sc.aliases.pop(name, None)
            else:
                keep.append((names, sc, depth, alias))
        pending_locals[:] = keep

    i = 0
    while i < n:
        t = toks[i]
        prev = toks[i - 1] if i else None

        # A statement boundary activates "local x = ..." declarations made at this block depth.
        if pending_locals and not brackets and prev is not None:
            if (
                t.is_op(";")
                or (t.kind == KEYWORD and t.value in STATEMENT_KW and t.value != "function")
                or (t.kind == NAME and t.line > prev.line and _is_value_end(prev))
                # "local t = {}" then "function t.x()" on the next line
                or (t.is_kw("function") and t.line > prev.line and _is_value_end(prev))
            ):
                flush_locals()

        if t.kind == OP:
            if t.value in "([{":
                brackets.append(t.value)
            elif t.value in ")]}":
                if brackets:
                    brackets.pop()
            i += 1
            continue

        if t.kind == KEYWORD:
            v = t.value
            if v == "local":
                nxt = toks[i + 1] if i + 1 < n else None
                if nxt is not None and nxt.is_kw("function"):
                    if i + 2 < n and toks[i + 2].kind == NAME:
                        scope.names.add(toks[i + 2].value)
                    i += 1  # handled as "function" on the next iteration (name is local now)
                    continue
                names, j = [], i + 1
                while j < n and toks[j].kind == NAME:
                    names.append(toks[j].value)
                    j += 1
                    if j < n and toks[j].is_op("<"):  # Lua 5.4 attribs, harmless
                        j += 3
                    if j < n and toks[j].is_op(","):
                        j += 1
                        continue
                    break
                if j < n and toks[j].is_op("="):
                    pending_locals.append((names, scope, len(blocks), _alias_of(toks, j + 1, names, scope)))
                else:
                    scope.names.update(names)
                i = j
                continue
            if v == "function":
                j = i + 1
                method = False
                if j < n and toks[j].kind == NAME:  # function a.b:c(
                    chain = [toks[j].value]
                    first = toks[j]
                    j += 1
                    while j + 1 < n and toks[j].is_op(".", ":") and toks[j + 1].kind == NAME:
                        method = method or toks[j].value == ":"
                        chain.append(toks[j + 1].value)
                        j += 2
                    if len(chain) == 1:
                        if not scope.has(chain[0]):
                            defines.add(chain[0])
                    elif not scope.has(chain[0]):
                        if not method:
                            defines.add(".".join(chain[:2]))
                        # using the table is a reference to it (function SomeGlobal.Foo() needs SomeGlobal)
                        refs.append(_Ref(chain[0], first, i + 1, i + 2, False, True))
                params: list[str] = ["self"] if method else []
                if j < n and toks[j].is_op("("):
                    j += 1
                    while j < n and not toks[j].is_op(")"):
                        if toks[j].kind == NAME:
                            params.append(toks[j].value)
                        j += 1
                    j += 1
                push(params)
                fn_brackets.append((len(blocks), brackets[:]))
                brackets.clear()
                i = j
                continue
            if v == "for":
                j = i + 1
                pending_loop = []
                while j < n and not (toks[j].is_op("=") or toks[j].is_kw("in")):
                    if toks[j].kind == NAME:
                        pending_loop.append(toks[j].value)
                    j += 1
                i = j
                continue
            if v == "do":
                push(pending_loop)
                pending_loop = []
            elif v in ("then", "repeat"):
                push()
            elif v == "elseif":
                pop()
            elif v == "else":
                pop()
                push()
            elif v in ("end", "until"):
                flush_locals()
                if v == "end" and fn_brackets and fn_brackets[-1][0] == len(blocks):
                    brackets[:] = fn_brackets.pop()[1]
                pop()
            elif v == "goto":
                i += 2
                continue
            i += 1
            continue

        if t.kind != NAME:
            i += 1
            continue

        # ---- a name
        if prev is not None and (prev.is_op(".", ":", "::") or prev.is_kw("goto")):
            i += 1
            continue
        nxt = toks[i + 1] if i + 1 < n else None
        # table constructor key: { Name = value }
        if brackets and brackets[-1] == "{" and nxt is not None and nxt.is_op("=") and prev is not None and prev.is_op("{", ",", ";"):
            i += 1
            continue
        owner = scope.owner(t.value)
        if owner is not None:
            if nxt is not None and nxt.is_op("=") and not brackets:
                reassigned.add(t.value)
                owner.aliases.pop(t.value, None)
                i += 1
                continue
            alias = owner.aliases.get(t.value)
            if alias is None:
                i += 1
                continue
            j = i + 1
            full = alias
            if "." not in alias and j + 1 < n and toks[j].is_op(".") and toks[j + 1].kind == NAME:
                full = f"{alias}.{toks[j + 1].value}"
                j += 2
            after = toks[j] if j < n else None
            is_call = after is not None and (after.is_op("(", "{", ":") or after.kind == STRING)
            refs.append(_Ref(full, t, i, j, is_call, False, True))
            i = j
            continue

        # Global reference. Build "Base.Member" for namespace tables.
        j = i + 1
        full = t.value
        if j + 1 < n and toks[j].is_op(".") and toks[j + 1].kind == NAME:
            full = f"{t.value}.{toks[j + 1].value}"
            j += 2
            while j + 1 < n and toks[j].is_op(".") and toks[j + 1].kind == NAME:
                j += 2
        after = toks[j] if j < n else None
        is_call = after is not None and (after.is_op("(") or after.kind == STRING or after.is_op("{"))
        if after is not None and after.is_op(":"):
            is_call = True

        # Assignment targets define the global (polyfill).
        assigned = after is not None and after.is_op("=") and not brackets
        multi_assign = after is not None and after.is_op(",") and not brackets and _looks_like_assignment(toks, j)
        if assigned or multi_assign:
            defines.add(full)
            if full != t.value:
                defines.add(full)
            i = j
            continue

        is_guard = (
            prev is not None
            and (
                (prev.kind == KEYWORD and prev.value in GUARD_BEFORE_KW)
                or (prev.is_op("(") and i >= 2 and toks[i - 2].kind == NAME and toks[i - 2].value in ("type", "issecurevariable", "rawget"))
            )
            and not is_call
        ) or (after is not None and ((after.kind == KEYWORD and after.value in GUARD_AFTER) or after.is_op("==", "~=")) and not is_call)
        if is_guard:
            guarded.add(full)
            guarded.add(t.value)
        refs.append(_Ref(full, t, i, j, is_call, is_guard))
        i = j if full != t.value else i + 1

    flush_locals(force=True)
    # "local X = X" followed by "X = replacement" later: the cached global is only a fallback.
    for r in refs:
        k = r.index
        if r.name in reassigned and k >= 3 and toks[k - 1].is_op("=") and toks[k - 2].value == r.name and toks[k - 3].is_kw("local"):
            r.guarded = True
    file_rules, line_rules = _suppressions(source)
    return FileScan(path, refs, defines, guarded, toks, file_rules, line_rules)


def _alias_of(toks: list[Token], k: int, names: list[str], scope: _Scope) -> dict[str, str] | None:
    """``local load = loadstring`` / ``local A, B = A, B``: the globals (``A`` or ``A.B``) the locals stand for."""
    n = len(toks)
    out: dict[str, str] = {}
    e = k
    for idx, name in enumerate(names):
        if e >= n or toks[e].kind != NAME or scope.has(toks[e].value):
            return None
        chain = [toks[e].value]
        e += 1
        while e + 1 < n and toks[e].is_op(".") and toks[e + 1].kind == NAME:
            chain.append(toks[e + 1].value)
            e += 2
        if len(chain) > 2:
            return None
        out[name] = ".".join(chain)
        if idx < len(names) - 1:
            if e >= n or not toks[e].is_op(","):
                return None
            e += 1
    end = e >= n or toks[e].is_op(";") or toks[e].line > toks[e - 1].line or (toks[e].kind == KEYWORD and toks[e].value in STATEMENT_KW)
    return out if end else None


def _looks_like_assignment(toks: list[Token], j: int) -> bool:
    """``a, b = ...`` at statement level: names and commas up to an ``=``."""
    k = j
    while k + 1 < len(toks) and toks[k].is_op(","):
        if toks[k + 1].kind != NAME:
            return False
        k += 2
        while k + 1 < len(toks) and toks[k].is_op(".") and toks[k + 1].kind == NAME:
            k += 2
    return k < len(toks) and toks[k].is_op("=")


# ---------------------------------------------------------------------------- rules


def _closing(toks: list[Token], open_index: int) -> int:
    depth = 0
    for k in range(open_index, len(toks)):
        if toks[k].is_op("(", "[", "{"):
            depth += 1
        elif toks[k].is_op(")", "]", "}"):
            depth -= 1
            if depth == 0:
                return k
    return len(toks) - 1


def check(scans: list[FileScan], db: ApiDb, is_library) -> list[Finding]:
    addon_defines: set[str] = set()
    for s in scans:
        addon_defines |= s.defines
    findings: list[Finding] = []
    trial_noted = False
    for s in scans:
        lib = is_library(s.path)
        out: list[Finding] = []
        _api_rules(s, db, addon_defines, out)
        _secret_rule(s, db, addon_defines, out)
        _string_rules(s, db, out)
        if not trial_noted and not lib:
            sender = next((r for r in s.refs if r.is_call and r.name in ADDON_MESSAGE_SENDERS), None)
            if sender is not None:
                trial_noted = True
                out.append(
                    Finding(
                        "FL012",
                        NOTE,
                        s.path,
                        sender.tok.line,
                        sender.tok.col,
                        "Trial accounts cannot send addon messages in Forever (1.60.1). Expect the send to fail for those players "
                        "and keep the addon working without replies.",
                        sender.name,
                    )
                )
        for f in out:
            f.library = lib
        findings += [f for f in out if not _suppressed(s, f)]
    return findings


def _suppressed(s: FileScan, f: Finding) -> bool:
    if s.suppress_all is not None and (not s.suppress_all or f.rule in s.suppress_all):
        return True
    rules = s.suppress_lines.get(f.line)
    return bool(rules and ("*" in rules or f.rule in rules))


def _replacement_text(entry: dict) -> str:
    use = entry.get("use") or []
    if not use:
        return ""
    joined = " or ".join(f"`{u}`" for u in use[:3])
    text = f" Probably replaced by {joined}." if entry.get("guess") else f" Use {joined}."
    shapes = entry.get("returns") or {}
    if shapes:
        text += " Note: " + "; ".join(f"`{u}` returns a {t} table, not separate values" for u, t in shapes.items() if u in use[:3]) + "."
    return text


def _api_rules(s: FileScan, db: ApiDb, addon_defines: set[str], out: list[Finding]) -> None:
    for r in s.refs:
        base = r.name.split(".", 1)[0]
        if base == "WOW_PROJECT_ID":
            _project_id(s, r, db, out)
            continue
        if r.name in addon_defines or base in addon_defines or (r.name in db.available and r.name not in db.deprecated):
            continue
        if r.alias:
            continue  # "local X = X": reported once, where the global is read
        guarded = r.guarded or r.name in s.guarded_names or base in s.guarded_names

        name, entry = r.name, db.removed.get(r.name)
        if entry is None and r.name != base and base in db.removed_namespaces:
            name, entry = base, {"namespace": True}
        if entry is not None:
            checked = _checked_replacement(s, entry)
            if checked and not guarded:
                msg = (
                    f"`{name}` does not exist in Forever, but this file checks for `{checked}` first, "
                    "so this looks like a fallback for older clients that does not run in Forever."
                )
                out.append(
                    Finding("FL001", NOTE, s.path, r.tok.line, r.tok.col, msg, name, extra={"use": entry.get("use", []), "fallback": True})
                )
                continue
            if entry.get("namespace"):
                msg = f"The `{name}` namespace does not exist in Forever."
            elif entry.get("restricted"):
                msg = f"`{name}` does not work in Forever: it points to `{entry['restricted']}`, which is not available to addons."
            elif entry.get("ui"):
                msg = f"`{name}` is a Blizzard UI function that does not exist in Forever (Forever uses the modern UI)."
            else:
                msg = f"`{name}` does not exist in Forever."
            msg += _replacement_text(entry)
            if guarded:
                msg += " The code checks for it first, so the fallback branch is what runs in Forever: make sure it works."
            out.append(
                Finding("FL001", NOTE if guarded else ERROR, s.path, r.tok.line, r.tok.col, msg, name, extra={"use": entry.get("use", [])})
            )
            continue

        dep = db.deprecated.get(r.name)
        if dep:
            to = dep.get("to")
            msg = (
                f"`{r.name}` only exists in Forever as a backwards-compatibility fallback "
                f"({dep['file']}, loaded while the CVar loadDeprecationFallbacks is on) and Blizzard says it will be removed."
            )
            if to:
                msg += f" Use `{to}`" + (f" (it returns a {dep['returns']} table)." if dep.get("returns") else ".")
            out.append(
                Finding(
                    "FL002", NOTE if guarded else WARNING, s.path, r.tok.line, r.tok.col, msg, r.name, extra={"use": [to] if to else []}
                )
            )


def _checked_replacement(s: FileScan, entry: dict) -> str | None:
    """A replacement (or its namespace) that the file tests in a condition, e.g. ``if C_Item and C_Item.GetItemSpell``."""
    for use in entry.get("use") or []:
        if use in s.guarded_names:
            return use
        ns = use.split(".", 1)[0]
        if "." in use and ns in s.guarded_names:
            return ns
    return None


def _project_id(s: FileScan, r: _Ref, db: ApiDb, out: list[Finding]) -> None:
    toks = s.tokens
    pid = db.project_id
    pname = db.project_name
    i = r.index
    other = None
    op = None
    if i + 2 < len(toks) and toks[i + 1].is_op("==", "~="):
        op, other = toks[i + 1].value, toks[i + 2]
    elif i >= 2 and toks[i - 1].is_op("==", "~="):
        op, other = toks[i - 1].value, toks[i - 2]
    if other is not None and (other.kind == NUMBER or (other.kind == NAME and other.value.startswith("WOW_PROJECT_"))):
        equal = (other.value == pname) if other.kind == NAME else _num(other.value) == pid
        result = equal if op == "==" else not equal
        what = other.value
        aware = any(t.line == r.tok.line and t.value in (pname, "WOW_PROJECT_FOREVER") for t in toks)
        if aware:
            out.append(
                Finding(
                    "FL004",
                    NOTE,
                    s.path,
                    r.tok.line,
                    r.tok.col,
                    f"`WOW_PROJECT_ID {op} {what}` is {str(result).lower()} in Forever; this line already handles `{pname}`.",
                    "WOW_PROJECT_ID",
                    extra={"evaluates": result},
                )
            )
            return
        msg = (
            f"In Forever `WOW_PROJECT_ID` is {pid} (`{pname}`), so `WOW_PROJECT_ID {op} {what}` is {str(result).lower()}."
            " Check that this takes the branch you want; Forever is neither Classic Era nor retail."
        )
        out.append(Finding("FL004", WARNING, s.path, r.tok.line, r.tok.col, msg, "WOW_PROJECT_ID", extra={"evaluates": result}))
    else:
        out.append(
            Finding(
                "FL004",
                NOTE,
                s.path,
                r.tok.line,
                r.tok.col,
                f"`WOW_PROJECT_ID` is {pid} (`{pname}`) in Forever. Lookups keyed by project ID need an entry for it.",
                "WOW_PROJECT_ID",
            )
        )


def _num(text: str) -> float | None:
    try:
        return int(text, 0)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def _secret_rule(s: FileScan, db: ApiDb, addon_defines: set[str], out: list[Finding]) -> None:
    toks = s.tokens
    secret_locals: dict[str, str] = {}
    for r in s.refs:
        info = db.secret.get(r.name)
        if not info or not info.get("numeric") or not r.is_call or r.name in addon_defines:
            continue
        if r.end >= len(toks) or not toks[r.end].is_op("("):
            continue
        close = _closing(toks, r.end)
        before = toks[r.index - 1] if r.index else None
        after = toks[close + 1] if close + 1 < len(toks) else None
        if (after is not None and after.kind == OP and after.value in ARITH) or (
            before is not None and before.kind == OP and before.value in ARITH
        ):
            out.append(_secret_finding(s.path, r.tok, r.name, info))
            continue
        # local hp = UnitHealth("target")  ...  hp / max
        if (
            before is not None
            and before.is_op("=")
            and r.index >= 2
            and toks[r.index - 2].kind == NAME
            and r.index >= 3
            and toks[r.index - 3].is_kw("local")
        ):
            if after is None or not after.is_op(","):
                secret_locals[toks[r.index - 2].value] = r.name
    if not secret_locals:
        return
    for k, t in enumerate(toks):
        if t.kind != NAME or t.value not in secret_locals:
            continue
        if k and toks[k - 1].is_op(".", ":"):
            continue
        before = toks[k - 1] if k else None
        after = toks[k + 1] if k + 1 < len(toks) else None
        if (after is not None and after.kind == OP and after.value in ARITH) or (
            before is not None and before.kind == OP and before.value in ARITH
        ):
            if _secret_checked(toks, k, t.value):
                continue
            fn = secret_locals[t.value]
            out.append(_secret_finding(s.path, t, fn, db.secret[fn], via=t.value))


def _secret_checked(toks: list[Token], k: int, name: str) -> bool:
    """``issecretvalue(hp)``, ``E:NotSecretValue(hp)``, ``self:IsSecret(hp)`` earlier on the same line guards ``hp``."""
    line = toks[k].line
    j = k - 1
    while j >= 2 and toks[j].line == line:
        if (
            toks[j].kind == NAME
            and toks[j].value == name
            and toks[j - 1].is_op("(")
            and toks[j - 2].kind == NAME
            and "secret" in toks[j - 2].value.lower()
        ):
            return True
        j -= 1
    return False


def _secret_finding(path: str, tok: Token, fn: str, info: dict, via: str | None = None) -> Finding:
    when = "always" if info.get("always") else ("when " + ", ".join(info["when"])) if info.get("when") else "conditionally"
    subject = f"`{via}` holds the result of `{fn}`, which" if via else f"`{fn}`"
    msg = (
        f"{subject} can return a secret value in Forever ({when}). Arithmetic and comparisons on a secret value "
        "raise a Lua error in addon code. Pass the value straight to a widget that accepts secrets "
        "(e.g. StatusBar:SetValue) or test it with `issecretvalue()` first."
    )
    return Finding("FL003", WARNING, path, tok.line, tok.col, msg, fn)


def _chain_start(toks: list[Token], k: int) -> int:
    """Index of the first token of ``a.b.c`` / ``a:b`` ending just before ``k`` (``k`` is the ``:`` of the method call)."""
    j = k - 1
    while j >= 2 and toks[j].kind == NAME and toks[j - 1].is_op(".", ":") and toks[j - 2].kind == NAME:
        j -= 2
    return j


def _string_rules(s: FileScan, db: ApiDb, out: list[Finding]) -> None:
    toks = s.tokens
    n = len(toks)
    for k, t in enumerate(toks):
        if t.kind != NAME:
            continue
        # frame:RegisterEvent("EVENT") / RegisterEvent(frame, "EVENT") / self:RegisterEvent("EVENT", handler)
        if t.value in EVENT_METHODS and k + 2 < n and toks[k + 1].is_op("("):
            arg = toks[k + 2]
            if arg.kind != STRING and k + 4 < n and toks[k + 3].is_op(","):
                arg = toks[k + 4]
            if arg.kind == STRING and re.fullmatch(r"[A-Z][A-Z0-9_]+", arg.value):
                ev = arg.value
                if ev in db.events_removed and ev not in db.events:
                    sev = ERROR if t.value in EVENT_REGISTER else WARNING
                    out.append(
                        Finding(
                            "FL005", sev, s.path, arg.line, arg.col, f"Event `{ev}` does not exist in Forever; registering it fails.", ev
                        )
                    )
                elif ev in BETA_EVENT_QUIRKS and t.value in EVENT_REGISTER:
                    out.append(
                        Finding("FL013", NOTE, s.path, arg.line, arg.col, f"`{ev}` in Forever {BETA_EVENT_QUIRKS[ev]} ({BETA_SOURCE}).", ev)
                    )
                elif ev in db.events_restricted and t.value in EVENT_REGISTER:
                    reasons = db.events_restricted[ev]
                    if "HasRestrictions" in reasons and ev.startswith("COMBAT_LOG"):
                        why = "the combat log is restricted for addons in Forever and `CombatLogGetCurrentEventInfo` is not available"
                    elif "SecretPayloads" in reasons:
                        why = "its payload arrives as secret values"
                    else:
                        why = "it has restrictions for addons"
                    out.append(
                        Finding("FL006", WARNING, s.path, arg.line, arg.col, f"Event `{ev}`: {why}.", ev, extra={"reasons": reasons})
                    )
        # SendChatMessage(msg, "SAY"|"YELL"|"EMOTE") needs a hardware event outside instances (EMOTE since 1.60.1)
        if t.value == "SendChatMessage" and k + 1 < n and toks[k + 1].is_op("("):
            close = _closing(toks, k + 1)
            depth, commas, arg = 0, 0, None
            for x in toks[k + 2 : close]:
                if x.is_op("(", "[", "{"):
                    depth += 1
                elif x.is_op(")", "]", "}"):
                    depth -= 1
                elif depth == 0 and x.is_op(","):
                    commas += 1
                elif depth == 0 and commas == 1 and arg is None:
                    arg = x
            kind = arg.value.upper() if arg is not None and arg.kind == STRING else ("SAY" if commas == 0 else None)
            if kind in ("SAY", "YELL", "EMOTE"):
                out.append(
                    Finding(
                        "FL011",
                        NOTE,
                        s.path,
                        t.line,
                        t.col,
                        f'Sending "{kind}" from addon code outside instances needs a hardware event (a key press or click) in Forever: '
                        "calls from timers or event handlers fail there. Inside dungeons and raids addons could always send them, but a beta tester "
                        "reported on 18/10 that in Forever they no longer can (not confirmed). EMOTE joined SAY and YELL in 1.60.1.",
                        "SendChatMessage",
                        extra={"chatType": kind},
                    )
                )
        # frame:GetFrameStrata() can return a secret value (1.60.1): comparing it raises an error
        if (
            t.value == "GetFrameStrata"
            and k >= 1
            and toks[k - 1].is_op(":")
            and k + 2 < n
            and toks[k + 1].is_op("(")
            and toks[k + 2].is_op(")")
        ):
            after = toks[k + 3] if k + 3 < n else None
            before_chain = _chain_start(toks, k - 1)
            before = toks[before_chain - 1] if before_chain else None
            if (after is not None and after.is_op("==", "~=")) or (before is not None and before.is_op("==", "~=")):
                out.append(
                    Finding(
                        "FL003",
                        WARNING,
                        s.path,
                        t.line,
                        t.col,
                        "`GetFrameStrata` returns a secret value in Forever for frames with the FrameStrata secret aspect (1.60.1), "
                        "and comparing a secret value raises a Lua error in addon code. Test it with `issecretvalue()` first.",
                        "GetFrameStrata",
                    )
                )
        # hooksecurefunc("GlobalName", fn)
        if t.value == "hooksecurefunc" and k + 2 < n and toks[k + 1].is_op("(") and toks[k + 2].kind == STRING:
            if k + 3 < n and toks[k + 3].is_op(","):
                name = toks[k + 2].value
                entry = db.removed.get(name)
                if entry is not None and name not in db.available and name not in s.guarded_names and name not in s.defines:
                    arg = toks[k + 2]
                    out.append(
                        Finding(
                            "FL001",
                            ERROR,
                            s.path,
                            arg.line,
                            arg.col,
                            f"`hooksecurefunc` target `{name}` does not exist in Forever, so the hook raises an error.{_replacement_text(entry)}",
                            name,
                        )
                    )
        # select(4, GetBuildInfo()) >= 20000
        if (
            t.value == "GetBuildInfo"
            and k >= 4
            and toks[k - 1].is_op(",")
            and toks[k - 2].kind == NUMBER
            and toks[k - 2].value == "4"
            and toks[k - 3].is_op("(")
            and toks[k - 4].value == "select"
        ):
            close = _closing(toks, k - 3)
            if (
                close + 2 < n
                and toks[close + 1].kind == OP
                and toks[close + 1].value in {"<", ">", "<=", ">=", "==", "~="}
                and toks[close + 2].kind == NUMBER
            ):
                op, num = toks[close + 1].value, _num(toks[close + 2].value)
                if num is not None:
                    v = db.interface
                    result = {"<": v < num, ">": v > num, "<=": v <= num, ">=": v >= num, "==": v == num, "~=": v != num}[op]
                    out.append(
                        Finding(
                            "FL007",
                            WARNING,
                            s.path,
                            t.line,
                            t.col,
                            f"Forever reports interface version {v}, so `select(4, GetBuildInfo()) {op} {toks[close + 2].value}` is {str(result).lower()}. "
                            "Forever has the modern API with a Classic-era version number.",
                            "GetBuildInfo",
                            extra={"evaluates": result},
                        )
                    )
