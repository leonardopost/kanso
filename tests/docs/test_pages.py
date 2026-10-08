"""`docs/` and `README.md` describe the package that ships, and disagree with it nowhere.

Each test here pins one place a page and the package once said different things: a
check `doctor` runs that the page did not name, a path the page named that did not
import, a count the page stated that the table did not hold. A page is a promise, so a
page that drifts is a defect and this is where it fails.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
import yaml

from kanso.classify import catalogue
from kanso.config import Config, ResearchConfig, render_config
from kanso.env.envelope import MIN_DECLARED_MEM_PER_LANE_GB
from kanso.models.wire import REQUEST_TIMEOUT_S
from kanso.nautilus.adapters.okx.reference import PAUSE_S, RETRIES
from kanso.research import driver
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


def test_the_pages_state_how_often_a_lane_explores_as_the_template_ships_it() -> None:
    """An operator learns from these two pages that a lane writes drafts unasked, and how
    often; a page still saying never would hide a model call from every workspace whose
    `kanso.toml` holds the template's number or omits the key. One initialised on 0.8.0 to
    0.13.x states 0 and keeps it, because `init` never rewrites `kanso.toml`."""
    stated = f"({ResearchConfig().explore_after_stalls} in the template; 0 is never)"
    assert stated in prose(page("cli.md"))
    assert stated in prose(page("concepts.md"))


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


def test_the_cli_page_says_the_daemon_starts_a_child_that_died_again() -> None:
    """A lane the kernel takes mid-card used to stay gone until the next `start`, its run
    worked by no one; an operator reading the page has to learn it now comes back, and
    where to see that it did."""
    rows = page("cli.md").splitlines()
    start = next(line for line in rows if line.startswith("| `kanso research start`"))
    assert "started again under the same name" in start
    assert "`lane_died`" in start and "`monitor_died`" in start
    assert "doubling" in start and "five minutes" in start
    stop = next(line for line in rows if line.startswith("| `kanso research stop`"))
    assert "is not started again" in stop
    status = next(line for line in rows if line.startswith("| `kanso research status`"))
    assert "started again" in status


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


def test_the_pages_say_a_prune_holds_the_daemon_off_and_copies_the_store_first() -> None:
    """A prune deletes research memory, so what makes that safe — the daemon held off, a
    whole copy first, room on the disk — and where the copy is have to be on the page."""
    row = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso state prune")
    )
    assert "Refused (exit 2) while a daemon runs" in row
    assert "`runs/state-<instant>.db`" in row
    assert "that copy and twice what is kept" in row
    assert "`--dry-run` counts what would go and writes nothing" in row
    workspace = prose(page("workspace.md"))
    assert "runs/state-<instant>.db" in workspace
    assert "`kanso state prune` deletes the books no run can select" in workspace
    assert "until `kanso state prune` deletes them" in prose(page("concepts.md"))


def test_both_pages_say_the_run_s_base_is_never_judged() -> None:
    """A model asked about the bytes a run was handed judged a seed nobody proposed, and
    each drift it reported "rewound" a run onto the bytes it was already on."""
    row = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso align check")
    )
    assert "The run's base is never judged" in row
    assert "Nor is the file the last check left the lane on" in row
    assert "`judged: false`" in row
    alignment = prose(section(page("concepts.md"), "Alignment"))
    assert "The run's base is never judged" in alignment
    assert "Nor is the file the last check left the lane on judged again" in alignment
    assert "`judged: false`" in alignment


def test_the_pages_say_a_closed_day_is_no_hole_and_an_answer_closes_nothing() -> None:
    """A chunk edge on a weekend split a series with no session missing, and the rule that
    first joined it counted an empty answer as coverage, a trading day the source lost
    included. The pages state the rule that replaced it: the market's calendar closes a
    day, and nothing a source says does."""
    row = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso data show")
    )
    assert "so every gap holds a day the market opened" in row
    assert "it is why the gap persists, and it closes nothing" in row
    concepts = prose(section(page("concepts.md"), "Snapshot"))
    assert "A day the market was closed is not a hole" in concepts
    assert "`kanso.data.closures`" in concepts
    assert "nothing a source says closes one" in concepts
    workspace = page("workspace.md")
    assert "Coverage counts only the days a market opened" in prose(workspace)
    assert "DEMO.SIM bar 1m · 2024-01-02..2024-04-30 · 33540 rows" in workspace


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


def test_the_pages_state_the_settlement_check_and_the_fee_rate_refusal() -> None:
    """Both refusals landed with the perpetual; the backlog row that asked for the first
    is closed, and the pages that stated its absence say what it refuses now."""
    workspace = prose(page("workspace.md"))
    assert "An instrument's own quote currency is not compared" not in workspace
    assert "settles in a currency other than its venue's account currency" in workspace
    assert "a non-zero `maker_fee` or `taker_fee`" in workspace
    doctor = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso doctor")
    )
    assert "non-zero maker or taker rate" in doctor
    backlog = next(line for line in page("backlog.md").splitlines() if line.startswith("| 46 |"))
    assert backlog.split("|")[3].strip().startswith("~~")


def test_the_concepts_page_says_what_a_perpetual_is_not_yet() -> None:
    concepts = prose(page("concepts.md"))
    assert "**A perpetual is a linear contract" in concepts
    assert "neither margin nor liquidation is simulated" in concepts


def test_the_concepts_page_states_how_funding_is_booked() -> None:
    """What is booked, when, the sign, the tie, where it lands, what the cost gates ignore
    and what the node lacks: each is a claim the funding suite checks."""
    concepts = prose(page("concepts.md"))
    assert "**A perpetual's funding is booked once, by the runner, beside every other cost.**" in (
        concepts
    )
    assert "`qty x mark x multiplier x rate` out of cash" in concepts
    assert "A long pays a positive rate and a short receives it" in concepts
    assert "of several prints at the instant, the greatest" in concepts
    assert "every fill stamped before the instant and no fill stamped at it" in concepts
    assert "deliberately not the `<=` rule" in concepts
    assert "an order placed in answer to the settlement changes nothing it settled" in concepts
    assert "inside the return and the equity of the period" in concepts
    assert "Its `pnl_net` is net of that and its `cost` is not" in concepts
    assert "leave funding exactly as it was booked" in concepts
    assert "fetched by the OKX package's `okx_funding` loader" in concepts


def test_the_workspace_page_states_the_funding_contract_and_its_refusal() -> None:
    """The type, its file columns, the realised-not-predicted rule, the validation refusal
    and the booking, each where an operator loading a perpetual reads."""
    from kanso.data.loaders.csv_parquet import columns_for
    from kanso.hyp.validate import FUNDING

    workspace = prose(page("workspace.md"))
    assert "**A perpetual's funding is data it requires.**" in workspace
    assert f"lists `{FUNDING}` in `data_requirements`" in workspace
    assert "remedy: add funding to data_requirements and load its realised funding history" in (
        workspace
    )
    assert "The rate is the **realised** rate of the period that just settled" in workspace
    required, optional = columns_for(FUNDING)
    assert required == ("ts_event", "rate") and optional == ("ts_init", "instrument_id")
    assert "A file maps `ts_event` and `rate`, and `instrument_id` where it holds one" in (
        workspace
    )
    assert "**The runner books each settlement once**" in workspace
    assert "`funding` is required of a hypothesis and asked of its perpetuals alone" in workspace
    assert "does not span the research and certification windows" in workspace
    refusals = section(page("workspace.md"), "What the workspace refuses")
    assert "does not list `funding`" in refusals
    backlog = next(line for line in page("backlog.md").splitlines() if line.startswith("| 107 |"))
    assert "~~A perpetual's funding is delivered and not booked~~ **booking closed**" in backlog
    assert "the OKX package's `okx_funding` loader serves the exchange's settled rates" in backlog


def test_the_pages_say_a_certification_groups_a_wrapped_custom_point_by_its_payload() -> None:
    """A custom point read back from the catalog travels inside `CustomData`; the pages
    say every in-process reader groups it by the type inside, and the backlog row that
    recorded a certification dying on the wrapper is closed in place."""
    extensions = prose(page("extensions.md"))
    assert "travels inside the engine's `CustomData` wrapper" in extensions
    assert "a certification's evidence gates among it" in extensions
    backlog = next(line for line in page("backlog.md").splitlines() if line.startswith("| 111 |"))
    assert "~~A certification of a hypothesis requiring a custom type died" in backlog
    assert "**closed.**" in backlog
    assert "`kanso.data.types.type_id_of` answers for the point a wrapper carries" in backlog


def test_the_pages_say_the_venue_is_settled_at_a_marker_as_the_research_path_settles() -> None:
    """A command that came due by a held point lands before the marker hands that point to
    the author on both code paths; the concepts page says where, the workspace page says it
    under `costs.latency_ms`, and the backlog row that recorded the parity failure of a
    level-two book under a latency is closed in place."""
    concepts = prose(section(page("concepts.md"), "Delivery"))
    assert "The simulated venue is settled where the research path settles it." in concepts
    assert "now settle at each marker, above the sleeve" in concepts
    assert "A change to the book also stamps `data_time`" in concepts
    workspace = prose(page("workspace.md"))
    assert "the command lands before the sleeve's handler for it on both code paths" in workspace
    backlog = next(line for line in page("backlog.md").splitlines() if line.startswith("| 118 |"))
    assert "~~`parity_replay` failed on a level-two book under a latency" in backlog
    assert "**closed.**" in backlog
    assert "`SimulatedVenue.on_marker` settles the exchange at each marker's instant" in backlog
    assert "`handle_order_book_deltas` stamps `data_time`" in backlog


def test_the_pages_say_kanso_checks_each_name_s_book_itself_of_every_point() -> None:
    """The engine's check that a level-two venue holds each name's book counts one point of
    each batch it is handed, and refused a quiet name with its book on every day; the
    workspace page says kanso makes that check itself, the concepts page names the window
    of two names every chunking is held to, and the engine fact is one `doctor` re-checks."""
    from kanso.nautilus import facts

    workspace = prose(page("workspace.md"))
    assert (
        "neither is a name whose changes all follow another name's in what the engine is handed"
        in workspace
    )
    assert "so kanso makes the check itself, of every point, and the engine's is off" in workspace
    concepts = prose(section(page("concepts.md"), "Card"))
    assert "and of two whose quieter one's book changes always follow the other's" in concepts
    assert any(
        "counts only the first point of each validated add_data call" in claim
        for claim, _ in facts.claims()
    )


def test_the_pages_say_a_day_s_volume_is_struck_with_the_instrument_s_multiplier() -> None:
    """A bar counts the instrument's unit and a fill is `qty x px x multiplier`; the pages
    say the capacity gate reads a day's volume in the same unit, and the backlog row that
    recorded a perpetual's contracts compared against its fills' notional is closed."""
    concepts = prose(page("concepts.md"))
    assert "The volume a certification holds a day's fills to is the same product." in concepts
    assert (
        "`capacity_vs_adv` reads each day's volume as `volume x close x multiplier` of the "
        "resolved definition the window was run with"
    ) in concepts
    assert "contracts against contracts, never contracts against the coins inside them" in concepts
    cert_run = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso cert run")
    )
    assert "each day's volume struck as `volume x close x multiplier`" in cert_run
    backlog = next(line for line in page("backlog.md").splitlines() if line.startswith("| 113 |"))
    assert backlog.split("|")[3].strip().startswith("~~`capacity_vs_adv` compared")
    assert "**closed**: the certifier strikes each day's volume with the multiplier" in backlog
    assert "`kanso cert plan ID --replan` mints the next plan version" in backlog


def test_the_pages_state_the_venue_model_s_precedence_and_its_five_origins() -> None:
    """`[research]` is a layer between the defaults and the broker, a restated default is
    not one, and the code must be one the engine registers."""
    research = prose(page("workspace.md"))
    assert "then these two `[research]` keys, then the broker's declaration" in research
    assert "a broker's declared currency still wins over `[research] currency`" in research
    assert "is not a layer and leaves the origin at `default`" in research
    assert "The code must be one the engine registers" in research
    origins = "`default`, `config`, `broker`, `venue_override` or `hypothesis`"
    assert origins in research
    assert "`config` (`[research]` in `kanso.toml`)" in prose(page("concepts.md"))
    refusals = section(page("workspace.md"), "What the workspace refuses")
    assert "an account currency the engine does not register" in refusals
    validate = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso hyp validate")
    )
    assert "an account currency the engine registers" in validate


def test_the_workspace_page_states_the_fixed_spread_a_bar_only_hypothesis_needs() -> None:
    text = page("workspace.md")
    assert "`costs.fixed_bps`" in text
    assert "`fixed_bps`" in section(text, "What the workspace refuses")


def test_the_workspace_page_says_an_exit_counts_the_exits_still_working() -> None:
    """`submit_exit` sizes against what the working exits leave, a cancel in flight among
    them under a latency (`tests/nautilus/backtest/test_exit_flat.py`)."""
    text = prose(page("workspace.md"))
    assert "An exit never goes past flat, counting the exits still working." in text
    assert "Under a stated latency a cancel is not instant" in text
    assert "What the cancel in flight held back is owed, not dropped" in text
    assert "With no latency stated, an exit at market is never held back by a resting one" in text
    assert "A cancel on that side takes back only an owed exit that has a price" in text
    assert "An order the engine's order emulator holds" in text
    assert "`cancel_orders` cancels it on its own, through the emulator" in text


def test_the_research_template_says_an_exit_can_return_none_while_exits_are_working() -> None:
    """The loop model writes `strategy.py` from `program.md`, so it is told what the docs say:
    `submit_exit` returns `None` while working exits cover it, and a cancel is not instant."""
    text = prose((ROOT / "src" / "kanso" / "templates" / "program.md").read_text())
    assert "An exit never goes past flat, counting the exits still working" in text
    assert "Under a latency a cancel is not instant" in text


def test_the_workspace_page_says_a_stage_speed_paces_nothing_in_this_version() -> None:
    assert "it paces nothing" in prose(section(page("workspace.md"), "`portfolio.yaml`"))


def test_the_wait_the_workspace_page_states_is_the_one_the_client_waits() -> None:
    """The page tells an operator what their own shim's timeout has to stay under."""
    models = prose(section(page("workspace.md"), "`models.yaml`"))
    stated = re.search(r"waits \*\*(\w+) minutes\*\*", models)
    assert stated is not None
    assert spelled(stated.group(1)) * 60 == REQUEST_TIMEOUT_S


def test_the_pages_say_a_run_asks_no_vendor_about_what_the_store_holds() -> None:
    """Five lanes beginning together each asked the reference for every instrument, and the
    exchange throttled four of them out of their runs; the pages say a run reads the store."""
    instruments = prose(section(page("workspace.md"), "`instruments.yaml`"))
    assert "A run asks the reference adapter nothing about an instrument the store holds." in (
        instruments
    )
    assert "`hyp validate` and `hyp add` build the definition in memory to check it" in (
        instruments
    )
    snapshot = prose(section(page("concepts.md"), "Snapshot"))
    assert "a run asks no reference adapter about an instrument its snapshot pins" in snapshot


def test_the_throttle_wait_the_adapters_page_states_is_the_one_the_client_waits() -> None:
    """The reference and the public history wait out a throttle through one client."""
    waits = [f"{PAUSE_S * attempt:g}" for attempt in range(1, RETRIES)]
    stated = f"is asked again after {', '.join(waits[:-1])} and {waits[-1]} seconds"
    okx = prose(section(page("adapters.md"), "The OKX adapter"))
    assert okx.count(stated) == 2
    fifth = re.search(r"does not lift by the (\w+) attempt", okx)
    assert fifth is not None
    assert _ORDINALS.index(fifth.group(1)) + 1 == RETRIES


_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")


def test_the_lane_memory_floor_the_workspace_page_states_is_the_one_the_package_clamps_to() -> None:
    """The page tells an operator what a small `[env] mem_per_lane_gb` is read as."""
    envelope = prose(section(page("workspace.md"), "`envelope.yaml`"))
    stated = re.search(r"figure under ([\d.]+) GB", envelope)
    assert stated is not None
    assert float(stated.group(1)) == MIN_DECLARED_MEM_PER_LANE_GB


def test_the_who_writes_what_table_lists_the_mock_script() -> None:
    table = section(page("workspace.md"), "Who writes what")
    assert "| `mock/responses.yaml` | `init --demo` |" in table


def test_the_concepts_page_states_both_sides_of_the_paper_gate() -> None:
    promotion = prose(section(page("concepts.md"), "Promotion and demotion"))
    assert "a shorter window is a `fail`, not a skip" in promotion
    assert "above the band as much a fail as below it" in promotion


YAML_11_BOOLEANS = ("y", "n", "yes", "no", "true", "false", "on", "off")
"""The words the YAML 1.1 type repository lists as booleans; which of them PyYAML reads as
one is what the test below measures."""


def test_the_catalog_section_names_the_words_pyyaml_reads_as_booleans() -> None:
    """The page tells an operator which tickers to quote; the loader decides, not the spec."""
    catalog = prose(section(page("workspace.md"), "`catalog/`"))
    start = catalog.index("PyYAML reads a bare")
    named = set(re.findall(r"`([A-Z]+)`", catalog[start : catalog.index("as a boolean", start)]))
    read_as_boolean = {
        word.upper()
        for word in YAML_11_BOOLEANS
        if all(
            isinstance(yaml.safe_load(case(word)), bool)
            for case in (str.lower, str.title, str.upper)
        )
    }
    assert named == read_as_boolean
    assert "`Y` and `N` are read as strings and need no quotes." in catalog
    assert [yaml.safe_load(word) for word in ("Y", "N")] == ["Y", "N"]


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


def resolves(dotted: str) -> bool:
    """Whether a dotted path names an attribute of its parent module or a module of its own.

    A submodule is an attribute of its package only once something has imported it, so the
    attribute alone would make the answer depend on which tests ran first. A name that is
    neither is refused; a module that exists and fails to import raises rather than reading
    as absent.
    """
    module, _, attribute = dotted.rpartition(".")
    if hasattr(importlib.import_module(module), attribute):
        return True
    try:
        importlib.import_module(dotted)
    except ModuleNotFoundError as error:
        if error.name != dotted:
            raise
        return False
    return True


def test_every_dotted_kanso_path_the_docs_name_resolves() -> None:
    found = set()
    for path in [*DOCS.glob("*.md"), ROOT / "README.md"]:
        found.update(re.findall(r"`(kanso\.[a-z_]+(?:\.[A-Za-z_]+)+)`", path.read_text()))
    assert found
    for dotted in sorted(found):
        assert resolves(dotted), dotted


def test_a_submodule_resolves_before_anything_imported_it_and_nothing_else_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured 2026-10-02: `kanso.certify.child` failed under `pytest tests/docs` alone and
    passed in the suite, because only the suite had imported it first."""
    package = tmp_path / "kanso_docs_probe"
    package.mkdir()
    (package / "__init__.py").write_text("NAMED = 1\n")
    (package / "child.py").write_text("")
    monkeypatch.syspath_prepend(tmp_path)
    try:
        assert not hasattr(importlib.import_module("kanso_docs_probe"), "child")
        assert resolves("kanso_docs_probe.child")
        assert resolves("kanso_docs_probe.NAMED")
        assert not resolves("kanso_docs_probe.absent")
        assert not resolves("kanso_docs_probe.child.Absent")
    finally:
        for name in ("kanso_docs_probe.child", "kanso_docs_probe"):
            sys.modules.pop(name, None)


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


def test_the_card_gate_count_the_concepts_page_states_is_the_count_its_table_holds() -> None:
    """The page once said five above a six-row table; a count it states must hold."""
    body = section(page("concepts.md"), "What a card must satisfy")
    stated = re.search(r"There are (\w+)\.", body)
    assert stated is not None
    rows = [line for line in body.splitlines() if line.startswith("| `")]
    assert spelled(stated.group(1)) == len(rows)


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


def test_every_backlog_row_has_its_own_number_and_follows_the_one_before() -> None:
    """Rows are cited by number — from other rows, from these tests, from pull requests — so
    two branches that each opened the next row must not both land it: a reader handed a
    number would find two rows, and a test asking for the first would read the wrong one."""
    numbers = [
        int(line.split("|")[1])
        for line in page("backlog.md").splitlines()
        if re.match(r"\| \d+ \|", line)
    ]
    assert len(numbers) == len(set(numbers))
    assert numbers == sorted(numbers)


def test_the_repair_budget_the_cli_page_states_is_the_one_the_driver_enforces() -> None:
    """A bound written in words on a page and held as a constant in a module: the two
    drifted apart once, when the driver spent it on the run's crash streak while the page
    promised it per idea."""
    stated = re.search(r"(\w+) repairs per idea", prose(page("cli.md")))
    assert stated is not None
    assert spelled(stated.group(1).lower()) == driver.REPAIRS


def test_both_pages_state_the_utc_period_rule_a_continuous_calendar_relies_on() -> None:
    """A round-the-clock series is annualised, warmed and scoped on the same UTC clock as
    an equity one; the pages say where its bars land and what the synthetic loader
    refuses."""
    card = prose(section(page("concepts.md"), "Card"))
    assert "Return periods are cut on the UTC clock" in card
    assert "closes at 00:00Z and lands in the following UTC period" in card
    assert "daily bar a vendor stamps at 05:00Z" in card
    assert "trading days are calendar days" in card
    assert "about 365 periods a year" in card
    assert "the calendar days that printed" in card
    assert "`[00:00Z, first market point)`" in card
    catalog = prose(section(page("workspace.md"), "`catalog/`"))
    assert "`calendar: continuous`" in catalog
    assert "`timezone`, `session_start` or `session_end` is refused (exit 3)" in catalog
    assert "`calendar: weekdays` is the default and is recorded in no manifest" in catalog


def test_the_pages_define_the_plateau_around_an_edge_and_the_bootstrap_on_both_its_numbers() -> (
    None
):
    """A fraction of a loss lies above the loss, so the pages say the plateau fails a
    non-positive objective before moving anything, that a failed window spares its
    backtests, and that the bootstrap judges the band it records; the three backlog rows
    that recorded a certificate right by accident are closed in place."""
    concepts = prose(section(page("concepts.md"), "Certification, the plan and the certificate"))
    assert (
        "An unperturbed objective at or below zero therefore fails the gate before any "
        "parameter is moved" in concepts
    )
    assert "The plateau is judged after every other cert gate" in concepts
    assert "A window gate that judged nothing spares nothing." in concepts
    assert "`bootstrap` judges both of the numbers it records" in concepts
    assert "a band that lies at or below zero says the population of trades carries no edge" in (
        concepts
    )
    cli = prose(page("cli.md"))
    assert "once `embargoed_window` has failed it is recorded as skipped with the reason" in cli
    rows = {
        114: (
            "~~`param_plateau` set its floor at a fraction of the unperturbed objective",
            "fails an unperturbed objective at or below zero before any parameter is moved",
        ),
        115: (
            "~~A certification ran the plateau's perturbation backtests after",
            "judges `param_plateau` after every other cert gate",
        ),
        116: (
            "~~`bootstrap` passed on the drawdown alone",
            "a ninety-percent band that lies at or below zero fails the gate whatever the drawdown",
        ),
    }
    for number, (claim, closure) in rows.items():
        row = next(
            line for line in page("backlog.md").splitlines() if line.startswith(f"| {number} |")
        )
        assert claim in row and "**closed.**" in row and closure in row, number


def test_the_pages_say_a_stall_certifies_in_a_child_held_to_the_lane_s_share() -> None:
    """A lane that certified in its own process kept what the windows cost and stopped
    answering `SIGTERM`; the pages say where a stall's certification is made, what bounds it,
    what records its cost, and that a lane stops whatever it ran, and both backlog rows that
    recorded the two defects are closed in place."""
    lanes = prose(section(page("concepts.md"), "Run, lane and the envelope"))
    assert "A stall's certification is a child held to the same figure" in lanes
    assert "(`cert_peak_mem_gb`, `cert_wall_s`)" in lanes
    assert "or `kanso cert run` by hand" in lanes
    assert "**A lane stops at its next safe point, whatever it ran.**" in lanes
    envelope = prose(section(page("workspace.md"), "`envelope.yaml`"))
    assert "A stall's certification is held to the same figure." in envelope
    cli = prose(page("cli.md"))
    assert "The certification is made in a child of the lane, exactly as `cert run` makes it" in cli
    stop = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso research stop`")
    )
    assert "a stall's certification in flight is killed the same way" in stop
    for number, opening in (
        ("119", "~~A stall's certification ran in the lane's own process"),
        ("120", "~~A lane that had replayed a parity on a node no longer answered `SIGTERM`~~"),
    ):
        row = next(
            line for line in page("backlog.md").splitlines() if line.startswith(f"| {number} |")
        )
        assert row.split("|")[3].strip().startswith(opening)
        assert "**closed.**" in row


def test_the_pages_say_a_lane_runs_its_benchmark_hold_in_a_child() -> None:
    """The hold a benchmark objective differences against was the one whole-window run a lane
    still made in its own process; the pages say a child runs it as a card is run, and the
    backlog closes the row that recorded it and the open half of the row before it."""
    lanes = prose(section(page("concepts.md"), "Run, lane and the envelope"))
    assert "it makes no run in its own process" in lanes
    assert "the hold a benchmark objective differences against" in lanes
    assert "one run it still makes in its own process" not in lanes
    assert "it is the same run, element by element, as the lane made of it" in lanes
    hold = prose(section(page("workspace.md"), "`hypotheses/<id>/`"))
    assert "each card of a run (once per run, in a child of the lane as a card is" in hold
    rows = {
        line.split("|")[1].strip(): line
        for line in page("backlog.md").splitlines()
        if re.match(r"\| 1(19|21) \|", line)
    }
    item = rows["121"].split("|")[3].strip()
    assert item.startswith("~~The hold a benchmark objective differences against was run in")
    assert "in the lane's own process~~ **closed.**" in item
    assert "a child runs it now (row 121)" in rows["119"].split("|")[5]


def test_the_pages_say_the_monitor_demotes_in_a_child() -> None:
    """A monitor pass that demoted ran its stages' nodes in a process that runs all day; the
    pages say a child makes it, that a stop leaves it to finish, and the backlog closes the
    row that recorded it and the open half of the row before it."""
    stages = prose(section(page("concepts.md"), "Stages"))
    assert "**except a demotion the monitor makes**, which runs in a child of the monitor" in stages
    moves = prose(section(page("concepts.md"), "Promotion and demotion"))
    assert "**The monitor demotes in a child of its own process.**" in moves
    assert "leaves it to finish" in moves
    monitoring = prose(section(page("cli.md"), "Monitoring"))
    assert (
        "A pass demotes in a child of its own process, exactly as `kanso demote` does" in monitoring
    )
    stop = next(
        line for line in page("cli.md").splitlines() if line.startswith("| `kanso research stop`")
    )
    assert "A demotion the monitor is making is not: it is left to finish" in stop
    rows = {
        line.split("|")[1].strip(): line
        for line in page("backlog.md").splitlines()
        if re.match(r"\| 12[12] \|", line)
    }
    item = rows["122"].split("|")[3].strip()
    assert item.startswith("~~A monitor pass that demoted a version ran its stages' nodes in")
    assert "the monitor's own process~~ **closed.**" in item
    assert "a child makes the monitor's demotion now (row 122)" in rows["121"].split("|")[5]


def test_the_screen_ranges_the_docs_state_are_the_library_s() -> None:
    """`docs/workspace.md` tables every measure parameter's range; the library is the source,
    so a range changed in one and not the other fails here."""
    from kanso.screen.library import catalogue as measures

    text = (DOCS / "workspace.md").read_text(encoding="utf-8")
    stated = {
        (measure, name): (low, high)
        for measure, name, low, high in re.findall(
            r"^\| `(lead_lag|response)` \| `(\w+)`[^|]*\| `([^`]+)` \| `([^`]+)` \|$", text, re.M
        )
    }
    declared = {
        (item.id, name): tuple(str(end) if isinstance(end, str) else f"{end:g}" for end in span)
        for item in measures().values()
        for name, span in item.ranges.items()
    }
    assert stated == declared
