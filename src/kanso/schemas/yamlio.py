"""Reading and writing the workspace YAML files.

Every workspace file except the id-keyed instrument cache declares `schema: 1` as its
first key. A file that omits it, or names a version this package does not read, is refused
here rather than half-parsed downstream; the key is emitted first on the way out, so a
file kanso writes reads back the way it was written.

Dates and timestamps are emitted as quoted strings, so a file round-trips through YAML
without a loader silently converting an offset to something else. A hand-written file may
still spell a date bare: the schema accepts both.

Every key of every mapping in a workspace file is text. YAML reads a bare `null` or `~` key
as no value, a bare `on`, `yes` or `true` as a boolean, and a bare number or date as such, so
such a key — `null:` where `null_by:` was meant, `on:` where `followers:` was — is refused here,
by its place in the file and by what YAML read it as, before any schema sees it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from kanso.errors import ValidationError
from kanso.schemas.base import SCHEMA_VERSION, Versioned


def parse_yaml[M: BaseModel](model: type[M], text: str, origin: str = "yaml") -> M:
    """Validate a YAML document as `model`, naming `origin` in any failure."""
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"{origin}: not valid YAML: {exc}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError(
            f"{origin}: expected a mapping at the top level, got {type(data).__name__}"
        )
    _check_keys(data, origin)
    if issubclass(model, Versioned):
        _check_schema(data, origin)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValidationError(f"{origin}: {exc.message}", exc.remedy) from None


def load_yaml[M: BaseModel](model: type[M], path: Path) -> M:
    """Read and validate a workspace file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"{path}: cannot be read: {exc}") from None
    return parse_yaml(model, text, str(path))


def dump_yaml(obj: BaseModel) -> str:
    """Render a model as the YAML a workspace file holds, `schema` first, no nulls."""
    data = obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def write_yaml(obj: BaseModel, path: Path) -> Path:
    """Write a model to a workspace file, replacing it in one step."""
    text = dump_yaml(obj)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


def _check_keys(node: Any, origin: str, place: tuple[str, ...] = ()) -> None:
    """Refuse the first mapping key, in the file's order, that YAML read as anything but text,
    naming where it is."""
    if isinstance(node, list):
        for n, item in enumerate(node):
            _check_keys(item, origin, (*place, str(n)))
    elif isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise ValidationError(
                    f"{origin}: {_where(place)}the key {_key(key)} is read by YAML as "
                    f"{_kind(key)}, and every key of a workspace file is text",
                    'quote the key, e.g. "on": rather than on:, or name the field as the '
                    "file's schema does",
                )
            _check_keys(value, origin, (*place, key))


def _where(place: tuple[str, ...]) -> str:
    return f"{'.'.join(place)}: " if place else ""


def _key(key: Any) -> str:
    if key is None:
        return "null"
    return str(key).lower() if isinstance(key, bool) else str(key)


def _kind(key: Any) -> str:
    if key is None:
        return "no value — a bare null, ~ or nothing"
    if isinstance(key, bool):
        return "a boolean — a bare on, off, yes, no, true or false"
    if isinstance(key, int | float):
        return "a number"
    return f"a {type(key).__name__}"


def _check_schema(data: dict[str, Any], origin: str) -> None:
    if "schema" not in data:
        raise ValidationError(
            f"{origin}: schema: missing; every kanso workspace file declares "
            f"`schema: {SCHEMA_VERSION}`"
        )
    version = data["schema"]
    if version != SCHEMA_VERSION:
        raise ValidationError(
            f"{origin}: schema: {version!r} is not a schema version this kanso reads "
            f"(it reads {SCHEMA_VERSION})"
        )
