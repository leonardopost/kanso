"""The rendered templates describe what the package does with what they hold.

A template is the first page an operator reads, in the file they are about to edit, so a
comment that describes a check the package does not make — or omits a key it does read —
is a page that lies where it is trusted most.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from kanso.config import Config, render_config
from kanso.criteria.integrity import DENIED_UNDER_SIZING, scan
from kanso.errors import ValidationError
from kanso.nautilus.clock import NEW_YORK, segment_of, session_of
from kanso.nautilus.strategy import KansoModifier
from kanso.research import driver
from kanso.schemas import CardStatus
from kanso.schemas.screen import Screen
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


def test_the_scaffold_says_a_full_book_budget_is_held_to_the_ceilings_once() -> None:
    """The comment named the check `hyp validate` makes and not that it is the only one: at
    run time a sized entry is the whole budget whatever the account has lost."""
    text = template("hypothesis.yaml")
    sizing = " ".join(
        line.lstrip("# ").strip()
        for line in text[text.index("# sizing:") : text.index("risk_limits:")].splitlines()
    )
    assert "capital x max_leverage >= budget, on the capital, once" in sizing
    assert "every entry is the whole budget whatever the account has lost" in sizing
    assert "at a leverage of budget / balance, past max_leverage once the balance" in sizing
    assert "still bought below a balance of zero" in sizing


def test_the_program_states_the_repair_budget_the_driver_enforces() -> None:
    """The program tells an interactive proposer how many goes one fault buys, in words,
    and the driver holds the same number as a constant. The two drifted apart once: the
    driver spent the budget on the run's crash streak while this page, and `docs/cli.md`
    with it, promised it per idea."""
    stated = re.search(r"if (\w+) goes do not fix it", template("program.md"))
    assert stated is not None
    assert spelled(stated.group(1)) == driver.REPAIRS


def fair_game() -> str:
    """The Scope bullet that says what the search may change, which the alignment judge and the
    proposer both read in the run's pinned `program.md`."""
    return next(
        line
        for line in template("program.md").splitlines()
        if line.startswith("- Fair game inside `strategy.py`")
    )


def test_the_program_says_a_filter_the_thesis_does_not_state_is_drift() -> None:
    """Counted in a live workspace on 2026-11-17: 22 of 37 `misaligned` rewinds were filters,
    gates or exits, directions the template's Scope line called fair game. The judge reads the
    run's `program.md` before the thesis, and so does the proposer, so the line is an
    instruction to both: it now says fair game is what the thesis leaves open, that a filter,
    gate, entry condition or exit the thesis does not state is drift the judge rewinds, and
    that the program may name one."""
    bullet = fair_game()
    assert "Fair game inside `strategy.py` is what the thesis leaves open" in bullet
    assert "any filter, gate, entry condition or exit the thesis states or this program names" in (
        bullet
    )
    assert (
        "A filter, gate, entry condition or exit the thesis does not state, and this program "
        "does not name, is not a refinement of the idea but another idea: it is drift, and the "
        "alignment judge rewinds it."
    ) in bullet
    assert "filters, entry/exit rules" not in template("program.md")


def test_the_program_says_every_keep_is_judged_and_the_clock_besides() -> None:
    """The line said drift was detected every `align_every` cards, which was false of the
    interactive path the same file describes — `research card` runs no check — and is false of
    the driver, which judges every keep as it is made."""
    bullet = fair_game()
    assert (
        "Under `kanso research run` and the daemon every keep is judged as it is made, and the "
        "run every `align_every` cards besides"
    ) in bullet
    assert "`kanso align check {{hyp_id}}` asks the same question by hand" in bullet
    assert "Drift is detected every `align_every` cards" not in template("program.md")


def test_the_program_says_a_stall_s_certification_can_be_spared() -> None:
    """An interactive proposer reads what a stall leads to here; told only that a keep above
    zero is certified, it would take a spared stall's missing certificate for a fault."""
    never_stop = template("program.md").split("## Never stop", 1)[1]
    assert (
        "unless a research gate of the plan — its search correction, `walk_forward_consistency`, "
        "`overfit_probability` where the plan carries it, or `research_plateau` — refuses it on "
        "research data, which spares the "
    ) in never_stop
    assert "the search's pick ranks at or below the median of its trials out of sample" in (
        never_stop
    ), "what the selection's gate refuses, beside what the others do"


def test_the_program_names_every_status_a_card_can_carry_and_only_the_printed_ones() -> None:
    """Step 3 tells the proposer to read one output line and names the statuses it can
    say; step 4 covers the one the command refuses instead of printing. A status in the
    wrong half is a page that contradicts itself, which `redundant` did."""
    text = template("program.md")
    listed = re.search(r"`([a-z|]+) \u00b7 metric \u00b7 \u0394 \u00b7 reason`", text)
    assert listed is not None
    assert set(listed.group(1).split("|")) == set(get_args(CardStatus)) - {
        "redundant",
        "runner_up",
    }
    assert "`redundant` (exit 2)" in text, "the status the command refuses rather than prints"
    assert "`runner_up` is written only by a turn of several cards" in text, "nor this one"


def test_the_program_tells_the_proposer_a_settlement_is_not_answered() -> None:
    """The proposer is handed each settlement's realised rate; the program says what the
    runner does with an order placed in answer to it, so the loop does not search for a
    carry the extraction refuses to pay."""
    text = re.sub(r"\s+", " ", template("program.md"))
    assert "on what you held before that instant" in text
    assert "an order placed in answer to the rate neither collects nor escapes it" in text


def test_the_program_s_print_rule_advice_says_a_sized_strategy_cannot_follow_it() -> None:
    """The print-rule bullet once told every proposer to size a taker to the quote or send an
    IOC limit, which a strategy under a `sizing` rule cannot do: `strategy_integrity` discards
    one that names a size, `submit_order` or `order_factory` before any backtest, and
    `submit_entry` takes no time in force. The advice is scoped to a strategy without one, and
    the names it gives for an IOC limit are the ones a sized strategy is denied."""
    bullet = line_with(template("program.md"), "- Under `costs.limit_fill: print_through`")
    unsized, sized = bullet.split("Without a `sizing` rule", 1)[1].split("Under `sizing:", 1)
    assert "immediate-or-cancel" in unsized
    for name in ("order_factory", "submit_order"):
        assert f"`{name}`" in unsized and f"`{name}`" in sized
        assert name in DENIED_UNDER_SIZING
    assert "discarded before any backtest" in sized


def test_the_program_says_the_print_rule_holds_where_nothing_states_it() -> None:
    """The proposer reads `program.md` and the raw `hypothesis.yaml`, never the resolved venue
    model, and at tick resolution the print rule is the default for a hypothesis that asks
    every name for quotes and prints. The bullet once opened "in `hypothesis.yaml`", which told
    a lane on the default that the tape's rules did not apply to it."""
    bullet = line_with(template("program.md"), "- Under `costs.limit_fill: print_through`")
    assert "in `hypothesis.yaml` the venue fills by the tape" not in bullet
    assert "asks every name for `quote` and `trade` and none for `bar`" in bullet
    assert "a `hypothesis.yaml` that names no `limit_fill` is no sign the rule is off" in bullet


def test_the_program_tells_the_strategy_an_entry_past_its_rate_is_dropped_not_refused() -> None:
    """The loop's strategy reads `program.md` and nothing else of the rate: it is told that an
    entry or a modify past it is dropped and counted while the card runs on, and which digest
    fields count them; that only an exit `submit_exit` sized is owed; and that an exit it built
    itself refuses the card, whatever its type or time in force — the day's, a size in the
    quote's currency and an execution algorithm named among them, where the bullet once
    promised an exit built at market or at a good-till-cancelled limit was owed as asked."""
    bullet = re.sub(
        r"\s+", " ", line_with(template("program.md"), "- Under `costs.orders_per_minute`")
    )

    assert "is dropped unsent and counted" in bullet
    assert "the card runs on" in bullet
    assert "`refused_submits` and `refused_modifies`" in bullet
    assert "Only an exit `submit_exit` sized is owed" in bullet
    assert "refuses the card (`order_rate`" in bullet
    for named in ("for the day", "sized in the quote's currency", "an execution algorithm"):
        assert named in bullet, named
    assert "an entry, a modify or an order list refuses the card" not in bullet
    assert "is owed as you asked it" not in bullet


def test_the_program_names_the_minute_a_bar_s_session_reads_as_the_clock_reads_it() -> None:
    """The loop reads `program.md` and not the clock's source: the bar it names as the first
    regular one, and the minutes it gives, are the clock's in winter and in summer."""
    bullet = line_with(template("program.md"), "- `self.session`")
    stated = (
        ((9, 30), "the bar stamped 09:30 is `pre`, minute 569", ("pre", 569)),
        ((9, 31), "the first regular bar is the one stamped 09:31, minute 570", ("rth", 570)),
        ((16, 0), "the bar stamped 16:00 is the last regular one, minute 959", ("rth", 959)),
    )
    for day in (date(2024, 1, 2), date(2024, 7, 2)):
        for (hour, minute), said, (segment, of_day) in stated:
            assert said in bullet
            stamp = int(datetime.combine(day, time(hour, minute), NEW_YORK).timestamp()) * 10**9
            assert session_of(stamp) == (segment, day, of_day)


def test_the_program_says_a_late_close_reads_its_own_minute_and_a_modifier_reads_its_host_s() -> (
    None
):
    """The bullet speaks to a sleeve: a close published after 16:00 still reads `rth` while
    its order lands in the post-market, and an attached construct, whose base has no
    `session`, reads its host's by a call `strategy_integrity` passes."""
    bullet = line_with(template("program.md"), "- `self.session`")
    assert bullet.startswith("- `self.session` is a sleeve's New York time")
    assert (
        "a close published fifteen minutes after its 16:00 reference time still reads `rth`, "
        "and an order sent on it goes in during the post-market"
    ) in bullet
    day = date(2024, 1, 2)
    close = int(datetime.combine(day, time(16, 0), NEW_YORK).timestamp()) * 10**9
    assert session_of(close)[::2] == ("rth", 959)
    assert segment_of(close + 15 * 60 * 10**9) == ("post", day)

    said = "read its host's as `session_of(ctx.ts_event)`, imported from `kanso.nautilus.strategy`"
    assert said in bullet
    assert not hasattr(KansoModifier, "session")
    modifier = (
        "from kanso.nautilus.strategy import Decision, KansoModifier, session_of\n"
        "class Gate(KansoModifier):\n"
        "    construct = 'filter'\n"
        "    def evaluate(self, ctx):\n"
        "        return Decision(allow=session_of(ctx.ts_event).segment == 'rth')\n"
    )
    assert scan(modifier) == []


def test_the_program_and_the_scaffold_say_an_overlay_s_closing_leg_or_clip_refuses_the_card() -> (
    None
):
    """An attached construct's research reads the same `program.md`, and an overlay cannot close
    with `submit_exit`: its leg or clip that closes past the rate refuses the card too, and the
    remedy it is given is fewer commands or the account's own figure. The bullet keeps no order
    among what waits beyond what the replay tests pin — it once promised that a token coming
    back went to the owed exits and the waiting cancels before any entry — and says a second
    ask past the rate does not replace an exit rule's."""
    bullet = re.sub(
        r"\s+", " ", line_with(template("program.md"), "- Under `costs.orders_per_minute`")
    )
    scaffold = re.sub(r"\s*#\s*", " ", template("hypothesis.yaml"))

    assert "So does an overlay's leg or clip that closes" in bullet
    assert "send fewer commands, or state the account's own figure" in bullet
    assert "unless the first was an exit rule's" in bullet
    assert "before any entry you send" not in bullet
    assert "So is an overlay's leg or clip that closes: send fewer commands" in scaffold


def uncommented_screen() -> dict[str, Any]:
    """The screen template's body with its examples uncommented: a `# key:` at the margin loses
    its marker and its space, an indented line its marker, and a line commented twice stays a
    comment, as it is in the file."""
    body = template("screen.yaml").split("schema: 1\n", 1)[1]
    rows = []
    for line in body.splitlines():
        if line.startswith("{{"):
            continue
        if re.match(r"# [a-z_]+:", line):
            line = line[2:]
        elif re.match(r"#\s+[^\s#]", line):
            line = line[1:]
        rows.append(line)
    found = yaml.safe_load("\n".join(rows))
    assert isinstance(found, dict)
    return found


def test_the_screen_template_s_condition_example_follows_catalog_legs_alone() -> None:
    """Every operator's screen starts from this file, and its example once named `follow`, a
    group holding the derived `miners`, beside a comment saying never a derived leg. Validated
    with the template's own legs, derived legs, groups and signals — the legs its derived ones
    read and it never declares stood in for by bars — the example is admitted, and the group it
    named is refused."""
    found = uncommented_screen()
    (example,) = [item for item in found["measures"] if item["id"] == "condition"]
    legs = dict(found["legs"])
    for name in ("riot", "coin", "mstr", "okx_btc", "binance_btc"):
        legs.setdefault(
            name, {"instrument": f"{name.upper()}.XNAS", "type": "bar", "resolution": "1s"}
        )

    def screen(measure: dict[str, Any]) -> Screen:
        return Screen.model_validate(
            {
                "schema": 1,
                "id": "probe",
                "title": "probe",
                "thesis": "probe",
                "window": {"start": "2024-01-02", "end": "2024-03-29"},
                "legs": legs,
                "derived": found["derived"],
                "groups": found["groups"],
                "signals": found["signals"],
                "clock": {"hours": {"tz": "America/New_York", "span": "09:31-16:01"}},
                "measures": [measure],
                "verdict": found["verdict"],
            }
        )

    assert example["followers"] in found["legs"]
    assert screen(example).measures[0].id == "condition"
    assert "miners" in found["groups"]["follow"] and "miners" in found["derived"]
    with pytest.raises(ValidationError, match="'miners' is a derived leg"):
        screen({**example, "followers": "follow"})
