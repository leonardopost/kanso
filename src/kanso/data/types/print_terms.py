"""The built-in `PrintTerms` custom data type: what a print's source says of it.

A `TradeTick` carries a price, a size, an aggressor and an id, and no field for where the
print traded or under what sale conditions. A source that says so writes it here, one point
per print, beside the print and under its own trade id: `exchange`, the source's code for the
market centre that reported it, and `conditions`, its sale-condition codes joined by commas,
"" for none, each as the source gives them; and `lit`, the source's reading of them — whether
the print was an execution on an exchange's displayed book in the regular way, one that a
displayed order resting at its price there could have been part of. What a code means is the
source's to say, so the core never reads one: a vendor adapter sets `lit` from its own
reference (`kanso.data.adapters`), and a `csv_parquet` file maps the column its maker wrote.

The two engine timestamps are the print's own, `ts_event` its reference time and `ts_init`
the instant it became public, so a point of this type and its print are one record read twice:
under the print rules the runner hands the venue each print whose terms say it is not lit as a
`kanso.nautilus.tape.Unlit`, which fills no resting order and moves no queue, and every other
print, one it holds no terms for among them, as a plain print, which the venue reads as lit
(`kanso.nautilus.backtest`). A hypothesis may also require the type, and is then handed the
points in `on_data`.

NautilusTrader facts (`nautilus_trader 1.231.0`) are those `kanso.data.types.funding` records
for any type built with `@customdataclass`, and so is the reason **this module must not use
`from __future__ import annotations`**: the decorator reads `cls.__annotations__` verbatim. A
string field is written as an Arrow string column under its own name, which is how the runner
reads `trade_id`, `ts_init` and `lit` off the files without building a point.
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

TYPE_ID = "print_terms"
"""The id under which this type is registered, loaded and named in `data_requirements`."""


@customdataclass
class PrintTerms(Data):  # type: ignore[misc]
    """One print's terms: its trade id, the source's market-centre and sale-condition codes,
    and whether the source reads them as a lit, regular-way execution."""

    instrument_id: InstrumentId
    trade_id: str
    exchange: str
    conditions: str
    lit: bool
