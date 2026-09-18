"""The rendered templates describe what the package does with what they hold.

A template is the first page an operator reads, in the file they are about to edit, so a
comment that describes a check the package does not make — or omits a key it does read —
is a page that lies where it is trusted most.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from kanso.config import Config, render_config
from kanso.research import driver
from kanso.schemas import CardStatus
from tests.docs.test_pages import spelled

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "kanso" / "templates"


def template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def line_with(text: str, key: str) -> str:
    return next(line for line in text.splitlines() if line.lstrip("# ").startswith(key))


def test_the_config_template_renders_every_section_the_parser_declares() -> None:
    rendered = render_config("0.1.0")
    headers = set(re.findall(r"^#? ?\[([a-z]+)", rendered, re.M))
    for name in Config.model_fields:
        if name in ("kanso_version", "schema_version"):
            assert re.search(rf"^{name} = ", rendered, re.M), name
        else:
            assert name.split("_")[0] in headers, name


def test_the_data_table_is_rendered_commented_with_the_parser_defaults() -> None:
    """Header included, so a `[data]` table an operator appends is not declared twice."""
    rendered = render_config("0.1.0")
    assert "\n# [data]" in rendered
    assert "\n[data]" not in rendered
    assert line_with(rendered, "reference").startswith('# reference = "none"')
    assert line_with(rendered, "adjusted").startswith("# adjusted = false")


def test_the_currency_key_is_commented_as_the_account_currency_check() -> None:
    line = line_with(template("kanso.toml"), "currency")
    assert "account currency of every venue" in line
    assert "venues carry more than one account currency" in line


def test_the_memory_per_lane_example_names_both_things_that_set_it() -> None:
    """The comment stated the derivation as the whole rule after a declaration could
    replace it, in the file an operator reads to learn where the figure came from."""
    line = line_with(template("envelope.yaml"), "mem_per_lane_gb:")
    assert "`[env] mem_per_lane_gb` when declared" in line
    assert "max(4, 1.5 × max baseline peak_mem_gb over runs)" in line


def test_the_stage_speed_is_commented_as_unpaced_in_this_version() -> None:
    line = line_with(template("portfolio.yaml"), "speed:")
    assert "replays unpaced whatever this says" in line
    assert "`kanso replay run --speed`" in line


def test_the_venue_costs_example_says_what_a_bar_only_hypothesis_needs() -> None:
    line = line_with(template("portfolio.yaml"), "costs:")
    assert "`spread: fixed_bps, fixed_bps: <width>`" in line


def test_the_scaffold_says_a_bar_only_hypothesis_needs_a_fixed_spread() -> None:
    text = template("hypothesis.yaml")
    costs = text[text.index("# costs:") : text.index("# capital:")]
    assert "`quotes` needs `quote` in data_requirements" in costs
    assert "`kanso hyp validate` refuses (exit 3)" in costs


def test_the_program_states_the_repair_budget_the_driver_enforces() -> None:
    """The program tells an interactive proposer how many goes one fault buys, in words,
    and the driver holds the same number as a constant. The two drifted apart once: the
    driver spent the budget on the run's crash streak while this page, and `docs/cli.md`
    with it, promised it per idea."""
    stated = re.search(r"if (\w+) goes do not fix it", template("program.md"))
    assert stated is not None
    assert spelled(stated.group(1)) == driver.REPAIRS


def test_the_program_names_every_status_a_card_can_carry_and_only_the_printed_ones() -> None:
    """Step 3 tells the proposer to read one output line and names the statuses it can
    say; step 4 covers the one the command refuses instead of printing. A status in the
    wrong half is a page that contradicts itself, which `redundant` did."""
    text = template("program.md")
    listed = re.search(r"`([a-z|]+) \u00b7 metric \u00b7 \u0394 \u00b7 reason`", text)
    assert listed is not None
    assert set(listed.group(1).split("|")) == set(get_args(CardStatus)) - {"redundant"}
    assert "`redundant` (exit 2)" in text, "the status the command refuses rather than prints"
