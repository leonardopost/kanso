"""`models.yaml`: the LLM register and the routing table.

The register lists the models a workspace may use; the routing table says which tier,
which thinking effort and which output cap each of the four task classes gets. The classes
are fixed — a fifth would be a call site the package does not have — so an unknown key is
refused rather than ignored.

Every entry is a name, never a value: `api_key_env` overrides the standard credential
variable name and is checked to look like one, because a key pasted there would end up in
a file the operator commits.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, model_validator

from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty, Versioned

Tier = Literal["cheap", "mid", "frontier"]
Protocol = Literal["anthropic", "openai_compat", "mock"]
Effort = Literal["none", "low", "medium", "high"]
TaskClass = Literal["classify", "propose", "align_check", "certify_plan"]

TIERS: tuple[Tier, ...] = ("cheap", "mid", "frontier")
TASK_CLASSES: tuple[TaskClass, ...] = ("classify", "propose", "align_check", "certify_plan")

EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class Route(KansoModel):
    """A fully resolved route: what the router actually sends."""

    tier: Tier
    effort: Effort
    max_output: int = Field(gt=0)


ROUTING_DEFAULTS: Final[dict[TaskClass, Route]] = {
    "classify": Route(tier="frontier", effort="high", max_output=1024),
    "certify_plan": Route(tier="frontier", effort="high", max_output=4096),
    "propose": Route(tier="mid", effort="medium", max_output=4096),
    "align_check": Route(tier="cheap", effort="none", max_output=256),
}
"""Spend where a wrong answer is dearest; think nothing where a rule already decided."""


class ModelSpec(KansoModel):
    """One model: how to reach it, what it costs and which tiers it may serve."""

    id: NonEmpty
    provider: NonEmpty
    protocol: Protocol
    base_url: NonEmpty | None = None
    api_key_env: EnvVarName | None = None
    tier: Tier | list[Tier]
    local: bool
    ctx: int = Field(gt=0)
    cost_in: float = Field(ge=0)
    cost_out: float = Field(ge=0)
    tools: bool
    script: NonEmpty | None = None

    @property
    def tiers(self) -> tuple[Tier, ...]:
        """The tiers this model serves, however the file spelled them."""
        return (self.tier,) if isinstance(self.tier, str) else tuple(self.tier)

    @model_validator(mode="after")
    def _tiers_unique(self) -> ModelSpec:
        if len(set(self.tiers)) != len(self.tiers):
            raise ValueError("tier: repeats a tier")
        if not self.tiers:
            raise ValueError("tier: names no tier")
        return self


class RoutingEntry(KansoModel):
    """A partial route: an absent field takes its task class's default."""

    tier: Tier | None = None
    effort: Effort | None = None
    max_output: int | None = Field(default=None, gt=0)


class Routing(KansoModel):
    """The four task classes, and nothing else."""

    classify: RoutingEntry | None = None
    propose: RoutingEntry | None = None
    align_check: RoutingEntry | None = None
    certify_plan: RoutingEntry | None = None

    def route(self, task: TaskClass) -> Route:
        """The resolved route for one task class, filling absent fields with its default."""
        default = ROUTING_DEFAULTS[task]
        entry: RoutingEntry | None = getattr(self, task)
        if entry is None:
            return default
        return Route(
            tier=entry.tier or default.tier,
            effort=entry.effort or default.effort,
            max_output=entry.max_output or default.max_output,
        )


class ModelsFile(Versioned):
    """The register and the routing table of one workspace."""

    models: list[ModelSpec] = Field(default_factory=list)
    routing: Routing = Field(default_factory=Routing)

    def routes(self) -> dict[TaskClass, Route]:
        """Every task class resolved against the defaults."""
        return {task: self.routing.route(task) for task in TASK_CLASSES}

    def for_tier(self, tier: Tier) -> list[ModelSpec]:
        """Every model serving a tier, in file order."""
        return [m for m in self.models if tier in m.tiers]

    @model_validator(mode="after")
    def _ids_unique(self) -> ModelsFile:
        ids = [m.id for m in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("models: names a model id twice")
        return self


def check_tier_coverage(register: ModelsFile) -> None:
    """Every tier needs a model, since a route escalates one tier and never skips.

    Checked where a task class is about to be called, not at load, so a half-configured
    workspace can still be inspected.
    """
    missing = [tier for tier in TIERS if not register.for_tier(tier)]
    if missing:
        raise ValidationError(
            f"models: no model serves the {', '.join(missing)} tier; every tier needs one"
        )
