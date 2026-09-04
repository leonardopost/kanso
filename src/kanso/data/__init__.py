"""The data package: one catalog, one set of types, and the loaders that fill it.

`Loader` and `register_custom_type` are the stable interface a vendor adapter or a
workspace extension implements; everything the package ships is reached through the same
two, so a first-party loader and an operator's own are indistinguishable to the rest of
kanso.
"""

from __future__ import annotations

from kanso.data.loader import DatasetRef, Loader, get_loader, loaders
from kanso.data.types import CorporateAction, data_types, register_custom_type

__all__ = [
    "CorporateAction",
    "DatasetRef",
    "Loader",
    "data_types",
    "get_loader",
    "loaders",
    "register_custom_type",
]
