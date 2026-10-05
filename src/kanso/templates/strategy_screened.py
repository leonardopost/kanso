"""strategy.py — {{hyp_id}} (construct: sleeve), seeded by screen {{screen_id}} from its cell
`{{cell}}`. The only file the research loop may edit.

The seed is the cell's own rule written as a sleeve, so the baseline card re-measures through
the runner, with real fills, what the screen measured: when {{trigger}} {{rule}}, enter
{{follower}} {{direction}} and leave it {{hold}} later, one position at a time. A baseline far
from the screen's ceiling is itself a finding — about fills, timing or capacity.

Time comes from `self.data_time`, never from a clock. Allowed imports are listed in program.md.
"""

import math

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

TRIGGER = "{{trigger_id}}"
FOLLOWER = "{{follower_id}}"
SIDE = {{side}}
"""+1 to trade the way the trigger moved, -1 against it."""
SCORED = {{scored}}
"""True when the trigger is a z-score over a trailing window, False when it is a move."""


class Config(KansoConfig):
    """The cell's threshold, its trigger window and its hold, in seconds."""

    threshold: float = {{threshold}}
    window_s: float = {{window_s}}
    hold_s: float = {{hold_s}}


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self) -> None:
        self.seen: list[tuple[int, float]] = []
        self.leave_at = 0

    def on_bar(self, bar) -> None:
        self.point(str(bar.bar_type.instrument_id), float(bar.close))

    def on_trade_tick(self, tick) -> None:
        self.point(str(tick.instrument_id), float(tick.price))

    def on_quote_tick(self, tick) -> None:
        self.point(str(tick.instrument_id), (float(tick.bid_price) + float(tick.ask_price)) / 2.0)

    def point(self, instrument: str, price: float) -> None:
        now = self.data_time
        if self.held(FOLLOWER) != 0 and now >= self.leave_at:
            self.submit_exit(FOLLOWER)
        if instrument != TRIGGER or price <= 0:
            return
        window = int(self.kanso_config.window_s * 1e9)
        self.seen.append((now, math.log(price)))
        while len(self.seen) > 1 and self.seen[1][0] <= now - window:
            self.seen.pop(0)
        score = self.score(now, window)
        if abs(score) < self.kanso_config.threshold or self.held(FOLLOWER) != 0:
            return
        side = "BUY" if SIDE * score > 0 else "SELL"
        if self.submit_entry(FOLLOWER, side) is not None:
            self.leave_at = now + int(self.kanso_config.hold_s * 1e9)

    def score(self, now: int, window: int) -> float:
        """The move since as of `window` ago in basis points, or the z-score over it."""
        if SCORED:
            levels = [level for stamp, level in self.seen if stamp > now - window]
            if len(levels) < 3:
                return 0.0
            mean = sum(levels) / len(levels)
            variance = sum((level - mean) ** 2 for level in levels) / len(levels)
            return 0.0 if variance <= 0 else (levels[-1] - mean) / math.sqrt(variance)
        stamp, level = self.seen[0]
        if stamp > now - window:
            return 0.0
        return (self.seen[-1][1] - level) * 1e4
