"""What makes a `hypothesis.yaml` admissible, checked in one pass over one file.

The file's own arithmetic — the duration grammar, the embargo between the research and
certification windows, their ordering and disjointness, the resolution and cost model
being answerable from the data the hypothesis asks for, and `strategy_integrity` among a
classified hypothesis's constraints — belongs to the model and is enforced when the
document is parsed. Everything else needs the workspace, and that is this module:

* the file lives at `hypotheses/<id>/`, so the id in the document and the directory that
  holds it are the same word. Directory names are unique, so this is what makes ids
  unique too, and it is what lets every later command find a hypothesis by its id alone;
* the id is not `portfolio`. The id grammar admits it, but a certified sleeve composes a
  strategy named after its hypothesis and `construct.host` spells the book itself as
  `portfolio`, so a sleeve of that name would make a strategy the host field could no
  longer tell from the book. The word is reserved here, where a hypothesis enters the
  workspace, rather than at the seam where the two meanings collide;
* every data requirement names a type the workspace knows — the three market-data types,
  or one an extension registered;
* every universe id resolves to an instrument as of the research window's first day. A
  manual entry answers on its own, a cached resolution answers when it was made as of
  that date, and anything else needs the configured reference adapter. An id that is
  unknown, ambiguous across venues, delisted before that date or listed after it fails,
  and every failing id is reported together. The question is asked and nothing is
  written — not the catalog's instrument store, not the cache: a validation that left
  something behind would not be one, and the store is written by `kanso data instruments
  resolve` alone;
* the venues those instruments trade on resolve to a complete cost model, and to one
  account currency. A spread taken from quotes needs quotes, and is admitted with a warning
  where it states no width (`fixed_bps`) over an instrument of its venue asked for neither
  quote nor book, since a fill there has no quote to read one from (`cost_warnings`); a fixed
  spread needs its width; a universe spanning two account currencies would have a leg priced
  at a rate nothing in the workspace records, so it is refused — and so, for the same reason,
  is an instrument that settles in a currency other than its venue's account currency;
* a venue model whose `limit_fill` is `print_through` or `print_through_whole` fills a
  resting limit on prints and a taker on quotes, on the top-of-book venue, so the hypothesis
  must require `quote` and `trade`, may not require `book`, and may not ask an instrument for
  `bar` beside `quote`, since a bar walks the engine's bid and ask past the quote in force. A
  hypothesis at tick resolution that asks every name for quotes and prints, and none for bars
  or a book, resolves to `print_through` when no layer states a rule (`default_limit_fill`),
  and every other to `touch`, with a warning naming the names that kept a tape of quotes and
  prints there when no layer states one, and each way off it (`cost_warnings`);
* at tick resolution, a venue model may not leave `latency_ms` at zero, which lands an order
  on the very point it was sent from, and on the top-of-book venue may not state a maker
  schedule under `touch` or `through`, which credit a resting limit with no queue ahead of
  it — the refusal naming the names where they alone kept a venue on `touch`; a print-rule
  model that states no maker schedule is admitted with a warning (`check_tick_costs`,
  `cost_warnings`);
* a universe holding a perpetual requires `funding`. A held perpetual pays or is paid its
  funding at every settlement, so a card not handed the realised rates measures a P&L the
  contract never had. What makes an instrument a perpetual is its resolved definition — the
  same one the settlement check reads — never its id;
* when classification has been written, its construct is in the catalogue, its host is
  present exactly when the construct needs one and names a certified strategy, its
  parameters are ones that construct declares with values inside the sets it declares
  them over, its objective applies to this hypothesis and its parameters are inside the
  toolbox's ranges, every constraint is a card-stage gate whose parameters are inside
  theirs, and every gate the toolbox declares `required_for` its objective is among the
  card gates — the operator's `required_constraints` or the classifier's `constraints` —
  with every parameter it declares set. The construct's own parameters are checked by
  asking the construct, which is the same call the runner makes when it builds the
  harness: a classification this command calls admissible is one the first card of a run
  does not reject. A draft may state its objective alone, which `kanso classify` keeps when
  the construct it chooses admits it, and it is then held to applying in some objective
  mode, inside the toolbox's ranges;
* a gate whose parameters mean nothing apart (`together`, as `edge_per_trade`'s
  `min_bps` and `k_se`) is named with every one of them, in either list, on a draft too.

A validation failure names the field and the reason, and where several are independent
they are reported together rather than one per attempt.

`check_strategy` is the one question asked of the `strategy.py` beside the file, and only
`kanso hyp validate` asks it: whether the strategy binds a name its base class owns, which
`strategy_integrity` refuses at a run's baseline card and at every card that carries it.
It is not part of `validate`, because `add` and `classify` validate the file they pin and
the strategy is the research loop's to change.

NautilusTrader facts this module relies on (nautilus_trader 1.231.0): an `Instrument`
carries its venue on `id.venue`, whose `value` is the venue code a venue model is
resolved for; `Instrument.get_settlement_currency()` answers the currency a trade in it
settles in — a `CryptoPerpetual`'s stated `settlement_currency`, and the quote currency of
every other class kanso builds, none of which is inverse; and `get_cost_currency()` answers
the currency the engine books positions, PnL and margin in — the quote currency of every
linear class, a perpetual's included — which its account manager converts to the account's
base currency, deferring the balance update when it holds no rate between the two. A
perpetual swap is built as `nautilus_trader.model.instruments.CryptoPerpetual`, the one
class kanso builds for `instrument_class: swap`, whether a manual entry or a reference
adapter supplied it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from kanso import ext
from kanso.classify.construct import PORTFOLIO
from kanso.classify.construct import catalogue as construct_catalogue
from kanso.criteria import applicable_objectives, check_params, required_card_gates
from kanso.criteria import catalogue as criteria_catalogue
from kanso.criteria.integrity import clashes
from kanso.criteria.objectives import measures_a_hold_over, measures_benchmark
from kanso.data import registry
from kanso.data.instruments import resolve_universe
from kanso.data.types import data_types
from kanso.errors import ValidationError
from kanso.hyp.scaffold import HYPOTHESES, HYPOTHESIS_FILE, hypothesis_dir, hypothesis_file
from kanso.hyp.scaffold import STRATEGY_FILE as STRATEGY_SOURCE
from kanso.nautilus import adapters
from kanso.nautilus.venue import known_currency
from kanso.schemas import (
    Benchmark,
    Book,
    ConstraintRef,
    ConstructRef,
    Hypothesis,
    ObjectiveRef,
    StrategyFile,
    VenueDeclaration,
    VenueModel,
    load_yaml,
    parse_yaml,
    resolve_venue_model,
    single_currency,
)
from kanso.schemas.duration import TICK_RESOLUTIONS, is_duration
from kanso.schemas.venue import (
    DEFAULT_ACCOUNT,
    DEFAULT_CURRENCY,
    DEFAULT_LIMIT_FILL,
    TICK_LIMIT_FILL,
    LimitFill,
)

PERCENT: Final = 100.0

SIZELESS: Final = frozenset({"filter", "exit"})
"""Constructs that place no order of their own, so a sizing rule on them sizes nothing."""

OVERLAY: Final = "overlay"
"""The attached construct that places orders of its own, and so carries a budget."""
"""Constructs that place no order of their own, so a sizing rule on them sizes nothing."""

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.workspace import Workspace

CARD_STAGE: Final = "card"
"""The stage a hypothesis's own constraints run at; the rest are planned at certification."""

QUOTE_TYPE: Final = "quote"

STATE_DB: Final = "state.db"
"""Where the workspace's state store lives, which records what every screen read."""
"""The data requirement a spread read from quotes needs."""

FUNDING: Final = "funding"
"""The data requirement a perpetual in the universe needs: its realised funding rates."""

STRATEGIES: Final = "strategies"
STRATEGY_FILE: Final = "strategy.yaml"

CLASSIFICATION: Final = ("construct", "objective", "constraints")
"""The three fields classification writes; they are written and validated together, but for
an objective a draft states alone."""

MODES: Final = ("absolute", "relative")
"""The objective modes a construct is measured in; a draft has no construct to say which."""


def read_source(path: Path) -> bytes:
    """The file's bytes, which are what a registration is pinned by."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"{path}: cannot be read: {exc}") from None


@dataclass(frozen=True)
class Admitted:
    """An admissible hypothesis, and what it is admitted with that an operator should read."""

    hyp: Hypothesis
    warnings: tuple[str, ...] = ()


def validate(ws: Workspace, path: Path, source: bytes | None = None) -> Hypothesis:
    """The hypothesis at `path`, refused unless everything above holds.

    `source` is the file's bytes when the caller has already read them, so a caller that
    pins those bytes validates exactly what it pins.
    """
    return admit(ws, path, source).hyp


def admit(ws: Workspace, path: Path, source: bytes | None = None) -> Admitted:
    """`validate`, with the warnings an admitted file carries (`cost_warnings`)."""
    raw = read_source(path) if source is None else source
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path}: is not UTF-8 text: {exc}") from None
    hyp = parse_yaml(Hypothesis, text, str(path))
    _check_id(hyp)
    _check_location(ws, path, hyp)
    _check_data_requirements(ws, hyp)
    instruments = resolve_universe(ws, hyp.universe, hyp.windows.research.start, record=False)
    models = venue_models(ws, hyp, instruments)
    _check_funding(hyp, instruments)
    _check_required(ws, hyp)
    _check_sizing(ws, hyp)
    _check_benchmark(hyp)
    _check_book(hyp, models)
    _check_print_through(hyp, models)
    check_tick_costs(ws, hyp, models)
    _check_classification(ws, hyp)
    _check_derived_from(ws, hyp)
    _check_screened(ws, hyp)
    return Admitted(hyp, cost_warnings(ws, hyp, instruments, models))


def _check_derived_from(ws: Workspace, hyp: Hypothesis) -> None:
    """Every hypothesis this one derives from is registered in this workspace.

    The trials a search is priced by are the family's (`kanso.research.records.family`),
    and a parent the store does not know has no cards to count: naming one would read as a
    search priced by more than it was, while counting nothing for it. A workspace whose
    store does not exist yet has registered nothing, so a draft that names a parent there
    is refused until the parent is registered.
    """
    if not hyp.derived_from:
        return
    from kanso.state import StateStore

    path = ws.path(STATE_DB)
    registered: set[str] = set()
    if path.is_file():
        with StateStore(path) as store:
            registered = {
                str(row[0]) for row in store.connection.execute("SELECT hyp_id FROM hypotheses")
            }
    missing = [parent for parent in hyp.derived_from if parent not in registered]
    if missing:
        raise ValidationError(
            f"derived_from: {', '.join(missing)} is not a registered hypothesis of this "
            "workspace, so its cards cannot count toward this one's trials",
            remedy="register the parent with `kanso hyp add` first, or drop it from derived_from",
        )


def _check_screened(ws: Workspace, hyp: Hypothesis) -> None:
    """A certification window may not meet data a recorded screen read on a shared instrument.

    A workspace whose store does not exist yet, or predates screens, has recorded none.
    """
    from kanso.screen.embargo import refuse_screened  # `kanso.screen` imports this package
    from kanso.state import StateStore

    path = ws.path(STATE_DB)
    if not path.is_file():
        return
    with StateStore(path) as store:
        if "screen_results" in store.tables():
            refuse_screened(store, hyp)


def check_strategy(ws: Workspace, hyp_id: str) -> None:
    """The workspace `strategy.py` of a hypothesis, refused when it binds a name its base owns.

    Each binding is named with its line, as the research loop would name it to a proposer.
    A file that is not there, or does not parse, binds nothing this can find.
    """
    directory = hypothesis_dir(ws, hyp_id)
    strategy = directory / STRATEGY_SOURCE
    source = strategy.read_bytes().decode("utf-8", "replace") if strategy.is_file() else ""
    found = clashes(source)
    if not found:
        return
    where = strategy.relative_to(ws.root)
    names = ", ".join(sorted({f"'{clash.name}'" for clash in found}))
    raise ValidationError(
        f"{where}: binds what its base class owns, which `strategy_integrity` refuses before "
        "any card runs: " + "; ".join(clash.problem for clash in found),
        remedy=f"rename {names} in {where} to a name {found[0].base} does not own, then "
        f"`kanso hyp validate {directory.relative_to(ws.root) / HYPOTHESIS_FILE}`",
    )


def _check_benchmark(hyp: Hypothesis) -> None:
    """A benchmark only on a horizon some objective measures a hold over, classified or not.

    A draft carries no objective, and without this it would pass `hyp validate` and `hyp add`
    only to be refused by `kanso classify` after the model had been asked.
    """
    if hyp.benchmark is None or measures_a_hold_over(list(criteria_catalogue().values()), hyp):
        return
    raise ValidationError(
        f"benchmark: declared on a {hyp.horizon} horizon, and no objective measures a hold "
        "over one; a hypothesis held under a day is measured per trade, and a hold has none",
        remedy="remove benchmark from this file, or lengthen the horizon to a day or more",
    )


def venue_models(
    ws: Workspace, hyp: Hypothesis, instruments: Mapping[str, Any]
) -> dict[str, VenueModel]:
    """The resolved trading model of every venue this universe trades on.

    Each venue inherits the account type and currency `[research]` states in `kanso.toml`,
    then the configured broker's declaration, then the operator's `venues.<MIC>` override,
    then the hypothesis's own `costs`. A `[research]` value that restates the shipped
    default is not a layer: it leaves the field's origin at `default`, so a workspace that
    never touched the two keys resolves the model it always did, byte for byte, and no card
    anchor moves. A cost model that cannot be completed — a spread from quotes the
    hypothesis does not require, or a fixed spread with no width — a universe spanning more
    than one account currency, and an account currency the engine does not register are
    all refused here, because each would put a number on a card that nothing in the
    workspace can account for. So is an instrument that settles, or is booked, in a currency
    other than its venue's account currency: its fills would be struck in a currency the
    account holds none of, at a conversion rate nothing in the workspace records.

    The broker is named in `kanso.toml` and its declaration is asked of whichever adapter
    provides it, so this reads a broker's account type, currency and costs without naming
    one. A workspace configured for a broker no adapter here provides inherits nothing and
    falls back to the shipped defaults, which the resolved model records as its origin: a
    missing adapter must not silently change the numbers a card is measured with.
    """
    venues = sorted({_venue_of(held) for held in instruments.values()})
    models = _resolved(ws, hyp, venues, default_limit_fill(hyp))
    for model in models.values():
        known_currency(model.currency)
    single_currency(models)
    _check_settlement(instruments, models)
    return models


def _resolved(
    ws: Workspace, hyp: Hypothesis, venues: Iterable[str], rule: LimitFill
) -> dict[str, VenueModel]:
    """Each venue's model through the layers `venue_models` names, `rule` the one no layer
    stating one leaves it under: resolved under another, a venue whose rule moves with it is
    one no layer states a rule for (`_left_on_touch`)."""
    from kanso.portfolio.files import venue_overrides  # `kanso.portfolio` imports this module

    overrides = venue_overrides(ws)
    quotes = QUOTE_TYPE in hyp.data_requirements
    research = ws.config.research
    broker = research.broker
    config = VenueDeclaration(
        account=None if research.account == DEFAULT_ACCOUNT else research.account,
        currency=None if research.currency == DEFAULT_CURRENCY else research.currency,
    )
    return {
        venue: resolve_venue_model(
            venue,
            config=config,
            broker=broker,
            declaration=adapters.venue_declaration(broker, venue),
            override=overrides.get(venue),
            hypothesis_costs=hyp.costs,
            max_leverage=hyp.risk_limits.max_leverage,
            quotes_available=quotes,
            default_limit_fill=rule,
        )
        for venue in venues
    }


def _check_settlement(instruments: Mapping[str, Any], models: Mapping[str, VenueModel]) -> None:
    """Every instrument settles in, and is booked in, the account currency of its venue.

    Two engine currencies are compared with the account's, and both must equal it: the
    settlement currency (a perpetual's stated one, every other class's quote currency) and
    the cost currency the engine books positions, PnL and margin in and converts to the
    account's base currency from (the quote currency of every linear class kanso builds).
    A perpetual quoted in USDT and settled in USDC settles in one and is booked in the
    other, so no account currency admits it. The check reads every resolved instrument of
    the universe, a data leg as well as a traded one. A mismatch is refused naming the
    instrument, both of its currencies where they differ, and what its venue's account
    holds.
    """
    wrong: dict[str, tuple[str, str, str, str]] = {}
    for held in instruments.values():
        venue = _venue_of(held)
        settles = str(held.get_settlement_currency().code)
        books = str(held.get_cost_currency().code)
        account = models[venue].currency
        if settles != account or books != account:
            wrong[str(held.id)] = (settles, books, venue, account)
    if not wrong:
        return
    named = "; ".join(
        f"{instrument} {_currencies(settles, books)}, and {venue}'s account currency is {account}"
        for instrument, (settles, books, venue, account) in sorted(wrong.items())
    )
    instrument, (settles, books, venue, _) = sorted(wrong.items())[0]
    if settles != books:
        remedy = (
            f"remove {instrument} from `universe` in hypothesis.yaml: no one account "
            f"currency is both its settlement currency {settles} and its booked currency {books}"
        )
    else:
        remedy = (
            f"set venues.{venue}.currency to {settles} in portfolio.yaml, or [research] "
            f"currency to {settles} in kanso.toml"
        )
    raise ValidationError(
        f"universe: {named}; a hypothesis's fills settle and are booked in the account's "
        "own currency",
        remedy=remedy,
    )


def _check_funding(hyp: Hypothesis, instruments: Mapping[str, Any]) -> None:
    """A universe holding a perpetual requires the `funding` data type.

    A perpetual is recognised by its resolved definition, a `CryptoPerpetual`, never by the
    spelling of its id. Every perpetual missing its funding is named together. The
    requirement is the hypothesis's, and coverage asks it of the perpetuals alone
    (`kanso.data.snapshot`), so a spot leg beside one needs no funding history.
    """
    from nautilus_trader.model.instruments import CryptoPerpetual

    if FUNDING in hyp.data_requirements:
        return
    perpetuals = sorted(
        str(held.id) for held in instruments.values() if isinstance(held, CryptoPerpetual)
    )
    if not perpetuals:
        return
    raise ValidationError(
        f"data_requirements: {', '.join(perpetuals)} "
        f"{'is a perpetual' if len(perpetuals) == 1 else 'are perpetuals'} and {FUNDING} is "
        "not required; a perpetual's P&L is not honest without the funding it paid and was paid",
        remedy=f"add {FUNDING} to data_requirements and load its realised funding history",
    )


def _currencies(settles: str, books: str) -> str:
    if settles == books:
        return f"settles in {settles}"
    return f"settles in {settles} and is booked in {books}"


def _venue_of(instrument: Any) -> str:
    return str(instrument.id.venue.value)


def _check_id(hyp: Hypothesis) -> None:
    """`portfolio` is the book, so it is not a hypothesis.

    The id grammar admits the word and both meanings would be legitimate: a certified
    sleeve composes a strategy named after its hypothesis, and `construct.host` names the
    book itself with that same word. A workspace holding both has a `host: portfolio` that
    means two things, and the construct reads it as the book — so an overlay properly
    attached to the sleeve is refused for the portfolio it was never on. Reserving the
    word costs an operator one id and keeps `construct.host` unambiguous.
    """
    if hyp.id == PORTFOLIO:
        raise ValidationError(
            f"id: {PORTFOLIO!r} is reserved: it is how a construct attached to the book names "
            f"its host, and a certified sleeve of that name would compose a strategy nothing "
            f"could tell from the book",
            remedy=f"choose another id, and rename {HYPOTHESES}/{PORTFOLIO}/ to match",
        )


def _check_location(ws: Workspace, path: Path, hyp: Hypothesis) -> None:
    """A file under `hypotheses/` sits in the directory its own id names."""
    root = ws.path(HYPOTHESES).resolve()
    directory = path.resolve().parent
    if directory.parent != root:
        return
    if directory.name != hyp.id:
        raise ValidationError(
            f"id: {hyp.id!r} is declared in {HYPOTHESES}/{directory.name}/, and a hypothesis "
            f"lives in the directory its id names",
            remedy=f"rename the directory to {hyp.id!r}, or set id to {directory.name!r}",
        )


def _check_data_requirements(ws: Workspace, hyp: Hypothesis) -> None:
    known = _known_types(ws)
    unknown = [required for required in hyp.data_requirements if required not in known]
    if unknown:
        raise ValidationError(
            f"data_requirements: {', '.join(unknown)} is not a data type this workspace knows; "
            f"it knows {', '.join(sorted(known))}",
            remedy="require one of those, or install the extension that registers the type",
        )


def _known_types(ws: Workspace) -> dict[str, type]:
    """Every type a `data_requirements` entry may name here.

    Discovery imports the workspace's extensions, and asking each registered adapter for
    its loaders imports those, which is when either registers a custom type of its own. So
    a type a vendor adapter or an extension introduces is known to validation and not only
    once something has loaded with it. Neither costs a credential: an adapter hands out
    loader factories and none is built here.
    """
    extensions = ext.discover(ws.root, ws.config.extensions_paths)
    registry.adapter_loaders(ws, extensions)
    return data_types()


def _check_required(ws: Workspace, hyp: Hypothesis) -> None:
    """The card-stage gates the operator requires, checked whenever the file is read.

    Unlike the classification's three fields these stand alone: a draft may carry them and
    a classified hypothesis may not have been re-read since they were written, so they are
    checked here rather than inside `_check_classification`. Naming one twice is refused,
    because the two entries would disagree about the parameters and nothing says which
    wins.
    """
    required = hyp.required_constraints
    if not required:
        return
    counted = Counter(ref.id for ref in required)
    twice = sorted(name for name, n in counted.items() if n > 1)
    if twice:
        raise ValidationError(
            f"required_constraints: {', '.join(twice)} named more than once",
            remedy="give each gate one entry, with the parameters it is to run under",
        )
    _check_constraints(ws, hyp, required, where="required_constraints")


def _check_sizing(ws: Workspace, hyp: Hypothesis) -> None:
    """A budget the ceilings can fund, on a construct that places orders.

    `full_book` sizes every entry to the whole budget in one instrument, so the position
    ceiling and the leverage ceiling must each reach it — a budget above either would size
    an order the limits then cut, and the rule would be a hope again. A filter places no
    order and an exit rule places only the host's, so neither has anything to size.
    """
    sizing = hyp.sizing
    if sizing is None:
        return
    if hyp.construct is not None and hyp.construct.id in SIZELESS:
        raise ValidationError(
            f"sizing: a {hyp.construct.id} places no order of its own, so it has nothing to size",
            remedy="remove `sizing`, or attach the rule to the host sleeve",
        )
    capital, source = _capital(ws, hyp)
    limits = hyp.risk_limits
    ceiling = capital * limits.max_position_pct / PERCENT
    if ceiling < sizing.budget:
        raise ValidationError(
            f"sizing.budget: {sizing.budget:g} is more than max_position_pct admits: "
            f"{limits.max_position_pct:g}% of the {capital:g} capital{source} is {ceiling:g}, "
            "and a full-book entry is the whole budget",
            remedy="raise risk_limits.max_position_pct, raise capital, or lower sizing.budget",
        )
    funded = capital * limits.max_leverage
    if funded < sizing.budget:
        raise ValidationError(
            f"sizing.budget: {sizing.budget:g} is more than max_leverage funds: "
            f"{limits.max_leverage:g} x {capital:g} capital{source} is {funded:g}",
            remedy="raise capital, raise risk_limits.max_leverage, or lower sizing.budget",
        )


def _check_book(hyp: Hypothesis, models: Mapping[str, VenueModel]) -> None:
    """A policy that moves money needs an account that can hold what it moves.

    A monthly reset restores a deficit from the cushion at the period end, and the
    strategy then sizes against the restored book while the venue's balance stands where
    the losses left it; a carry is a charge on notional held above the equity, which a
    cash account cannot hold at all. Both are margin-account bookkeeping, so on a cash
    venue they are refused rather than applied to a balance the venue would not fund.
    """
    if hyp.book is None or not hyp.book.funded:
        return
    cash = sorted(venue for venue, model in models.items() if model.account == "cash")
    if not cash:
        return
    moved = "a monthly reset" if hyp.book.reset != "none" else "a financing carry"
    if hyp.book.reset != "none" and hyp.book.financing_rate_bps > 0:
        moved = "a monthly reset and a financing carry"
    raise ValidationError(
        f"book: {moved} needs a margin account, and {', '.join(cash)} resolves to a cash "
        "account, which cannot fund a restore or hold a borrowed notional",
        remedy=f"set venues.{cash[0]}.account to margin in portfolio.yaml, or set "
        "book.reset to none and book.financing_rate_bps to 0",
    )


PRINT_RULES: Final = frozenset({"print_through", "print_through_whole"})
"""The `limit_fill` values under which a resting limit fills on prints and a taker on quotes."""

TRADE_TYPE: Final = "trade"
BOOK_TYPE: Final = "book"
BAR_TYPE: Final = "bar"


def _check_print_through(hyp: Hypothesis, models: Mapping[str, VenueModel]) -> None:
    """The print rules fill a resting limit only on a print through it and a taker only on
    a quote, on the top-of-book venue: a hypothesis under one, from whichever layer it was
    stated at, requires both feeds and no book, and asks no instrument for bars beside its
    quotes — a bar walks the engine's own bid and ask past the quote in force, so the venue
    would judge a taker marketable on prices the quote never showed."""
    ruled = sorted(v for v, model in models.items() if model.costs.limit_fill in PRINT_RULES)
    if not ruled:
        return
    rule = models[ruled[0]].costs.limit_fill
    missing = [kind for kind in (QUOTE_TYPE, TRADE_TYPE) if kind not in hyp.data_requirements]
    if missing:
        raise ValidationError(
            f"costs.limit_fill: {rule} on {', '.join(ruled)} fills a resting limit only on a "
            f"print through it and a taker only on the last quote, and data_requirements has "
            f"no {' or '.join(missing)}",
            remedy="add quote and trade to data_requirements, or state limit_fill: touch or "
            "through",
        )
    if BOOK_TYPE in hyp.data_requirements:
        raise ValidationError(
            f"costs.limit_fill: {rule} on {', '.join(ruled)} is a rule for the top-of-book "
            "venue, and requiring book builds a level-two one",
            remedy="drop book from data_requirements, or state limit_fill: touch or through",
        )
    barred = _barred(hyp)
    if barred:
        raise ValidationError(
            f"costs.limit_fill: {rule} on {', '.join(ruled)} fills a taker on the quote in "
            f"force, and {', '.join(barred)} is asked for bar beside quote: a bar moves the "
            "book the venue judges a taker marketable from past that quote",
            remedy="ask each instrument that carries quotes for quote and trade alone under "
            "data_by_instrument, keeping bar for an instrument asked for bar alone; with none "
            + (
                "left, drop bar from data_requirements, and bar_resolution with it; "
                if hyp.bar_resolution
                else "left, drop bar from data_requirements and research at resolution: tick, "
                "since a bar size requires bar; "
            )
            + "or state limit_fill: touch or through",
        )


def _barred(hyp: Hypothesis) -> list[str]:
    """The names asked for bar beside quote, over which a print rule is refused."""
    return [name for name in hyp.universe if {BAR_TYPE, QUOTE_TYPE} <= set(hyp.required_of(name))]


def _both_feeds(hyp: Hypothesis) -> str:
    """The way off `touch` the names leave open: every name asked for both feeds and none for
    `bar`, each keeping every other type it is asked for, and `bar` dropped from
    `data_requirements` where it is there, and `bar_resolution` with it. A type no name is
    asked for is refused, so the `funding` a perpetual requires, the series a session scope
    reads and any other type stay asked of the names that were asked for them, and `bar`,
    which no name is left asking for, goes — with the bar size a tick resolution reads it at,
    which no bar is left to be read at. Keyed on `bar` being required, not on a name asked for
    it beside quote: a signal asked for bars alone leaves it required as well."""
    if BAR_TYPE in hyp.data_requirements:
        return (
            "ask every name, traded or not, for quote and trade and not for bar under "
            "data_by_instrument, keeping every other type it is asked for, and drop bar from "
            "data_requirements, and bar_resolution with it"
        )
    return (
        "ask every name, traded or not, for quote and trade under data_by_instrument, keeping "
        "every other type it is asked for"
    )


def default_limit_fill(hyp: Hypothesis) -> LimitFill:
    """The rule a hypothesis's venues resolve to when no layer states one.

    `print_through` for a hypothesis at tick resolution that asks every name of its universe for
    both quotes and prints, no name for bars and nothing for a book, and `touch` for every
    other: bars, a book whose venue keeps a queue of its own, a tape with no prints for a
    resting order to be filled from, and a tape with a name asked for one feed and not the other
    (`_off_the_tape`, which `cost_warnings` names). The print rule fills a taker only on a quote
    in force and a resting limit only on a print, so a taker in a name with no quotes never
    fills and a resting limit in one with no prints never does: a file that fills under `touch`
    would fill nothing there, and nothing would say so. On a tape `touch` credits a resting
    limit with no queue ahead of it, all that is left of it from any point beyond its price, and
    the size a quote shows again at every quote that shows it; the print rule credits it only
    from a print through it, by that print's size, which is the tape's own evidence that the
    order would have traded. A rule a layer states is the operator's and stands, `touch`
    included.
    """
    if hyp.resolution not in TICK_RESOLUTIONS or BOOK_TYPE in hyp.data_requirements:
        return DEFAULT_LIMIT_FILL
    if not {QUOTE_TYPE, TRADE_TYPE} <= set(hyp.data_requirements) or _off_the_tape(hyp):
        return DEFAULT_LIMIT_FILL
    return TICK_LIMIT_FILL


def _off_the_tape(hyp: Hypothesis) -> list[str]:
    """The names the print rule could not fill as `touch` does: each asked for no quote or no
    trade, or for a bar beside them, which walks the engine's bid and ask past the quote."""
    return [
        name
        for name in hyp.universe
        if not {QUOTE_TYPE, TRADE_TYPE} <= set(hyp.required_of(name))
        or BAR_TYPE in hyp.required_of(name)
    ]


def check_tick_costs(ws: Workspace, hyp: Hypothesis, models: Mapping[str, VenueModel]) -> None:
    """Two venue models a hypothesis at tick resolution cannot be measured under.

    A `latency_ms` of zero, from every layer: the venue lands every order on the very point the
    strategy was handed when it sent it, before anything later on the tape — a fill no route to
    a venue gives, worth a lane's whole edge where it was measured (+10.7 bp a session, gone at
    any real delay). The refusal states no latency: which one, measured on the account and route
    the strategy trades through, is the operator's to say, and any above zero is admitted.

    A maker schedule — `maker_bps` or `maker_per_share`, from any layer — under `touch` or
    `through` on the top-of-book venue: those rules credit a resting limit with no queue ahead
    of it, all that is left of it from any point beyond its price and, under `touch`, the size
    a quote shows again at every quote that shows it, so the schedule would price as a maker's
    fills the tape did not give. The print rules credit a resting limit only from a print
    through it, and a level-two book keeps a queue of its own, so neither is refused. A venue
    no layer states a rule for that its names alone keep on `touch` (`_left_on_touch`) is
    refused naming each such name and what it is asked for, and the remedy leads with asking
    every name for both feeds and none for bar (`_both_feeds`), which moves it to the print
    rule: the warning that would say so (`cost_warnings`) is never reached. A print rule is
    refused over a name asked for bar beside quote (`_check_print_through`), so where one is,
    stating a print rule is offered only beside asking the names for both feeds — and so only
    where both are required, since a name is asked only for a type that is — and not where
    that asking alone moves a venue no layer states a rule for.

    Both are structural: each refuses a configuration that cannot describe a market, the way a
    spread from quotes with no quotes is refused, and neither names a number a hypothesis must
    clear. Bars are left alone — a bar's walk is the run's own approximation, and there is no
    rule that fills a resting limit from a tape there.
    """
    if hyp.resolution not in TICK_RESOLUTIONS:
        return
    instant = sorted(venue for venue, model in models.items() if model.costs.latency_ms <= 0)
    if instant:
        raise ValidationError(
            f"costs.latency_ms: 0 on {', '.join(instant)} at resolution {hyp.resolution} lands "
            "every order on the point it was sent from, before anything later on the tape, "
            "which no route to a venue does",
            remedy="state costs.latency_ms — the round trip measured on the account and route "
            f"you trade through — in the hypothesis costs or under venues.{instant[0]}.costs "
            "in portfolio.yaml",
        )
    if BOOK_TYPE in hyp.data_requirements:
        return
    queue_free = sorted(
        venue
        for venue, model in models.items()
        if model.costs.limit_fill not in PRINT_RULES
        and (model.costs.maker_bps is not None or model.costs.maker_per_share is not None)
    )
    if not queue_free:
        return
    costs = models[queue_free[0]].costs
    key = "maker_bps" if costs.maker_bps is not None else "maker_per_share"
    left = [venue for venue in _left_on_touch(ws, hyp, models) if venue in queue_free]
    named = (
        f"; no layer states a rule for {', '.join(left)}, which a hypothesis holding quotes and "
        "prints at tick resolution resolves to print_through only if every name is asked for "
        f"quote and trade and none for bar: {_asked(hyp)}"
        if left
        else ""
    )
    moved = (
        f"{_both_feeds(hyp)}, which moves a venue no layer states a rule for to print_through; or "
        if left
        else ""
    )
    ruled = (
        "state limit_fill: print_through or print_through_whole, which fills a resting limit "
        "only on a later print through it and needs every name the strategy trades asked for "
        "quote and trade, and no book; or "
    )
    if _barred(hyp):
        taped = {QUOTE_TYPE, TRADE_TYPE} <= set(hyp.data_requirements)
        ruled = f"{_both_feeds(hyp)}, and {ruled}" if taped and not left else ""
    raise ValidationError(
        f"costs.{key}: a maker schedule on {', '.join(queue_free)} under limit_fill: "
        f"{costs.limit_fill} prices as a maker's the fills that rule credits a resting limit "
        "with no queue ahead of it — all that is left of it from any point beyond its price, "
        f"and under touch the size a quote shows again at every quote that shows it{named}",
        remedy=f"{moved}{ruled}remove maker_bps and maker_per_share from the hypothesis costs "
        f"and from venues.{queue_free[0]}.costs in portfolio.yaml",
    )


def cost_warnings(
    ws: Workspace,
    hyp: Hypothesis,
    instruments: Mapping[str, Any],
    models: Mapping[str, VenueModel],
) -> tuple[str, ...]:
    """What an admitted hypothesis's venue models charge that an operator should read.

    A print rule with no maker schedule charges a fill that rested on the book as any fill at
    its price — `slippage_bps` and half the spread on top of the commission, which a resting
    order filled at its own price inside the quote never paid in the market. A hypothesis that
    holds quotes and prints at tick resolution and that its names alone keep on `touch`
    (`_left_on_touch`) is told which names those are and why: `touch` credits its resting
    limits with no queue ahead of them. A rule a layer states, `touch` included, is the
    operator's and draws no word. And a spread read from quotes with no width over a name
    asked for neither quote nor book is named (`_unpriced`).
    """
    warnings: list[str] = []
    unscheduled = sorted(
        venue
        for venue, model in models.items()
        if model.costs.limit_fill in PRINT_RULES
        and model.costs.maker_bps is None
        and model.costs.maker_per_share is None
    )
    if unscheduled:
        rule = models[unscheduled[0]].costs.limit_fill
        warnings.append(
            f"costs: no maker_bps or maker_per_share on {', '.join(unscheduled)} under "
            f"limit_fill: {rule}, so a fill that rested on the book is charged as any fill at "
            "its price — slippage_bps and half the spread on top of the commission; state the "
            "account's maker schedule, 0 for none and negative for a rebate"
        )
    left = _left_on_touch(ws, hyp, models)
    if left:
        warnings.append(_off_the_tape_warning(hyp, left))
    unpriced = _unpriced(hyp, models, instruments)
    if unpriced:
        warnings.append(unpriced)
    return tuple(warnings)


def _left_on_touch(ws: Workspace, hyp: Hypothesis, models: Mapping[str, VenueModel]) -> list[str]:
    """The venues a tape of quotes and prints is on `touch` for only because of its names
    (`default_limit_fill`): resolved again with the print rule as the default, each moves to
    it, where a venue a layer states a rule for stays where that layer put it."""
    required = set(hyp.data_requirements)
    tape = hyp.resolution in TICK_RESOLUTIONS and BOOK_TYPE not in required
    if not (tape and {QUOTE_TYPE, TRADE_TYPE} <= required and _off_the_tape(hyp)):
        return []
    taped = _resolved(ws, hyp, models, TICK_LIMIT_FILL)
    return sorted(
        venue
        for venue, model in models.items()
        if model.costs.limit_fill != taped[venue].costs.limit_fill
    )


def _asked(hyp: Hypothesis) -> str:
    """What each name that keeps a tape off the print rule (`_off_the_tape`) is asked for."""
    return "; ".join(
        f"{name} is asked for {', '.join(hyp.required_of(name))}" for name in _off_the_tape(hyp)
    )


def _off_the_tape_warning(hyp: Hypothesis, venues: list[str]) -> str:
    """Why the names left these venues on `touch`, each reason only where a name gives it, and
    each way off the warning the names leave open.

    The default reads every name of the universe, traded or not, since the file cannot say
    which names the strategy trades: asking each for both feeds and none for `bar`, each
    keeping every other type it is asked for, with `bar` dropped from `data_requirements` where
    it is there (`_both_feeds`), moves the venues to the print rule.
    Stating the print rule moves them too where no name is asked for bar beside quote, over
    which it is refused, but a name it starves of a feed then never fills, so that way is
    offered for signals alone. Stating `touch` keeps the rule and ends the warning.
    """
    off = [set(hyp.required_of(name)) for name in _off_the_tape(hyp)]
    short = any(not {QUOTE_TYPE, TRADE_TYPE} <= kinds for kinds in off)
    barred = any({BAR_TYPE, QUOTE_TYPE} <= kinds for kinds in off)
    reasons = []
    if short:
        reasons.append(
            "the print rules fill a taker only on a quote in force and a resting limit only on "
            "a print, so a name asked for no quote or no trade could not fill as it does under "
            "touch"
        )
    if barred:
        reasons.append(
            "a bar walks the engine's bid and ask past the quote in force, on which the print "
            "rules fill a taker, so a print rule over a name asked for bar beside quote is "
            "refused"
        )
    because = ", and ".join(reasons)
    signals = (
        ", or, if each name short of a feed is a signal the strategy never trades, state "
        "limit_fill: print_through, under which a market order in a name asked for no quote, "
        "and a resting limit in one asked for no trade, never fills"
        if short and not barred
        else ""
    )
    return (
        f"costs.limit_fill: touch on {', '.join(venues)}, which credits a resting limit with "
        "no queue ahead of it, and not print_through, which a hypothesis holding quotes and "
        "prints at tick resolution resolves to when no layer states a rule only if every name "
        f"is asked for quote and trade and none for bar: {_asked(hyp)}. {because[0].upper()}"
        f"{because[1:]}; to move to print_through, {_both_feeds(hyp)}{signals}; to stay on touch "
        "without this warning, state limit_fill: touch"
    )


def _unpriced(
    hyp: Hypothesis, models: Mapping[str, VenueModel], instruments: Mapping[str, Any]
) -> str | None:
    """A model that reads a fill's half-spread from quotes and states no width, over an
    instrument asked for neither quote nor book, charges a fill there no spread at all: struck
    at a print or a bar's close, its price carries none, and there is no quote before it to
    read one from. A stated `fixed_bps` is the width such a fill pays. On a venue that matches
    a level-two book every fill is struck at the book and pays no half-spread, so it needs
    none. It is a warning, not a refusal, because the file cannot say whether the strategy
    trades the instrument: a signal read and never traded — the trigger `kanso screen draft`
    writes as prints beside a follower read as quotes — is charged nothing either way, and a
    screen's hurdle refuses a leg it trades read so (`kanso.screen.hurdle`)."""
    if BOOK_TYPE in hyp.data_requirements:
        return None
    unpriced = {
        venue
        for venue, model in models.items()
        if model.costs.spread == "quotes" and model.costs.fixed_bps is None
    }
    named = [
        name
        for name in hyp.universe
        if _venue_of(instruments[name]) in unpriced
        and not {QUOTE_TYPE, BOOK_TYPE} & set(hyp.required_of(name))
    ]
    if not named:
        return None
    venues = sorted({_venue_of(instruments[name]) for name in named})
    one = len(named) == 1
    return (
        f"costs.spread: quotes on {', '.join(venues)} reads a fill's half-spread from the "
        f"quote before it, and {', '.join(named)} {'is' if one else 'are'} asked for no quote "
        "or book, so a fill there is charged no spread; if the strategy trades "
        f"{'it' if one else 'them'}, state fixed_bps in the hypothesis costs or under "
        f"venues.{venues[0]}.costs in portfolio.yaml — a fill with no quote before it pays "
        f"half of it — or ask {'it' if one else 'them'} for quote under data_by_instrument"
    )


def _capital(ws: Workspace, hyp: Hypothesis) -> tuple[float, str]:
    """The capital a run of this hypothesis starts with, and where the number came from."""
    if hyp.capital is not None:
        return hyp.capital, ""
    return ws.config.research.capital, " (from kanso.toml research.capital)"


def _check_classification(ws: Workspace, hyp: Hypothesis) -> None:
    """The three fields classification writes, when they are written.

    A draft may state its objective alone: it is the operator's naming of what the
    hypothesis is scored on — a return target as a contribution — and `kanso classify`
    keeps it when the construct it chooses is measured in its mode. Until then nothing says
    which mode that is, so it is held to applying in one of them.
    """
    written = (hyp.construct, hyp.objective, hyp.constraints)
    absent = [name for name, value in zip(CLASSIFICATION, written, strict=True) if value is None]
    if len(absent) == len(CLASSIFICATION):
        return
    construct, objective, constraints = written
    if construct is None and constraints is None and objective is not None:
        _check_stated(ws, hyp, objective)
        return
    if construct is None or objective is None or constraints is None:
        raise ValidationError(
            "; ".join(
                f"{name}: missing; a classified hypothesis carries "
                f"{', '.join(CLASSIFICATION)} together"
                for name in absent
            ),
            remedy="write the missing field, or clear all three and classify again",
        )
    mode = _check_construct(ws, hyp, construct)
    _check_objective(ws, hyp, objective, mode)
    _check_constraints(ws, hyp, constraints)
    _check_guarded(hyp, objective)


def _check_stated(ws: Workspace, hyp: Hypothesis, ref: ObjectiveRef) -> None:
    """An objective a draft states applies to it in some objective mode, at a keep rule
    inside the toolbox's ranges."""
    item = criteria_catalogue().get(ref.id)
    if item is None or item.kind != "objective":
        raise ValidationError(
            f"objective.id: {ref.id!r} is not an objective in the toolbox",
            remedy="name one the toolbox holds, or clear it and let `kanso classify` name one",
        )
    applicable = sorted({found for mode in MODES for _, found in applicable_objectives(hyp, mode)})
    if ref.id not in applicable:
        raise ValidationError(
            f"objective.id: {ref.id!r} does not apply to this hypothesis in either objective "
            f"mode; the applicable objectives are {', '.join(applicable)}",
            remedy="name one of the applicable objectives, or clear it and let "
            "`kanso classify` name one",
        )
    problems = check_params(item, ref.params.model_dump(), hyp, ws.config.research.folds)
    if problems:
        raise ValidationError("; ".join(f"objective.params.{problem}" for problem in problems))


def _check_guarded(hyp: Hypothesis, objective: ObjectiveRef) -> None:
    """Every card gate the objective requires is carried.

    The toolbox says which, in each gate's `required_for`: `edge_per_trade` guards the
    contributions, because a contribution moves toward zero as losing trades are dropped and
    a card that trades less at a worse loss per trade is kept under one. Whether it runs is
    not a decision; the values it runs at are, and either list may carry it. A gate that is
    `required_for` an objective declares `together`, so wherever it is named it was already
    held to every parameter it declares.
    """
    named = {ref.id for ref in hyp.card_gates}
    missing = [gate for gate in required_card_gates(objective.id) if gate not in named]
    if not missing:
        return
    items = criteria_catalogue()
    gate = missing[0]
    ranges = items[gate].ranges
    params = ", ".join(
        f"{name}: <{ranges[name][0]}..{ranges[name][1]}>" if name in ranges else f"{name}: …"
        for name in items[gate].params
    )
    raise ValidationError(
        "; ".join(
            f"constraints: {one} is required of a hypothesis scored on {objective.id}, which "
            "rewards trading less once every trade loses"
            for one in missing
        ),
        remedy=f"add `{{id: {gate}, params: {{{params}}}}}` to required_constraints or "
        "constraints, the values yours, then `kanso hyp validate` the file again",
    )


def _check_construct(ws: Workspace, hyp: Hypothesis, ref: ConstructRef) -> str:
    """The construct's objective mode, once it exists, its params fit and its host is right.

    The parameters are checked by asking the construct rather than against a copy of its
    declarations kept here, so this command and the harness the runner builds refuse the
    same classification in the same words: a parameter the construct does not declare, or a
    value outside its set, is refused where the file is judged rather than inside the first
    card of a run.
    """
    construct = construct_catalogue(ws).get(ref.id)
    construct.check_params(ref.params)
    needs = construct.needs_host
    if needs == "none":
        if ref.host is not None:
            raise ValidationError(
                f"construct.host: {ref.id} is a strategy of its own and attaches to nothing, "
                f"but names {ref.host!r}"
            )
    elif ref.host is None:
        raise ValidationError(
            f"construct.host: {ref.id} attaches to a {needs}, so it names the host it attaches to",
            remedy=f"set construct.host to a certified strategy, or classify onto a {PORTFOLIO}",
        )
    elif needs == PORTFOLIO:
        if ref.host != PORTFOLIO:
            raise ValidationError(
                f"construct.host: {ref.id} attaches to the book, so its host is {PORTFOLIO!r}, "
                f"not {ref.host!r}"
            )
    else:
        _check_host_strategy(ws, hyp, ref.id, ref.host)
    return str(construct.objective_mode)


def _check_host_strategy(ws: Workspace, hyp: Hypothesis, construct_id: str, host: str) -> None:
    """The host names a composed strategy, which is what a certified hypothesis becomes."""
    path = ws.path(STRATEGIES, host, STRATEGY_FILE)
    if not path.is_file():
        raise ValidationError(
            f"construct.host: {host!r} is not a certified strategy of this workspace; "
            f"a {construct_id} attaches to one",
            remedy="certify and compose the host sleeve first, or name another host",
        )
    strategy = load_yaml(StrategyFile, path)
    if strategy.id != host:
        raise ValidationError(
            f"construct.host: {path} declares the strategy {strategy.id!r}, not {host!r}"
        )
    _check_host_pairing(ws, hyp, construct_id, host, strategy)


def _check_host_pairing(
    ws: Workspace, hyp: Hypothesis, construct_id: str, host: str, strategy: StrategyFile
) -> None:
    """What an attached construct must agree with its host on: the grain, the warmup, the
    book policy, the size the host's bars are read at and the sizing rule.

    A filter or an exit rule is consulted on the host's grain and has no clock of its own,
    so its resolution is the host's — and, over a host that reads bars beside ticks, its
    `bar_resolution` with it. A card of an attached construct runs the host sleeve
    underneath it, warmed as this file says, so the warmup has to be the host's own or the
    host is measured cold — or warm — under a construct that never asked for that; the
    book policy likewise, since a construct's cards run under this file's policy and its
    version is deployed under the sleeve's; and `bar_resolution` too, since the host's bars
    are read at the size this file states, and a host certified on one size would be
    handed another, bars of no size, or bars it never read (`_host_bars`). So an overlay
    with a bar clock of its own is refused over a host that reads bars beside ticks: a file
    at a bar-size resolution states no `bar_resolution`, so its cards would hand the host
    bars of no size, and the one way off is the host's own resolution and size. A
    budgeted overlay attaches to a budgeted host and an unbudgeted one to an unbudgeted
    host, because the overlay's book is the host's budget and its own together; and that
    book has to fit the ceilings the overlay's own file declares, since an overlay card
    funds the venue and configures the host with them. Read from the host sleeve's
    hypothesis file where the workspace holds one.
    """
    latest = strategy.latest()
    path = hypothesis_file(ws, latest.sleeve.hyp_id)
    if not path.is_file():
        return
    host_hyp = load_yaml(Hypothesis, path)
    label = f"{host}@{latest.version}"
    if hyp.warmup != host_hyp.warmup:
        theirs = host_hyp.warmup.sessions if host_hyp.warmup else 0
        raise ValidationError(
            f"warmup: {label} warms on {theirs} session(s) before its window and this file "
            f"declares {hyp.warmup.sessions if hyp.warmup else 0}; a construct runs its host "
            "underneath it, and the host warms as the construct's own file says",
            remedy=(
                f"drop warmup to match {path}"
                if host_hyp.warmup is None
                else f"set warmup to {{sessions: {theirs}}} to match {path}"
            ),
        )
    if hyp.book != host_hyp.book:
        host_book = "none" if host_hyp.book is None else _book_text(host_hyp.book)
        raise ValidationError(
            f"book: {label} runs under a book policy of {host_book} and this file declares "
            f"{'none' if hyp.book is None else _book_text(hyp.book)}; a construct's cards run "
            "its host under this file's policy, and its version is deployed under the host's",
            remedy=(
                f"drop book to match {path}"
                if host_hyp.book is None
                else f"set book to {host_book} to match {path}"
            ),
        )
    if construct_id in SIZELESS and hyp.resolution != host_hyp.resolution:
        raise ValidationError(
            f"resolution: {hyp.resolution} is not the host's {host_hyp.resolution}; a "
            f"{construct_id} is consulted on the host's grain, and only an overlay has a "
            "clock of its own",
            remedy=(
                f"set resolution to {host_hyp.resolution} and bar_resolution to "
                f"{host_hyp.bar_resolution}, requiring bar"
                if host_hyp.bar_resolution is not None
                else f"set resolution to {host_hyp.resolution}, or attach as an overlay"
                if hyp.bar_resolution is None
                else _host_bars(host_hyp, hyp, path, regrain=True)[1]
            ),
        )
    if host_hyp.bar_resolution is not None and is_duration(hyp.resolution):
        raise ValidationError(
            f"resolution: {hyp.resolution} is a bar clock of this {construct_id}'s own, and "
            f"{label} reads its bars beside its {host_hyp.resolution} grain at "
            f"{host_hyp.bar_resolution}; a construct's cards run its host reading bars at the "
            "size this file states, and a file at a bar-size resolution states none",
            remedy=f"set resolution to {host_hyp.resolution} and bar_resolution to "
            f"{host_hyp.bar_resolution}, requiring bar, to match {path}",
        )
    if hyp.bar_resolution != host_hyp.bar_resolution:
        reason, remedy = _host_bars(host_hyp, hyp, path)
        raise ValidationError(f"bar_resolution: {label} {reason}", remedy=remedy)
    if construct_id != OVERLAY:
        return
    if hyp.sizing is not None and host_hyp.sizing is None:
        raise ValidationError(
            f"sizing: {label} was composed without a sizing rule, so its budget is unknown; "
            "a budgeted construct attaches to a budgeted host",
            remedy=f"add sizing to {path}, certify and compose it again, then re-pin this file",
        )
    if hyp.sizing is None and host_hyp.sizing is not None:
        raise ValidationError(
            f"sizing: {label} sizes to a budget of {host_hyp.sizing.budget:g}, and a construct "
            "attached to a budgeted host declares its own",
            remedy="add sizing to this file",
        )
    if hyp.sizing is None or host_hyp.sizing is None:
        return
    capital, source = _capital(ws, hyp)
    limits = hyp.risk_limits
    together = host_hyp.sizing.budget + hyp.sizing.budget
    funded = capital * limits.max_leverage
    if funded < together:
        raise ValidationError(
            f"sizing.budget: {hyp.sizing.budget:g} on an overlay is funded by its host's book, "
            f"and {capital:g} capital{source} x {limits.max_leverage:g} leverage is {funded:g}, "
            f"less than the host's {host_hyp.sizing.budget:g} budget and this one together",
            remedy=f"set capital to at least {together:g}, raise risk_limits.max_leverage, "
            "or lower sizing.budget",
        )
    ceiling = capital * limits.max_position_pct / PERCENT
    if ceiling < host_hyp.sizing.budget:
        raise ValidationError(
            f"risk_limits.max_position_pct: {limits.max_position_pct:g}% of the {capital:g} "
            f"book is {ceiling:g}, and the host's {host_hyp.sizing.budget:g} budget is one "
            "instrument",
            remedy=f"set max_position_pct to at least "
            f"{host_hyp.sizing.budget / capital * PERCENT:g}",
        )


def _host_bars(
    host_hyp: Hypothesis, hyp: Hypothesis, path: Path, *, regrain: bool = False
) -> tuple[str, str]:
    """Why this file's `bar_resolution` does not pair it with its host, and the remedy that does.

    A host that states `bar_resolution` reads its bars at that size, and a construct's cards run
    it reading them at the size the construct's own file states. One that states none reads
    them at its own resolution where that is a bar size, and, at `tick`, `quote` or `trade`,
    reads no bars at all, since the schema requires the key wherever `bar` is required there.
    A construct that states one is at such a resolution and requires `bar`, and dropping the
    key alone would leave that requirement at no size, which the schema refuses. So over a
    host reading none it drops the bars with the size — and a name it asked for bars alone,
    since a name left asking for nothing is refused too, and with no remedy — and over a
    bar-size host it takes the host's resolution and drops the key, reading bars at the
    host's size on the host's own clock. Only an overlay states a size over a bar-size host
    here, since a filter or an exit stating one is off the host's grain and refused on it first,
    and the overlay's card would hand the host the overlay's own tick, quote or trade clock as a
    bar grain beside the host's, at which no bar can be subscribed (`docs/backlog.md` row 234),
    whatever size it states. `regrain` is that grain refusal's: its one remedy takes the host's
    resolution as well — and, at `quote` or `trade`, requires that kind, since a file at either
    resolution requires it and asks some instrument for it, which a file off that grain need
    not have done.
    """
    stated = hyp.bar_resolution or "none"
    size = host_hyp.bar_resolution
    runs = (
        f"and this file declares {stated}; a construct runs its host underneath it, and the host "
        "reads its bars at the size the construct's own file states"
    )
    if size is not None:
        return f"reads its bars at {size} {runs}", (
            f"set bar_resolution to {size} to match {path}, requiring bar at resolution "
            f"{host_hyp.resolution}"
        )
    if is_duration(host_hyp.resolution):
        return (
            f"states none, reading its bars at its own resolution, {host_hyp.resolution}, and "
            f"this file declares {stated}; an overlay's card hands its host this file's "
            f"{hyp.resolution} clock as a bar grain beside its own, at which no bar can be "
            "subscribed"
        ), f"set resolution to {host_hyp.resolution} and drop bar_resolution, to match {path}"
    grain = f"set resolution to {host_hyp.resolution}, " if regrain else ""
    kind = (
        f", and require {host_hyp.resolution} in data_requirements and in every "
        "data_by_instrument entry left"
        if regrain and host_hyp.resolution != "tick"
        else ""
    )
    return f"reads no bars beside its {host_hyp.resolution} grain {runs}", (
        f"{grain}drop bar_resolution, and bar from data_requirements and from every "
        "data_by_instrument entry, an entry that asked for bar alone dropped with its name from "
        f"the universe{kind}, to match {path}"
    )


def _book_text(book: Book) -> str:
    """A book policy as the file would spell it, for a refusal naming the host's."""
    fields = book.model_dump(exclude_none=True)
    return "{" + ", ".join(f"{name}: {value}" for name, value in fields.items()) + "}"


def _check_objective(ws: Workspace, hyp: Hypothesis, ref: ObjectiveRef, mode: str) -> None:
    item = criteria_catalogue().get(ref.id)
    if item is None or item.kind != "objective":
        raise ValidationError(
            f"objective.id: {ref.id!r} is not an objective in the toolbox",
            remedy="run `kanso classify` to have one chosen, or name one the toolbox holds",
        )
    applicable = [found for _, found in applicable_objectives(hyp, mode)]
    if ref.id not in applicable:
        raise ValidationError(
            f"objective.id: {ref.id!r} does not apply to this hypothesis; the applicable "
            f"{mode} objectives are {', '.join(sorted(applicable))}",
            remedy=_objective_remedy(hyp, ref, mode, applicable),
        )
    problems = check_params(item, ref.params.model_dump(), hyp, ws.config.research.folds)
    if problems:
        raise ValidationError("; ".join(f"objective.params.{problem}" for problem in problems))
    if hyp.benchmark is not None and not measures_benchmark(hyp):
        raise ValidationError(
            f"benchmark: declared, and {ref.id} measures nothing against it; a {mode} "
            "objective is measured against the host's run, and only a sleeve or an alpha "
            "against a hold of its first leg",
            remedy="remove benchmark from this file; the construct is measured against its host",
        )


def _objective_remedy(hyp: Hypothesis, ref: ObjectiveRef, mode: str, applicable: list[str]) -> str:
    """What to write when an objective does not apply, naming `benchmark` when it moved the grid.

    Adding or removing the key on a classified file changes which absolute Sharpe applies,
    so the objective id has to move in the same edit; the parameters do not.
    """
    toggled = hyp.model_copy(
        update={"benchmark": None if hyp.benchmark else Benchmark(hold="first_leg")}
    )
    if ref.id in (found for _, found in applicable_objectives(toggled, mode)):
        if hyp.benchmark is not None:
            return f"set objective.id to {applicable[0]}, or remove benchmark"
        return f"declare `benchmark: {{hold: first_leg}}`, or set objective.id to {applicable[0]}"
    return (
        "name one of the applicable objectives, or clear construct, objective and "
        "constraints and run `kanso classify`"
    )


def _check_constraints(
    ws: Workspace, hyp: Hypothesis, constraints: Sequence[ConstraintRef], where: str = "constraints"
) -> None:
    """Every named gate is a card-stage gate of the toolbox and its parameters fit.

    `where` names the list being checked, because a hypothesis has two: `constraints`,
    which classification writes, and `required_constraints`, which only the operator does.
    The rules are the same for both and the complaint has to say which list it is about.
    """
    items = criteria_catalogue()
    folds = ws.config.research.folds
    problems: list[str] = []
    for constraint in constraints:
        item = items.get(constraint.id)
        if item is None or item.kind != "gate":
            problems.append(f"{where}.{constraint.id}: is not a gate in the toolbox")
        elif item.stage != CARD_STAGE:
            problems.append(
                f"{where}.{constraint.id}: runs at the {item.stage} stage, and a "
                f"hypothesis constrains only the {CARD_STAGE} stage"
            )
        else:
            problems.extend(
                f"{where}.{constraint.id}.{problem}"
                for problem in check_params(item, constraint.params, hyp, folds)
            )
    if problems:
        raise ValidationError("; ".join(problems))
