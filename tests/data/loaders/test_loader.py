"""The loader interface itself: the registry, the invariants and the Arrow path."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.core.data import Data

from kanso.data.loader import (
    DatasetRef,
    Loader,
    arrow_batches,
    checked,
    get_loader,
    loaders,
    manifest_for,
    register_custom_type,
    to_ns,
    utc_day,
)
from kanso.data.loaders import BUILTIN_LOADERS
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.errors import ValidationError
from kanso.ext import Extension

SPAN = (date(2024, 3, 4), date(2024, 3, 5))


def a_ref(**over: Any) -> DatasetRef:
    fields: dict[str, Any] = {
        "dataset_id": "DEMO.XNAS-bar-5m-raw-20240305",
        "instrument": "DEMO.XNAS",
        "type": "bar",
        "resolution": "5m",
        "span": SPAN,
        "adjusted": False,
        "publication": "realtime",
    }
    fields.update(over)
    return DatasetRef(**fields)


class OtherLoader:
    """A loader an extension might provide."""

    id = "other"

    def discover(self, spec: Any) -> list[DatasetRef]:
        return [a_ref()]

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> list[object]:
        return []

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> None:
        return None

    def manifest(self, ref: DatasetRef) -> Any:
        raise NotImplementedError


def an_extension(name: str, provides: dict[str, tuple[str, ...]], module: object) -> Extension:
    return Extension(name=name, path=Path(name), module=module, provides=provides)


def test_the_two_reference_loaders_are_the_built_ins() -> None:
    assert set(BUILTIN_LOADERS) == {"csv_parquet", "synthetic"}
    assert set(loaders()) == {"csv_parquet", "synthetic"}


def test_an_unknown_loader_names_the_ones_that_exist() -> None:
    with pytest.raises(ValidationError, match="known loaders are csv_parquet, synthetic"):
        get_loader("bloomberg")


def test_an_extension_adds_its_declared_loaders() -> None:
    extension = an_extension(
        "ext", {"loaders": ("other",)}, SimpleNamespace(LOADERS={"other": OtherLoader()})
    )
    assert get_loader("other", [extension]).id == "other"


def test_a_loader_an_extension_did_not_declare_is_ignored() -> None:
    """The `PROVIDES` table is the truth `doctor` reads, so it is also the gate."""
    extension = an_extension(
        "ext", {"loaders": ()}, SimpleNamespace(LOADERS={"other": OtherLoader()})
    )
    assert "other" not in loaders([extension])


def test_an_extension_with_no_loader_table_is_ignored() -> None:
    assert "other" not in loaders([an_extension("ext", {"loaders": ("other",)}, SimpleNamespace())])
    assert "other" not in loaders(
        [an_extension("ext", {"loaders": ("other",)}, SimpleNamespace(LOADERS=["other"]))]
    )


def test_something_that_is_not_a_loader_is_ignored() -> None:
    extension = an_extension(
        "ext", {"loaders": ("other",)}, SimpleNamespace(LOADERS={"other": object()})
    )
    assert "other" not in loaders([extension])


def test_a_built_in_id_cannot_be_shadowed() -> None:
    """The fixture the whole suite stands on stays the package's own."""
    extension = an_extension(
        "ext", {"loaders": ("synthetic",)}, SimpleNamespace(LOADERS={"synthetic": OtherLoader()})
    )
    assert loaders([extension])["synthetic"] is BUILTIN_LOADERS["synthetic"]


def test_the_reference_loaders_satisfy_the_interface() -> None:
    assert all(isinstance(loader, Loader) for loader in BUILTIN_LOADERS.values())


def test_a_window_is_clipped_to_the_span() -> None:
    ref = a_ref()
    assert ref.window((date(2024, 1, 1), date(2024, 12, 31))) == SPAN
    assert ref.window((date(2024, 3, 5), date(2024, 3, 9))) == (SPAN[1], SPAN[1])
    assert ref.window((date(2024, 3, 6), date(2024, 3, 9))) is None


def test_availability_never_precedes_the_event() -> None:
    ok = SimpleNamespace(ts_event=5, ts_init=9)
    assert list(checked([ok], "where")) == [ok]
    with pytest.raises(ValidationError, match="availability cannot precede"):
        list(checked([SimpleNamespace(ts_event=9, ts_init=5)], "where"))


def test_something_without_timestamps_is_not_a_data_point() -> None:
    with pytest.raises(ValidationError, match="is not an engine data point"):
        list(checked([object()], "where"))


def test_a_dataset_that_served_nothing_has_no_manifest() -> None:
    """An empty manifest would claim coverage a snapshot could then pin."""
    with pytest.raises(ValidationError, match="served no points"):
        manifest_for(a_ref(), "synthetic", [])


def test_a_point_with_no_canonical_form_cannot_be_checksummed() -> None:
    with pytest.raises(ValidationError, match="has no to_dict"):
        manifest_for(a_ref(), "synthetic", [SimpleNamespace(ts_event=1, ts_init=1)])


def test_arrow_is_offered_only_for_a_type_the_catalog_knows() -> None:
    unknown = type("KansoTestArrowless", (Data,), {})
    assert arrow_batches([], unknown) is None


def test_arrow_batches_split_at_the_batch_size() -> None:
    from nautilus_trader.model.data import Bar

    loader = SyntheticLoader()
    spec = {
        "loader": "synthetic",
        "seed": 1,
        "instruments": ["B"],
        "resolution": "1m",
        "start": "2024-03-04",
        "end": "2024-03-04",
    }
    ref = loader.discover(spec)[0]
    points = list(loader.load(ref, ref.span))
    tables = arrow_batches(points, Bar, batch_size=100)
    assert tables is not None
    assert [table.num_rows for table in tables] == [100, 100, 100, 90]


def test_a_moment_needs_a_time_zone_to_be_an_instant() -> None:
    assert to_ns(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert to_ns(datetime(2024, 3, 4, 14, 35, tzinfo=UTC)) == 1_709_562_900_000_000_000
    with pytest.raises(ValidationError, match="carries no timezone"):
        to_ns(datetime(2024, 3, 4, 14, 35))


def test_a_timestamp_reports_the_day_it_falls_in() -> None:
    assert utc_day(0) == date(1970, 1, 1)
    assert utc_day(1_709_562_900_000_000_000) == date(2024, 3, 4)


def test_the_type_registry_is_reachable_from_the_loader_interface() -> None:
    """A loader and the type it yields are written together, so they import together."""
    with pytest.raises(ValidationError, match="is a built-in type"):
        register_custom_type("bar", Data)
