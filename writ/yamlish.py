"""The small piece of YAML a writ config is written in.

Writ has no dependencies, and a YAML library would be its first. The config is
a few nested mappings of scalars, so this reads that and nothing more:

- block mappings nested by indentation (spaces, not tabs): `key: value`, or
  `key:` with the nested block on the following, deeper lines;
- scalars: `null`/`~`/nothing, `true`/`false`, integers, decimals, single- or
  double-quoted strings, and plain strings;
- lists of scalars, either `[a, b]` on one line or `- a` items under a key;
- `#` comments, on their own line or after a value.

Anything else — anchors, multi-line strings, flow mappings, documents — is
refused with the line it is on, rather than read as something it is not.
"""
from __future__ import annotations

import re
from typing import Any

from .state import WritError

_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s+(.*))?$")
_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)$")


class _Line:
    __slots__ = ("number", "indent", "text")

    def __init__(self, number: int, indent: int, text: str) -> None:
        self.number = number
        self.indent = indent
        self.text = text


def loads(text: str, *, where: str = "config") -> Any:
    """Parse the subset above. An empty document is an empty mapping."""
    lines = _lines(text, where)
    if not lines:
        return {}
    value, rest = _block(lines, 0, lines[0].indent, where)
    if rest < len(lines):
        raise _error(where, lines[rest], "unexpected indentation")
    return value


def _lines(text: str, where: str) -> list[_Line]:
    out = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        body = stripped.lstrip(" ")
        if body.startswith("\t") or "\t" in stripped[: len(stripped) - len(body)]:
            raise WritError(f"{where}: line {number}: indent with spaces, not tabs")
        if body in ("---", "..."):
            raise WritError(f"{where}: line {number}: one document only")
        out.append(_Line(number, len(stripped) - len(body), body))
    return out


def _strip_comment(line: str) -> str:
    """Drop a `#` comment that is not inside quotes."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _block(lines: list[_Line], start: int, indent: int, where: str) -> tuple[Any, int]:
    """A mapping or a list whose lines all sit at `indent`."""
    if lines[start].text.startswith("- ") or lines[start].text == "-":
        return _sequence(lines, start, indent, where)
    out: dict[str, Any] = {}
    index = start
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        match = _KEY.match(line.text)
        if not match:
            raise _error(where, line, "expected `key: value`")
        key, rest = match.group(1), (match.group(2) or "").strip()
        if key in out:
            raise _error(where, line, f"{key!r} appears twice")
        index += 1
        nested = index < len(lines) and lines[index].indent > indent
        if rest and nested:
            raise _error(where, lines[index], "unexpected indentation")
        if nested:
            out[key], index = _block(lines, index, lines[index].indent, where)
        elif (
            not rest
            and index < len(lines)
            and lines[index].indent == indent
            and lines[index].text.startswith("-")
        ):
            # a list may sit at its key's own indentation
            out[key], index = _sequence(lines, index, indent, where)
        else:
            out[key] = _scalar(rest, where, line)
    if index < len(lines) and lines[index].indent > indent:
        raise _error(where, lines[index], "unexpected indentation")
    return out, index


def _sequence(
    lines: list[_Line], start: int, indent: int, where: str
) -> tuple[list, int]:
    out = []
    index = start
    while (
        index < len(lines)
        and lines[index].indent == indent
        and (lines[index].text.startswith("- ") or lines[index].text == "-")
    ):
        line = lines[index]
        item = line.text[1:].strip()
        if _KEY.match(item) or item.startswith(("[", "{")):
            raise _error(where, line, "a list here holds plain values only")
        out.append(_scalar(item, where, line))
        index += 1
    return out, index


def _scalar(text: str, where: str, line: _Line) -> Any:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text.startswith("["):
        if not text.endswith("]"):
            raise _error(where, line, "a `[` list must close on the same line")
        inner = text[1:-1].strip()
        if not inner:
            return []
        parts = _split(inner, where, line)
        return [_scalar(part.strip(), where, line) for part in parts]
    if text[0] in "'\"":
        if len(text) < 2 or text[-1] != text[0]:
            raise _error(where, line, "unterminated quoted string")
        body = text[1:-1]
        if text[0] == "'":
            return body.replace("''", "'")
        return (
            body.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
            .replace("\\\\", "\\")
        )
    if text[0] in "&*!|>{@`%":
        raise _error(where, line, f"writ's config does not read {text[0]!r} values")
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _split(inner: str, where: str, line: _Line) -> list[str]:
    """Split a flow list on commas outside quotes."""
    parts, current, quote = [], "", ""
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current += char
        elif char == ",":
            parts.append(current)
            current = ""
        elif char in "[]{}":
            raise _error(where, line, "a list here holds plain values only")
        else:
            current += char
    parts.append(current)
    return parts


def _error(where: str, line: _Line, message: str) -> WritError:
    return WritError(f"{where}: line {line.number}: {message}")


def scalar(value: Any) -> str:
    """Write one value so `loads` reads back the same thing."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(scalar(item) for item in value) + "]"
    text = str(value)
    plain = (
        text
        and text == text.strip()
        and _scalar_is_string(text)
        and not any(char in text for char in "#:'\"\n,[]{}")
    )
    if plain:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _scalar_is_string(text: str) -> bool:
    return (
        text not in ("~", "null", "Null", "NULL", "true", "True", "TRUE", "false",
                     "False", "FALSE")
        and not _INT.match(text)
        and not _FLOAT.match(text)
        and text[0] not in "&*!|>{@`%-"
    )
