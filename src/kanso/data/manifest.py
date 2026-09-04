"""Dataset identity, the manifest a written dataset carries, and the store's layout.

A **dataset** is one instrument, one data type, one resolution and one adjustment basis,
served over one span of dates. Its id is derived, never invented: the same five
dimensions always produce the same id, and two loads that differ in any of them are two
datasets. The id is filename-safe because it names a file — `catalog/manifests/<id>.yaml`
— so every component is reduced to letters, digits, dots and underscores, and the hyphen
is left as the separator.

The span in a manifest is the span that was **served**, never the span that was asked
for. A source may return less than the requested range with a success status and no
warning; recording the request would claim coverage the dataset lacks, and snapshots are
pinned by coverage. `shortfall` renders the difference so a caller can surface it.

Because the id carries the span's end but not its start, re-loading the same series to
the same end reuses the id and is therefore a replacement rather than a duplicate, while
a `sync` that extends the end, and a `backfill` that ends where the held data begins,
both produce fresh ids and record the dataset they follow in `supersedes`.

The store's layout is `catalog/` in the workspace: `data/` is the engine's own
`ParquetDataCatalog` tree, `manifests/<dataset_id>.yaml` and
`snapshots/<snapshot_id>.yaml` sit beside it, and `.cache/` is an adapter's scratch
space. This module owns the paths and the manifest files; `catalog.py` owns the engine.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from kanso.errors import PreconditionError, ValidationError
from kanso.schemas.base import NonEmpty, Sha256, Versioned
from kanso.schemas.yamlio import load_yaml, write_yaml

if TYPE_CHECKING:  # pragma: no cover - kept out of the runtime import graph
    from kanso.workspace import Workspace

CATALOG_DIR: Final = "catalog"
"""The store's directory in the workspace."""

DATA_DIR: Final = "data"
MANIFESTS_DIR: Final = "manifests"
SNAPSHOTS_DIR: Final = "snapshots"
CACHE_DIR: Final = ".cache"

Publication = Literal["realtime", "delayed", "unknown"]
"""How a dataset's points became public: at once, on a delay, or by a route nobody declared."""

PUBLICATIONS: Final = ("realtime", "delayed", "unknown")

_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._]+")
_COMPONENT: Final = r"[A-Za-z0-9._]+"
DATASET_ID_PATTERN: Final = rf"^{_COMPONENT}-{_COMPONENT}-{_COMPONENT}-(adj|raw)-[0-9]{{8}}$"
_DATASET_ID: Final = re.compile(DATASET_ID_PATTERN)

NO_RESOLUTION: Final = "none"
"""What an unaggregated type's absent resolution is spelled as inside an id."""


@runtime_checkable
class DatasetRefLike(Protocol):
    """What a dataset reference must expose for this package to write it.

    Structural rather than nominal so the loader package's `DatasetRef` satisfies it
    without either package importing the other. `span` is the **requested** span on the
    way in; what the manifest records is what came back. A reference's own precomputed
    dataset id is deliberately not among these: it is derived from the span that was
    asked for, and the id a dataset keeps is derived from the span that was served.

    Every member is read-only, which is what makes a frozen reference satisfy the
    protocol: a writable member would demand a mutable attribute of every implementation,
    and a dataset reference is immutable by construction.
    """

    @property
    def instrument(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def resolution(self) -> str | None: ...

    @property
    def span(self) -> tuple[date, date]: ...

    @property
    def adjusted(self) -> bool: ...

    @property
    def publication(self) -> str: ...

    @property
    def publication_rule(self) -> str | None: ...

    @property
    def vendor(self) -> str | None: ...

    @property
    def vendor_dataset(self) -> str | None: ...

    @property
    def request_params(self) -> dict[str, str] | None: ...


def sanitise(text: str) -> str:
    """One id component: letters, digits, dots and underscores, never empty."""
    cleaned = _UNSAFE.sub("_", text)
    return cleaned or "_"


def dataset_id(
    instrument: str,
    type: str,
    resolution: str | None,
    adjusted: bool,
    end: date,
) -> str:
    """The id of the dataset holding `type` for `instrument`, served to `end`.

    Every dimension that makes two loads different is in the id: the instrument, the
    type, the resolution, whether the prices are vendor-adjusted, and where the served
    span ends. Deterministic, so the same load always addresses the same dataset.
    """
    parts = (
        sanitise(instrument),
        sanitise(type),
        sanitise(resolution or NO_RESOLUTION),
        "adj" if adjusted else "raw",
        _yyyymmdd(end),
    )
    return "-".join(parts)


def as_publication(value: str) -> Publication:
    """`value` as a publication class, or a validation failure naming the three."""
    match value:
        case "realtime" | "delayed" | "unknown":
            return value
    raise ValidationError(
        f"publication: {value!r} is not one of {', '.join(PUBLICATIONS)}",
        remedy="an adapter declares its dataset's publication class; realtime is the default",
    )


def is_dataset_id(text: str) -> bool:
    """True when `text` has the shape `dataset_id` produces."""
    return _DATASET_ID.match(text) is not None


class Manifest(Versioned):
    """What one written dataset is, where it came from, and when it became public.

    `span` is the served span in whole UTC days, `checksum` covers the bytes that were
    written for it, and `row_count` is how many points those bytes hold. A dataset whose
    prices are vendor-adjusted must name its `adjustment_basis`, because such a series is
    adjusted as of its request date and is therefore mutable; a dataset published on a
    delay must name the `publication_rule` its availability timestamps came from.
    """

    dataset_id: NonEmpty
    source: NonEmpty
    instrument: NonEmpty
    type: NonEmpty
    resolution: str | None = None
    span: tuple[date, date]
    adjusted: bool = False
    row_count: int = Field(ge=0)
    checksum: Sha256
    vendor: str | None = None
    vendor_dataset: str | None = None
    request_params: dict[str, str] | None = None
    publication: Publication = "realtime"
    publication_rule: str | None = None
    as_of: date | None = None
    adjustment_basis: str | None = None
    supersedes: str | None = None

    @property
    def start(self) -> date:
        """The first day the dataset serves."""
        return self.span[0]

    @property
    def end(self) -> date:
        """The last day the dataset serves."""
        return self.span[1]

    @property
    def filed_under(self) -> tuple[str, str, str | None]:
        """What the store files this dataset under: its instrument, type and resolution.

        Adjustment is deliberately absent. The engine keys its files by instrument and
        bar type, so an adjusted and an unadjusted series of the same bars land in the
        same place: they are two datasets to kanso and one series to the store, and the
        store can hold only one of them over a given span.
        """
        return (self.instrument, self.type, self.resolution)

    def covers(self, window: tuple[date, date]) -> bool:
        """True when this dataset's span contains `window` end to end."""
        return self.span[0] <= window[0] and window[1] <= self.span[1]

    @model_validator(mode="after")
    def _validate(self) -> Manifest:
        if self.span[1] < self.span[0]:
            raise ValueError(f"span: {self.span[1]} is before {self.span[0]}")
        derived = dataset_id(
            self.instrument, self.type, self.resolution, self.adjusted, self.span[1]
        )
        if self.dataset_id != derived:
            raise ValueError(
                f"dataset_id: {self.dataset_id!r} is not the id these dimensions derive "
                f"({derived!r}); a dataset id is derived, never chosen"
            )
        if self.adjusted and not self.adjustment_basis:
            raise ValueError(
                "adjustment_basis: required of an adjusted dataset, which is adjusted as of "
                "its request date and is therefore not reproducible without one"
            )
        if self.publication == "delayed" and not self.publication_rule:
            raise ValueError(
                "publication_rule: required of a delayed dataset, whose availability "
                "timestamps must come from a declared rule rather than from ingest time"
            )
        return self


def overlaps(left: tuple[date, date], right: tuple[date, date]) -> bool:
    """True when two dated spans share at least one day."""
    return left[0] <= right[1] and right[0] <= left[1]


def merge(spans: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """The spans merged into the fewest that cover the same days.

    Two spans join when they overlap or when one begins the day after the other ends:
    coverage is counted in whole days, so back-to-back days leave no hole between them.
    """
    ordered = sorted(spans)
    merged: list[tuple[date, date]] = []
    for span in ordered:
        if merged and span[0] <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(span)
    return merged


def contains(spans: list[tuple[date, date]], window: tuple[date, date]) -> bool:
    """True when the union of `spans` contains every day of `window`."""
    return any(span[0] <= window[0] and window[1] <= span[1] for span in merge(spans))


def shortfall(requested: tuple[date, date], served: tuple[date, date]) -> str | None:
    """One sentence naming the days that were asked for and not served, or `None`.

    A source may answer a range it cannot fill with a success status and no warning, so
    the difference is stated rather than assumed away.
    """
    missing: list[str] = []
    if served[0] > requested[0]:
        missing.append(f"{requested[0]} to {served[0] - timedelta(days=1)} at the start")
    if served[1] < requested[1]:
        missing.append(f"{served[1] + timedelta(days=1)} to {requested[1]} at the end")
    if not missing:
        return None
    return (
        f"served {served[0]}..{served[1]} of the requested {requested[0]}..{requested[1]}; "
        f"missing {' and '.join(missing)}"
    )


def catalog_path(ws: Workspace) -> Path:
    """`<workspace>/catalog`: the store's root."""
    return Path(ws.root) / CATALOG_DIR


def data_path(ws: Workspace) -> Path:
    """Where the engine's parquet tree lives."""
    return catalog_path(ws) / DATA_DIR


def manifests_path(ws: Workspace) -> Path:
    """Where the dataset manifests live."""
    return catalog_path(ws) / MANIFESTS_DIR


def snapshots_path(ws: Workspace) -> Path:
    """Where the snapshots live."""
    return catalog_path(ws) / SNAPSHOTS_DIR


def cache_path(ws: Workspace) -> Path:
    """An adapter's download scratch space, which nothing else reads."""
    return catalog_path(ws) / CACHE_DIR


def manifest_file(ws: Workspace, dataset: str) -> Path:
    """The file holding one dataset's manifest."""
    if not is_dataset_id(dataset):
        raise ValidationError(
            f"{dataset!r} is not a dataset id",
            remedy="derive ids with kanso.data.manifest.dataset_id",
        )
    return manifests_path(ws) / f"{dataset}.yaml"


def write_manifest(ws: Workspace, manifest: Manifest) -> Path:
    """Persist a manifest, replacing any previous one for the same dataset."""
    path = manifest_file(ws, manifest.dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_yaml(manifest, path)


def read_manifest(ws: Workspace, dataset: str) -> Manifest:
    """The manifest of `dataset`, or a precondition failure when it is not held."""
    path = manifest_file(ws, dataset)
    if not path.is_file():
        raise PreconditionError(
            f"no manifest for dataset {dataset!r} in {manifests_path(ws)}",
            remedy="run `kanso data show` to list the datasets this workspace holds",
        )
    return load_yaml(Manifest, path)


def manifests(ws: Workspace) -> dict[str, Manifest]:
    """Every manifest the workspace holds, keyed by dataset id."""
    directory = manifests_path(ws)
    if not directory.is_dir():
        return {}
    found: dict[str, Manifest] = {}
    for path in sorted(directory.glob("*.yaml")):
        manifest = load_yaml(Manifest, path)
        found[manifest.dataset_id] = manifest
    return found


def remove_manifest(ws: Workspace, dataset: str) -> None:
    """Forget a dataset's manifest; silent when it is already gone."""
    manifest_file(ws, dataset).unlink(missing_ok=True)


def _yyyymmdd(day: date) -> str:
    """A date as eight digits, formatted rather than strftime'd.

    `strftime("%Y%m%d")` does not zero-pad a year below 1000 on glibc while it does on
    BSD, so the same date would produce a different dataset id — and therefore a different
    snapshot id — on Linux and on macOS. Formatting the fields explicitly is portable.
    """
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"
