"""`docs/` and `README.md` describe the package that ships, and disagree with it nowhere.

Each test here pins one place a page and the package once said different things: a
check `doctor` runs that the page did not name, a path the page named that did not
import, a count the page stated that the table did not hold. A page is a promise, so a
page that drifts is a defect and this is where it fails.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from kanso.classify import catalogue
from kanso.config import Config, render_config
from tests.cli.test_doctor import CHECKS

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def page(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def prose(text: str) -> str:
    """The text with its line wrapping undone, so a sentence is matched as one string."""
    return re.sub(r"\s+", " ", text)


def section(text: str, heading: str) -> str:
    """The body of one `## heading`, up to the next heading of that level."""
    start = text.index(f"\n## {heading}")
    rest = text[start + 1 :]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


# -- docs/cli.md --------------------------------------------------------------------


def test_the_doctor_row_names_every_check_doctor_runs() -> None:
    row = next(line for line in page("cli.md").splitlines() if line.startswith("| `kanso doctor"))
    for name in CHECKS:
        assert f"`{name}`" in row, name


def test_the_cli_page_says_research_begin_needs_no_register() -> None:
    row = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso research begin")
    )
    assert "Needs no model and opens no register" in row


def test_the_cli_page_says_start_reads_the_envelope_as_last_detected() -> None:
    row = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso research start`")
    )
    assert "does not re-detect" in row


def test_the_cli_page_says_the_transport_is_the_loader_the_spec_names() -> None:
    row = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso data backfill")
    )
    assert "never for you" in row


def test_the_cli_page_says_a_stage_speed_paces_nothing_in_this_version() -> None:
    text = prose(page("cli.md"))
    assert "a stage's `speed` paces nothing yet" in text
    assert "the catch-up itself runs unpaced" in text


def test_the_cli_page_states_both_sides_of_the_paper_gate() -> None:
    monitoring = prose(section(page("cli.md"), "Monitoring"))
    assert "a short window is a fail" in monitoring
    assert "a result above the band fails exactly as one below it does" in monitoring


# -- docs/workspace.md and docs/concepts.md -------------------------------------------


def test_both_pages_say_a_lane_writes_no_log_of_its_own() -> None:
    for name in ("workspace.md", "concepts.md"):
        text = prose(page(name))
        assert "A lane writes no log of its own" in text, name
        assert "`kanso research show`" in text, name
        assert "`events` table" in text, name


def test_the_workspace_page_lists_every_section_the_parser_declares() -> None:
    toml = section(page("workspace.md"), "`kanso.toml`")
    for name in Config.model_fields:
        if name in ("kanso_version", "schema_version"):
            assert f"`{name}`" in toml, name
        elif name in ("extensions_paths", "skills_targets"):
            assert f"`[{name.split('_')[0]}]`" in toml, name
        elif name == "adapters":
            assert "`[adapters.<id>]`" in toml
        else:
            assert f"`[{name}]`" in toml, name
    for header in re.findall(r"^\[([a-z]+)\]", render_config("0.1.0"), re.M):
        assert f"`[{header}]`" in toml, header


def test_the_workspace_page_says_the_two_top_level_keys_are_read_by_nothing() -> None:
    toml = prose(section(page("workspace.md"), "`kanso.toml`"))
    assert "written by `init` and read by nothing" in toml
    assert "`PRAGMA user_version`" in toml


def test_the_workspace_page_states_the_currency_check_as_the_account_currency() -> None:
    assert "it is the account currency that `kanso hyp validate` checks" in prose(
        page("workspace.md")
    )
    refusals = section(page("workspace.md"), "What the workspace refuses")
    assert "different account currencies" in refusals


def test_the_workspace_page_states_the_fixed_spread_a_bar_only_hypothesis_needs() -> None:
    text = page("workspace.md")
    assert "`costs.fixed_bps`" in text
    assert "`fixed_bps`" in section(text, "What the workspace refuses")


def test_the_workspace_page_says_a_stage_speed_paces_nothing_in_this_version() -> None:
    assert "it paces nothing" in prose(section(page("workspace.md"), "`portfolio.yaml`"))


def test_the_who_writes_what_table_lists_the_mock_script() -> None:
    table = section(page("workspace.md"), "Who writes what")
    assert "| `mock/responses.yaml` | `init --demo` |" in table


def test_the_concepts_page_states_both_sides_of_the_paper_gate() -> None:
    promotion = prose(section(page("concepts.md"), "Promotion and demotion"))
    assert "a shorter window is a `fail`, not a skip" in promotion
    assert "above the band as much a fail as below it" in promotion


# -- docs/constructs.md ---------------------------------------------------------------

ATTACHES = {"none": "nothing", "sleeve": "a sleeve", "portfolio": "the portfolio"}


def test_the_construct_table_attaches_each_construct_where_its_item_says() -> None:
    rows = {
        cells[1].strip("` "): cells
        for line in section(page("constructs.md"), "The catalogue").splitlines()
        if line.startswith("| `")
        for cells in [line.split("|")]
    }
    for construct_id, entry in catalogue().entries.items():
        attaches = rows[construct_id][3].strip()
        stated = re.split(r" \(|;", attaches)[0].strip()
        assert stated == ATTACHES[entry.item.needs_host], construct_id


# -- docs/extensions.md and docs/adapters.md -------------------------------------------


def test_the_provider_is_named_at_the_path_it_imports_from() -> None:
    for name in ("extensions.md", "adapters.md"):
        assert "`kanso.data.instruments.InstrumentProvider`" in page(name), name
    assert "`kanso.data.instruments.ResolveError`" in page("extensions.md")


def test_every_dotted_kanso_path_the_docs_name_resolves() -> None:
    found = set()
    for path in [*DOCS.glob("*.md"), ROOT / "README.md"]:
        found.update(re.findall(r"`(kanso\.[a-z_]+(?:\.[A-Za-z_]+)+)`", path.read_text()))
    assert found
    for dotted in sorted(found):
        module, _, attribute = dotted.rpartition(".")
        assert hasattr(importlib.import_module(module), attribute), dotted


# -- docs/maintainers.md --------------------------------------------------------------


def test_the_maintainer_page_records_how_credentialed_acceptance_is_run() -> None:
    text = prose(page("maintainers.md"))
    assert "no `tests/live/` tree and no `live` marker" in text
    assert "maintainer-driven CLI run, recorded in the pull request" in text


def test_the_maintainer_page_does_not_ask_for_a_schema_version_bump() -> None:
    text = prose(page("maintainers.md"))
    assert "`schema_version` bump" not in text
    assert "follows from the newest migration file" in text


def test_the_maintainer_page_says_the_supervisor_checks_the_schema() -> None:
    text = prose(page("maintainers.md"))
    assert "the supervisor entry point does not check" not in text
    assert "the supervisor entry point checks the same thing" in text


def test_the_maintainer_page_says_the_supervisor_does_not_redetect() -> None:
    assert "does not re-detect at startup" in prose(page("maintainers.md"))


# -- README.md ------------------------------------------------------------------------

_UNITS = [
    *["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],
    *["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen"],
    *["seventeen", "eighteen", "nineteen"],
]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def spelled(word: str) -> int:
    head, _, tail = word.partition("-")
    if head in _UNITS:
        return _UNITS.index(head)
    return (_TENS.index(head) + 2) * 10 + (spelled(tail) if tail else 0)


@pytest.mark.parametrize("word, number", [("seven", 7), ("thirty-four", 34), ("twenty", 20)])
def test_spelled_numbers_read_as_the_test_expects(word: str, number: int) -> None:
    assert spelled(word) == number


def test_a_backlog_count_the_readme_states_is_the_count_the_table_holds() -> None:
    """The README may describe the backlog without counting it; a count it states must hold."""
    rows = [
        line.split("|")[1:4]
        for line in page("backlog.md").splitlines()
        if re.match(r"\| \d+ \|", line)
    ]
    closed = sum(1 for _, _, item in rows if item.strip().startswith("~~"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    stated = re.search(r"([a-z-]+) entries, ([a-z-]+) of them open", readme)
    if stated is None:
        return
    assert (spelled(stated[1]), spelled(stated[2])) == (len(rows), len(rows) - closed)
