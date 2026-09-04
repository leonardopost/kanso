"""When a datum became public: the rules, keyed by data class, and the checks they license.

Every point kanso writes carries two nanosecond timestamps. `ts_event` is the economic
reference time — the close the bar summarises, the quarter the statement covers.
`ts_init` is the instant the information first became publicly available. NautilusTrader
orders, filters and merges by `ts_init` alone (verified against `nautilus_trader 1.231.0`:
the catalog's query path appends `ts_init >= start` / `ts_init <= end` and `ORDER BY
ts_init`, `BacktestEngine` sorts on `x.ts_init`, and `BacktestDataIterator` merges on a
heap keyed by `ts_init`), so a strategy sees exactly the availability its data was
stamped with and nothing earlier. `ts_init >= ts_event` therefore holds for every point,
and `ts_init` is never taken from `ts_event` and never from the moment of ingest.

**The rules are keyed by data class, not by vendor.** When a corporate action becomes
public is a fact about corporate actions; two vendors carrying the same action do not
make it public at two different instants. Keeping the rules in one module rather than in
each adapter is what makes them auditable and what stops a vendor's convenience from
becoming a research result.

**Rules are conservative by construction.** Where the exact dissemination instant is not
knowable from the reference time alone, a rule takes the later bound: a `ts_init` that is
too late can only cost a backtest an opportunity, while a `ts_init` that is too early
leaks the future into it. Rules whose publication instant cannot be derived at all carry
no lag; for those, the loader must supply a `ts_init` it obtained from the source, and
this module verifies it is strictly later than the reference time rather than inventing
one.

**Windows are primed, not truncated.** A series that only changes when it is published —
a corporate action, a filing, a statistical release — has no value at the first instant
of a window unless the last point published before the window opens is loaded too. That
point is an input to evaluation, never a point inside the window: it dates from before
the window and is loaded so that "the value known at time t" is answerable at t = the
window's open. Without it the answer is missing for as long as the publication interval,
which for quarterly fundamentals is a quarter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from kanso.errors import ValidationError

NANOS_PER_SECOND: Final = 1_000_000_000


@dataclass(frozen=True, slots=True)
class PublicationRule:
    """How a data class reaches the public, stated once for every vendor that carries it.

    `lag` is the delay between the reference time and the moment the information becomes
    public, where that delay is a property of the data class. `lag=None` means the
    instant is a property of the individual datum — a filing date, a press release — and
    must come from the source; such a rule stamps nothing and only checks.

    `primed` marks a series that changes only when it is published, and whose windows are
    therefore primed with the last point published before the window opens.
    """

    id: str
    description: str
    lag: timedelta | None
    primed: bool = False

    @property
    def derived(self) -> bool:
        """True when this rule can compute an availability instant from a reference time."""
        return self.lag is not None

    def available_at(self, ts_event: int) -> int:
        """The instant a point with this reference time became public, in nanoseconds."""
        if self.lag is None:
            raise ValidationError(
                f"publication rule {self.id!r} derives no availability instant; the source "
                "must supply ts_init for every point of this data class"
            )
        return ts_event + int(self.lag.total_seconds() * NANOS_PER_SECOND)


PUBLICATION_RULES: Final[dict[str, PublicationRule]] = {
    rule.id: rule
    for rule in (
        PublicationRule(
            id="delayed_quote",
            description=(
                "a consolidated quote carried under a delayed, non-professional entitlement. "
                "The exchange vendor agreements that govern such feeds set the delay at 15 "
                "minutes, so a quote timestamped at its exchange time is public a quarter of "
                "an hour later."
            ),
            lag=timedelta(minutes=15),
        ),
        PublicationRule(
            id="delayed_trade",
            description=(
                "a consolidated trade print carried under a delayed, non-professional "
                "entitlement: the same 15 minutes as the quote tier it travels with."
            ),
            lag=timedelta(minutes=15),
        ),
        PublicationRule(
            id="official_close",
            description=(
                "the official closing price of a session, which is struck in the closing "
                "auction and reaches the consolidated tape within minutes of the close. The "
                "exact dissemination instant varies by venue and by day, so the rule takes "
                "the conservative bound of 15 minutes after the reference close: a later "
                "availability can cost an opportunity, an earlier one leaks the future."
            ),
            lag=timedelta(minutes=15),
        ),
        PublicationRule(
            id="settlement_price",
            description=(
                "a derivatives settlement price, which the clearing house computes from the "
                "closing range and publishes after the session. The publication follows the "
                "clearing house's own schedule, so the rule takes the conservative bound of "
                "one hour after the reference close."
            ),
            lag=timedelta(hours=1),
        ),
        PublicationRule(
            id="corporate_action",
            description=(
                "an issuer corporate action. Its reference time is the effective or ex date; "
                "it becomes public when the issuer announces it and the venue disseminates "
                "the notice, which is days to months earlier and cannot be derived from the "
                "effective date. The source must supply the announcement instant."
            ),
            lag=None,
            primed=True,
        ),
        PublicationRule(
            id="fundamental",
            description=(
                "an issuer's periodic financial statement. Its reference time is the end of "
                "the period it covers; it becomes public when the filing is accepted and "
                "disseminated, weeks later and on no fixed offset. The source must supply "
                "the filing instant, and restatements arrive as further points with a later "
                "availability rather than as overwrites."
            ),
            lag=None,
            primed=True,
        ),
        PublicationRule(
            id="economic_release",
            description=(
                "a scheduled statistical release. Its reference time is the period it "
                "measures and its availability is the release instant published in the "
                "issuing agency's calendar, which the source must supply."
            ),
            lag=None,
            primed=True,
        ),
    )
}
"""Every publication rule kanso knows, keyed by the data class it governs."""


def resolve(rule: str | PublicationRule) -> PublicationRule:
    """The declared rule, given it or the data class that names it."""
    if isinstance(rule, PublicationRule):
        return rule
    found = PUBLICATION_RULES.get(rule)
    if found is None:
        known = ", ".join(sorted(PUBLICATION_RULES))
        raise ValidationError(
            f"publication_rule: {rule!r} is not a declared publication rule (known: {known})",
            remedy="declare the rule in kanso.data.publication, keyed by its data class",
        )
    return found


def primes(rule: str | PublicationRule | None) -> bool:
    """True when loading a window of this data class must also load its primer."""
    return rule is not None and resolve(rule).primed


def check_availability(points: Iterable[Any]) -> None:
    """Refuse any point whose availability precedes its reference time.

    `ts_init >= ts_event` is the invariant the whole loading path rests on: the engine
    delivers a point at its `ts_init`, so a `ts_init` before the reference time would
    hand a strategy information that did not exist yet.
    """
    for index, point in enumerate(points):
        if point.ts_init < point.ts_event:
            raise ValidationError(
                f"point {index} of {type(point).__name__} has ts_init {point.ts_init} before "
                f"ts_event {point.ts_event}; availability cannot precede the reference time"
            )


def check_delayed(points: Iterable[Any], rule: str | PublicationRule | None) -> PublicationRule:
    """Refuse a delayed dataset whose availability did not come from a declared rule.

    A delayed dataset with no rule, with a rule nobody declared, or holding a point whose
    availability equals its reference time, is a dataset whose `ts_init` was copied from
    `ts_event` or from the moment of ingest. Either way the delay it declares is not in
    its timestamps, and a backtest over it would trade on information it did not have.
    """
    if rule is None:
        raise ValidationError(
            "publication_rule: a delayed dataset must name the rule its availability "
            "timestamps were derived from",
            remedy=f"declare one of {', '.join(sorted(PUBLICATION_RULES))}",
        )
    declared = resolve(rule)
    for index, point in enumerate(points):
        if point.ts_init == point.ts_event:
            raise ValidationError(
                f"point {index} of {type(point).__name__} has ts_init equal to ts_event "
                f"({point.ts_event}), so the {declared.id!r} delay is not in its timestamps"
            )
        if declared.derived and point.ts_init != declared.available_at(point.ts_event):
            raise ValidationError(
                f"point {index} of {type(point).__name__} has ts_init {point.ts_init}, which "
                f"rule {declared.id!r} does not derive from ts_event {point.ts_event} "
                f"({declared.available_at(point.ts_event)})"
            )
    return declared


def stamp(points: Iterable[Any], rule: str | PublicationRule) -> Iterator[Any]:
    """Re-stamp each point's availability from the rule, leaving its reference time alone.

    Nautilus data objects are immutable, so a point whose `ts_init` the rule changes is
    rebuilt through the class's own `to_dict`/`from_dict` pair — which every `Data`
    subclass and every `@customdataclass` type in `nautilus_trader 1.231.0` provides, and
    which round-trips the two timestamps independently. A rule that derives no instant
    stamps nothing: its points must already carry the availability the source published,
    and each is checked instead.
    """
    declared = resolve(rule)
    for index, point in enumerate(points):
        if not declared.derived:
            if point.ts_init <= point.ts_event:
                raise ValidationError(
                    f"point {index} of {type(point).__name__} carries no availability later "
                    f"than its reference time {point.ts_event}, and rule {declared.id!r} derives "
                    "none; the source must supply ts_init for this data class"
                )
            yield point
            continue
        ts_init = declared.available_at(point.ts_event)
        yield point if point.ts_init == ts_init else restamp(point, ts_init)


def restamp(point: Any, ts_init: int) -> Any:
    """The same point with a new availability timestamp.

    Rebuilt rather than mutated: `nautilus_trader 1.231.0` data objects are Cython
    extension types with read-only timestamps. `to_dict` is reached through the class so
    that both shapes the engine ships work — the static `Cls.to_dict(obj)` of the built-in
    types and the bound `to_dict(self)` of a `@customdataclass` type.
    """
    cls = type(point)
    if not (hasattr(cls, "to_dict") and hasattr(cls, "from_dict")):
        raise ValidationError(
            f"{cls.__name__} has no to_dict/from_dict pair, so its availability cannot be "
            "re-stamped; the loader must construct it with the right ts_init"
        )
    values = dict(cls.to_dict(point))
    values["ts_init"] = ts_init
    return cls.from_dict(values)


def last_before(points: Iterable[Any], ts_ns: int) -> Any | None:
    """The primer: the last point published strictly before `ts_ns`, or `None`.

    Publication order is availability order, so the primer is the latest `ts_init` below
    the window's opening instant. It is returned apart from the window's own points
    because it is an evaluation input, not a member of the window.
    """
    primer: Any | None = None
    for point in points:
        if point.ts_init < ts_ns and (primer is None or point.ts_init >= primer.ts_init):
            primer = point
    return primer
