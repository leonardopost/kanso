"""`kanso research card` under a stated `costs.orders_per_minute`, through the commands typed.

A card runs in a child of its own, so what this asserts is the whole route: the rate stated
in `hypothesis.yaml`, resolved into the venue model the card child is handed, held by the
sleeve on the data clock, and back across the process boundary as a refusal or as the counts
a card's digest carries.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.cli.research import _digest_lines
from kanso.errors import Exit
from kanso.schemas import CardDigest, Commands

from .conftest import HYP_ID, HYPOTHESIS, REVERTING, at, classify, edit, payload, write_hypothesis

BURST = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Rests three buys far under the market on its tenth bar: one more than two a minute."""

    config_cls = Config

    def on_start(self) -> None:
        self.seen = 0

    def on_bar(self, bar) -> None:
        self.seen += 1
        if self.seen == 10:
            for share in (0.5, 0.6, 0.7):
                price = round(float(bar.close) * share, 2)
                self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=1, price=price)
'''


def rated(runner: CliRunner, root: Path, per_minute: int) -> None:
    """The demo hypothesis stating `per_minute`, registered, classified and begun."""
    costs = {**HYPOTHESIS["costs"], "orders_per_minute": per_minute}
    assert at(runner, root, "hyp", "add", write_hypothesis(root, costs=costs)).exit_code == 0
    classify(root)
    assert at(runner, root, "research", "begin", HYP_ID, "--json").exit_code == Exit.OK


def test_a_card_that_sends_past_its_rate_is_discarded_with_the_refusal_as_its_sizing_gate(
    runner: CliRunner, loaded: Path
) -> None:
    rated(runner, loaded, 2)
    edit(loaded, BURST)

    result = at(runner, loaded, "research", "card", HYP_ID, "--desc", "three at once", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    card = payload(result)
    assert card["status"] == "discard"
    assert card["digest"] is None, "a refused card measured nothing"
    sizing = next(gate for gate in card["gate_results"] if gate["id"] == "sizing")
    assert sizing["pass"] is False
    assert sizing["evidence"]["rule"] == "order_rate"
    assert sizing["evidence"]["asked"] == "BUY"
    assert "needed 1 of the 2 tokens a minute" in sizing["evidence"]["why"]


def test_a_card_within_its_rate_reports_what_it_sent_beside_its_trades(
    runner: CliRunner, loaded: Path
) -> None:
    rated(runner, loaded, 2)
    edit(loaded, REVERTING)

    result = at(runner, loaded, "research", "card", HYP_ID, "--desc", "fade", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    card = payload(result)
    assert card["status"] == "keep"
    assert card["venue_model"]["costs"]["orders_per_minute"] == 2
    sent = Commands.model_validate(card["digest"]["commands"])
    assert sent.submits == 2 * card["n_trades"]
    assert (sent.modifies, sent.cancels) == (0, 0)
    assert 1 <= sent.p99_60s <= sent.peak_60s <= 2, "hourly bars, an order an hour at most"


def test_a_digest_that_counted_its_commands_reads_as_a_third_line() -> None:
    counted = CardDigest(
        sessions=0, fills=0, commands=Commands(submits=5, cancels=4, peak_60s=7, p99_60s=6)
    )

    lines = _digest_lines(counted)

    assert len(lines) == 3
    assert lines[2].split(None, 1) == [
        "commands",
        "5 submit(s) · 0 modify(ies) · 4 cancel(s) · peak 7 in 60 s · p99 6",
    ]
    assert len(_digest_lines(CardDigest(sessions=0, fills=0))) == 2, "a card from before it"
