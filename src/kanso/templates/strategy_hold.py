"""strategy_hold.py — the benchmark a hypothesis declaring `benchmark: {hold: first_leg}` must beat.

kanso ships this file and runs it itself; it is never copied into a workspace and never edited by
the research loop. It buys the universe's first instrument on the first point it may trade on and
never exits; an entry the venue refused for want of a market — under `limit_fill: print_through`,
one sent on a print before any quote, or after a print traded outside the last quote — is sent
again on the next point, so the hold waits for a quote rather than holding nothing. The entry
names no size, so it takes the whole room the risk limits leave — the whole budget under a
`sizing` rule — and every cost, split, book rule and warmup is the runner's own, applied exactly
as it is to the strategy measured against it. It is not a card: no gate judges it, and
`max_hold` does not bound it.
"""

from nautilus_trader.model.enums import OrderStatus

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    """No parameters: a benchmark has nothing to tune."""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self) -> None:
        self.entry = None

    def on_bar(self, bar) -> None:
        self.hold()

    def on_quote_tick(self, tick) -> None:
        self.hold()

    def on_trade_tick(self, tick) -> None:
        self.hold()

    def hold(self) -> None:
        # `None` until an order is placed: no price for the leg yet, or a warmup still dropping
        # every order before the window opens. A refusal is read off the order itself, so it is
        # seen whenever and however the venue reported it.
        if self.entry is None or refused(self.entry):
            self.entry = self.submit_entry(self.universe[0], "BUY")


def refused(order) -> bool:
    """Whether the venue rejected the order for want of a market to fill it on."""
    return order.status == OrderStatus.REJECTED and str(order.last_event.reason).startswith(
        "no market"
    )
