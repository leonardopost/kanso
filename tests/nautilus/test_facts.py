"""The engine facts are checked against the engine actually installed here.

These tests are the guard on the module docstring: they assert that every claim
is checked, that the checks agree with the docstring's record, and that the claims
the package records as design constraints are exactly the ones that fail. An engine
upgrade that closes one of those gaps, or opens a new one, fails here first.
"""

from __future__ import annotations

import nautilus_trader
import pytest

from kanso.nautilus import facts
from kanso.nautilus.facts import DESIGN_CONSTRAINTS, ENGINE_VERSION, Fact, verify

CLAIMS = [claim for claim, _ in facts.claims()]


@pytest.fixture(scope="module")
def verified() -> list[Fact]:
    return verify()


def test_engine_version_matches_the_installed_package() -> None:
    assert nautilus_trader.__version__ == ENGINE_VERSION


def test_docstring_records_the_engine_version() -> None:
    assert facts.__doc__ is not None
    assert ENGINE_VERSION in facts.__doc__


def test_one_fact_per_claim(verified: list[Fact]) -> None:
    assert [fact.claim for fact in verified] == CLAIMS


def test_claims_are_unique() -> None:
    assert len(set(CLAIMS)) == len(CLAIMS)


def test_every_fact_carries_evidence(verified: list[Fact]) -> None:
    for fact in verified:
        assert fact.evidence.strip(), fact.claim
        assert isinstance(fact.holds, bool)


def test_facts_are_immutable(verified: list[Fact]) -> None:
    with pytest.raises(AttributeError):
        verified[0].holds = True  # type: ignore[misc]


def test_every_claim_but_the_design_constraints_holds(verified: list[Fact]) -> None:
    failed = {fact.claim: fact.evidence for fact in verified if not fact.holds}
    assert set(failed) == DESIGN_CONSTRAINTS, failed


def test_design_constraints_are_claims() -> None:
    assert set(CLAIMS) >= DESIGN_CONSTRAINTS
    assert len(DESIGN_CONSTRAINTS) == 5


BINDINGS = {
    "the risk engine performs no balance or margin check for a margin account",
    "LeveragedMarginModel asks zero margin of an instrument whose margin rates are zero",
    "handle_bar(historical=True) routes to on_historical_data and never to on_bar",
    "a Bar carries low and high, and every market point carries ts_init",
    "an order whose cancel was sent is not closed until the cancel lands, and under a "
    "latency the market can fill it first",
    "close_position sends a reduce-only order, which the simulated venue trims to what "
    "is left of the position and refuses once the position is already closed",
    "cancel_all_orders marks an order open at the venue pending cancel, leaves one in "
    "flight as it is, and cancels both",
    "nautilus_pyo3.HttpClient holds a request that names a key to its default quota, and "
    "one key's quota is shared by every thread that sends under it",
    "a level-two book sets the size an add or an update names, ignores a delete of a "
    "price it does not hold, and empties both sides on a clear",
    "an OrderBookDeltas is applied whole by the simulated exchange and matched once",
    "the data engine publishes an OrderBookDeltas whole, after its book has applied it, "
    "and a lone OrderBookDelta as a batch of one",
    "a catalog query handed its files returns the points of one instant in file order, "
    "whatever span it reads, with ts_init inclusive at both ends",
    "write_data files a series in the one directory class_to_filename and "
    "urisafe_identifier name, each file named by its first and last ts_init, and "
    "filter_files names its files whose interval meets a span, ends included or open",
    "write_data writes a file in row groups of 5,000 rows, each footer stating its rows "
    "and its least and greatest ts_init",
    "an order's events grow only at its end, and its strategy is handed each one as the "
    "order's last when the order takes it",
    "a level-two venue refuses to run a name it holds data and no book data for, and "
    "counts only the first point of each validated add_data call",
    "a level-one venue ignores a quote or a print whose ts_event is earlier than its book's "
    "last update, and applies one stamped at it",
    "a level-one book that kanso's Availability module resets applies the next quote or "
    "print whatever its ts_event, and holds what that point alone sets",
    "a print at a resting limit's price fills it by its own size, so a larger clip fills in parts",
    "on a level-one venue a quote beyond a resting limit's price, or a print beyond it from "
    "the side that can trade with it, fills all that is left of the order at its price, "
    "whatever its own size, when it is the best-priced order the point reaches",
    "a point beyond two resting limits on a level-one venue fills all that is left of the "
    "better-priced and nothing of the other",
    "on a level-one venue a quote at the price two limits rest at, or a print at it under "
    "touch, fills each of them by the point's whole size",
    "a quote whose far side sits at a resting limit's price on a level-one venue fills it "
    "by the size shown, and again at every quote that shows it",
    "a print is both sides of a level-one book until the next quote, so a market order sent "
    "on it fills the print's size at its price and the rest one increment worse",
    "liquidity_consumption on a level-one venue fills a resting limit from the first of two "
    "identical prints at its price and not from the second",
    "a fill model's book replaces the engine's own for the fills of an order it has "
    "matched, and a zero-quantity fill in it ends the fill there, before a limit's "
    "remainder is filled whole",
    "the engine asks a fill model for a market order's fills and fills it from the book "
    "the model answers, walking one increment past it for what that book does not cover",
    "a lone zero-quantity fill refuses a market order for want of a market, and leaves a "
    "limit accepted on arrival resting at its price",
    "the backtest engine re-matches every resting order after it drains a command, and "
    "asks a fill model with the arguments of the point's own match",
    "a simulation module that calls its exchange's process from pre_process lands every "
    "command due by then before the matching engine applies the point",
    "a print inside the last quote leaves the engine's bid and ask no wider than that "
    "quote, re-matched or not, so a limit the quote makes marketable is matched on landing",
    "kanso's print_through venue fills a resting limit only from a later print strictly "
    "through its price, by that print's size or whole, never from a quote, and fills a "
    "taker at the last quote's touch, an IOC limit no further than the quote shows",
    "under the NETTING OMS the cache keeps one position per (instrument, strategy), "
    "named {instrument}-{strategy}, and the portfolio's net position is every "
    "strategy's sum",
    "under the NETTING OMS the simulated venue holds a reduce-only close to the first "
    "open position in the instrument sorted by id, whichever strategy's it is",
    "a topic's handlers at one priority are called in the order they subscribed, higher "
    "priorities first, whatever order their strategies were added in",
    "two market orders sent into one name at one instant on a level-one venue are each "
    "filled from the whole touch, neither sharing what the other took",
    "of two orders linked one-cancels-other only one fills, held by the venue or the "
    "order emulator, under a latency or none, while two linked one-updates-other and sent "
    "marketable both fill",
}
"""The claims recorded ahead of the work that rests on them; deleting one fails here."""


def test_the_claims_later_work_rests_on_stay_listed_and_hold(verified: list[Fact]) -> None:
    assert set(CLAIMS) >= BINDINGS
    assert all(fact.holds for fact in verified if fact.claim in BINDINGS)


def test_verify_is_repeatable() -> None:
    first = {fact.claim: fact.holds for fact in verify()}
    second = {fact.claim: fact.holds for fact in verify()}
    assert first == second


def test_a_raising_check_is_reported_rather_than_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> tuple[bool, str]:
        raise RuntimeError("engine gone")

    monkeypatch.setattr(facts, "claims", lambda: (("a claim that cannot be checked", explode),))
    (fact,) = verify()
    assert fact.holds is False
    assert "RuntimeError: engine gone" in fact.evidence


def test_a_broker_s_claims_are_checked_after_the_core_s_own() -> None:
    """A broker's package may name its broker and this module may not, so each broker
    states its own claims and they are collected through the registry."""
    from kanso.nautilus import adapters

    stated = [claim for claim, _ in adapters.engine_facts()]
    assert stated, "a packaged broker states the engine facts its package rests on"
    assert [claim for claim, _ in facts._CHECKS] + stated == CLAIMS


def test_raises_helper_reports_the_exception() -> None:
    def boom() -> object:
        raise ValueError("nope")

    assert facts._raises(boom) == "ValueError: nope"
    assert facts._raises(lambda: 1) is None


# -- the perpetual ------------------------------------------------------------------


def test_the_perpetual_builds_from_exactly_the_fields_the_engine_requires() -> None:
    """Each required field omitted raises, and the two kanso requires on top default to one.

    The builder's own list is the engine's plus `multiplier` and `lot_size`: the engine
    would carry a contract value of one, which kanso reads as a claim about the contract.
    """
    from nautilus_trader.model.instruments import CryptoPerpetual

    from kanso.data.instruments import _REQUIRED

    assert set(_REQUIRED["CryptoPerpetual"]) == {
        *facts.PERPETUAL_REQUIRED,
        "multiplier",
        "lot_size",
    }
    bare = facts._sample_perpetual()
    fields = facts._perpetual_fields(bare)
    assert set(fields) == set(facts.PERPETUAL_REQUIRED)
    for field in facts.PERPETUAL_REQUIRED:
        refused = facts._without(CryptoPerpetual, fields, field)
        assert refused is not None and refused.startswith("TypeError"), field
    assert facts._without(CryptoPerpetual, fields, "multiplier") is None
    assert (str(bare.multiplier), str(bare.lot_size)) == ("1", "1")


def test_the_instrument_claim_counts_the_six_classes(verified: list[Fact]) -> None:
    [fact] = [fact for fact in verified if fact.claim.startswith("the six instrument classes")]
    assert fact.holds
    assert "CryptoPerpetual" in fact.evidence


def test_the_fee_and_settlement_claims_hold(verified: list[Fact]) -> None:
    held = {fact.claim: fact.holds for fact in verified}
    assert held[
        "MakerTakerFeeModel charges a fill the instrument's maker or taker rate on its notional"
    ]
    [settlement] = [fact for fact in verified if fact.claim.startswith("get_settlement_")]
    assert settlement.claim == (
        "get_settlement_currency answers a perpetual's settlement currency and every other "
        "class's quote currency; get_cost_currency, which the account manager books and "
        "converts from, answers the quote currency of every class"
    )
    assert settlement.holds
    assert "settles in USDC and is booked in USDT" in settlement.evidence


def test_a_client_whose_quota_holds_no_request_fails_the_quota_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stand-in failing every request at once, as the engine's client does a request that
    names no key: the check reads that as a quota that holds nothing."""
    from nautilus_trader.core import nautilus_pyo3

    class Unmetered:
        def __init__(self, **_: object) -> None:
            pass

        async def request(self, *_: object, **__: object) -> object:
            raise RuntimeError("not a URL")

    monkeypatch.setattr(nautilus_pyo3, "HttpClient", Unmetered)

    holds, evidence = facts._check_http_client_meters_named_keys()

    assert not holds
    assert "['RuntimeError']" in evidence


def test_a_netting_position_is_one_strategy_s_and_the_net_position_the_account_s() -> None:
    """What `KansoStrategy`'s own-position reader rests on, read in the first strategy's
    handler before its sale and again at the end of the run."""
    before, after = facts._probe_two_strategies_in_one_name()

    assert before == (
        {"AAPL.XNAS-Probe-A": 10.0, "AAPL.XNAS-Probe-B": 5.0},
        (10.0, 5.0),
        15.0,
    ), "two positions, each named for its strategy, under the account's sum"
    assert after == ({"AAPL.XNAS-Probe-B": 5.0}, (0.0, 5.0), 5.0), (
        "A's sale closed A's position alone"
    )


def test_a_reduce_only_close_is_held_to_the_first_position_in_the_name_by_id() -> None:
    """What the node's flatten order rests on: the later-sorted strategy's close beside a
    mate is cut to the mate's quantity, on either side, and closes sent in id order each
    close their own."""
    a, b = "AAPL.XNAS-Probe-A", "AAPL.XNAS-Probe-B"

    assert facts._probe_a_reduce_only_close_beside_a_mate() == (
        {a: 10.0, b: 490.0},
        {},
        {a: -5.0, b: 95.0},
        {},
    )


def test_handlers_at_one_priority_are_called_in_the_order_they_subscribed() -> None:
    """What the book's allotment refuses to rest on: the strategy added first, subscribing
    to prints late, hears a print after the one added second, and a bus calls a higher
    priority first and then one priority's handlers in subscription order."""
    heard, called = facts._probe_subscription_order()

    assert heard == ["B", "A"], "subscription order, not the order the strategies were added"
    assert called == ["first", "second", "third"]


def test_two_takers_on_one_touch_are_each_filled_from_all_of_it() -> None:
    """What the book's shared touch budget is for: the venue fills each market order from
    the whole touch, so two buys of 80 under an ask of 100 both fill at the ask."""
    assert facts._probe_two_takers_on_one_touch() == [("A", 80.0, 10.01), ("B", 80.0, 10.01)]


def test_of_two_orders_linked_one_cancels_other_one_fills_and_of_one_updates_other_both() -> None:
    """What a book's count of a member's contingent orders rests on: two sell limits of 100
    linked one-cancels-other, resting until one quote makes both marketable, fill 100
    between them under the emulator and a latency; linked one-updates-other and marketable
    when sent, both fill."""
    from nautilus_trader.model.enums import ContingencyType

    held = facts._probe_contingent_pair(
        ContingencyType.OCO, 10.50, emulated=True, latency_ns=20_000_000
    )

    assert held == [100.0, 0.0]
    assert facts._probe_contingent_pair(ContingencyType.OUO, 9.00) == [100.0, 100.0]
