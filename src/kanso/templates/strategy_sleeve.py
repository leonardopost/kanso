"""strategy.py — {{hyp_id}} (construct: sleeve). The only file the research loop may edit.

Contract: subclass KansoStrategy; kanso injects universe, resolution and costs from
hypothesis.yaml and runs this class unchanged in backtest, paper and live nodes. Allowed imports are
listed in program.md; anything else fails the strategy_integrity gate.

Time comes from `self.data_time` (the ts_event of the last data event), never from a clock.
Orders go through `self.submit_entry(...)` and `self.submit_exit(...)`, which size against the
hypothesis capital and risk limits and leave room for costs.
"""

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    """Numeric fields here are the parameters the param_plateau gate perturbs."""

    lookback: int = 20


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self) -> None:
        # kanso has already subscribed to the hypothesis's data_requirements at `resolution`
        # for every instrument in `universe`. Initialise indicators/state here.
        pass

    def on_bar(self, bar) -> None:  # or on_quote_tick / on_trade_tick per `resolution`
        # Baseline stub: no trades. The first card that trades and passes the constraints becomes `best`.
        pass
