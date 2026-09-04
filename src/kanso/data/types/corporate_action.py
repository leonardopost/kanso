"""The built-in `CorporateAction` custom data type.

A corporate action is the record of an issuer event that changes what a share is:
a split, a cash or stock dividend, a spin-off, a rights issue, a merger or a plain
symbol change. kanso loads unadjusted prices and carries the actions beside them, so
an adjustment is a function of the two rather than a property baked into a price
series by whoever served it.

The two engine timestamps mean here exactly what they mean everywhere: `ts_event` is
the instant this record refers to and `ts_init` the instant it became public, with
`ts_init >= ts_event`. An issuer announces an action weeks before it takes effect, so
the announcement and the ex-date are two different instants and neither is the other's
availability. The announcement is the point: `ts_event` and `ts_init` are both the
announcement instant, because that is when the fact existed and when it was knowable,
and the ex-date travels as `ex_date_ns`, a field, where a strategy can read it the day
it is announced. Stamping the ex-date as `ts_event` and the announcement as `ts_init`
would invert the invariant and hand the engine a point that is public before it
happened; a source that knows only the ex-date stamps all three with it.

`ratio` and `cash` are the two numbers an adjustment needs. `ratio` is shares held
after the event per share held before — 4.0 for a four-for-one split, 0.25 for a
one-for-four reverse split, 1.0 for a pure cash event. `cash` is the cash paid per
share held before the event, in `currency` — 0.0 for a pure share event. A price
adjustment factor follows from those and the price at the time, which is why neither
a factor nor an adjusted price is stored here.

Restatements are additional points with a later `ts_init`, never overwrites, so "the
action known at time t" is the latest point at or before t.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
A custom data type is a `nautilus_trader.core.data.Data` subclass decorated with
`@customdataclass` (`nautilus_trader.model.custom`). The decorator synthesises the
constructor, the dict, bytes and Arrow conversions and a `_schema`, and registers the
class for msgspec and Arrow, which is what lets the catalog persist it; a read returns
it wrapped in `CustomData`. Field annotations are restricted to `InstrumentId`, `str`,
`bool`, `float`, `int`, `bytes`, `ndarray` and `dict` — `Decimal` and `datetime` raise
`TypeError` at class definition — so a ratio and a cash amount travel as `float` and a
currency as `str`.

**This module must not use `from __future__ import annotations`.** The decorator reads
`cls.__annotations__` verbatim and resolves nothing, so postponed annotations reach it
as strings and it raises `TypeError: Unsupported custom data annotation: 'str'`.
Registration is also keyed by the bare class name across the whole process, so this
class is defined exactly once, here, and imported everywhere else.
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

TYPE_ID = "corporate_action"
"""The id under which this type is registered and named in `data_requirements`."""

KINDS = (
    "split",
    "dividend",
    "stock_dividend",
    "spinoff",
    "rights",
    "merger",
    "symbol_change",
)
"""The event kinds kanso recognises; a loader may carry any string, these are the ones
every part of kanso understands."""


@customdataclass
class CorporateAction(Data):  # type: ignore[misc]
    """An issuer event against one instrument.

    `ts_event` and `ts_init` are the instant the event was announced; `ex_date_ns` is
    the instant it takes effect.
    """

    instrument_id: InstrumentId
    kind: str
    ratio: float
    cash: float
    currency: str
    ex_date_ns: int
