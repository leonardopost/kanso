"""strategy.py — {{hyp_id}} (construct: sleeve), seeded by screen {{screen_id}} from its cell
`{{cell}}`. The only file the research loop may edit.

The seed is the cell's own rule written as a sleeve, so the baseline card re-measures through
the runner, with real fills, what the screen measured. Over the trailing {{within}}, {{leader}}'s
move times BETA is the move it implies for {{follower}}, and the gap is that less {{follower}}'s
own move. When both are at least `enter_bp` the same way, enter {{follower}} the gap's way. From
then on the gap is followed from the same anchor, so a leader that keeps moving moves the
target: leave when the gap has shrunk to `close` of its size at entry, or grown to `stop` times
it, and after {{max_hold}} at the latest. One position at a time. Every exit is a condition of
the two series; the hold is what they produce. A baseline far from the screen's ceiling is
itself a finding — about fills, timing or capacity.

Time comes from `self.data_time`, never from a clock. Allowed imports are listed in program.md.
"""

import math

from kanso.nautilus.strategy import KansoConfig, KansoStrategy

LEADER = "{{leader_id}}"
FOLLOWER = "{{follower_id}}"
BETA = {{beta}}
"""The follower's fitted response to its leader; negative when it moves the other way."""


class Config(KansoConfig):
    """The cell's entry in basis points, its close and stop as shares of the entry gap, and
    the trailing window and the safety cap, in seconds."""

    enter_bp: float = {{enter_bp}}
    close: float = {{close}}
    stop: float = {{stop}}
    within_s: float = {{within_s}}
    max_hold_s: float = {{max_hold_s}}


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self) -> None:
        self.seen: dict[str, list[tuple[int, float]]] = {LEADER: [], FOLLOWER: []}
        self.anchors = (0.0, 0.0)
        self.opened = 0.0
        self.way = 0
        self.leave_at = 0

    def on_bar(self, bar) -> None:
        self.point(str(bar.bar_type.instrument_id), float(bar.close))

    def on_trade_tick(self, tick) -> None:
        self.point(str(tick.instrument_id), float(tick.price))

    def on_quote_tick(self, tick) -> None:
        self.point(str(tick.instrument_id), (float(tick.bid_price) + float(tick.ask_price)) / 2.0)

    def point(self, instrument: str, price: float) -> None:
        if instrument not in self.seen or price <= 0:
            return
        now = self.data_time
        window = int(self.kanso_config.within_s * 1e9)
        seen = self.seen[instrument]
        seen.append((now, math.log(price)))
        while len(seen) > 1 and seen[1][0] <= now - window:
            seen.pop(0)
        lead, follow = self.seen[LEADER], self.seen[FOLLOWER]
        if not lead or not follow:
            return
        if self.held(FOLLOWER) != 0:
            self.follow_up(now, lead[-1][1], follow[-1][1])
            return
        if lead[0][0] > now - window or follow[0][0] > now - window:
            return
        implied = BETA * (lead[-1][1] - lead[0][1]) * 1e4
        gap = implied - (follow[-1][1] - follow[0][1]) * 1e4
        enter = self.kanso_config.enter_bp
        if min(abs(implied), abs(gap)) < enter or implied * gap <= 0:
            return
        if self.submit_entry(FOLLOWER, "BUY" if gap > 0 else "SELL") is not None:
            self.anchors = (lead[0][1], follow[0][1])
            self.opened = abs(gap)
            self.way = 1 if gap > 0 else -1
            self.leave_at = now + int(self.kanso_config.max_hold_s * 1e9)

    def follow_up(self, now: int, lead: float, follow: float) -> None:
        """Leave when the gap from the entry's anchor has closed, stopped, or the cap came."""
        gap = self.way * (BETA * (lead - self.anchors[0]) - (follow - self.anchors[1])) * 1e4
        config = self.kanso_config
        if gap <= config.close * self.opened or gap >= config.stop * self.opened:
            self.submit_exit(FOLLOWER)
        elif now >= self.leave_at:
            self.submit_exit(FOLLOWER)
