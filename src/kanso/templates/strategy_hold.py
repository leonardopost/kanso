"""strategy_hold.py — the benchmark a hypothesis declaring `benchmark: {hold: first_leg}` must beat.

kanso ships this file and runs it itself; it is never copied into a workspace and never edited by
the research loop. It buys the universe's first instrument on the first print it may trade on and
never exits. The entry names no size, so it takes the whole room the risk limits leave — the whole
budget under a `sizing` rule — and every cost, split, book rule and warmup is the runner's own,
applied exactly as it is to the strategy measured against it. It is not a card: no gate judges
it, and `max_hold` does not bound it.
"""

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    """No parameters: a benchmark has nothing to tune."""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self) -> None:
        self.bought = False

    def on_bar(self, bar) -> None:
        self.hold()

    def on_quote_tick(self, tick) -> None:
        self.hold()

    def on_trade_tick(self, tick) -> None:
        self.hold()

    def hold(self) -> None:
        # `None` until an order is placed: no price for the leg yet, or a warmup still dropping
        # every order before the window opens.
        if not self.bought:
            self.bought = self.submit_entry(self.universe[0], "BUY") is not None
