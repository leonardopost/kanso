"""The patch engine: what applies, what does not, and what a diff may name.

Applying a diff is where a proposal becomes a card, so the line between "this is the file
the model described" and "this is something else" is drawn here and nowhere later.
"""

from __future__ import annotations

import pytest

from kanso.errors import ValidationError
from kanso.research import diff

SOURCE = b"one\ntwo\nthree\n# end\n"


def apply(patch: str, source: bytes = SOURCE) -> bytes:
    return diff.apply(source, patch)


def test_a_round_trip_through_unified_and_apply_reproduces_the_target() -> None:
    before = b"alpha\nbeta\ngamma\n"
    after = b"alpha\nBETA\ngamma\ndelta\n"

    assert diff.apply(before, diff.unified(before, after)) == after


def test_a_hunk_replaces_the_lines_it_names() -> None:
    patch = "--- a/strategy.py\n+++ b/strategy.py\n@@ -2,1 +2,1 @@\n-two\n+TWO\n"

    assert apply(patch) == b"one\nTWO\nthree\n# end\n"


def test_a_pure_insertion_needs_no_context_and_lands_at_the_line_it_names() -> None:
    assert apply("@@ -0,0 +1,1 @@\n+zero\n") == b"zero\none\ntwo\nthree\n# end\n"
    assert apply("@@ -4,0 +5,1 @@\n+four\n") == b"one\ntwo\nthree\n# end\nfour\n"


def test_the_same_anchored_hunk_applies_however_often_the_file_has_grown() -> None:
    """The property the driver's script depends on: a surviving anchor is reusable."""
    patch = "@@ -4,1 +4,2 @@\n+added\n # end\n"

    once = apply(patch)
    twice = apply(patch, once)

    assert once == b"one\ntwo\nthree\nadded\n# end\n"
    assert twice == b"one\ntwo\nthree\nadded\nadded\n# end\n"


def test_a_hunk_whose_line_number_is_wrong_still_finds_its_context() -> None:
    """Offsets are tolerated because a hand-written diff miscounts; fuzz is not."""
    assert apply("@@ -99,1 +99,1 @@\n-three\n+THREE\n") == b"one\ntwo\nTHREE\n# end\n"


def test_two_hunks_apply_in_order_and_do_not_overlap() -> None:
    patch = "@@ -1,1 +1,1 @@\n-one\n+ONE\n@@ -3,1 +3,1 @@\n-three\n+THREE\n"

    assert apply(patch) == b"ONE\ntwo\nTHREE\n# end\n"


def test_a_file_with_no_trailing_newline_keeps_not_having_one() -> None:
    source = b"one\ntwo"

    assert diff.apply(source, "@@ -1,1 +1,1 @@\n-one\n+ONE\n") == b"ONE\ntwo"


def test_a_context_that_is_not_there_is_refused_with_the_line_it_wanted() -> None:
    with pytest.raises(ValidationError) as caught:
        apply("@@ -1,1 +1,1 @@\n-nowhere\n+here\n")

    assert "hunk 1 does not apply cleanly" in caught.value.message
    assert "'nowhere'" in caught.value.message


def test_an_insertion_past_the_end_of_the_file_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a place to insert"):
        apply("@@ -99,0 +99,1 @@\n+late\n")


def test_a_diff_naming_another_file_is_refused_before_a_line_is_read() -> None:
    patch = "--- a/hypothesis.yaml\n+++ b/hypothesis.yaml\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"

    with pytest.raises(ValidationError) as caught:
        apply(patch)

    assert "only strategy.py may change" in caught.value.message


def test_a_diff_under_a_directory_is_the_same_file() -> None:
    patch = (
        "--- a/runs/op/demo/strategy.py\n+++ b/runs/op/demo/strategy.py\n@@ -1 +1 @@\n-one\n+ONE\n"
    )

    assert apply(patch) == b"ONE\ntwo\nthree\n# end\n"


def test_a_diff_with_no_hunk_changes_nothing_and_says_so() -> None:
    with pytest.raises(ValidationError, match="holds no hunk"):
        apply("--- a/strategy.py\n+++ b/strategy.py\n")


def test_a_hunk_header_that_is_not_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="no @@ -old"):
        apply("@@ nonsense @@\n-one\n+ONE\n")


def test_a_body_line_that_is_neither_context_nor_a_change_is_refused() -> None:
    with pytest.raises(ValidationError, match="neither context"):
        apply("@@ -1,1 +1,1 @@\n-one\n+ONE\nprose about the change\n")


def test_a_fenced_diff_and_a_no_newline_marker_are_read_through() -> None:
    patch = "```diff\n@@ -1,1 +1,1 @@\n-one\n+ONE\n\\ No newline at end of file\n```\n\n"

    assert apply(patch) == b"ONE\ntwo\nthree\n# end\n"


def test_an_empty_context_line_is_a_context_line() -> None:
    source = b"one\n\ntwo\n"

    assert diff.apply(source, "@@ -1,3 +1,3 @@\n one\n\n-two\n+TWO\n") == b"one\n\nTWO\n"


def test_a_second_file_section_ends_the_previous_hunk_and_is_refused_by_name() -> None:
    patch = (
        "--- a/strategy.py\n+++ b/strategy.py\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"
        "--- a/other.py\n+++ b/other.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    )

    with pytest.raises(ValidationError, match="only strategy.py may change"):
        apply(patch)


def test_a_file_that_is_not_utf8_cannot_be_patched() -> None:
    with pytest.raises(ValidationError, match="not valid UTF-8"):
        diff.apply(b"\xff\xfe binary", "@@ -1,1 +1,1 @@\n-one\n+ONE\n")


def test_an_empty_file_takes_an_insertion() -> None:
    assert diff.apply(b"", "@@ -0,0 +1,1 @@\n+first\n") == b"first"


def test_two_sections_on_the_same_file_are_two_hunks() -> None:
    """A git-style patch repeats the headers; they end a hunk rather than joining it."""
    patch = (
        "--- a/strategy.py\n+++ b/strategy.py\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"
        "diff --git a/strategy.py b/strategy.py\n"
        "--- a/strategy.py\n+++ b/strategy.py\n@@ -3,1 +3,1 @@\n-three\n+THREE\n"
    )

    assert apply(patch) == b"ONE\ntwo\nTHREE\n# end\n"
