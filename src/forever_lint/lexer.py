"""A small Lua 5.1 tokenizer.

It only needs to be good enough to tell code from comments and strings and to
find identifiers, so it keeps every token with its line and column and never
raises on odd input: an unterminated string or comment simply runs to the end
of the file, the same way the client would refuse it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NAME, NUMBER, STRING, OP, KEYWORD, COMMENT = "name", "number", "string", "op", "keyword", "comment"

KEYWORDS = frozenset(
    "and break do else elseif end false for function goto if in local nil not or repeat return then true until while".split()
)

# Longest operators first so ".." does not become two dots.
_OPS = ["...", "..", "==", "~=", "<=", ">=", "<<", ">>", "//", "::"] + list("+-*/%^#&~|<>=(){}[];:,.")

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(
    r"0[xX](?:[0-9A-Fa-f]*\.?[0-9A-Fa-f]+|[0-9A-Fa-f]+\.?)(?:[pP][+-]?[0-9]+)?"
    r"|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_LONG_OPEN_RE = re.compile(r"\[(=*)\[")


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    col: int  # 1-based
    end_line: int
    pos: int = 0  # offset of the first character

    def is_op(self, *values: str) -> bool:
        return self.kind == OP and self.value in values

    def is_kw(self, *values: str) -> bool:
        return self.kind == KEYWORD and self.value in values


def tokenize(source: str, keep_comments: bool = False) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(source)
    line, line_start = 1, 0

    if source.startswith("#"):  # shebang line
        i = source.find("\n")
        i = n if i < 0 else i

    def emit(kind: str, start: int, end: int, value: str | None = None) -> None:
        nonlocal line, line_start
        text = source[start:end]
        start_line, col = line, start - line_start + 1
        newlines = text.count("\n")
        if newlines:
            line += newlines
            line_start = start + text.rfind("\n") + 1
        if kind != COMMENT or keep_comments:
            tokens.append(Token(kind, text if value is None else value, start_line, col, line, start))

    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
            line_start = i
            continue
        if c in " \t\r\f\v":
            i += 1
            continue

        if source.startswith("--", i):
            m = _LONG_OPEN_RE.match(source, i + 2)
            if m:
                close = "]" + m.group(1) + "]"
                end = source.find(close, m.end())
                end = n if end < 0 else end + len(close)
            else:
                end = source.find("\n", i)
                end = n if end < 0 else end
            emit(COMMENT, i, end)
            i = end
            continue

        if c in "\"'":
            j = i + 1
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == c or ch == "\n":
                    break
                j += 1
            end = min(j + 1, n)
            emit(STRING, i, end, _unquote(source[i + 1 : j]))
            i = end
            continue

        if c == "[":
            m = _LONG_OPEN_RE.match(source, i)
            if m:
                close = "]" + m.group(1) + "]"
                end = source.find(close, m.end())
                body_end = n if end < 0 else end
                end = n if end < 0 else end + len(close)
                body = source[m.end() : body_end]
                if body.startswith("\r\n"):
                    body = body[2:]
                elif body.startswith("\n"):
                    body = body[1:]
                emit(STRING, i, end, body)
                i = end
                continue

        if c.isdigit() or (c == "." and i + 1 < n and source[i + 1].isdigit()):
            m = _NUMBER_RE.match(source, i)
            if m and m.end() > i:
                emit(NUMBER, i, m.end())
                i = m.end()
                continue

        m = _NAME_RE.match(source, i)
        if m:
            word = m.group(0)
            emit(KEYWORD if word in KEYWORDS else NAME, i, m.end())
            i = m.end()
            continue

        for op in _OPS:
            if source.startswith(op, i):
                emit(OP, i, i + len(op))
                i += len(op)
                break
        else:
            i += 1  # stray byte (e.g. a BOM in the middle); skip it

    return tokens


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'", "\n": "\n"}


def _unquote(body: str) -> str:
    if "\\" not in body:
        return body
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt.isdigit():
                m = re.match(r"\d{1,3}", body[i + 1 :])
                assert m
                out.append(chr(int(m.group(0)) % 256))
                i += 1 + len(m.group(0))
                continue
        out.append(ch)
        i += 1
    return "".join(out)
