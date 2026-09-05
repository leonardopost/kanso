"""Instrument resolution: from an id and a date to an engine instrument definition.

A hypothesis names its universe as ids. Everything downstream — a bar type, an order, a
position, a risk check — needs a NautilusTrader `Instrument`, and getting from one to the
other is this module's whole job. It is a *dated* question: an instrument definition is
what it was on a date, so resolution takes an `as_of`, and an id that was not yet listed on
that date, or was already delisted, is a failure rather than a definition.

Four things can go wrong and each is named separately, because the operator's next action
differs for each: the id is **unknown**, it is **ambiguous** across venues, it was
**delisted** before the date, or it was **listed after** it. A failure names the id and the
reason, and every id that failed is reported together, so one round trip fixes the file.

Three sources answer, in order. The **cache** — an entry whose `resolved` block records a
resolution made as of exactly this date, whose definition is still in the catalog's
instrument store, and which still honours the operator's `override`. A **manual** entry,
which suppresses resolution entirely and carries its own constructor fields: the path the
file loaders, the synthetic loader and the demo take, so a workspace needs no vendor and no
credential to run. Otherwise the workspace's configured **reference adapter**, of which the
core ships none — kanso knows no vendor — so a workspace with none configured refuses,
naming the id.

`override` is applied after resolution and before construction: it is the operator's
correction of a definition, not a note about one, so it reaches the constructor. Tick and
lot come from the dated convention table, never from a vendor.

What is resolved is written to the catalog's instrument store, which is the registry of
record, and the cache is written back to the workspace file, touching only the fields kanso
owns — `nautilus_id`, `asset_class`, `resolved` and `sources` — and leaving `override`,
`attributes`, `corporate_actions` and `manual` exactly as the operator wrote them. The file
is rewritten only when a resolution actually changed it.

NautilusTrader facts this module relies on (nautilus_trader 1.231.0):

* The five instrument classes are `Equity`, `OptionContract`, `FuturesContract`,
  `CurrencyPair` and `IndexInstrument`. Their constructors are Cython, have no
  introspectable signature, and raise `TypeError` for a missing or unexpected argument, so
  the accepted field set per class is stated here and checked before construction.
* Tick size, lot size and multiplier are constructor inputs with no engine defaults.
  `Equity` fixes `size_precision` to 0 and `size_increment` and `multiplier` to 1 itself,
  and rejects all three as arguments.
* `price_precision` must equal `price_increment.precision`, and `size_precision`
  `size_increment.precision`, so the precisions are derived from the increments rather than
  asked for.
* `ParquetDataCatalog.write_data([instrument])` stores a definition and `instruments()`
  reads it back unchanged, which is what makes a content address a usable cache key.
* Every instrument class exposes `to_dict`, a canonical field map of plain scalars, which
  is what is content-addressed.
* The engine's own `InstrumentProvider` is a different interface: it takes an already
  fully qualified `InstrumentId`, is asynchronous, and answers through an internal cache.
  Discovering a venue for a bare symbol, and answering a synchronous caller, are outside it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from kanso.data import conventions
from kanso.errors import ValidationError
from kanso.schemas import InstrumentEntry, InstrumentsFile, Resolved, load_yaml, write_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.workspace import Workspace

CACHE_NAME = "instruments.yaml"
"""The workspace file holding the cache, its provenance and the operator's overrides."""

REFERENCE_PRICE = 1.0
"""The price the convention table is asked at when an entry states none.

A tick schedule may band by price, so a definition needs a price to be built. One unit of
the quote currency is the standard band; an instrument that trades below it says so in
`attributes.reference_price`, or declares `price_increment` in its `override`.
"""

UNKNOWN = "unknown"
AMBIGUOUS = "ambiguous across venues"
DELISTED = "delisted"
NOT_YET_LISTED = "listed after"
"""The four ways resolution fails. Each names a different next action for the operator."""


@dataclass(frozen=True)
class ResolveError:
    """One id a provider could not turn into a definition, and why, in words."""

    id: str
    reason: str

    def __str__(self) -> str:
        return f"{self.id}: {self.reason}"


class InstrumentProvider(Protocol):
    """Answers "what was this instrument on this date", synchronously, for bare ids.

    Unlike the engine's own provider it discovers the venue rather than requiring it, and
    it reports a failure per id instead of returning nothing: which of the four reasons
    applies is the caller's business, not a log line.
    """

    id: ClassVar[str]

    def resolve(self, ids: Sequence[str], as_of: date) -> dict[str, object]:
        """A definition or a `ResolveError` for every id, keyed by the id as asked."""
        ...

    def sources(self, instrument_id: str) -> dict[str, str]:
        """The symbol this instrument carries at each vendor or broker, where known."""
        ...


PROVIDERS: dict[str, Callable[[Workspace], InstrumentProvider]] = {}
"""Reference providers by adapter id, for the workspace's `[data] reference` to name.

Empty in the core, and deliberately: the core knows no vendor, so every entry here is
placed by an adapter package or a workspace extension. A workspace that configures none
still resolves manual entries and the cache, which is what the file loaders, the synthetic
loader and the demo need.
"""

# --- the engine's instrument classes ------------------------------------------

_REQUIRED: dict[str, tuple[str, ...]] = {
    "Equity": (
        "instrument_id",
        "raw_symbol",
        "currency",
        "price_precision",
        "price_increment",
        "lot_size",
        "ts_event",
        "ts_init",
    ),
    "OptionContract": (
        "instrument_id",
        "raw_symbol",
        "asset_class",
        "currency",
        "price_precision",
        "price_increment",
        "multiplier",
        "lot_size",
        "underlying",
        "option_kind",
        "strike_price",
        "activation_ns",
        "expiration_ns",
        "ts_event",
        "ts_init",
    ),
    "FuturesContract": (
        "instrument_id",
        "raw_symbol",
        "asset_class",
        "currency",
        "price_precision",
        "price_increment",
        "multiplier",
        "lot_size",
        "underlying",
        "activation_ns",
        "expiration_ns",
        "ts_event",
        "ts_init",
    ),
    "CurrencyPair": (
        "instrument_id",
        "raw_symbol",
        "base_currency",
        "quote_currency",
        "price_precision",
        "size_precision",
        "price_increment",
        "size_increment",
        "ts_event",
        "ts_init",
    ),
    "IndexInstrument": (
        "instrument_id",
        "raw_symbol",
        "currency",
        "price_precision",
        "size_precision",
        "price_increment",
        "size_increment",
        "ts_event",
        "ts_init",
    ),
}
"""What each class must be given. Omitting one raises `TypeError` from the engine."""

_FEES = ("margin_init", "margin_maint", "maker_fee", "taker_fee")

_OPTIONAL: dict[str, tuple[str, ...]] = {
    "Equity": (*_FEES, "max_quantity", "min_quantity", "isin", "tick_scheme_name"),
    "OptionContract": (*_FEES, "exchange", "tick_scheme_name"),
    "FuturesContract": (*_FEES, "exchange", "tick_scheme_name"),
    "CurrencyPair": (
        *_FEES,
        "multiplier",
        "lot_size",
        "max_quantity",
        "min_quantity",
        "max_price",
        "min_price",
        "tick_scheme_name",
    ),
    "IndexInstrument": ("tick_scheme_name",),
}
"""What each class also accepts, so an override may correct a fee, a bound or an isin."""

_BY_ASSET_CLASS: dict[str, str] = {
    "EQUITY": "Equity",
    "FX": "CurrencyPair",
    "CRYPTOCURRENCY": "CurrencyPair",
    "INDEX": "IndexInstrument",
}
"""The class an asset class implies when the entry names no `instrument_class`."""

_BY_INSTRUMENT_CLASS: dict[str, str] = {"OPTION": "OptionContract", "FUTURE": "FuturesContract"}
"""A derivative is not implied by its underlying's asset class, so the entry states it."""

_NAMED_CLASS: dict[str, str] = {name: kind.lower() for kind, name in _BY_INSTRUMENT_CLASS.items()}
"""The same mapping read backwards, for writing a new entry for a resolved derivative."""

_RESERVED = ("instrument_id", "raw_symbol", "asset_class")
"""Fields the entry's own identity supplies; an override of one is refused, not ignored."""

_CONSUMED = ("instrument_class",)
"""Override keys kanso reads to choose the class rather than passing to the constructor."""

_DERIVED = (("price_increment", "price_precision"), ("size_increment", "size_precision"))
"""Precisions the engine requires to equal an increment's, so they are never asked for."""


# --- coercion -----------------------------------------------------------------


def _fields(instrument: Any) -> dict[str, Any]:
    """The engine's own canonical field map for a definition."""
    dumped: dict[str, Any] = type(instrument).to_dict(instrument)
    return dumped


def _precision(value: Decimal) -> int:
    _, _, fraction = format(value, "f").partition(".")
    return len(fraction)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{value!r} is not a number") from None


def _integer(value: object) -> int:
    return int(_decimal(value))


def _text(value: object) -> str:
    return str(value)


def _price(value: object) -> Any:
    from nautilus_trader.model.objects import Price

    return Price.from_str(format(_decimal(value), "f"))


def _quantity(value: object) -> Any:
    from nautilus_trader.model.objects import Quantity

    return Quantity.from_str(format(_decimal(value), "f"))


def _currency(value: object) -> Any:
    from nautilus_trader.model.objects import Currency

    return Currency.from_str(str(value))


def _instrument_id(value: object) -> Any:
    from nautilus_trader.model.identifiers import InstrumentId

    return InstrumentId.from_str(str(value))


def _symbol(value: object) -> Any:
    from nautilus_trader.model.identifiers import Symbol

    return Symbol(str(value))


def _asset_class(value: object) -> Any:
    from nautilus_trader.model.enums import AssetClass

    return AssetClass[str(value).upper()]


def _option_kind(value: object) -> Any:
    from nautilus_trader.model.enums import OptionKind

    return OptionKind[str(value).upper()]


def _as_date(value: object) -> date:
    """`value` as a calendar day, whether it is a date, a timestamp or nanoseconds."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError(f"{value!r} is not a date") from None


def _nanoseconds(value: object) -> int:
    """A timestamp as the engine holds it: whole nanoseconds since the epoch.

    A date is accepted too, because a listing or an expiry is written as a date by anyone
    writing one by hand.
    """
    if isinstance(value, int):
        return value
    day = _as_date(value)
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


_COERCE: dict[str, Callable[[object], Any]] = {
    "instrument_id": _instrument_id,
    "raw_symbol": _symbol,
    "currency": _currency,
    "base_currency": _currency,
    "quote_currency": _currency,
    "price_increment": _price,
    "strike_price": _price,
    "max_price": _price,
    "min_price": _price,
    "lot_size": _quantity,
    "size_increment": _quantity,
    "multiplier": _quantity,
    "max_quantity": _quantity,
    "min_quantity": _quantity,
    "price_precision": _integer,
    "size_precision": _integer,
    "ts_event": _nanoseconds,
    "ts_init": _nanoseconds,
    "activation_ns": _nanoseconds,
    "expiration_ns": _nanoseconds,
    "asset_class": _asset_class,
    "option_kind": _option_kind,
    "margin_init": _decimal,
    "margin_maint": _decimal,
    "maker_fee": _decimal,
    "taker_fee": _decimal,
    "underlying": _text,
    "isin": _text,
    "exchange": _text,
    "tick_scheme_name": _text,
}
"""How a YAML scalar becomes the engine type each constructor argument requires."""


# --- construction -------------------------------------------------------------


def _engine_class(entry: InstrumentEntry) -> str:
    stated = entry.override.get("instrument_class")
    if stated is not None:
        named = _BY_INSTRUMENT_CLASS.get(str(stated).upper())
        if named is None:
            raise ValidationError(
                f"{entry.nautilus_id}: instrument_class {stated!r} names no instrument class "
                f"kanso builds; expected one of {', '.join(sorted(_BY_INSTRUMENT_CLASS))}"
            )
        return named
    implied = _BY_ASSET_CLASS.get(entry.asset_class)
    if implied is None:
        raise ValidationError(
            f"{entry.nautilus_id}: asset class {entry.asset_class} implies no instrument class",
            remedy="name `instrument_class: option` or `instrument_class: future` in `override`",
        )
    return implied


def conventions_for(
    entry: InstrumentEntry, as_of: date, price: float | None = None
) -> dict[str, object]:
    """The constructor fields kanso supplies before the operator's override.

    Tick and lot come from the dated convention table and both timestamps from `as_of`: a
    definition's economic reference time is the date it was resolved as of, and it was
    public on that date, so availability holds. `price` selects the tick band; absent one,
    `attributes.reference_price` is used, and absent that, one unit of the quote currency.

    An asset class and venue with no schedule on file contributes no tick and no lot rather
    than a guessed one — the entry's `override` supplies them instead, and construction says
    so by name when it does not. `multiplier` is never defaulted for the same reason: a
    contract size of one is a claim about the contract, not an absence of one.
    """
    when = _nanoseconds(as_of)
    defaults: dict[str, object] = {"ts_event": when, "ts_init": when}
    stated = price if price is not None else entry.attributes.get("reference_price")
    level = REFERENCE_PRICE if stated is None else float(stated)
    try:
        tick = conventions.tick_size(entry.asset_class, entry.venue, level, as_of)
        lot = conventions.lot_size(entry.asset_class, entry.venue, as_of)
    except ValidationError:
        return defaults
    defaults.update(
        {
            "price_increment": tick,
            "price_precision": _precision(tick),
            "size_increment": lot,
            "size_precision": _precision(lot),
            "lot_size": lot,
        }
    )
    return defaults


def build(entry: InstrumentEntry, conventions: Mapping[str, object]) -> object:
    """The engine instrument this entry describes, with `override` applied last.

    `conventions` supplies the fields kanso defaults; those the chosen class does not accept
    are dropped from it silently, because a default is kanso's business. An `override` key
    the class does not accept is refused loudly, because it is the operator's assertion and
    dropping it would build something other than what the file says.
    """
    name = _engine_class(entry)
    accepted = (*_REQUIRED[name], *_OPTIONAL[name])

    reserved = sorted(field for field in _RESERVED if field in entry.override)
    if reserved:
        raise ValidationError(
            f"{entry.nautilus_id}: override may not set {', '.join(reserved)}; an instrument's "
            f"identity comes from `nautilus_id` and `asset_class`"
        )
    unknown = sorted(
        field
        for field in entry.override
        if field not in accepted and field not in _CONSUMED and field not in _RESERVED
    )
    if unknown:
        raise ValidationError(
            f"{entry.nautilus_id}: {name} has no field {', '.join(unknown)}; it accepts "
            f"{', '.join(sorted(accepted))}"
        )

    fields: dict[str, object] = {k: v for k, v in conventions.items() if k in accepted}
    fields.update({k: v for k, v in entry.override.items() if k in accepted})
    fields["instrument_id"] = entry.nautilus_id
    fields["raw_symbol"] = entry.symbol
    if "asset_class" in accepted:
        fields["asset_class"] = entry.asset_class
    try:
        for increment, precision in _DERIVED:
            if increment in fields and precision in accepted and precision not in entry.override:
                fields[precision] = _precision(_decimal(fields[increment]))
    except ValueError as exc:
        raise _rejected(entry, name, exc) from None

    missing = [field for field in _REQUIRED[name] if field not in fields]
    if missing:
        raise ValidationError(
            f"{entry.nautilus_id}: {name} needs {', '.join(missing)}, which neither the "
            f"convention table nor `override` supplies",
            remedy=f"declare {missing[0]} in this instrument's `override` in {CACHE_NAME}",
        )

    try:
        arguments = {field: _COERCE[field](fields[field]) for field in accepted if field in fields}
        instrument: object = _engine(name)(**arguments)
    except (TypeError, ValueError) as exc:
        raise _rejected(entry, name, exc) from None
    return instrument


def _rejected(entry: InstrumentEntry, name: str, exc: Exception) -> ValidationError:
    """The engine's own refusal, reported as it is rather than translated."""
    return ValidationError(f"{entry.nautilus_id}: {name} rejected its fields: {exc}")


def _engine(name: str) -> Any:
    from nautilus_trader.model import instruments as engine

    return getattr(engine, name)


# --- content addressing -------------------------------------------------------


def definition_checksum(instrument: object) -> str:
    """The content address of one definition: sha256 over its canonical field map."""
    canonical = json.dumps(_fields(instrument), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def instruments_checksum(instruments: Iterable[object]) -> str:
    """The content address of a whole instrument set, for a snapshot to pin.

    Order-independent and duplicate-independent: it addresses the set, so the same universe
    resolved in a different order pins the same instruments, and a run reproduces exactly
    the definitions it ran against.
    """
    digests = sorted({definition_checksum(item) for item in instruments})
    return hashlib.sha256("\n".join(digests).encode()).hexdigest()


# --- the manual provider ------------------------------------------------------


@dataclass(frozen=True)
class ManualProvider(InstrumentProvider):
    """Builds definitions from the `manual` entries of an instruments file.

    It knows no vendor and reaches no network: a manual entry declares its own constructor
    fields, so this is the path a workspace with no credentials takes, and the one the file
    loaders, the synthetic loader and the demo run on.
    """

    id: ClassVar[str] = "manual"
    file: InstrumentsFile

    def resolve(self, ids: Sequence[str], as_of: date) -> dict[str, object]:
        out: dict[str, object] = {}
        for wanted in dict.fromkeys(ids):
            found = _lookup(self.file, wanted)
            if isinstance(found, ResolveError):
                out[wanted] = found
            elif not found.manual:
                out[wanted] = ResolveError(
                    wanted,
                    f"{UNKNOWN}: its entry is not `manual`, and this provider resolves only "
                    f"manual entries",
                )
            else:
                out[wanted] = _dated(found, wanted, as_of) or build(
                    found, conventions_for(found, as_of)
                )
        return out

    def sources(self, instrument_id: str) -> dict[str, str]:
        found = _lookup(self.file, instrument_id)
        return {} if isinstance(found, ResolveError) else dict(found.sources)


def _lookup(file: InstrumentsFile, wanted: str) -> InstrumentEntry | ResolveError:
    """The entry for an id: its own key, else the one entry whose symbol or id it is."""
    if wanted in file:
        return file[wanted]
    matches = sorted(
        key for key, entry in file.root.items() if wanted in (entry.symbol, entry.nautilus_id)
    )
    if len(matches) == 1:
        return file[matches[0]]
    if matches:
        listed = ", ".join(file[key].nautilus_id for key in matches)
        return ResolveError(
            wanted, f"{AMBIGUOUS}: {listed}; name the {CACHE_NAME} key, not the bare symbol"
        )
    return ResolveError(wanted, f"{UNKNOWN}: no entry in {CACHE_NAME}")


def _listing_window(entry: InstrumentEntry) -> tuple[date | None, date | None]:
    """When this instrument started and stopped trading, as far as the entry says.

    A dated derivative states it in the engine's own `activation_ns` and `expiration_ns`;
    anything else states it as `listed` and `delisted` in `attributes`, since the engine's
    cash instruments carry no such field.
    """
    listed = entry.override.get("activation_ns", entry.attributes.get("listed"))
    delisted = entry.override.get("expiration_ns", entry.attributes.get("delisted"))
    return (
        None if listed is None else _as_date(listed),
        None if delisted is None else _as_date(delisted),
    )


def _dated(entry: InstrumentEntry, wanted: str, as_of: date) -> ResolveError | None:
    """The failure for an instrument that did not exist on `as_of`, if it did not."""
    try:
        listed, delisted = _listing_window(entry)
    except ValueError as exc:
        raise ValidationError(f"{wanted}: {exc}") from None
    if delisted is not None and delisted < as_of:
        return ResolveError(wanted, f"{DELISTED} {delisted}, before {as_of}")
    if listed is not None and listed > as_of:
        return ResolveError(wanted, f"{NOT_YET_LISTED} {as_of}: it was listed {listed}")
    return None


# --- the catalog's instrument store -------------------------------------------


def _catalog(ws: Workspace) -> Any:
    """The workspace's store, opened the one way the whole package opens it."""
    from kanso.data.catalog import open_catalog

    return open_catalog(ws)


def read_store(ws: Workspace) -> dict[str, object]:
    """Every definition the catalog holds, keyed by its content address."""
    held: list[object] = list(_catalog(ws).instruments())
    return {definition_checksum(item): item for item in held}


def write_store(ws: Workspace, instruments: Iterable[object]) -> None:
    """Write definitions to the catalog, which is the registry of record."""
    fresh = list(instruments)
    if fresh:
        _catalog(ws).write_data(fresh)


# --- resolution ---------------------------------------------------------------


def resolve_universe(
    ws: Workspace, ids: Sequence[str], as_of: date, refresh: bool = False
) -> dict[str, object]:
    """Every id in the universe as the definition that held on `as_of`.

    Answers from the cache when it is fresh for exactly this date, from the entry when it is
    manual, and otherwise through the workspace's configured reference adapter. Raises a
    validation failure naming every id that could not be resolved and why, so one report
    covers the whole universe rather than one id per attempt.

    `refresh` passes the cache by: every id is resolved again, from its manual entry or
    through the reference adapter, which is how an operator picks up a definition that
    changed at the source. The definitions already held stay in the store, so a run pinned
    to one of them still reproduces.

    On success the definitions the store does not already hold are written to it, and the
    cache is written back — leaving the operator's fields untouched, and rewriting the file
    only when a resolution changed something in it.
    """
    path = ws.path(CACHE_NAME)
    file = load_yaml(InstrumentsFile, path) if path.is_file() else InstrumentsFile({})
    cached = read_store(ws)

    resolved: dict[str, object] = {}
    failures: list[ResolveError] = []
    manual: list[str] = []
    unresolved: dict[str, str] = {}

    for wanted in dict.fromkeys(ids):
        found = _lookup(file, wanted)
        if isinstance(found, ResolveError):
            if found.reason.startswith(AMBIGUOUS):
                failures.append(found)
            else:
                unresolved[wanted] = found.reason
            continue
        stale = _dated(found, wanted, as_of)
        if stale is not None:
            failures.append(stale)
        elif not refresh and (hit := _from_cache(found, cached, as_of)) is not None:
            resolved[wanted] = hit
        elif found.manual:
            manual.append(wanted)
        else:
            unresolved[wanted] = f"{UNKNOWN}: its entry is neither `manual` nor freshly resolved"

    _collect(ManualProvider(file).resolve(manual, as_of), resolved, failures)

    provider = _reference_provider(ws)
    updates: dict[str, InstrumentEntry] = {}
    if provider is None:
        absent = _no_provider(ws)
        failures.extend(
            ResolveError(wanted, f"{reason}, and {absent}") for wanted, reason in unresolved.items()
        )
    else:
        answered = _reconstructed(file, provider.resolve(sorted(unresolved), as_of))
        _collect(answered, resolved, failures)
        updates = _cache_updates(file, answered, provider, as_of)

    if failures:
        raise ValidationError(
            "; ".join(str(failure) for failure in sorted(failures, key=lambda item: item.id)),
            remedy=f"correct these ids in {CACHE_NAME}, or resolve them as of another date",
        )

    write_store(ws, [held for held in resolved.values() if definition_checksum(held) not in cached])
    _write_cache(path, file, updates)
    return resolved


def _reconstructed(file: InstrumentsFile, answered: Mapping[str, object]) -> dict[str, object]:
    """Every answer rebuilt with the entry's `override` applied over what was resolved.

    A provider reports what the reference source says; the operator's correction is applied
    after that and before construction, so the definition that reaches the engine, the store
    and the content address is the corrected one rather than the reported one.
    """
    out: dict[str, object] = {}
    for wanted, outcome in answered.items():
        if isinstance(outcome, ResolveError):
            out[wanted] = outcome
        else:
            out[wanted] = build(_entry_for(file, wanted, outcome), _resolved_fields(outcome))
    return out


def _resolved_fields(instrument: object) -> dict[str, object]:
    """A resolved definition as constructor fields, for an override to be applied over."""
    fields: dict[str, object] = {
        key: value for key, value in _fields(instrument).items() if value is not None
    }
    fields.pop("type", None)
    fields.pop("id", None)
    return fields


def _collect(
    answered: Mapping[str, object], resolved: dict[str, object], failures: list[ResolveError]
) -> None:
    for wanted, outcome in answered.items():
        if isinstance(outcome, ResolveError):
            failures.append(outcome)
        else:
            resolved[wanted] = outcome


def _from_cache(entry: InstrumentEntry, cached: Mapping[str, object], as_of: date) -> object | None:
    """The stored definition for this entry, when it still answers the question asked.

    It does only when the resolution was made as of exactly this date — a definition is a
    dated fact, and another date is another question — when the catalog still holds what was
    resolved, and when that definition still agrees with every field the operator's
    `override` asserts. An override edited since the resolution makes the cache stale.
    """
    if entry.resolved is None or entry.resolved.as_of != as_of:
        return None
    held = cached.get(entry.resolved.checksum)
    if held is None:
        return None
    stored = _fields(held)
    for field, value in entry.override.items():
        if field in _CONSUMED:
            continue
        if field not in stored or str(stored[field]) != str(value):
            return None
    return held


def _reference_provider(ws: Workspace) -> InstrumentProvider | None:
    """The provider `[data] reference` names, from this table or from a registered adapter.

    `PROVIDERS` is consulted first, so a workspace or a test can put a provider under an id
    and have it win; otherwise the adapter registry is asked, which is how a vendor package
    supplies one without anything here naming it. Both are resolved at the moment of use,
    so an unconfigured workspace neither imports a credential nor opens a connection.
    """
    from kanso import ext
    from kanso.data import registry

    factory = PROVIDERS.get(ws.config.data.reference)
    if factory is not None:
        return factory(ws)
    extensions = ext.discover(ws.root, ws.config.extensions_paths)
    return registry.provider_for(ws, ws.config.data.reference, extensions)


def _no_provider(ws: Workspace) -> str:
    configured = ws.config.data.reference
    if configured == "none":
        return "no reference adapter is configured ([data] reference)"
    return f"no reference adapter named {configured!r} is installed"


def _cache_updates(
    file: InstrumentsFile,
    answered: Mapping[str, object],
    provider: InstrumentProvider,
    as_of: date,
) -> dict[str, InstrumentEntry]:
    """The entries a resolution rewrites, keyed by the id they are filed under."""
    at = datetime.now(UTC)
    updates: dict[str, InstrumentEntry] = {}
    for wanted, outcome in answered.items():
        if isinstance(outcome, ResolveError):
            continue
        entry = _entry_for(file, wanted, outcome)
        updates[_key_for(file, wanted, entry)] = entry.model_copy(
            update={
                "resolved": Resolved(
                    adapter=provider.id,
                    as_of=as_of,
                    at=at,
                    checksum=definition_checksum(outcome),
                ),
                "sources": {**entry.sources, **provider.sources(wanted)},
            }
        )
    return updates


def _entry_for(file: InstrumentsFile, wanted: str, instrument: object) -> InstrumentEntry:
    """The entry to rewrite, or a new one for an instrument the file had never seen.

    A new entry records the instrument class when the definition is a derivative, since an
    asset class alone does not imply one and the entry has to be able to rebuild itself.
    """
    found = _lookup(file, wanted)
    if not isinstance(found, ResolveError):
        return found
    stated = _NAMED_CLASS.get(type(instrument).__name__)
    stored = _fields(instrument)
    return InstrumentEntry(
        nautilus_id=str(stored["id"]),
        asset_class=str(stored.get("asset_class", "EQUITY")),
        corporate_actions="adjust_all",
        override={} if stated is None else {"instrument_class": stated},
    )


def _key_for(file: InstrumentsFile, wanted: str, entry: InstrumentEntry) -> str:
    for key, held in file.root.items():
        if held.nautilus_id == entry.nautilus_id:
            return key
    return wanted


def _write_cache(path: Path, file: InstrumentsFile, updates: Mapping[str, InstrumentEntry]) -> None:
    """Rewrite the cache with these entries replaced, and only when something changed."""
    merged = {**file.root, **updates}
    if merged == file.root:
        return
    write_yaml(InstrumentsFile(merged), path)
