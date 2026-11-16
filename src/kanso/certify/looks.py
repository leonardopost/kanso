"""How many times a hypothesis's lineage has already looked at a certification window.

A certification window is the one sample research never saw, and every certificate reads it:
pass or fail, the verdict is a look, and a lineage that looks again — a new best, a replan,
an engine upgrade's re-certification, a sibling written after the first failed — is drawing
again from the same sample. Nothing about a certificate's own gates counts the draws, so this
module does, and the deflation splits its level across them
(`kanso.criteria.gates.look_level`).

**A look is a certificate of the lineage that read a window intersecting this one.** The
lineage is the connected component of `derived_from` among registered hypotheses
(`kanso.research.records.lineage`): a sibling certified on the same window looked at the same
data, so it is counted, where `family` — ancestors only — would let each new sibling start
again at one. Every certificate of every member counts, under any plan version, engine,
pin or verdict, whose certification window — the `windows.certification` of the hypothesis
file its row names, read as a plain mapping so a pin written under an older schema is still
read — intersects the window asked about. Intersection rather than equality, so a re-pin that
moves the window by a day does not reset the count, and a re-pin onto a window wholly after
it starts a new one. A row written before certificates recorded their pin names none and is
read under its hypothesis's pin as it stands; a row whose file the store does not hold, or
which declares no certification window, cannot be shown to have looked elsewhere and counts.

**A certificate that read no window is not a look.** On a snapshot that cannot support a
claim certification runs nothing and records every gate as failed with the snapshot's
reasons under `inadmissible` (`kanso.certify.run`); it read nothing of the window, so it is
left out. A stall spared by its search correction writes no certificate and is no look
either.

Nothing here is stored. The count is struck from the certificates table each time it is
asked for, so certificates written before kanso counted looks count at once, and the one a
certificate records (`Certificate.holdout`) is what stood when it was made.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import yaml

from kanso.research import records
from kanso.schemas import Holdout

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore

__all__ = ["INADMISSIBLE", "Look", "Looks", "certification_window", "looks", "read_no_window"]

INADMISSIBLE: Final = "inadmissible"
"""The evidence key a gate failed on a snapshot that cannot support a claim records its
reasons under: a certificate whose every gate carries it read no window."""

Window = tuple[date, date]


@dataclass(frozen=True)
class Look:
    """One certificate that looked at the window: whose, of which bytes, under which plan,
    when, and what it found."""

    hyp_id: str
    sha7: str
    plan_version: int
    created_at: str
    verdict: str


@dataclass(frozen=True)
class Looks:
    """The looks a lineage has already taken at one certification window."""

    window: Window
    lineage: tuple[str, ...]
    prior: tuple[Look, ...]

    @property
    def look(self) -> int:
        """Which look the next certificate at this window is: one more than those taken."""
        return len(self.prior) + 1

    def holdout(self) -> Holdout:
        """What the next certificate at this window records about the looks before it."""
        start, end = self.window
        return Holdout(
            window_start=start, window_end=end, look=self.look, lineage=list(self.lineage)
        )


def looks(store: StateStore, hyp_id: str, window: Window) -> Looks:
    """The certificates `hyp_id`'s lineage has made at a certification window intersecting
    `window` that read one, oldest first."""
    lineage = records.lineage(store, hyp_id)
    marks = ", ".join("?" for _ in lineage)
    pins = {
        str(row["hyp_id"]): row["hypothesis_sha"]
        for row in store.connection.execute(
            f"SELECT hyp_id, hypothesis_sha FROM hypotheses WHERE hyp_id IN ({marks})", lineage
        ).fetchall()
    }
    rows = store.connection.execute(
        "SELECT hyp_id, hypothesis_sha, strategy_sha, plan_version, created_at, verdict, gates"
        f" FROM certificates WHERE hyp_id IN ({marks}) ORDER BY created_at, rowid",
        lineage,
    ).fetchall()
    windows: dict[object, Window | None] = {}
    prior: list[Look] = []
    for row in rows:
        if read_no_window(gate.get("evidence") or {} for gate in json.loads(str(row["gates"]))):
            continue
        pin = row["hypothesis_sha"] or pins.get(str(row["hyp_id"]))
        if pin not in windows:
            windows[pin] = certification_window(store, pin)
        seen = windows[pin]
        if seen is not None and not _intersect(seen, window):
            continue
        prior.append(
            Look(
                hyp_id=str(row["hyp_id"]),
                sha7=str(row["strategy_sha"])[:7],
                plan_version=int(row["plan_version"]),
                created_at=str(row["created_at"]),
                verdict=str(row["verdict"]),
            )
        )
    return Looks(window=window, lineage=lineage, prior=tuple(prior))


def certification_window(store: StateStore, pin: object) -> Window | None:
    """The certification window a pinned hypothesis file declares, read as a plain mapping
    so a file pinned under an older schema is still read; `None` for no pin, a file the store
    does not hold, or one that declares no certification window."""
    if not pin or not store.has_blob(str(pin)):
        return None
    found = yaml.safe_load(store.get_blob(str(pin)).decode("utf-8"))
    windows = found.get("windows") if isinstance(found, dict) else None
    span = windows.get("certification") if isinstance(windows, dict) else None
    if not isinstance(span, dict):
        return None
    return date.fromisoformat(str(span["start"])), date.fromisoformat(str(span["end"]))


def _intersect(one: Window, other: Window) -> bool:
    """Whether two closed spans of days share a day."""
    return one[0] <= other[1] and other[0] <= one[1]


def read_no_window(evidence: Iterable[Mapping[str, object]]) -> bool:
    """Whether a certificate whose gates recorded this evidence failed every one on a
    snapshot that cannot support a claim, so it ran no window and is no look. A certificate
    names at least one gate."""
    return all(INADMISSIBLE in recorded for recorded in evidence)
