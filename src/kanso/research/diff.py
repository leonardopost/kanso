"""Unified diffs between two versions of one file, computed and applied in-package.

The research loop moves `strategy.py` forward one diff at a time, and the diff is the
whole of what a proposer may return: a file would be a rewrite, and a rewrite is not one
experiment. So a diff is the unit both directions — `unified` renders one between two
blobs for a prompt, and `apply` puts one back on the lane copy.

Both are the standard library and nothing else. kanso never invokes git and never shells
out to `patch`: the lane copy is a file kanso versions itself, so patching it is
arithmetic on lines rather than a tool the host may or may not have.

**Applying is a judgement, not a best effort.** A hunk finds its place by the lines it
claims to replace, at the position it names or, failing that, at the first place those
lines actually occur — the offset tolerance a hand-written diff needs and no more. Nothing
is fuzzed and nothing is partially applied: either every hunk lands and the result is the
file the proposer described, or the diff is refused with the hunk that failed. A refusal
is the caller's signal that the answer was invalid, which is what puts it on the retry
ladder rather than into a card.

**The file is the scope.** A diff naming any file but the one it was asked for is refused
before a line is examined, because the scope rule that keeps a card to `strategy.py` is
worth nothing if a diff can rename its way out of it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Final

from kanso.errors import ValidationError

__all__ = ["HUNK_HEADER", "STRATEGY", "apply", "unified"]

STRATEGY: Final = "strategy.py"
"""The one file the research loop may change, and so the one a diff may name."""

HUNK_HEADER: Final = r"^@@ -(?P<old>\d+)(?:,(?P<oldn>\d+))? \+(?P<new>\d+)(?:,(?P<newn>\d+))? @@"
"""A unified hunk header. The counts are optional, as they are when a hunk is one line."""

_HUNK: Final = re.compile(HUNK_HEADER)

_FILE_HEADERS: Final = ("--- ", "+++ ")
_STRIP_PREFIXES: Final = ("a/", "b/", "./")
_FENCE: Final = "```"
_NO_NEWLINE: Final = "\\"


@dataclass(frozen=True)
class _Hunk:
    """One hunk: where it claims to be, what it expects there, what it leaves behind."""

    old_start: int
    before: tuple[str, ...]
    after: tuple[str, ...]


def unified(before: bytes, after: bytes, path: str = STRATEGY) -> str:
    """The unified diff from `before` to `after`, as a prompt or a record reads one.

    Decoding is lenient because the result is text for a reader; `apply` is strict
    because its result is a file.
    """
    return "".join(
        difflib.unified_diff(
            before.decode("utf-8", errors="replace").splitlines(keepends=True),
            after.decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def apply(source: bytes, patch: str, path: str = STRATEGY) -> bytes:
    """`source` with `patch` applied, or a validation failure naming what did not fit."""
    hunks = _parse(patch, path)
    lines, trailing = _split(source)
    out: list[str] = []
    cursor = 0
    for number, hunk in enumerate(hunks, start=1):
        at = _locate(lines, hunk, cursor, number)
        out.extend(lines[cursor:at])
        out.extend(hunk.after)
        cursor = at + len(hunk.before)
    out.extend(lines[cursor:])
    return ("\n".join(out) + ("\n" if trailing else "")).encode("utf-8")


def _parse(patch: str, path: str) -> list[_Hunk]:
    """The hunks of a patch, refusing one that names another file or holds none."""
    lines = [line for line in patch.splitlines() if not line.startswith(_FENCE)]
    while lines and not lines[-1].strip():
        lines.pop()
    for line in lines:
        if line.startswith(_FILE_HEADERS):
            _check_path(line, path)
    starts = [index for index, line in enumerate(lines) if line.startswith("@@")]
    if not starts:
        raise ValidationError(
            "diff: holds no hunk, so it changes nothing",
            remedy=f"return a unified diff of {path} with at least one @@ hunk",
        )
    bounds = [*starts[1:], len(lines)]
    return [
        _hunk(lines[start], lines[start + 1 : end], number)
        for number, (start, end) in enumerate(zip(starts, bounds, strict=True), start=1)
    ]


def _hunk(header: str, body: list[str], number: int) -> _Hunk:
    """One hunk from its header and the lines under it."""
    match = _HUNK.match(header)
    if match is None:
        raise ValidationError(
            f"diff: hunk {number} has no @@ -old +new @@ header ({header.strip()!r})",
            remedy="write each hunk header as '@@ -<line>,<count> +<line>,<count> @@'",
        )
    before: list[str] = []
    after: list[str] = []
    for line in body:
        if line.startswith(_FILE_HEADERS) or line.startswith("diff "):
            break
        if line.startswith(_NO_NEWLINE):
            continue
        marker, text = (line[:1], line[1:]) if line else (" ", "")
        if marker == " ":
            before.append(text)
            after.append(text)
        elif marker == "-":
            before.append(text)
        elif marker == "+":
            after.append(text)
        else:
            raise ValidationError(
                f"diff: hunk {number} holds {line!r}, which is neither context, a removal "
                "nor an addition",
                remedy="prefix every line of a hunk with a space, a '-' or a '+'",
            )
    return _Hunk(old_start=int(match.group("old")), before=tuple(before), after=tuple(after))


def _check_path(header: str, path: str) -> None:
    """Refuse a diff whose file header names anything but the file being researched."""
    named = header[4:].split("\t")[0].strip()
    stripped = named
    for prefix in _STRIP_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
    if stripped.rsplit("/", 1)[-1] != path:
        raise ValidationError(
            f"diff: names {named!r}; only {path} may change",
            remedy=f"return a diff of {path} alone",
        )


def _split(source: bytes) -> tuple[list[str], bool]:
    """The file as lines, and whether it ends with a newline, so joining is exact."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(
            "diff: the file being patched is not valid UTF-8",
            remedy="restore the lane copy from its blob",
        ) from None
    if not text:
        return [], False
    parts = text.split("\n")
    trailing = parts[-1] == ""
    return (parts[:-1] if trailing else parts), trailing


def _locate(lines: list[str], hunk: _Hunk, cursor: int, number: int) -> int:
    """Where this hunk applies: the line it names, else the first place its lines occur."""
    if not hunk.before:
        if cursor <= hunk.old_start <= len(lines):
            return hunk.old_start
        raise _refuse(hunk, number, f"line {hunk.old_start} is not a place to insert")
    wanted = list(hunk.before)
    preferred = max(cursor, hunk.old_start - 1)
    if lines[preferred : preferred + len(wanted)] == wanted:
        return preferred
    for at in range(cursor, len(lines) - len(wanted) + 1):
        if lines[at : at + len(wanted)] == wanted:
            return at
    raise _refuse(hunk, number, f"{hunk.before[0]!r} is not at line {hunk.old_start} or after it")


def _refuse(hunk: _Hunk, number: int, why: str) -> ValidationError:
    return ValidationError(
        f"diff: hunk {number} does not apply cleanly: {why}",
        remedy="diff against the exact bytes given, with three lines of context",
    )
