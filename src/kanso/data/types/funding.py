"""The built-in `Funding` custom data type.

A funding point is the record of one settlement of a perpetual swap's funding: the
periodic payment between longs and shorts that keeps a perpetual's price near its index.
`rate` is the **realised** rate of the period that just settled, as a fraction of the
position's notional — `0.0001` is one basis point, paid by longs to shorts when positive
and by shorts to longs when negative. It is never the rate a venue publishes for the
period in progress: that is a prediction, which moves until the instant it settles, and a
series of predictions read as payments charges a book what it was never charged.

The two engine timestamps mean here exactly what they mean everywhere: `ts_event` is the
instant this record refers to and `ts_init` the instant it became public, with
`ts_init >= ts_event`. A funding payment is both at once: the settlement instant is when
the rate stopped moving and when the payment was made, so `ts_event` and `ts_init` are
both that instant. A realised rate cannot be public before it settled, and a point whose
`ts_init` precedes its `ts_event` is refused wherever a point is loaded or written
(`kanso.data.publication.check_availability`). A funding dataset is `realtime`: its
availability is its settlement, not a delay derived from it, so it needs no publication
rule — `check_delayed`, which consults the rules, is asked only of a dataset declared
`delayed`.

The type is data and nothing more. A hypothesis that requires `funding` is handed every
point its window holds, in `on_data`, at the settlement instant; what a funding payment
does to a card's balance is the runner's to decide, in the one place costs are applied.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
The engine ships its own `nautilus_trader.model.data.FundingRateUpdate`, whose `rate` is
documented as "the current funding rate" with an optional `next_funding_ns`: the rate a
venue publishes for the period in progress, which is the prediction this type refuses to
be. Its arrow schema is registered under that class name, so this type is a distinct
class, `Funding`, and neither is ever read as the other.

A custom data type is a `nautilus_trader.core.data.Data` subclass decorated with
`@customdataclass` (`nautilus_trader.model.custom`). The decorator synthesises the
constructor, the dict, bytes and Arrow conversions and a `_schema`, and registers the class
for msgspec and Arrow, which is what lets the catalog persist it; a read returns it wrapped
in `CustomData`. Field annotations are restricted to `InstrumentId`, `str`, `bool`,
`float`, `int`, `bytes`, `ndarray` and `dict`, so the rate travels as a `float`.

**This module must not use `from __future__ import annotations`.** The decorator reads
`cls.__annotations__` verbatim and resolves nothing, so postponed annotations reach it as
strings and it raises `TypeError: Unsupported custom data annotation`. Registration is
also keyed by the bare class name across the whole process, so this class is defined
exactly once, here, and imported everywhere else.
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

TYPE_ID = "funding"
"""The id under which this type is registered and named in `data_requirements`."""


@customdataclass
class Funding(Data):  # type: ignore[misc]
    """One settled funding payment of a perpetual.

    `rate` is the realised rate of the period that just settled, a fraction of notional;
    `ts_event` and `ts_init` are both the settlement instant.
    """

    instrument_id: InstrumentId
    rate: float
