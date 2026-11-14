"""A stage charges a dated spread at the instant its venue priced each fill at.

A stage is one node whose venue is handed every version's series in an order of its own, and a
restarted stage hands its venue the warmup sessions again before the points after its clock.
Neither is the order a version's own view of the window holds, which is what its extraction is
struck from, so the instant each fill's price was struck at is read off the venue
(`kanso.nautilus.struck`) by the harness and the extraction alike: the balance a sleeve reads
once flat is the equity the stage realises, and each fill records the bar its price came from.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.nautilus import backtest, node
from kanso.nautilus.node import Placement, StageNode
from kanso.schemas import Hypothesis, Limits, VenueModel
from kanso.strategy.impl import Built, Loaded

from .conftest import CAPITAL, SNAPSHOT, VENUE, catalog, instrument
from .test_dated_spread import BPS, WIDTHS, _request

LEAD, LAGG = "LEAD", "LAGG"
TUESDAY, WEDNESDAY = date(2024, 1, 2), date(2024, 1, 3)
"""Two days of January: New York is five hours behind UTC."""
MINUTE_NS = 60 * 1_000_000_000


def _stamp(day: date, hhmm: str) -> int:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return midnight_ns(day) + ((hours + 5) * 60 + minutes) * MINUTE_NS


def _series(symbol: str, step: int, stamps: list[tuple[date, str]], base: float) -> list[Bar]:
    """One name's bars of one step, each closing a cent above the last."""
    kind = BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(step, BarAggregation.MINUTE, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    made = []
    for index, (day, hhmm) in enumerate(stamps):
        close = Price(base + index * 0.01, 2)
        ts = _stamp(day, hhmm)
        made.append(Bar(kind, close, close, close, close, Quantity.from_int(100_000), ts, ts))
    return made


def _placed(
    request: backtest.RunRequest, directory: Path, grains: tuple[str, ...] = ()
) -> Placement:
    cls, config = backtest._sleeve(request)
    if grains:
        config = node._reconfigured(config, extra_resolutions=grains[1:])
    return Placement(
        strategy_id="dated",
        version=1,
        capital=CAPITAL,
        hyp=request.hyp,
        venue_model=VenueModel.model_validate(dict(request.venue_model)),
        snapshot_id=SNAPSHOT,
        period="1d",
        source=request.strategy_source,
        loaded=Loaded(
            manifest=None,  # type: ignore[arg-type]
            directory=directory,
            sleeve=Built("sleeve", request.hyp.id, cls, config),
        ),
        grains=grains,
    )


def _stage(placed: Placement, held: Path, day: date, after: int | None = None) -> StageNode:
    return StageNode(
        stage="paper",
        capital=CAPITAL,
        limits=Limits(
            max_gross_pct=100, max_net_pct=100, per_strategy_max_pct=100, daily_loss_pct=50
        ),
        placements=(placed,),
        window=(day, day),
        catalog=held,
        after=after,
    )


def _balance(record: Path) -> float:
    return float([line for line in record.read_text().splitlines() if line][-1].split()[-1])


TWO_GRAINS = b"""
from pathlib import Path

from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        if bar.bar_type.spec.step != 1:
            return
        name = bar.bar_type.instrument_id.value
        minute = self.session.minute
        if name == "LAGG.XNAS" and minute == 573:
            self.submit_entry(InstrumentId.from_str("LEAD.XNAS"), "BUY", qty=100)
        elif name == "LEAD.XNAS" and minute == 599:
            self.submit_exit(InstrumentId.from_str("LEAD.XNAS"))
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{self.balance!r}\\n")
"""


def test_a_stage_whose_venue_another_series_opened_an_instant_for_charges_what_it_priced(
    tmp_path: Path,
) -> None:
    """LAGG's five-minute bar stamped 09:35 opens that instant in the stage's feed, ahead of
    LEAD's one-minute bar, though a version's own view lists the one-minute bars first. A buy of
    LEAD sent on LAGG's bar stamped 09:34 lands there under 10 ms and fills at LEAD's 09:30
    close, a pre-market price: it pays `pre`, as the venue priced it, in the balance and the
    realised window alike."""
    one = [(TUESDAY, hhmm) for hhmm in ("09:29", "09:30", "09:31", "09:32", "09:33", "09:34")]
    later = [(TUESDAY, hhmm) for hhmm in ("09:36", "09:40", "10:00", "10:01", "10:02", "10:05")]
    fives = [(TUESDAY, hhmm) for hhmm in ("09:30", "09:35", "09:40", "10:00", "10:05")]
    lead = [(TUESDAY, hhmm) for hhmm in ("09:29", "09:30", "09:35")] + later
    held = catalog(
        tmp_path / "catalog",
        [
            *_series(LAGG, 1, one + later, 20.0),
            *_series(LAGG, 5, fives, 70.0),
            *_series(LEAD, 1, lead, 10.0),
            *_series(LEAD, 5, fives, 60.0),
        ],
        [instrument(LEAD), instrument(LAGG)],
    )
    record = tmp_path / "balance.txt"
    base = _request(10.0, record)
    request = backtest.RunRequest(
        hyp=base.hyp,
        strategy_source=TWO_GRAINS,
        window=(TUESDAY, TUESDAY),
        snapshot_id=SNAPSHOT,
        venue_model=base.venue_model,
        capital=CAPITAL,
        overrides={"record": str(record)},
        grains=("1m", "5m"),
    )

    (realised,) = node.run(_stage(_placed(request, tmp_path, ("1m", "5m")), held, TUESDAY)).realised

    bought = next(fill for fill in realised.run.fills if fill.side == "BUY")
    assert bought.px == pytest.approx(10.01)  # LEAD's bar stamped 09:30
    assert bought.price_ts_ns == _stamp(TUESDAY, "09:30")
    assert bought.cost == pytest.approx(bought.notional * WIDTHS[LEAD]["pre"] / 2 / BPS)
    assert _balance(record) == pytest.approx(realised.run.equity[-1], rel=1e-12)


RESTARTED = b"""
from pathlib import Path

from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    record: str = ""


class Strategy(KansoStrategy):
    config_cls = Config

    def on_bar(self, bar):
        name = bar.bar_type.instrument_id.value
        session = self.session
        if name == "LAGG.XNAS" and session.minute == 240 and session.day.day == 3:
            self.submit_entry(InstrumentId.from_str("LEAD.XNAS"), "BUY", qty=100)
        elif name == "LEAD.XNAS" and session.minute == 249 and session.day.day == 3:
            self.submit_exit(InstrumentId.from_str("LEAD.XNAS"))
        with Path(self.kanso_config.record).open("a") as out:
            out.write(f"{self.balance!r}\\n")
"""


def test_a_restarted_stage_charges_a_fill_the_warmup_s_last_bar_priced(tmp_path: Path) -> None:
    """A stage restarted at Tuesday's 19:59 is handed its warmup session again before the
    points after its clock, and a version's own view holds only those. A buy of LEAD sent on
    LAGG's bar stamped 04:01 on Wednesday fills at LEAD's last close, Tuesday's 19:59, with no
    latency at all: it pays Tuesday's `post`, as the venue priced it, not the pre-market of
    the instant it filled at."""
    lagg = [(TUESDAY, "19:58"), (TUESDAY, "19:59")] + [
        (WEDNESDAY, hhmm) for hhmm in ("04:01", "04:02", "04:10", "04:11")
    ]
    lead = [(TUESDAY, "19:58"), (TUESDAY, "19:59"), (WEDNESDAY, "04:10"), (WEDNESDAY, "04:11")]
    held = catalog(
        tmp_path / "catalog",
        [*_series(LAGG, 1, lagg, 20.0), *_series(LEAD, 1, lead, 10.0)],
        [instrument(LEAD), instrument(LAGG)],
    )
    record = tmp_path / "balance.txt"
    base = _request(0.0, record)
    hyp = Hypothesis.model_validate({**base.hyp.model_dump(mode="json"), "warmup": {"sessions": 1}})
    request = backtest.RunRequest(
        hyp=hyp,
        strategy_source=RESTARTED,
        window=(WEDNESDAY, WEDNESDAY),
        snapshot_id=SNAPSHOT,
        venue_model=base.venue_model,
        capital=CAPITAL,
        overrides={"record": str(record)},
    )
    stage = _stage(_placed(request, tmp_path), held, WEDNESDAY, _stamp(TUESDAY, "19:59"))

    (realised,) = node.run(stage).realised

    bought = next(fill for fill in realised.run.fills if fill.side == "BUY")
    assert bought.px == pytest.approx(10.01)  # LEAD's bar stamped 19:59 on Tuesday
    assert bought.price_ts_ns == _stamp(TUESDAY, "19:59")
    assert bought.cost == pytest.approx(bought.notional * WIDTHS[LEAD]["post"] / 2 / BPS)
    assert _balance(record) == pytest.approx(realised.run.equity[-1], rel=1e-12)
