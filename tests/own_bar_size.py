"""`Config` classes that give `bar_resolution` a default of their own, spelt the ways a file can.

kanso sets `bar_resolution` from the hypothesis — on a card always, `None` where the file states
none, and on a stage from the version's manifest, which carries it only where the file states
one — so a class's own default would stand on a stage and not on its card, and the runner
refuses one wherever it builds the class: in a card's child, where a version's manifest is
written and where a node loads one. `strategy_integrity` reads the first spelling, a top-level
class body; each of the other four makes the same class by a spelling its scan does not follow.
Each block defines `Config` at module level, in place of a test source's own; so do the two whose
annotation resolves nowhere at run time, one giving the field a default of its own.
"""

from __future__ import annotations

from typing import Final

import pytest

TOP_LEVEL: Final = b'class Config(KansoConfig):\n    bar_resolution: str | None = "5m"\n'
IN_AN_IF: Final = (
    b'if True:\n\n    class Config(KansoConfig):\n        bar_resolution: str | None = "5m"\n'
)

DECLARING: Final = pytest.mark.parametrize(
    "config",
    [
        TOP_LEVEL,
        IN_AN_IF,
        b'Base = KansoConfig\n\n\nclass Config(Base):\n    bar_resolution: str | None = "5m"\n',
        b'class Config(KansoStrategy.config_cls):\n    bar_resolution: str | None = "5m"\n',
        b"Config = type(\n"
        b'    "Config",\n'
        b"    (KansoConfig,),\n"
        b'    {"__annotations__": {"bar_resolution": "str | None"}, "bar_resolution": "5m"},\n'
        b")\n",
    ],
    ids=[
        "a top-level class",
        "in a module-level if",
        "off a base under another name",
        "off KansoStrategy.config_cls",
        "built by type() with its annotation",
    ],
)

UNRESOLVED: Final = (
    b"from typing import TYPE_CHECKING\n\n"
    b"if TYPE_CHECKING:\n"
    b"    from decimal import Decimal\n\n\n"
    b"class Config(KansoConfig):\n"
    b'    cap: "Decimal | None" = None\n'
)
"""A `Config` annotating a field with a type imported for a type checker alone, a name that
resolves nowhere at run time; `strategy_integrity` admits the file."""

UNRESOLVED_DECLARING: Final = UNRESOLVED + b'    bar_resolution: str | None = "5m"\n'
"""The same class giving `bar_resolution` a default of its own."""

REFUSED: Final = (
    "Config gives bar_resolution a default of '5m', where KansoConfig gives None; kanso sets the "
    "field from the hypothesis — on a card always, None where the file states none, and on a "
    "stage from the version's manifest, which carries it only where the file states one — so a "
    "stage's sleeve would not be configured as its card's was"
)
"""What each spelling is refused with, after the name of the file it was read from."""


def card_remedy(hyp_id: str) -> str:
    """The remedy on a card: the field deleted from the hypothesis's file, and research begun
    from that file, since a run starts from the best card's bytes where there is one."""
    return (
        f"delete bar_resolution from Config in hypotheses/{hyp_id}/strategy.py and begin from "
        f"that file with `kanso research begin {hyp_id} --from-workspace`, since a run starts "
        "from the best card's bytes where there is one: kanso sets the field, and a file reading "
        "bars beside ticks states their size as bar_resolution in hypothesis.yaml"
    )


def version_remedy(hyp_id: str) -> str:
    """The remedy on a version, whose bytes are certified: research begun from the file with the
    field deleted, and the best it finds certified."""
    return (
        f"delete bar_resolution from Config in hypotheses/{hyp_id}/strategy.py, research from "
        f"that file with `kanso research begin {hyp_id} --from-workspace` and certify the best "
        f"it finds with `kanso cert run {hyp_id}`: a version runs the bytes it was certified with"
    )


STRATEGY: Final = b"class Strategy(KansoStrategy):"


def hidden_after_its_body(source: bytes) -> bytes:
    """`source` with `bar_resolution` set on its `Config` class once the class is made: an
    attribute that hides the field without declaring it, so the class's default stays `None`,
    and that no class body shows `strategy_integrity`."""
    assert source.count(STRATEGY) == 1
    return source.replace(STRATEGY, b'Config.bar_resolution = "5m"\n\n\n' + STRATEGY)
