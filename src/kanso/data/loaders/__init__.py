"""The loaders the package ships, and nothing else.

Two are enough for the whole framework to be exercised without a vendor: `synthetic`
generates the fixture every test and the demo run on, and `csv_parquet` reads files an
operator already has. Every further loader — a vendor adapter's, a workspace
extension's — reaches the same registry through `kanso.data.loader.loaders`, which is
also where the rule that a built-in id cannot be shadowed lives.

This module holds only the table. `kanso.data.loader` holds the interface and the
lookup, so a loader can import the interface without importing its siblings.
"""

from __future__ import annotations

from typing import Final

from kanso.data.loader import Loader
from kanso.data.loaders.csv_parquet import CsvParquetLoader
from kanso.data.loaders.synthetic import SyntheticLoader

__all__ = ["BUILTIN_LOADERS", "CsvParquetLoader", "SyntheticLoader"]

BUILTIN_LOADERS: Final[dict[str, Loader]] = {
    CsvParquetLoader.id: CsvParquetLoader(),
    SyntheticLoader.id: SyntheticLoader(),
}
"""The shipped loaders, by id."""
