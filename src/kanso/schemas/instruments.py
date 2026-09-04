"""`instruments.yaml`: the resolved-instrument cache, its provenance and its overrides.

The registry of record is the catalog's instrument store; this file is its human-readable
pin. It is written by instrument resolution and hand-edited only for `override`,
`attributes`, `corporate_actions` and `manual`.

The venue in `nautilus_id` is the listing venue and is the same in backtest, sandbox,
broker paper and live: a broker is never a venue, because a forked instrument id would
break positions, risk checks and reconciliation. A symbol may itself contain dots, so the
venue is the part after the last one, as the engine parses it (nautilus_trader 1.231.0).

A `manual` entry suppresses resolution, so it must carry the constructor fields itself,
in `override`; which fields an asset class needs is the business of construction, not of
this file.

Unlike every other workspace file this one carries no `schema` key: it is keyed by
instrument id all the way down, and a reserved top-level id would be a trap.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from kanso.schemas.base import (
    FreeForm,
    KansoModel,
    KansoRootModel,
    NonEmpty,
    Sha256,
)

CorporateActions = Literal["adjust_all", "none"]

NautilusId = Annotated[str, StringConstraints(pattern=r"^[^\s.]+(\.[^\s.]+)*\.[A-Z0-9]{1,16}$")]
"""`<SYMBOL>.<VENUE>`, the venue being everything after the last dot."""

InstrumentKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


class Resolved(KansoModel):
    """Who resolved this instrument, as of when, and the checksum of what they produced."""

    adapter: NonEmpty
    as_of: date
    at: datetime
    checksum: Sha256


class InstrumentEntry(KansoModel):
    """One instrument: its engine identity, its provenance and the operator's corrections."""

    nautilus_id: NautilusId
    asset_class: NonEmpty
    resolved: Resolved | None = None
    override: FreeForm = Field(default_factory=dict)
    manual: bool = False
    corporate_actions: CorporateActions
    attributes: FreeForm = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)

    @property
    def symbol(self) -> str:
        """The part of `nautilus_id` before the last dot."""
        return self.nautilus_id.rsplit(".", 1)[0]

    @property
    def venue(self) -> str:
        """The listing venue: the part of `nautilus_id` after the last dot."""
        return self.nautilus_id.rsplit(".", 1)[1]

    @field_validator("asset_class")
    @classmethod
    def _known_asset_class(cls, value: str) -> str:
        from nautilus_trader.model.enums import AssetClass

        names = {member.name for member in AssetClass}
        if value not in names:
            raise ValueError(
                f"{value!r} is not an engine asset class; expected one of "
                f"{', '.join(sorted(names))}"
            )
        return value

    @model_validator(mode="after")
    def _manual_is_self_sufficient(self) -> InstrumentEntry:
        if self.manual:
            if self.resolved is not None:
                raise ValueError("resolved: a manual entry is never resolved")
            if not self.override:
                raise ValueError(
                    "override: a manual entry carries its own constructor fields, and this "
                    "one carries none"
                )
        return self


class InstrumentsFile(KansoRootModel):
    """The whole file: instrument id to entry."""

    root: dict[InstrumentKey, InstrumentEntry] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> InstrumentEntry:
        return self.root[key]

    def __contains__(self, key: str) -> bool:
        return key in self.root

    def __len__(self) -> int:
        return len(self.root)

    def venues(self) -> dict[str, str]:
        """The listing venue of every entry, keyed by instrument id."""
        return {key: entry.venue for key, entry in self.root.items()}

    @model_validator(mode="after")
    def _ids_are_unambiguous(self) -> InstrumentsFile:
        seen: dict[str, str] = {}
        for key, entry in self.root.items():
            clash = seen.get(entry.nautilus_id)
            if clash is not None:
                raise ValueError(
                    f"{key}: nautilus_id {entry.nautilus_id} is already used by {clash}"
                )
            seen[entry.nautilus_id] = key
        return self
