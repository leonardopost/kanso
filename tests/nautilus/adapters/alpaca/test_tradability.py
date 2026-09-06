"""What the broker says about an instrument, and what this adapter refuses to invent.

Four properties are under test. The measured equity row is carried across whole — the
five flags, the status, the exchange and the venue it implies — and the three increment
fields whose *absence* was measured refuse nothing, because they are crypto fields and an
overlay that required them would refuse every equity there is. A flag the broker did not
send is never given a value: the instrument is recorded as undescribed with the field
named, and every permission that flag would have granted is withheld, so a card cannot
short a name on a borrow flag nobody read. What is held has an age, and a flag older than
the bound is refused rather than acted on. And nothing — a repr, a report, a refusal —
carries a credential.

Nothing here opens a socket or resolves a real credential. The keys are the suite's own
non-credentials; only their prefixes matter.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from kanso import creds
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus.adapters.alpaca import BROKER, CREDENTIALS
from kanso.nautilus.adapters.alpaca.config import (
    DATA_HOST,
    KEY_HEADER,
    LIVE_CLIENT,
    LIVE_HOST,
    PAPER_CLIENT,
    PAPER_HOST,
    SECRET_HEADER,
    Response,
    credential_names,
)
from kanso.nautilus.adapters.alpaca.tradability import (
    ACTIVE,
    ASSETS_PATH,
    DEFAULT_MAX_AGE_S,
    Overlay,
    Tradability,
    Undescribed,
    overlay,
)
from kanso.workspace import Workspace, init

from . import ASSET, LIVE_KEY, PAPER_KEY, SECRET, Replay, body

NOW = 1_788_000_000_000_000_000
"""The instant every test's clock reads, so an age is exact rather than approximate."""

APPLE = InstrumentId.from_str("AAPL.XNAS")
CLIENT_KEYS = {PAPER_CLIENT: PAPER_KEY, LIVE_CLIENT: LIVE_KEY}

CRYPTO: Mapping[str, Any] = {
    **ASSET,
    "symbol": "BTCUSD",
    "class": "crypto",
    "exchange": "CRYPTO",
    "min_order_size": "0.0001",
    "min_trade_increment": "0.0001",
    "price_increment": "1",
}
"""A row of the class this adapter does not trade, carrying the three increments an equity
row does not. Constructed rather than measured — the read-only pass saw no crypto row —
and its only job is to show that the class check refuses before an increment is read."""


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A fresh workspace with none of this adapter's variables set anywhere."""
    for names in CREDENTIALS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    return init(tmp_path / "ws")


def with_credentials(ws: Workspace, client_id: str = PAPER_CLIENT) -> Workspace:
    """The same workspace whose `.env` holds that client's key and secret."""
    key_name, secret_name = credential_names(client_id)
    path = ws.root / creds.ENV_FILE
    lines = [f"{key_name}={CLIENT_KEYS[client_id]}", f"{secret_name}={SECRET}"]
    path.write_text(path.read_text() + "\n".join(lines) + "\n")
    return ws


def rows(**by_symbol: Any) -> Replay:
    """A transport answering each symbol with a frozen body, and 404 for anything else."""

    def answer(url: str, params: Mapping[str, str]) -> Response:
        found = by_symbol.get(url.rsplit("/", 1)[-1])
        if found is None:
            return body({"code": 40410000, "message": "asset not found"}, status=404)
        return found if isinstance(found, Response) else body(found)

    return Replay(answer)


def opened(
    ws: Workspace,
    transport: Replay,
    *,
    client_id: str | None = PAPER_CLIENT,
    now: int = NOW,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> Overlay:
    """An overlay on a frozen transport and a frozen clock."""
    return overlay(
        ws,
        client_id=client_id,
        transport=transport,
        clock=lambda: now,
        max_age_s=max_age_s,
    )


def described(ws: Workspace, row: Mapping[str, Any] = ASSET, **kwargs: Any) -> Tradability:
    """One loaded row, asserted to be one the broker described."""
    symbol = str(row["symbol"])
    found = opened(with_credentials(ws), rows(**{symbol: row}), **kwargs).load([symbol])[symbol]
    assert isinstance(found, Tradability)
    return found


# --- what the broker says, carried across --------------------------------------


def test_the_measured_equity_row_becomes_the_flags_the_broker_reported(ws: Workspace) -> None:
    """AAPL as the live account described it: active, tradable, lendable, fractionable."""
    found = described(ws)

    assert (found.symbol, found.asset.status, found.asset.exchange) == ("AAPL", ACTIVE, "NASDAQ")
    assert (found.asset.tradable, found.asset.marginable) == (True, True)
    assert (found.asset.shortable, found.asset.easy_to_borrow) == (True, True)
    assert found.asset.fractionable is True


def test_the_broker_s_own_exchange_becomes_the_venue_of_the_instrument(ws: Workspace) -> None:
    found = described(ws)

    assert (found.instrument_id, found.venue) == (APPLE, "XNAS")


def test_the_increments_an_equity_row_omits_refuse_nothing(ws: Workspace) -> None:
    """Their absence was measured. They are crypto fields; requiring them refuses equities."""
    found = described(ws)

    assert (found.asset.min_order_size, found.asset.min_trade_increment) == (None, None)
    assert found.asset.price_increment is None
    assert found.forbids(side=OrderSide.BUY, quantity=Decimal(10)) is None


def test_the_lookup_is_the_measured_endpoint_on_the_account_s_own_host(ws: Workspace) -> None:
    transport = rows(AAPL=ASSET)
    opened(with_credentials(ws), transport).load(["AAPL"])

    asked = transport.asked[0]
    assert (asked.method, asked.url) == ("GET", f"{PAPER_HOST}{ASSETS_PATH}/AAPL")
    assert asked.headers[KEY_HEADER] == PAPER_KEY
    assert asked.headers[SECRET_HEADER] == SECRET
    assert asked.params == {}


def test_the_live_account_addresses_the_live_host_and_reads_its_own_variables(
    ws: Workspace,
) -> None:
    transport = rows(AAPL=ASSET)
    opened(with_credentials(ws, LIVE_CLIENT), transport, client_id=LIVE_CLIENT).load(["AAPL"])

    assert transport.asked[0].url.startswith(LIVE_HOST)
    assert transport.asked[0].headers[KEY_HEADER] == LIVE_KEY
    assert DATA_HOST not in transport.asked[0].url


# --- a flag the broker did not send --------------------------------------------


@pytest.mark.parametrize(
    "flag", ["tradable", "marginable", "shortable", "easy_to_borrow", "fractionable"]
)
def test_a_flag_the_row_omits_is_undescribed_rather_than_defaulted(
    ws: Workspace, flag: str
) -> None:
    """Read as true it shorts what it could not borrow; as false it stops a name it could."""
    row = {key: value for key, value in ASSET.items() if key != flag}
    found = opened(with_credentials(ws), rows(AAPL=row)).load(["AAPL"])["AAPL"]

    assert isinstance(found, Undescribed)
    assert flag in found.reason


def test_an_instrument_whose_row_was_refused_is_granted_no_permission(ws: Workspace) -> None:
    row = {key: value for key, value in ASSET.items() if key != "shortable"}
    held = opened(with_credentials(ws), rows(AAPL=row))
    held.load(["AAPL"])

    refusal = held.forbids("AAPL", side=OrderSide.BUY, quantity=Decimal(1))

    assert refusal is not None
    assert "did not describe AAPL" in refusal


def test_a_short_is_refused_when_a_neighbouring_flag_was_the_missing_one(ws: Workspace) -> None:
    """`shortable` said true; `easy_to_borrow` said nothing, so the row is not evidence.

    The headline of this module: a card may not short a name on a row nobody could read,
    however encouraging the half of it that arrived happens to be.
    """
    row = {key: value for key, value in ASSET.items() if key != "easy_to_borrow"}
    held = opened(with_credentials(ws), rows(AAPL=row))
    held.load(["AAPL"])

    assert row["shortable"] is True
    assert held.forbids(APPLE, side=OrderSide.SELL, quantity=Decimal(1)) is not None


def test_an_instrument_nothing_was_read_about_is_refused_for_that_reason(ws: Workspace) -> None:
    """A different sentence from the broker having forbidden it, and it says so."""
    refusal = opened(ws, rows()).forbids("AAPL", side=OrderSide.BUY, quantity=Decimal(1))

    assert refusal is not None
    assert "nothing has been read from the broker about AAPL" in refusal


# --- what the flags permit ------------------------------------------------------


def test_a_short_of_a_name_the_broker_will_not_lend_is_refused(ws: Workspace) -> None:
    found = described(ws, {**ASSET, "shortable": False})

    refusal = found.forbids(side=OrderSide.SELL, quantity=Decimal(10))

    assert refusal is not None
    assert "does not permit a short sale of AAPL" in refusal


def test_a_short_of_a_hard_to_borrow_name_is_permitted_and_carries_a_caveat(
    ws: Workspace,
) -> None:
    """The broker accepts these; it is the locate that may fail, not the permission."""
    found = described(ws, {**ASSET, "easy_to_borrow": False})

    assert found.forbids(side=OrderSide.SELL, quantity=Decimal(10)) is None
    assert found.caveats == ("AAPL is shortable but not easy to borrow, so a locate may fail",)


def test_selling_into_a_long_position_is_not_a_short_sale(ws: Workspace) -> None:
    found = described(ws, {**ASSET, "shortable": False})

    assert (
        found.forbids(side=OrderSide.SELL, quantity=Decimal(10), position_qty=Decimal(10)) is None
    )
    assert found.forbids(side=OrderSide.SELL, quantity=Decimal(11), position_qty=Decimal(10))


def test_a_sale_with_no_position_stated_is_read_as_opening_a_short(ws: Workspace) -> None:
    """The conservative default: the borrow flag is consulted rather than passed over."""
    found = described(ws, {**ASSET, "shortable": False})

    assert found.forbids(side=OrderSide.SELL, quantity=Decimal(1)) is not None


def test_a_buy_never_consults_the_borrow_flag(ws: Workspace) -> None:
    found = described(ws, {**ASSET, "shortable": False, "easy_to_borrow": False})

    assert found.forbids(side=OrderSide.BUY, quantity=Decimal(10)) is None


def test_a_fractional_quantity_needs_a_fractionable_instrument(ws: Workspace) -> None:
    whole = described(ws, {**ASSET, "fractionable": False})
    part = described(ws)

    refusal = whole.forbids(side=OrderSide.BUY, quantity=Decimal("1.5"))

    assert refusal is not None
    assert "fractional quantity of AAPL" in refusal
    assert part.forbids(side=OrderSide.BUY, quantity=Decimal("1.5")) is None


def test_a_quantity_of_nothing_is_not_an_order(ws: Workspace) -> None:
    found = described(ws)

    assert (
        found.forbids(side=OrderSide.BUY, quantity=Decimal(0)) == "a quantity of 0 is not an order"
    )


def test_a_status_other_than_active_refuses_by_name(ws: Workspace) -> None:
    found = described(ws, {**ASSET, "status": "inactive"})

    refusal = found.forbids(side=OrderSide.BUY, quantity=Decimal(1))

    assert refusal is not None
    assert "'inactive'" in refusal and ACTIVE in refusal


def test_a_row_the_broker_does_not_mark_tradable_is_refused(ws: Workspace) -> None:
    found = described(ws, {**ASSET, "tradable": False})

    assert found.forbids(side=OrderSide.BUY, quantity=Decimal(1)) == (
        "the broker does not accept orders for AAPL"
    )


def test_a_class_kanso_does_not_trade_here_is_refused_before_its_increments_are_read(
    ws: Workspace,
) -> None:
    """The one row that carries the increments is the one the class check refuses first."""
    found = described(ws, CRYPTO)

    refusal = found.forbids(side=OrderSide.BUY, quantity=Decimal("0.5"))

    assert refusal is not None
    assert "'crypto'" in refusal
    assert found.asset.min_order_size == Decimal("0.0001")


def test_marginable_is_reported_and_enforced_nowhere(ws: Workspace) -> None:
    """It describes how a position is financed, not whether an order is accepted."""
    found = described(ws, {**ASSET, "marginable": False})

    assert found.forbids(side=OrderSide.BUY, quantity=Decimal(1)) is None
    assert found.caveats == ("AAPL is not marginable, so a position in it is financed in cash",)


# --- the venue must agree -------------------------------------------------------


def test_a_row_filed_under_another_venue_does_not_vouch_for_this_instrument(
    ws: Workspace,
) -> None:
    held = opened(with_credentials(ws), rows(AAPL=ASSET))
    held.load(["AAPL"])

    refusal = held.forbids(
        InstrumentId.from_str("AAPL.XNYS"), side=OrderSide.BUY, quantity=Decimal(1)
    )

    assert refusal is not None
    assert "lists AAPL on XNAS" in refusal and "order is for XNYS" in refusal


def test_an_exchange_this_adapter_does_not_map_yields_no_instrument_id(ws: Workspace) -> None:
    """An invented venue would re-key every card, order and manifest the name appears in."""
    found = described(ws, {**ASSET, "exchange": "XLON"})

    assert (found.instrument_id, found.venue) == (None, None)


def test_an_unmapped_exchange_is_named_when_an_order_disagrees_with_it(ws: Workspace) -> None:
    held = opened(with_credentials(ws), rows(AAPL={**ASSET, "exchange": "XLON"}))
    held.load(["AAPL"])

    refusal = held.forbids(APPLE, side=OrderSide.BUY, quantity=Decimal(1))

    assert refusal is not None
    assert "lists AAPL on XLON" in refusal


def test_a_symbol_asked_for_by_name_is_answered_without_a_venue_to_compare(
    ws: Workspace,
) -> None:
    held = opened(with_credentials(ws), rows(AAPL=ASSET))
    held.load([APPLE])

    assert held.forbids("AAPL", side=OrderSide.BUY, quantity=Decimal(1)) is None


# --- what is held has an age ----------------------------------------------------


def test_a_flag_older_than_the_bound_is_refused_rather_than_acted_on(ws: Workspace) -> None:
    held = opened(with_credentials(ws), rows(AAPL=ASSET), max_age_s=60.0)
    held.load(["AAPL"])
    held._clock = lambda: NOW + 61_000_000_000

    refusal = held.forbids(APPLE, side=OrderSide.BUY, quantity=Decimal(1))

    assert refusal is not None
    assert "read 61 seconds ago" in refusal and "60 seconds is not evidence" in refusal


def test_a_flag_read_within_the_bound_is_still_evidence(ws: Workspace) -> None:
    held = opened(with_credentials(ws), rows(AAPL=ASSET), max_age_s=60.0)
    held.load(["AAPL"])
    held._clock = lambda: NOW + 60_000_000_000

    assert held.forbids(APPLE, side=OrderSide.BUY, quantity=Decimal(1)) is None


def test_an_age_is_never_negative(ws: Workspace) -> None:
    """A clock that stepped backwards makes a row younger than new, not fresher than fresh."""
    found = described(ws)

    assert found.age_s(NOW - 5_000_000_000) == 0.0
    assert found.stale(NOW - 5_000_000_000, 0.0) is False


# --- what the broker answers with -----------------------------------------------


def test_a_symbol_the_broker_does_not_list_is_one_instrument_s_failure(ws: Workspace) -> None:
    found = opened(with_credentials(ws), rows(AAPL=ASSET)).load(["AAPL", "NOPE"])

    assert isinstance(found["AAPL"], Tradability)
    assert found["NOPE"] == Undescribed("NOPE", "the broker does not list it")


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_stops_the_call_rather_than_marking_the_symbol(
    ws: Workspace, status: int
) -> None:
    """It says nothing about the instrument, so it must not be recorded against one."""
    transport = rows(AAPL=Response(status=status, body=b"{}"))

    with pytest.raises(PreconditionError) as failure:
        opened(with_credentials(ws), transport).load(["AAPL"])

    assert failure.value.code is Exit.PRECONDITION
    assert "credentials of 'alpaca_paper'" in failure.value.message
    assert "KANSO_ALPACA_PAPER_API_KEY" in str(failure.value.remedy)


def test_a_rate_limit_names_the_setting_that_produces_it(ws: Workspace) -> None:
    transport = rows(AAPL=Response(status=429))

    with pytest.raises(PreconditionError) as failure:
        opened(with_credentials(ws), transport).load(["AAPL"])

    assert "requests_per_minute" in str(failure.value.remedy)


def test_any_other_refusal_stops_the_call(ws: Workspace) -> None:
    transport = rows(AAPL=Response(status=503))

    with pytest.raises(KansoError) as failure:
        opened(with_credentials(ws), transport).load(["AAPL"])

    assert failure.value.code is Exit.ERROR
    assert "HTTP 503" in failure.value.message


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"null"])
def test_a_body_that_is_not_one_row_leaves_the_instrument_undescribed(
    ws: Workspace, payload: bytes
) -> None:
    transport = rows(AAPL=Response(status=200, body=payload))

    found = opened(with_credentials(ws), transport).load(["AAPL"])["AAPL"]

    assert found == Undescribed("AAPL", "the broker answered with a body that is not one row")


def test_a_row_about_another_symbol_is_not_taken_for_this_one(ws: Workspace) -> None:
    transport = rows(AAPL={**ASSET, "symbol": "AAPL.OLD"})

    found = opened(with_credentials(ws), transport).load(["AAPL"])["AAPL"]

    assert found == Undescribed("AAPL", "the broker answered about 'AAPL.OLD' rather than 'AAPL'")


def test_a_fault_below_the_answer_is_one_outcome_named_as_such(ws: Workspace) -> None:
    """The fault's own text is not repeated: only its type, the host and the symbol."""

    def explode(url: str, params: Mapping[str, str]) -> Response:
        raise TimeoutError("connection to 10.0.0.1 timed out")

    with pytest.raises(KansoError) as failure:
        opened(with_credentials(ws), Replay(explode)).load(["AAPL"])

    assert failure.value.code is Exit.ERROR
    assert "TimeoutError" in failure.value.message
    assert "10.0.0.1" not in failure.value.message


def test_a_refusal_the_transport_already_named_passes_through(ws: Workspace) -> None:
    def refuse(url: str, params: Mapping[str, str]) -> Response:
        raise PreconditionError("alpaca: the connection is not configured")

    with pytest.raises(PreconditionError) as failure:
        opened(with_credentials(ws), Replay(refuse)).load(["AAPL"])

    assert failure.value.message == "alpaca: the connection is not configured"


# --- a symbol is never interpolated into a url ----------------------------------


@pytest.mark.parametrize(
    "bad", ["../orders", "AA/PL", "", "  ", "A B", "AAAAAAAAAAAAAAAAA", "%2e%2e"]
)
def test_a_lookup_is_never_built_out_of_an_arbitrary_string(ws: Workspace, bad: str) -> None:
    """A symbol becomes a path segment, so `/v2/assets/../orders` is refused before it is sent."""
    transport = rows(AAPL=ASSET)

    with pytest.raises(ValidationError) as failure:
        opened(with_credentials(ws), transport).load([bad])

    assert failure.value.code is Exit.VALIDATION
    assert transport.asked == []


def test_a_symbol_is_keyed_the_same_whether_it_arrives_as_text_or_an_instrument(
    ws: Workspace,
) -> None:
    held = opened(with_credentials(ws), rows(AAPL=ASSET))
    held.load(["aapl"])

    assert isinstance(held.of(APPLE), Tradability)
    assert held.of("AAPL") is held.of(APPLE)


# --- load is the refresh --------------------------------------------------------


def test_nothing_is_asked_and_no_credential_resolved_when_nothing_is_wanted(
    ws: Workspace,
) -> None:
    """With every variable unset, which is how the whole suite runs."""
    transport = rows()

    assert opened(ws, transport).load([]) == {}
    assert transport.asked == []


def test_a_repeated_symbol_is_asked_once(ws: Workspace) -> None:
    transport = rows(AAPL=ASSET)
    opened(with_credentials(ws), transport).load(["AAPL", "aapl", APPLE])

    assert len(transport.asked) == 1


def test_load_always_asks_because_it_is_the_refresh(ws: Workspace) -> None:
    transport = rows(AAPL=ASSET)
    held = opened(with_credentials(ws), transport)
    held.load(["AAPL"])
    held.load(["AAPL"])

    assert len(transport.asked) == 2


def test_reading_what_is_held_never_asks(ws: Workspace) -> None:
    """A lookup from inside an order hook would block the trading loop on a socket."""
    transport = rows(AAPL=ASSET)
    held = opened(with_credentials(ws), transport)

    assert held.of("AAPL") is None
    assert held.forbids("AAPL", side=OrderSide.BUY, quantity=Decimal(1)) is not None
    assert transport.asked == []


# --- one connection -------------------------------------------------------------


def test_the_overlay_borrows_the_connection_it_is_handed(ws: Workspace) -> None:
    transport = rows(AAPL=ASSET)

    assert opened(ws, transport).transport() is transport


def test_a_standalone_overlay_builds_the_one_rate_limited_connection_once(
    ws: Workspace,
) -> None:
    """Three clients for one account would be three times the broker's published limit."""

    class Engine:
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("no test reaches the network")

    built: list[Engine] = []

    def factory(requests_per_minute: int, timeout_s: int) -> Engine:
        built.append(Engine())
        return built[-1]

    held = overlay(with_credentials(ws), factory=factory)

    assert held.transport() is held.transport()
    assert len(built) == 1


# --- which account is read ------------------------------------------------------


def test_the_paper_account_is_read_when_a_caller_names_none(ws: Workspace) -> None:
    """Both accounts see the same reference data; only one of them can move money."""
    with_credentials(ws, PAPER_CLIENT)
    with_credentials(ws, LIVE_CLIENT)

    assert overlay(ws).client_id == PAPER_CLIENT


def test_the_live_account_is_read_when_it_is_the_only_one_configured(ws: Workspace) -> None:
    with_credentials(ws, LIVE_CLIENT)

    assert overlay(ws).client_id == LIVE_CLIENT
    assert overlay(ws).host == LIVE_HOST


def test_with_no_account_configured_the_failure_names_the_paper_variables(ws: Workspace) -> None:
    held = opened(ws, rows(AAPL=ASSET), client_id=None)

    with pytest.raises(PreconditionError) as failure:
        held.load(["AAPL"])

    assert held.client_id == PAPER_CLIENT
    assert "KANSO_ALPACA_PAPER_API_KEY is not set" in failure.value.message


def test_a_client_this_broker_does_not_declare_is_refused(ws: Workspace) -> None:
    with pytest.raises(PreconditionError):
        overlay(ws, client_id="sandbox")


# --- reports, and what they may not carry ---------------------------------------


def test_a_report_holds_every_instrument_in_symbol_order(ws: Workspace) -> None:
    held = opened(with_credentials(ws), rows(AAPL=ASSET))
    held.load(["AAPL", "NOPE"])

    report = held.describe()

    assert report["adapter"] == "alpaca"
    assert report["client"] == PAPER_CLIENT
    assert [row["symbol"] for row in report["instruments"]] == ["AAPL", "NOPE"]  # type: ignore[union-attr,index]


def test_a_described_row_reports_the_increments_the_broker_stated(ws: Workspace) -> None:
    equity = described(ws).as_dict()
    crypto = described(ws, CRYPTO).as_dict()

    assert equity["min_order_size"] is None
    assert equity["described"] is True
    assert (crypto["min_order_size"], crypto["price_increment"]) == ("0.0001", "1")


def test_an_undescribed_row_reports_why(ws: Workspace) -> None:
    assert Undescribed("NOPE", "the broker does not list it").as_dict() == {
        "symbol": "NOPE",
        "described": False,
        "reason": "the broker does not list it",
    }


def test_no_credential_reaches_a_repr_a_report_or_a_refusal(ws: Workspace) -> None:
    """A repr reaches tracebacks, logs and crash reports; a report reaches a commit."""
    transport = rows(AAPL=ASSET)
    held = opened(with_credentials(ws), transport)
    held.load(["AAPL", "NOPE"])

    written = [
        repr(held),
        str(held.describe()),
        str(held.forbids("NOPE", side=OrderSide.SELL, quantity=Decimal(1))),
        str(held.forbids("MSFT", side=OrderSide.SELL, quantity=Decimal(1))),
        transport.asked[0].url,
        str(transport.asked[0].params),
    ]

    for text in written:
        assert PAPER_KEY not in text
        assert SECRET not in text
    assert repr(held) == "Overlay(client_id='alpaca_paper', held=2)"


# --- the entry point the registry reaches this module by ------------------------


def test_the_overlay_opens_without_a_credential_and_without_a_socket(ws: Workspace) -> None:
    """`doctor` and every listing run with all four variables unset."""
    found = BROKER.tradability(ws)

    assert isinstance(found, Overlay)
    assert found.client_id == PAPER_CLIENT
    assert found._transport is None
