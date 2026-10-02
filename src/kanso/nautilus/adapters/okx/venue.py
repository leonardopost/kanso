"""The one venue this broker serves, how its instruments are named, and what it declares.

**One venue, the exchange itself.** A crypto exchange lists and matches its own
instruments, so the venue code is the exchange's — `OKX`, the engine's own `OKX_VENUE` —
and an instrument id is the exchange's instrument id with it appended:
`BTC-USDT-SWAP.OKX`. The venue code and every other name this exchange uses live in this
package and nowhere else in kanso.

**What is declared.** The package declares perpetual swaps: a margin account, settled in
USDT, charged 5 basis points on a fill that takes liquidity and 2 on one that rests on the
book (`commission_bps` and `maker_bps`). These are the exchange's **global** Regular (Lv1)
perpetual tier, from its published schedule — and no more than that. The exchange serves
accounts through regional entities, each with a Regular tier of its own, so the
declaration is right for an account on the global entity and can be wrong for any other.

Measured with the operator's read-only key on `us.okx.com`, on an account of the
Australian entity: on 2026-10-01, in futures mode (`acctLv` 2),
`GET /api/v5/account/trade-fee?instType=SWAP` answered level `Lv1`, maker `-0.0005` and
taker `-0.0007` on `maker`/`taker`, `makerU`/`takerU` and `makerUSDC`/`takerUSDC` alike —
the exchange signs a fee the account pays as negative — so 5 and 7 basis points, and the
account's fee page showed Futures 0.0500 % maker and 0.0700 % taker. The day before, in spot
mode (`acctLv` 1), the same call had answered `-0.0002` and `-0.0005`, the global figures,
and was taken for this account's tier: a workspace costed on the declaration charged it 3 bp
too little on every fill that rested and 2 bp on every one that took. The same call for
`SPOT` answered 0.70 %, the Australian retail spot schedule; it is not declared, because
this package declares perpetuals. The account's position mode read `net_mode` on both days,
and a client (a later change) will require net mode at connect.

**Declared, not fetched — and checked when asked.** The fee tier is a fact about an
account on a day, and a card is costed before any account is opened — so the tier is stated
here from the published schedule, and an account on another tier states its own rates under
`venues.OKX.costs` in `portfolio.yaml` (origin `venue_override`), exactly as an operator
stresses any venue's costs. `kanso doctor --check-adapters` reads the account's own tier
with its key and prints those lines where the workspace charges otherwise (`account.py`).

**The rates are charged once, by the runner, and never by the engine.** Costs are
deducted per fill in the extraction and nowhere else, and the simulated venue charges
nothing only because kanso's resolved instruments carry maker and taker rates of zero. So
the public reference in `reference.py` states `maker_fee` and `taker_fee` as zero on every
instrument it hands kanso — an instrument that arrived with the rates declared here would be
charged them by the venue and again by the runner, and the second charge is silent.

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
"""The global Regular (Lv1) perpetual taker and maker rates. A regional entity's Regular tier
differs — the Australian one's was 7 and 5, measured 2026-10-01 — and is stated under
`venues.OKX.costs`."""

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
