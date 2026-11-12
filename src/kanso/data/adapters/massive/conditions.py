"""Which of the vendor's prints are lit and regular-way: the reading behind a print's terms.

A print the consolidated tape carries is one of three things to a displayed limit resting on an
exchange's book: an execution on such a book in the regular way, which that order could have
been part of; an execution off every exchange — a broker's internalised fill or an alternative
trading system's, reported to a FINRA trade reporting facility — which no displayed order
there was; or a print that is not a regular-way execution at its instant at all — an odd lot,
a print reported late or out of sequence, an average-price or contingent trade, an auction
print, a correction. Only the first fills a resting order (`kanso.nautilus.tape`), and the
vendor says which is which on every row: an `exchange` code, a `trf_id` on a print reported to
a facility, `conditions` codes, a `correction` code, and the two instants. `lit` reads them.

A print is lit when every one of these holds, each measured on the vendor's own rows:

* it was not reported to a facility: its exchange is not `TRF_EXCHANGE` and it carries no
  `trf_id` — the facility prints of the recorded responses carry both;
* every condition it carries is one the vendor's reference lets set the consolidated last
  price (`updates_open_close`), or an indicator the reference gives no update rule — an odd lot,
  an average-price, a derivatively priced, a prior-reference, an extended-hours or an
  out-of-sequence print does not — and none is a print kanso does not count as regular-way
  (`IRREGULAR`), the auction, official open and close, cross, cash and next-day prints among
  them; a code the reference does not list is not lit;
* it is a print and not the vendor's later record of one: its correction code is not one of
  `RECORDS`;
* it is priced in whole cents, as a displayed order is;
* the tape carried it within `PROMPT_NS` of the participant's own instant.

`REFERENCE` is the vendor's condition reference as `/v3/reference/conditions?asset_class=stocks&
data_type=trade` answered it on 2026-10-28 (the suite holds the response and checks this table
against it). Measured by an independent derivation of these rules over the vendor's flat files
of 139 sessions of twenty names, which also keeps a print to the regular session, 18.4 % of the
992.5 million prints in the session were lit, 34.4 % of the shares: 41.4 % traded off-exchange
and 67.6 % were odd lots, and 16,991 prints outside the session met every other rule. On the
376 prints of SPY the suite records from two seconds of 2025-01-02, the rule reads 134 lit,
print for print the derivation's reading of the same prints in that day's file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "IRREGULAR",
    "PROMPT_NS",
    "RECORDS",
    "REFERENCE",
    "TRF_EXCHANGE",
    "lit",
]

TRF_EXCHANGE: Final = 4
"""The exchange code the vendor gives a print reported to a FINRA trade reporting facility."""

PROMPT_NS: Final = 1_000_000_000
"""How late after the participant's instant the tape may carry a print that is lit: a second."""

RECORDS: Final = frozenset({10, 11, 12})
"""The correction codes of the vendor's records amending an earlier print — a cancel, an error
and a correction record — each its own row at the instant it reached the tape."""

REFERENCE: Final[Mapping[int, bool | None]] = {
    1: True, 2: False, 3: True, 4: True, 5: False, 6: True, 7: False, 8: True, 9: True,
    10: False, 11: True, 12: False, 13: False, 14: True, 15: False, 16: False, 17: True,
    18: True, 19: True, 20: False, 21: False, 22: False, 23: True, 24: True, 25: True,
    27: True, 28: True, 29: False, 30: True, 31: True, 32: False, 33: False, 34: True,
    35: True, 36: True, 37: False, 38: True, 41: True, 52: False, 53: False, 55: True,
    57: None, 58: None, 59: None, 60: None, 62: None, 63: None, 64: None, 65: None,
    66: None, 67: None, 68: None, 69: None, 70: None, 71: None,
}  # fmt: skip
"""Each trade condition the vendor lists, by code: whether its consolidated update rule lets it
set the last price (`updates_open_close`), or `None` for an indicator it gives no rule."""

IRREGULAR: Final = frozenset(
    {
        2,
        7,
        8,
        10,
        12,
        13,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        25,
        28,
        29,
        30,
        31,
        32,
        33,
        35,
        38,
        52,
        53,
    }
)
"""The conditions of a print kanso does not count as a regular-way execution at its instant,
whatever the reference says of the last price: average-price, cash, closing, derivatively
priced, extended-hours, official open and close, opening, reopening and closing, next-day,
price-variation, prior-reference, seller, sold-last and out-of-sequence, stock-option,
corrected-close and contingent prints."""


def lit(row: Mapping[str, Any], ts_event: int, ts_init: int) -> bool:
    """Whether one row of the vendor's trades is a lit, regular-way execution, by the rules
    above. `ts_event` and `ts_init` are the print's participant and tape instants, and a row
    whose participant instant the vendor left out is read at its tape instant alone, which no
    rule takes as prompt."""
    exchange = row.get("exchange")
    if exchange == TRF_EXCHANGE or row.get("trf_id"):
        return False
    if row.get("correction") in RECORDS:
        return False
    if "participant_timestamp" not in row or ts_init - ts_event > PROMPT_NS:
        return False
    conditions: Sequence[Any] = row.get("conditions") or ()
    for code in conditions:
        if code in IRREGULAR or REFERENCE.get(code, False) is False:
            return False
    price = row.get("price")
    if not isinstance(price, int | float) or isinstance(price, bool):
        return False
    cents = float(price) * 100.0
    return abs(cents - round(cents)) < 1e-6
