"""Workspace configuration: parse and render `kanso.toml`.

`kanso.toml` is the operator's file and the packaged template `templates/kanso.toml`
is the reference for its keys and their defaults; this module accepts that shape and
nothing else. An unknown key is a validation failure that names the key, so a typo is
caught when the workspace is opened rather than silently ignored at the point it would
have mattered. Every value here is configuration: credentials are named, never held,
and are resolved elsewhere from the environment.

`[adapters.<id>]` is deliberately free-form. Each adapter validates its own table with
its own model, which keeps every vendor key out of a kanso-owned schema.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic import ValidationError as PydanticValidationError

from kanso.errors import PreconditionError, ValidationError

CONFIG_NAME = "kanso.toml"
_TEMPLATE = "kanso.toml"

Duration = Annotated[str, StringConstraints(pattern=r"^[0-9]+(s|m|h|d|w)$")]
"""A whole number of seconds, minutes, hours, days or weeks."""

_STRICT = ConfigDict(extra="forbid", strict=True)


class ResearchConfig(BaseModel):
    """Defaults for research runs, certification runs and composition backtests."""

    model_config = _STRICT

    capital: float = Field(default=100_000, gt=0)
    broker: str | None = None
    account: Literal["margin", "cash"] = "margin"
    currency: str = "USD"
    return_period: Duration = "1d"
    annualisation: Literal["auto"] | float = "auto"
    align_every: int = Field(default=10, gt=0)
    stall_k: int = Field(default=30, gt=0)
    context_cards: int = Field(default=20, ge=0)
    folds: int = Field(default=4, ge=2)
    max_lines_per_keep: int = Field(default=40, gt=0)
    baseline_budget_s: int = Field(default=1800, gt=0)


class CertifyConfig(BaseModel):
    """Certification policy."""

    model_config = _STRICT

    n_fail: int = Field(default=3, gt=0)


class DataConfig(BaseModel):
    """Data policy: which adapter resolves instruments, and whether prices are adjusted."""

    model_config = _STRICT

    reference: str = "none"
    adjusted: bool = False


class EnvConfig(BaseModel):
    """Operator overrides for the detected lane plan; unset means "use the envelope"."""

    model_config = _STRICT

    reserved_cores: int | None = Field(default=None, ge=0)
    reserved_mem_gb: float | None = Field(default=None, ge=0)
    cores_per_lane: int | None = Field(default=None, gt=0)


class MonitorConfig(BaseModel):
    """Monitoring cadence."""

    model_config = _STRICT

    interval: Duration = "5m"


class WebhookConfig(BaseModel):
    """Where escalations are POSTed; unset falls back to the standard variable name."""

    model_config = _STRICT

    url: str | None = None


# [extensions] and [skills] hold a single key each, flattened onto `Config` so callers
# reach them without a section object that would carry nothing else.
_FLATTENED: dict[str, dict[str, str]] = {
    "extensions": {"paths": "extensions_paths"},
    "skills": {"targets": "skills_targets"},
}


class Config(BaseModel):
    """A parsed `kanso.toml`.

    Defaults match the packaged template, so a file that omits a section behaves as the
    template's rendering of it would. `broker` has no default here: the venue model a
    run inherits names a broker, and the core names none — the template supplies it, and
    a consumer that finds it unset refuses rather than guesses.
    """

    model_config = _STRICT

    kanso_version: str
    schema_version: int = Field(ge=1)
    extensions_paths: list[str] = Field(default_factory=lambda: ["kanso_ext"])
    skills_targets: list[str] = Field(default_factory=list)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    certify: CertifyConfig = Field(default_factory=CertifyConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    env: EnvConfig = Field(default_factory=EnvConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    adapters: dict[str, dict[str, object]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data: Any) -> Any:
        """Lift `[extensions] paths` and `[skills] targets` onto the model."""
        if not isinstance(data, dict):
            return data
        out: dict[str, Any] = dict(data)
        for section, keys in _FLATTENED.items():
            if section not in out:
                continue
            table = out.pop(section)
            if not isinstance(table, dict):
                raise ValueError(f"[{section}] must be a table")
            for key, value in table.items():
                field = keys.get(key)
                if field is None:
                    raise ValueError(f"unknown key '{section}.{key}'")
                out[field] = value
        return out


def load_config(path: Path) -> Config:
    """Parse `kanso.toml` at `path` (a file, or the workspace holding one).

    Raises a precondition failure when the file is absent and a validation failure when
    it is not valid TOML or holds a key or value the template does not describe.
    """
    file = path / CONFIG_NAME if path.is_dir() else path
    try:
        raw = file.read_bytes()
    except FileNotFoundError as exc:
        raise PreconditionError(
            f"no {CONFIG_NAME} at {file}", remedy="run `kanso init` to scaffold a workspace"
        ) from exc
    except OSError as exc:
        raise PreconditionError(f"cannot read {file}: {exc.strerror}") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"{file}: not valid TOML: {exc}") from exc
    try:
        return Config.model_validate(document)
    except PydanticValidationError as exc:
        raise ValidationError(f"{file}: {_explain(exc)}") from exc


def render_config(kanso_version: str) -> str:
    """The packaged `kanso.toml` template with its placeholders filled."""
    text = _template().replace("{{kanso_version}}", kanso_version)
    if "{{" in text:
        raise ValidationError(f"{_TEMPLATE}: unfilled placeholder")
    return text


def _template() -> str:
    return resources.files("kanso.templates").joinpath(_TEMPLATE).read_text(encoding="utf-8")


def _explain(exc: PydanticValidationError) -> str:
    """One human sentence per rejected key, joined; never echoes a value."""
    parts: list[str] = []
    for error in exc.errors():
        where = ".".join(str(p) for p in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        if error["type"] == "extra_forbidden":
            parts.append(f"unknown key '{where}'")
        elif where:
            parts.append(f"{where}: {message}")
        else:
            parts.append(message)
    return "; ".join(parts)
