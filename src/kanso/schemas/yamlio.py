"""Reading and writing the workspace YAML files.

Every workspace file except the id-keyed instrument cache declares `schema: 1` as its
first key. A file that omits it, or names a version this package does not read, is refused
here rather than half-parsed downstream; the key is emitted first on the way out, so a
file kanso writes reads back the way it was written.

Dates and timestamps are emitted as quoted strings, so a file round-trips through YAML
without a loader silently converting an offset to something else. A hand-written file may
still spell a date bare: the schema accepts both.
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
