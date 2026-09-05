"""The frozen flat-file store the CLI slice replays, beside the REST wire it belongs to.

A module of its own rather than a second half of `massive_wire`, because the two halves
answer the same `answer(url, params)` shape with the same kinds of helper — a listing, a
read, a page — and a name defined twice in one module silently keeps the last one. That
is not hypothetical: written as one module, the object store's `_listing` shadowed the
reference listings' `_listing`, and four entitled listings were reported `unavailable` by
a survey that had asked nothing.

The objects here hold the aggregates the REST wire serves for the same sessions, spelled
the way the store spells them — a column per field, and the window start in nanoseconds
where the API writes milliseconds. Seeding both transports from one row is what makes a
test over the two of them a comparison of two code paths rather than of two fixtures.

Nothing here opens a socket or carries a recorded secret.
"""

from __future__ import annotations

import gzip
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

from kanso.data.adapters.massive.client import Response
from kanso.data.adapters.massive.loaders.bulk import COLUMNS, day_of, key_for

from ..data.adapters.massive import bar, refuse_host

BUCKET = "flatfiles"
"""The store's one bucket, path-style, as the adapter addresses it."""

ASSET_CLASS = "stocks"
RESOLUTION = "1d"
"""The one class and resolution laid out here. The store carries more; what this fixture
owes the suite is a transport that serves the same sessions the API does."""

FIRST = date(2024, 1, 2)
LAST = date(2024, 3, 29)
"""The days the store holds an object for. Weekends are absent, exactly as they are live,
which is what makes a chunk seam a real seam rather than an unbroken run of dates."""

LISTED = "http://s3.amazonaws.com/doc/2006-03-01/"
"""The namespace a `ListObjectsV2` page comes back in."""

NO_SUCH_KEY = b"<Error><Code>NoSuchKey</Code><Message>no such key</Message></Error>"
"""What a well-formed key with no object behind it is answered with."""


def days() -> list[date]:
    """Every day the store holds an object for: the weekdays of the fixture's range."""
    span = (LAST - FIRST).days
    found = [FIRST + timedelta(days=step) for step in range(span + 1)]
    return [day for day in found if day.weekday() < 5]


def prefix() -> str:
    """The prefix a listing of this class and resolution is asked under."""
    return key_for(ASSET_CLASS, RESOLUTION, FIRST).rsplit("/", 3)[0] + "/"


def obj(day: date, ticker: str = "AAPL") -> bytes:
    """One gzipped aggregate object, holding the day the API serves for the same session.

    The row is the API's own aggregate respelled. Both transports are therefore seeded
    from one vendor row, which is the whole reason a test may compare them.
    """
    row = bar(day)
    cells: dict[str, object] = {
        "ticker": ticker,
        "volume": row["v"],
        "open": row["o"],
        "close": row["c"],
        "high": row["h"],
        "low": row["l"],
        "window_start": row["t"] * 1_000_000,
        "transactions": row["n"],
    }
    lines = [",".join(COLUMNS), ",".join(str(cells[name]) for name in COLUMNS)]
    return gzip.compress(("\n".join(lines) + "\n").encode())


def answer(path: str, params: Mapping[str, str]) -> Response:
    """The store's two answers: a listing of a prefix, and a ranged read of one object.

    A listing is not scoped by plan and a read is, which is the whole reason the adapter
    proves entitlement with a GET; here both are served, because what this wire is for is
    the loader over a plan that includes the class.
    """
    if params.get("list-type"):
        return _page(
            [key_for(ASSET_CLASS, RESOLUTION, day) for day in days()]
            if params.get("prefix", "") == prefix()
            else []
        )
    day = day_of(key_of(path))
    if day is None or day not in days():
        return Response(404, NO_SUCH_KEY)
    return Response(206, obj(day)[:1])


def download(url: str, filepath: str, headers: Mapping[str, str]) -> None:
    """The engine's bulk download, frozen: one whole object streamed to a file.

    Held to the same `Host` rule as the transport, because it is the second real sender
    and the store answers a request bearing two `Host` headers with a `400` and an HTML
    page rather than an object.
    """
    refuse_host(headers, "download")
    day = day_of(key_of("/" + url.split("//", 1)[-1].split("/", 1)[-1]))
    assert day is not None
    Path(filepath).write_bytes(obj(day))


def key_of(path: str) -> str:
    """The object key a store path names, bucket stripped."""
    return path.split(f"/{BUCKET}/", 1)[1]


def _page(keys: Sequence[str]) -> Response:
    """One `ListObjectsV2` page holding every key, in the namespace the store answers in."""
    contents = "".join(
        f"<Contents><Key>{key}</Key><Size>{index + 1}</Size>"
        f"<LastModified>2024-04-01T00:00:00.000Z</LastModified></Contents>"
        for index, key in enumerate(keys)
    )
    return Response(
        200,
        (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<ListBucketResult xmlns="{LISTED}"><Name>{BUCKET}</Name>'
            f"<IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
        ).encode(),
    )
