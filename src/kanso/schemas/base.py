"""Base class, scalar types and error rendering for the workspace data model.

Every model in this package forbids unknown fields. The exceptions are the maps whose
keys belong to somebody else — a strategy's `config`, an instrument's `attributes` and
`override`, a gate's `params` and `evidence`, an adapter's own table — which are
free-form by contract.

A failed validation leaves this package as `kanso.errors.ValidationError` (exit 3) whose
message names the offending field and the reason, whether the model was built directly or
validated from a mapping.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticUndefined

from kanso.errors import ValidationError

SCHEMA_VERSION: Final[Literal[1]] = 1
"""The only workspace schema version this package reads or writes."""

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A content address: 64 lower-case hex digits."""

HypId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{3,40}$")]
"""A hypothesis or strategy id."""

CatalogueId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
"""A construct, objective or gate id."""

NonEmpty = Annotated[str, StringConstraints(min_length=1)]

ParamValue = bool | int | float | str
"""A gate or construct parameter: a YAML scalar chosen by an agent."""

Params = dict[str, ParamValue]
FreeForm = dict[str, Any]

_PREFIXES = ("Value error, ", "Assertion failed, ")


def render_errors(exc: PydanticValidationError) -> str:
    """One line per error, each naming the field path and the reason."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"]
        for prefix in _PREFIXES:
            msg = msg.removeprefix(prefix)
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


_inside = ContextVar("kanso_schema_validating", default=False)
"""True while an outer model is validating, so nested failures aggregate before they leave."""


class KansoModel(BaseModel):
    """A workspace model: unknown fields are refused, failures are `ValidationError`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def __init__(self, **data: Any) -> None:
        if _inside.get():
            super().__init__(**data)
            return
        token = _inside.set(True)
        try:
            super().__init__(**data)
        except PydanticValidationError as exc:
            raise ValidationError(render_errors(exc)) from None
        finally:
            _inside.reset(token)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        token = _inside.set(True)
        try:
            return super().model_validate(obj, **kwargs)
        except PydanticValidationError as exc:
            raise ValidationError(render_errors(exc)) from None
        finally:
            _inside.reset(token)


class KansoRootModel(RootModel[Any]):
    """A model whose YAML document is an id-keyed map rather than a fixed object."""

    def __init__(self, root: Any = PydanticUndefined, **data: Any) -> None:
        token = _inside.set(True)
        try:
            if root is PydanticUndefined:
                super().__init__(**data)
            else:
                super().__init__(root)
        except PydanticValidationError as exc:
            raise ValidationError(render_errors(exc)) from None
        finally:
            _inside.reset(token)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        token = _inside.set(True)
        try:
            return super().model_validate(obj, **kwargs)
        except PydanticValidationError as exc:
            raise ValidationError(render_errors(exc)) from None
        finally:
            _inside.reset(token)


class Versioned(KansoModel):
    """A model persisted as a YAML file, which declares its schema version first."""

    schema_: Literal[1] = Field(default=SCHEMA_VERSION, alias="schema")
