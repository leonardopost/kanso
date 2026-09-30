"""The one venue this broker serves, how its instruments are named, and what it declares.

**One venue, the exchange itself.** A crypto exchange lists and matches its own
instruments, so the venue code is the exchange's — `OKX`, the engine's own `OKX_VENUE` —
and an instrument id is the exchange's instrument id with it appended:
`BTC-USDT-SWAP.OKX`. The venue code and every other name this exchange uses live in this
package and nowhere else in kanso.

**What is declared.** The package declares perpetual swaps: a margin account, settled in
USDT, charged 5 basis points on a fill that takes liquidity and 2 on one that rests on the
book (`commission_bps` and `maker_bps`). Provenance: the exchange's published Regular
(Lv1) perpetual schedule, and the operator's account measured on 2026-09-30 with
`GET /api/v5/account/trade-fee?instType=SWAP`, which answered level `Lv1`, maker `-0.0002`
and taker `-0.0005` — the exchange signs a fee the account pays as negative — so 2 and 5
basis points. The same call for `SPOT` answered 0.70 %, the Australian retail spot schedule;
it is not declared, because this package declares perpetuals. The account's position mode
read `net_mode` on the same day, and a client (a later change) will require net mode at
connect.

**Declared, not fetched.** The fee tier is a fact about an account on a day, and a card is
costed before any account is opened — so the tier is stated here from the published
schedule and the measurement above, and an account on another tier states its own rates
under `venues.OKX.costs` in `portfolio.yaml` (origin `venue_override`), exactly as an
operator stresses any venue's costs.

**What is not declared.** No slippage and no spread. They fall to kanso's shipped
defaults, exactly as the equity broker's declaration leaves them, so a hypothesis on bars
alone must still state a spread width — with no quotes and no `fixed_bps` the venue model
is refused rather than resolved with a spread of zero.
"""

from __future__ import annotations

from typing import Final

from kanso.schemas import CostsOverride, VenueDeclaration

__all__ = [
    "COMMISSION_BPS",
    "CURRENCY",
    "MAKER_BPS",
    "PERPETUAL",
    "VENUE",
    "VENUES",
    "declaration",
    "instrument_id",
]

VENUE: Final = "OKX"
"""The venue code, the engine's own `OKX_VENUE`: the exchange is the venue."""

CURRENCY: Final = "USDT"
"""The settlement currency of the linear perpetuals this package declares."""

COMMISSION_BPS: Final = 5.0
MAKER_BPS: Final = 2.0
"""The Regular (Lv1) perpetual taker and maker rates, measured on the operator's account."""

PERPETUAL: Final = VenueDeclaration(
    account="margin",
    currency=CURRENCY,
    costs=CostsOverride(commission_bps=COMMISSION_BPS, maker_bps=MAKER_BPS),
)
"""What this broker declares about its venue, and nothing else."""

VENUES: Final[dict[str, VenueDeclaration]] = {VENUE: PERPETUAL}


def declaration(venue: str) -> VenueDeclaration | None:
    """What this broker declares about `venue`, or `None` for one it does not serve."""
    return VENUES.get(venue)


def instrument_id(inst_id: str) -> str:
    """The kanso instrument id of the exchange's `instId`: `BTC-USDT-SWAP.OKX`."""
    return f"{inst_id}.{VENUE}"
