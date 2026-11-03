"""Reads Lua table constructors (the format of Blizzard_APIDocumentationGenerated).

Only literal data is supported: strings, numbers, booleans, nil, nested tables
and dotted names (kept as strings, e.g. ``Enum.Foo.Bar``). That is all the
documentation files contain.
"""

from __future__ import annotations

from typing import Any

from .lexer import KEYWORD, NAME, NUMBER, STRING, Token, tokenize


class LuaTableError(ValueError):
    pass


def parse_tables(source: str) -> dict[str, Any]:
    """Returns ``{name: value}`` for every ``local name = { ... }`` in the file."""
    toks = tokenize(source)
    out: dict[str, Any] = {}
    i = 0
    while i < len(toks) - 3:
        t = toks[i]
        if t.is_kw("local") and toks[i + 1].kind == NAME and toks[i + 2].is_op("=") and toks[i + 3].is_op("{"):
            name = toks[i + 1].value
            value, i = _value(toks, i + 3)
            out[name] = value
            continue
        i += 1
    return out


def _value(toks: list[Token], i: int) -> tuple[Any, int]:
    t = toks[i]
    if t.is_op("{"):
        return _table(toks, i)
    if t.kind == STRING:
        return t.value, i + 1
    if t.kind == NUMBER:
        return _number(t.value), i + 1
    if t.is_op("-") and i + 1 < len(toks) and toks[i + 1].kind == NUMBER:
        return -_number(toks[i + 1].value), i + 2
    if t.kind == KEYWORD and t.value in ("true", "false", "nil"):
        return {"true": True, "false": False, "nil": None}[t.value], i + 1
    if t.kind == NAME:
        parts = [t.value]
        i += 1
        while i + 1 < len(toks) and toks[i].is_op(".") and toks[i + 1].kind == NAME:
            parts.append(toks[i + 1].value)
            i += 2
        return ".".join(parts), i
    raise LuaTableError(f"line {t.line}: unexpected {t.value!r}")


def _table(toks: list[Token], i: int) -> tuple[Any, int]:
    i += 1  # {
    array: list[Any] = []
    fields: dict[str, Any] = {}
    while i < len(toks) and not toks[i].is_op("}"):
        t = toks[i]
        if t.kind == NAME and i + 1 < len(toks) and toks[i + 1].is_op("="):
            v, i = _value(toks, i + 2)
            fields[t.value] = v
        elif t.is_op("["):
            k, i = _value(toks, i + 1)
            if not toks[i].is_op("]") or not toks[i + 1].is_op("="):
                raise LuaTableError(f"line {toks[i].line}: bad key")
            v, i = _value(toks, i + 2)
            fields[str(k)] = v
        else:
            v, i = _value(toks, i)
            array.append(v)
        if i < len(toks) and not toks[i].is_op(",", ";", "}"):
            # An expression such as "1 + 2" or "Constants.X * 3": keep nothing, skip to the next field.
            depth = 0
            while i < len(toks) and not (depth == 0 and toks[i].is_op(",", ";", "}")):
                depth += toks[i].is_op("(", "{", "[") - toks[i].is_op(")", "}", "]")
                i += 1
            if fields and t.kind == NAME:
                fields[t.value] = None
        if i < len(toks) and toks[i].is_op(",", ";"):
            i += 1
    if i >= len(toks):
        raise LuaTableError("unterminated table")
    i += 1  # }
    if fields and array:
        fields["__array"] = array
        return fields, i
    return (fields if fields or not array else array), i


def _number(text: str) -> float | int:
    try:
        return int(text, 0)
    except ValueError:
        return float(text)
