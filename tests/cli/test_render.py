"""The two-column human layout: what it aligns, and what it does when it cannot."""

from __future__ import annotations

from kanso.cli.render import LABEL, field


def test_a_short_label_is_padded_to_the_column() -> None:
    assert field("best", "f729a53") == "best       f729a53"


def test_a_label_as_long_as_the_column_keeps_a_space() -> None:
    """`kanso hyp show` labels a row with a hypothesis id, which may be forty characters."""
    long = "a" * LABEL
    assert field(long, "researching") == f"{long} researching"
    assert field("instruments", "none resolved") == "instruments none resolved"
