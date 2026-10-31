"""`kanso research card` under a stated `costs.orders_per_minute`, through the commands typed.

A card runs in a child of its own, so what this asserts is the whole route: the rate stated
in `hypothesis.yaml`, resolved into the venue model the card child is handed, held by the
sleeve on the data clock, and back across the process boundary as the counts a card's digest
carries, the commands the rate dropped among them.
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
    """Buys a share at market three times on its tenth bar: one more than two a minute."""

    config_cls = Config

    def on_start(self) -> None:
        self.seen = 0

    def on_bar(self, bar) -> None:
        self.seen += 1
        if self.seen == 10:
            for _ in range(3):
                self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=1)
'''


def rated(runner: CliRunner, root: Path, per_minute: int) -> None:
    """The demo hypothesis stating `per_minute`, registered, classified and begun."""
    costs = {**HYPOTHESIS["costs"], "orders_per_minute": per_minute}
    assert at(runner, root, "hyp", "add", write_hypothesis(root, costs=costs)).exit_code == 0
    classify(root)
    assert at(runner, root, "research", "begin", HYP_ID, "--json").exit_code == Exit.OK


def test_a_card_that_sends_entries_past_its_rate_runs_on_and_counts_what_it_dropped(
    runner: CliRunner, loaded: Path
) -> None:
    """Three entries at once at two a minute: two go, the third is dropped unsent and counted,
    and the card runs to a result rather than being refused, its digest and its `commands`
    line saying what the rate dropped."""
    rated(runner, loaded, 2)
    edit(loaded, BURST)

    result = at(runner, loaded, "research", "card", HYP_ID, "--desc", "three at once", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    card = payload(result)
    assert card["digest"] is not None, "a card that dropped an entry still measured"
    sizing = [gate for gate in card["gate_results"] if gate["id"] == "sizing"]
    assert all(gate["pass"] for gate in sizing), "nothing was refused"
    sent = Commands.model_validate(card["digest"]["commands"])
    assert (sent.submits, sent.refused_submits, sent.refused_modifies) == (2, 1, 0)
    assert sent.peak_60s == 2
    lines = _digest_lines(CardDigest.model_validate(card["digest"]))
    assert lines[2].endswith("· refused past the rate 1 submit(s), 0 modify(ies)")


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

    dropped = CardDigest(
        sessions=0,
        fills=0,
        commands=Commands(submits=5, peak_60s=5, p99_60s=5, refused_submits=3, refused_modifies=2),
    )
    assert _digest_lines(dropped)[2].split(None, 1)[1] == (
        "5 submit(s) · 0 modify(ies) · 0 cancel(s) · peak 5 in 60 s · p99 5"
        " · refused past the rate 3 submit(s), 2 modify(ies)"
    )
