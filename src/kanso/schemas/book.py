"""The shared book block: one capital and one room for every member of a book.

A book is several strategy versions run on one account, handed room from one capital by one
ledger (`kanso.nautilus.book`). The block is the operator's declaration of that capital, the
leverage each segment of the session gives it, the cap on any one name, and how the members
are ranked against each other. Nothing in it has a default an agent's choice would otherwise
fill: the leverage the operator rules on is required and refused at any other value, and the
ranking — `rank_key` and `k_min` — is required, because a plan that leaves the order of its
members to the framework has let the framework decide it.

The leverage is a multiple of the smaller of the capital and the book's balance, the capital
plus the realised net profit of every member. Regular hours are 09:30–16:00 New York time;
before and after them the leverage is the operator's to set and is not set yet, so a block
that declares either is refused and every entry outside regular hours is refused by the
ledger. `overnight` is the room a position held past 16:00 has, declared at its one value.

There is no daily stop. The book's daily exit — `daily_target_bp` and its marked profit — is
a later version's; a block that names it, or a stop, is refused rather than read as if it
did something.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import Field, model_validator

from kanso.schemas.base import KansoModel

__all__ = [
    "OVERNIGHT_LEVERAGE",
    "RANK_KEYS",
    "RTH_LEVERAGE",
    "BookLeverage",
    "SharedBook",
]

RTH_LEVERAGE: Final = 4.0
"""The leverage regular hours give the book: the operator's standing four times intraday."""

OVERNIGHT_LEVERAGE: Final = 2.0
"""The leverage a position held past the close has: the operator's two times overnight."""

RANK_KEYS: Final = ("net_per_trade_bp",)
"""What a book may rank its members by: each certificate's net per trade over its
certification window (`kanso.schemas.certification.NetPerTrade`)."""

_NOT_BUILT: Final = {
    "daily_stop_bp": "a book has no daily stop, so a block that names one is refused",
    "daily_target_bp": "the book's daily exit is not built in this version, so a block that "
    "names one is refused rather than read as if it closed the day",
    "daily_exit_pnl": "the book's daily exit is not built in this version, so a block that "
    "names its basis is refused",
}


class BookLeverage(KansoModel):
    """The book's leverage in each segment of the session, × min(capital, balance)."""

    rth: float
    """Regular hours, 09:30–16:00 New York: four, and nothing else."""
    pre: float | None = None
    """Before 09:30: the operator's to set, and not set, so any value is refused."""
    post: float | None = None
    """After 16:00: the operator's to set, and not set, so any value is refused."""
    overnight: float | None = None
    """A position held past 16:00: two when declared."""

    @model_validator(mode="after")
    def _the_operator_s_values(self) -> BookLeverage:
        if self.rth != RTH_LEVERAGE:
            raise ValueError(f"rth: {self.rth:g} is not the book's {RTH_LEVERAGE:g}x intraday")
        for segment in ("pre", "post"):
            if getattr(self, segment) is not None:
                raise ValueError(
                    f"{segment}: the leverage outside regular hours is not set yet, so no value "
                    "is accepted and every entry outside them is refused"
                )
        if self.overnight is not None and self.overnight != OVERNIGHT_LEVERAGE:
            raise ValueError(
                f"overnight: {self.overnight:g} is not the book's {OVERNIGHT_LEVERAGE:g}x overnight"
            )
        return self

    @property
    def largest(self) -> float:
        """The largest leverage the block declares: what the venue's account is opened at, so
        the ledger's room, and never the venue's margin, is what binds."""
        return max(value for value in (self.rth, self.overnight) if value is not None)


class SharedBook(KansoModel):
    """One capital and one room for every member of a book."""

    capital: float = Field(gt=0)
    """The book's capital in the account currency: every member is run, and certified, at it."""
    leverage: BookLeverage
    max_name_c: float | None = Field(default=None, gt=0)
    """The most the book may hold in one name — open, resting and in flight — over the
    capital; absent, no name is capped beyond the room."""
    rank_key: Literal["net_per_trade_bp"]
    """What the members are ranked by: required, so the order is the plan's."""
    k_min: int = Field(ge=1)
    """The fewest sessions a member's certification window must have closed trades on for
    its `rank_key` to rank it; a member below it ranks after every ranked one."""

    @model_validator(mode="before")
    @classmethod
    def _nothing_unbuilt(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, why in _NOT_BUILT.items():
                if key in data:
                    raise ValueError(f"{key}: {why}")
        return data
