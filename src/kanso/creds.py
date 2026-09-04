"""Credential resolution.

Every credential kanso needs has a standard variable name, `KANSO_<SUBJECT>_<PURPOSE>`,
where the subject is the id under which its consumer is configured — a model provider, a
data or execution client, a loader — and the purpose is `API_KEY` unless the consumer
declares another. A consumer may name a different variable, which replaces the standard
name rather than adding to it.

A name is resolved at the moment of use: the workspace `.env` first, then the process
environment, first non-empty wins. kanso never writes `.env`, never reads or executes a
shell file, and injects nothing into its own environment, so a value lives only in the
call that asked for it and in the processes deliberately given it. Nothing in this module
puts a value in a return path other than the resolved value itself: not in an error
message, not in a log line, not in a repr.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from kanso.errors import PreconditionError, ValidationError

ENV_FILE = ".env"
PREFIX = "KANSO_"
FROM_ENV_FILE = ".env"
FROM_ENVIRONMENT = "environment"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def standard_name(subject: str, purpose: str = "API_KEY") -> str:
    """The standard variable name for a consumer's credential.

    The subject is upper-cased with every run of characters outside `A-Z0-9` replaced by
    a single underscore and any leading or trailing underscore dropped, so a hyphenated
    id such as `acme-paper` yields `KANSO_ACME_PAPER_API_KEY`. The purpose is
    normalised the same way. An id with nothing to derive a name from is refused, since
    the alternative is a variable the operator cannot set.
    """
    return f"{PREFIX}{_token(subject, 'subject')}_{_token(purpose, 'purpose')}"


def _token(value: str, what: str) -> str:
    token = _NON_ALNUM.sub("_", value).strip("_").upper()
    if not token:
        raise ValidationError(f"credential {what} is empty")
    return token


def parse_env_file(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping.

    `KEY=VALUE` per line. Blank lines and lines whose first non-space character is `#`
    are ignored, an `export ` prefix is accepted, and one matching pair of surrounding
    single or double quotes is stripped. Only the first `=` splits, so a value may hold
    any number of them. Nothing else is interpreted: no escape sequences, no variable
    expansion, and no trailing-comment rule, because a `#` is a legal character in a
    secret. A line with no `=` is skipped, and a repeated key takes its last value.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key:
            continue
        out[key] = _unquote(value.strip())
    return out


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_env_file(workspace: Path) -> dict[str, str]:
    """The workspace `.env`, or an empty mapping when there is none.

    Undecodable bytes are replaced rather than raised on, so a damaged file degrades to
    the process environment instead of failing every command that touches a credential.
    """
    path = workspace / ENV_FILE
    if not path.is_file():
        return {}
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise PreconditionError(f"cannot read {path}: {exc.strerror}") from exc
    return parse_env_file(text)


def _lookup(name: str, workspace: Path) -> tuple[str, str] | None:
    """The value and where it came from, or None."""
    value = read_env_file(workspace).get(name, "")
    if value:
        return value, FROM_ENV_FILE
    value = os.environ.get(name, "")
    if value:
        return value, FROM_ENVIRONMENT
    return None


def resolve(name: str, workspace: Path) -> str | None:
    """The value of `name`, from the workspace `.env` then the process environment."""
    found = _lookup(name, workspace)
    return found[0] if found else None


def origin(name: str, workspace: Path) -> str | None:
    """Where `name` resolves from — `.env`, `environment` — or None when nowhere.

    This is what `doctor` reports: the name and its source, never the value.
    """
    found = _lookup(name, workspace)
    return found[1] if found else None


def require(name: str, workspace: Path) -> str:
    """`resolve`, failing the step when the credential is set in neither place."""
    found = _lookup(name, workspace)
    if found is None:
        raise PreconditionError(
            f"{name} is not set: looked in {workspace / ENV_FILE} and the process environment",
            remedy=f"add {name}=... to {workspace / ENV_FILE}, or export it",
        )
    return found[0]


def scrub(environ: Mapping[str, str], extra: Iterable[str] = ()) -> dict[str, str]:
    """`environ` without credential variables, for a subprocess that must see none.

    Every `KANSO_` variable is dropped, together with any name the caller adds — the
    place where a consumer's own variable name, which need not carry the prefix, is
    removed.
    """
    drop = set(extra)
    return {k: v for k, v in environ.items() if not k.startswith(PREFIX) and k not in drop}
