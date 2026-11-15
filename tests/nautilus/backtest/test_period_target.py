"""A period target on every path: the card's equity is the balance the target reads, the flatten
is charged as any fill, and a replay and a stage stop the same week at the same instants.

The rule lives in the harness (`kanso.nautilus.strategy.KansoStrategy`), which the runner hands
it to beside the book policy (`kanso.nautilus.backtest.targeted`), so a card, a replay on the
live code path and a stage node run the same code and stop where each other stops. The flatten
it sends is an ordinary fill, charged once by the runner in the extraction, and the harness's
balance — what the target is read on — is that equity at every period end. A filter attached
to the host leaves the weeks it stops as they were.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kanso.nautilus import node, session
from kanso.nautilus.backtest import execute
from kanso.nautilus.costs import fill_cost
from tests.nautilus.strategy.test_period_target import (
    LAGG,
    LAGG_ID,
    LEAD,
    LEAD_ID,
    MONDAY,
    _two_weeks,
    card,
    fills,
    ny,
    request,
    series,
)

from .conftest import FILTER_MODIFIER, catalog, instrument
from .test_dated_stage import _placed, _stage

BPS = 10_000.0
COSTS = {"commission_bps": 1.0, "slippage_bps": 2.0, "spread": "fixed_bps", "fixed_bps": 4.0}
"""Five basis points a fill not struck at the touch: one of commission, two of slippage and
half the four-point width."""

STAMPS = ("04:01", "04:02", "09:33", "09:34", "09:35")


def _pair() -> tuple[list[tuple[int, float]], list[tuple[int, float]], dict[object, object]]:
    """Both legs bought at 04:01 and stopped at the 09:33 close, flattened at the next."""
    lead = list(
        zip((ny(MONDAY, s) for s in STAMPS), (10.00, 10.00, 10.26, 10.27, 10.28), strict=True)
    )
    lagg = list(
        zip((ny(MONDAY, s) for s in STAMPS), (11.00, 11.00, 11.26, 11.27, 11.28), strict=True)
    )
    plan: dict[object, object] = {
        (ny(MONDAY, "04:01"), LEAD_ID): [("entry", LEAD_ID, "BUY"), ("entry", LAGG_ID, "BUY")]
    }
    return lead, lagg, plan


def test_the_card_s_equity_is_the_balance_the_target_reads_and_the_flatten_pays_once(
    tmp_path: Path,
) -> None:
    """Under five basis points a fill, the week's P&L at 09:33 is 520 less the 10.5 the two
    entries paid, at or above the 500 target; the flatten is two fills at the 09:34 closes,
    each charged its notional times five basis points by hand, and the balance the sleeve read
    on the last bar, nothing filling after it, is the equity the card ended on."""
    lead, lagg, plan = _pair()

    result, seen = card(plan, lead, lagg, tmp_path, costs=COSTS)

    assert min(row.ts for row in seen if row.stopped) == ny(MONDAY, "09:33")
    sold = [fill for fill in result.run.fills if fill.side == "SELL"]
    assert sorted((fill.instrument_id, fill.px, fill.ts_ns) for fill in sold) == [
        (LAGG_ID, 11.27, ny(MONDAY, "09:34")),
        (LEAD_ID, 10.27, ny(MONDAY, "09:34")),
    ]
    for fill in result.run.fills:
        assert fill.cost == pytest.approx(fill.notional * 5.0 / BPS, rel=1e-12)
        assert fill.cost == pytest.approx(
            fill_cost(fill.notional, fill.qty, 1.0, 2.0, 2.0 / BPS, None, 0.0, maker=False),
            rel=1e-12,
        )
    assert seen[-1].balance == pytest.approx(result.run.equity[-1], rel=1e-12)


def test_the_card_s_equity_is_the_balance_at_a_period_end_with_a_stop(tmp_path: Path) -> None:
    """Over two weeks, the second stopped at the 09:33 close and flattened at the next, each
    fill charged five basis points: the equity of every period whose last point fills nothing
    is the balance the sleeve read there — the stopped day's among them — so what the target
    is read on is what the card strikes. An order a handler sends fills after it, so the
    balance a handler reads at a point that fills is the equity before that fill."""
    lead, lagg, plan = _two_weeks()

    result, seen = card(plan, lead, lagg, tmp_path, costs=COSTS)

    run = result.run
    filled = {fill.ts_ns for fill in run.fills}
    compared = []
    for end, equity in zip(run.period_ends_ns, run.equity, strict=True):
        last = next(row for row in reversed(seen) if row.ts <= end)
        if last.ts not in filled:
            assert last.balance == pytest.approx(equity, rel=1e-12)
            compared.append(last)
    assert any(row.stopped for row in compared)


def test_a_filter_attached_to_the_host_stops_the_weeks_its_own_card_does(tmp_path: Path) -> None:
    """A construct's card runs its host under the construct's own file, which states the
    host's `period_target` exactly (`hyp validate`): with a filter attached that lets every
    entry through, the card stops at the same points, and sends and fills what the host's own
    card does."""
    lead, lagg, plan = _two_weeks()
    allows = (("filter", FILTER_MODIFIER, {"allow": True}),)

    host, alone = card(plan, lead, lagg, tmp_path)
    filtered, attached = card(plan, lead, lagg, tmp_path, modifiers=allows)

    assert any(row.stopped for row in alone)
    assert [(r.ts, r.name, r.stopped) for r in attached] == [
        (r.ts, r.name, r.stopped) for r in alone
    ]
    assert filtered.intents == host.intents
    assert filtered.run.fills == host.run.fills


@pytest.mark.parametrize("order", [(LEAD, LAGG), (LAGG, LEAD)])
@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_a_replay_stops_and_flattens_where_the_card_does(
    order: tuple[str, str], latency_ms: float, tmp_path: Path
) -> None:
    """The live code path runs the same sleeve under the same rule: the same intents, element
    by element, and the same fills at the same prices and instants."""
    lead, lagg, plan = _pair()
    made = request(plan, tmp_path / "seen.txt", latency_ms=latency_ms, costs=COSTS)
    names = {LEAD: series(LEAD, lead), LAGG: series(LAGG, lagg)}
    instruments = [instrument(symbol) for symbol in order]
    groups = [names[symbol] for symbol in order]

    researched = execute(made, instruments, groups)
    replayed = session.run_node(made, instruments, groups).result

    assert [side for side, _, _ in fills(researched.run)] == ["BUY", "SELL"]
    assert replayed.intents == researched.intents
    assert replayed.run.fills == researched.run.fills
    assert replayed.run.equity == researched.run.equity


def test_a_stage_window_stops_and_flattens_where_the_card_does(tmp_path: Path) -> None:
    """A stage node over the same day, its venue handed the same series, stops the same week
    at the same close and flattens at the same prints: its realised fills are the card's."""
    lead, lagg, plan = _pair()
    made = request(plan, tmp_path / "card.txt", costs=COSTS)
    one_day = replace(made, window=(MONDAY, MONDAY))
    names = {LEAD: series(LEAD, lead), LAGG: series(LAGG, lagg)}
    researched = execute(one_day, [instrument(LEAD), instrument(LAGG)], [names[LEAD], names[LAGG]])
    held = catalog(
        tmp_path / "catalog", [*names[LEAD], *names[LAGG]], [instrument(LEAD), instrument(LAGG)]
    )
    staged = replace(one_day, overrides={"record": str(tmp_path / "stage.txt")})

    (realised,) = node.run(_stage(_placed(staged, tmp_path), held, MONDAY)).realised

    assert [(f.instrument_id, f.side, f.px, f.ts_ns) for f in realised.run.fills] == [
        (f.instrument_id, f.side, f.px, f.ts_ns) for f in researched.run.fills
    ]
    assert any(f.side == "SELL" and f.ts_ns == ny(MONDAY, "09:34") for f in realised.run.fills)
