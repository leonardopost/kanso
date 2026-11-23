"""The skills instruct an agent to do what the package accepts, and nothing it refuses."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import get_args

import yaml

from kanso.config import EnvConfig, ResearchConfig
from kanso.criteria import catalogue, check_params
from kanso.nautilus.clock import NEW_YORK, session_of
from kanso.schemas import CardStatus
from tests.criteria.builders import make_hyp
from tests.docs.test_pages import page, prose, scope_names, section

ROOT = Path(__file__).resolve().parents[2]
PACKAGED = ROOT / "src" / "kanso" / "skills"
MAINTAINER = ROOT / "skills"


def skill(root: Path, name: str) -> str:
    return (root / name / "SKILL.md").read_text(encoding="utf-8")


def test_the_env_skill_names_every_override_the_configuration_declares() -> None:
    """A key the skill omits is one an operator in exactly its situation — a host
    planning a single lane — cannot learn exists."""
    text = skill(PACKAGED, "kanso-env")
    overrides = next(line for line in text.splitlines() if "`kanso.toml [env]`" in line)
    for name in EnvConfig.model_fields:
        assert f"`{name}`" in overrides, name
    assert "**replaces** the derived memory per lane" in text


def test_the_hypothesis_skill_names_the_spread_a_bar_only_hypothesis_needs() -> None:
    text = skill(PACKAGED, "kanso-hypothesis")
    assert "`costs`, `risk_limits`: keep defaults" not in text
    assert "set `costs: {spread: fixed_bps, fixed_bps: <width>}`" in text


def test_the_hypothesis_skill_writes_a_session_the_gate_accepts() -> None:
    """The example is copied into hypotheses as written, so it must validate as written."""
    text = skill(PACKAGED, "kanso-hypothesis")
    found = re.search(r"`required_constraints: \[\{id: trading_hours, params: (\{.*?\})\}\]`", text)
    assert found is not None
    params = yaml.safe_load(found.group(1))
    assert check_params(catalogue()["trading_hours"], params, make_hyp(), 4) == []


def test_the_hypothesis_skill_states_a_grid_in_the_keys_the_schema_reads() -> None:
    """The block is copied into hypotheses as written, so its keys are the schema's, and the
    count is the operator's, never the agent's."""
    from kanso.schemas import PriorTrials

    text = skill(PACKAGED, "kanso-hypothesis")
    found = re.search(r"`prior_trials: \{(.*?)\}`", text)
    assert found is not None
    keys = [part.split(":")[0].strip() for part in found.group(1).split(",")]
    assert keys == list(PriorTrials.model_fields)
    assert "take N from the operator, never from your own reading of the file" in text


def test_the_hypothesis_skill_says_a_draft_states_its_parent_s_mechanism() -> None:
    """The agent an `explored` entry lands in front of reads here that the mechanism is the
    parent's unless the workspace widens it, and that the rule binds the field, not the code."""
    text = skill(PACKAGED, "kanso-hypothesis")
    assert (
        "It states its parent's `mechanism` unless `[research] explore_mechanisms` widens it;"
        " the rule reads the field and not the code"
    ) in text


def test_the_hypothesis_skill_reads_a_draft_s_seed_beside_the_parent_s_best_and_its_trials() -> (
    None
):
    """The agent recommending `hyp add` is told the score is the seed's as the parent's card,
    and to read it beside the two numbers that make it mean something — named as the summary
    `kanso inbox` shows spells them, since no command shows the rest of the event they are also
    on. `T` is what the draft is priced from, traded cards and declared grids, which the skill
    once said of every card of the family; and the rationale the summary cuts is in the inbox."""
    from tests.docs.test_pages import SUMMARY

    text = skill(PACKAGED, "kanso-hypothesis")
    assert f"`{SUMMARY}`" in text
    assert "Its seed was measured once as one more card of the parent" in text
    assert "`B` is `parent_best`" in text and "`T` is `inherited_trials`" in text
    assert "which every result it reaches is priced against" not in text
    assert (
        "the cards of the families its `derived_from` names that ran and traded, plus the "
        "settings their `prior_trials` grids declared"
    ) in text
    assert "`kanso inbox` prints the whole rationale beneath the entry" in text
    assert "is the operator's to write with `kanso hyp new`" in text


def test_the_certify_skill_counts_a_declared_grid_where_kanso_does() -> None:
    """A plan carries its correction, a pinned one is refused without it and a search is
    priced over its draws, a declared grid's settings among them: a skill counting cards alone
    tells an agent a plan one card behind a grid needs no correction."""
    text = skill(PACKAGED, "kanso-certify")
    assert "and the `prior_trials` its family declares tried outside kanso" in text
    assert (
        "from the second draw: by `cert plan` and `cert run` from the second card or the first "
        "behind a declared grid, and by `cert plan` already with no card behind a grid of two "
        "settings or more, where `cert run` has no card to certify"
    ) in text
    assert "or none yet behind a grid" not in text
    assert "or the first card behind a declared grid" in text
    assert "can exceed the certificate's `n_trials`, which counts cards alone" in text
    assert "every card it has, reaches two" not in text


def test_the_classify_skill_deflates_a_re_pin_over_a_declared_grid_too() -> None:
    """After a re-pin to another objective certification deflates over the cards behind it and
    the settings of its family's declared grid, as `kanso.certify.run` counts them."""
    text = skill(PACKAGED, "kanso-classify")
    assert (
        "deflates by every card behind it and every setting of a grid its family declares in "
        "`prior_trials`"
    ) in text


def test_the_promote_skill_names_the_one_cost_a_broker_does_not_supply() -> None:
    text = skill(PACKAGED, "kanso-promote")
    assert "must be `fixed_bps` on the hypothesis or under `venues.<MIC>.costs`" in text


def test_the_release_skill_does_not_ask_for_a_schema_version_bump() -> None:
    text = skill(MAINTAINER, "kanso-release")
    assert "confirm `schema_version` was bumped" not in text
    assert "nothing is bumped by hand" in text


def test_the_release_skill_names_a_migration_fixture_that_exists() -> None:
    """Step 1 once sent the maintainer to a fixture the suite did not have."""
    text = skill(MAINTAINER, "kanso-release")
    assert "on a workspace created by the previous release" not in text
    named = re.findall(r"`(tests/state/fixtures/[a-z0-9_]+\.sql)`", text)
    assert named
    for path in named:
        assert (ROOT / path).is_file(), path


def test_the_align_skill_says_the_run_s_base_is_never_judged() -> None:
    """This file ships into every workspace, and it is what an agent reads to explain a
    `misaligned` entry. Told only that the model is asked once the syntax tree passes, an
    agent would take a check of a run still on its base for a verdict on it."""
    text = skill(PACKAGED, "kanso-align")
    assert "The run's base is never judged" in text


def test_the_align_skill_names_the_rewinds_of_earlier_runs() -> None:
    """An agent explaining a drift verdict that cites a direction this run never took needs
    to know the judge reads the hypothesis's earlier runs, and that a base is not re-judged
    by them, or it would report the verdict as a fault or the standing best as an oversight."""
    text = skill(PACKAGED, "kanso-align")
    assert "the reasons the hypothesis was rewound for in its earlier runs" in text
    assert "(`same_program`, `same_thesis`)" in text
    assert "Earlier rewinds bear on new proposals only" in text
    assert "The proposer is shown the earlier runs' directions too" in text


def test_the_align_skill_says_every_keep_is_judged_and_which_lane_it_checks() -> None:
    """An agent asked to check a run before it is ended reads here when the driver checks and
    which run this command can check: told only of the clock, it would take a keep for one no
    check had judged, and told nothing of the lane, it would offer a command that refuses."""
    text = skill(PACKAGED, "kanso-align")
    assert "The driver runs this check on every keep as it is made, and every `align_every`" in (
        text
    )
    assert "The loop runs this check every `align_every` cards" not in text
    assert "refuses a run a daemon lane owns: that lane's next turn judges it" in text


def test_the_research_skill_says_every_keep_is_judged_and_the_proposer_reads_earlier_rewinds() -> (
    None
):
    """An agent explaining why a driven run steers clear of a direction, or asked why a lane
    keeps walking into one, reads this file: drift is judged on every keep, an end calls no
    model and how a keep it would carry is judged in either kind of lane, and both lists the
    proposer is shown, the earlier runs' among them."""
    text = skill(PACKAGED, "kanso-research")
    rules = text.split("## Rules", 1)[1]
    assert "drift is judged on every keep as it is made" in rules
    assert "and every `align_every` cards besides" in rules
    assert "by `kanso align check <id>` in lane `op`, or by the lane's next turn in a daemon" in (
        rules
    )
    assert (
        "A `program.md` written from the template since this release makes a filter, gate, "
        "entry condition or exit the thesis does not state drift unless the program names it; "
        "an older one permits what it permits."
    ) in rules
    assert "`rewound_for`, the newest reasons this run was rewound for" in rules
    assert (
        "`rewound_in_earlier_runs`, one line per direction the hypothesis's earlier runs were "
        "rewound for, the newest ten"
    ) in rules


def test_no_page_or_skill_says_an_unstated_filter_is_drift_whatever_the_program_says() -> None:
    """An unstated filter, gate, entry condition or exit is drift only where the run's
    `program.md` says so: the judge's own instruction says a thesis that does not mention a
    filter does not forbid one, and a program written from the earlier template permits them.
    So every sentence that calls such a change drift names the program or the template."""
    texts = {
        "concepts": prose(section(page("concepts.md"), "Alignment")),
        "kanso-research": prose(skill(PACKAGED, "kanso-research")),
        "kanso-align": prose(skill(PACKAGED, "kanso-align")),
    }
    for name, text in texts.items():
        for sentence in re.split(r"(?<=[.;])\s", text):
            if "does not state" in sentence and ("drift" in sentence or "rewinds" in sentence):
                assert "program" in sentence or "template" in sentence, (name, sentence)
    assert "a `program.md` written from the template since this release says that fair game" in (
        texts["concepts"].lower()
    )
    assert (
        "one written from an earlier template, which called filters and entry and exit"
        in (texts["concepts"])
    )


def test_the_certify_skill_says_a_stall_its_correction_refuses_is_spared() -> None:
    """An agent asked why a stall certified nothing reads this file; told nothing of the
    precheck, it would report a missing certificate as a fault, or certify by hand a best the
    search correction had already refused on research data."""
    text = skill(PACKAGED, "kanso-certify")
    assert "pass on research data. One they refuse is **spared**" in text
    for gate in ("walk_forward_consistency", "overfit_probability", "research_plateau", "`gates`"):
        assert gate in text, gate
    assert "It prices the selection, not the edge" in text
    assert "never judged again" in text
    assert "`hypotheses[].last_stall.precheck`" in text
    assert "`kanso cert run` by hand is never spared" in text
    # `measured` is whether the research window was run for the precheck; the child runs it
    # at every stall it is started for but one on a snapshot that cannot support a claim, so
    # a certified stall says `true` whatever its card was measured under, but for that one.
    assert "because the card was measured under another" not in text
    assert "`measured` — whether the research window was run for it, not why" in text
    # A lane that could not judge starts the child, whose precheck replaces the lane's, so the
    # lane's reason never reaches `last_stall` or `stall`: the one lane precheck recorded is a
    # spare, and a `measured: false` that carries a reason is the snapshot's.
    assert "with a `reason` where it could not" not in text
    assert "its precheck is recorded only where it spared the stall" in text
    assert "its reason is not recorded: the certification child is started" in text
    assert "so a stall on one is never spared" in text
    assert "so every stall that certified says `true` — but one on a snapshot that" in text
    assert "`null` beside a spared stall, whatever an earlier plan or `hypothesis.yaml`" in text
    assert "`dsr` against `min_dsr` under `deflated_sharpe`" in text


def test_the_certify_skill_says_every_certificate_is_a_look_and_that_it_moves_no_bar() -> None:
    """An agent reporting a certificate reads this file; told nothing of the looks, it would
    report a pass at a later look as a first look's, or re-plan to make a later look pass.
    Told a re-pin never resets the count, it would report a count a re-pin moved as standing;
    told the lineage's bound is `L` times the threshold being set, it would understate it
    wherever an earlier look was held lower, and told each look was held to the threshold in
    its correction's `params`, it would understate it at every look whose correction skipped,
    which passes with those `params` in place."""
    text = skill(PACKAGED, "kanso-certify")
    assert "Every certificate is a **look** at its certification window" in text
    assert "The count judges nothing" in text
    assert "held to the threshold the plan pins at every look" not in text
    assert "does so by the threshold the plan pins at every look" in text
    assert (
        "up to the sum of each look's complement of the threshold its correction judged it by, "
        "one for a look whose correction judged nothing, under a plan that carried none or "
        "because it skipped — `L × (1 − threshold)` only where all `L` judged by one threshold"
    ) in text
    assert "read the gate's `skipped` before its threshold" in text
    assert "up to `L × (1 − threshold)`, not the threshold" not in text
    assert "`hypotheses[].holdout_looks`" in text and "the `hold-out` line" in text
    assert "A replan or a re-pin does not reset the count" not in text
    assert (
        "A replan does not reset the count, nor does a re-pin that keeps `derived_from` and a "
        "certification window sharing a day with the last; a re-pin onto a window sharing no "
        "day with it starts a new count once the subject is read there, and one that drops a "
        "`derived_from` link can split the lineage"
    ) in text
    assert (
        "a best carried across a re-pin that moved only the window is certified on — and "
        "counted at — the old window until a research run under the new file cards it again; "
        "`holdout_looks` counts where the best's next certificate reads"
    ) in text
    # A plan `cert run --sha` makes, none being pinned, is shown the looks at the named card's
    # window (`kanso.certify.plan.plan`), which a re-pin that moved only the window leaves
    # apart from the best's: told `inputs.looks` always counts where the best's next
    # certificate reads, an agent would report that plan's count as the best's prior looks.
    assert "`holdout_looks` and `inputs.looks` count where the best's next certificate" not in text
    assert (
        "`inputs.looks` where the next certificate of the card its plan was made for read — "
        "the best's, or the named card's where `cert run --sha` made the plan"
    ) in text
    assert "never re-plan to make a later look pass" in text
    assert "(`inputs.looks`)" in text


def test_the_research_skill_says_a_stall_s_certification_can_be_spared() -> None:
    """An agent driving the loop reads what a stall leads to here; told only that a keep above
    zero is certified, it would report a spared stall's missing certificate as a fault."""
    text = skill(PACKAGED, "kanso-research")
    assert (
        "unless a research gate of the plan — its search correction, `walk_forward_consistency` "
        "or `research_plateau` — refuses it on research data, which **spares**"
    ) in text


def test_the_hypothesis_skill_states_every_fold_positive_as_the_operator_s_value() -> None:
    """An agent asked for an edge that recurs writes the card gate from here; told nothing of
    the count, it would leave the gate bare, which `hyp validate` refuses, or pick a count the
    operator never stated."""
    text = skill(PACKAGED, "kanso-hypothesis")
    assert "`required_constraints: [{id: positive_folds, params: {min_positive_folds: N}}]`" in text
    assert "N is the operator's, never yours" in text
    classify = skill(PACKAGED, "kanso-classify")
    assert "`positive_folds` holds a card to a positive objective" in classify
    assert "never carried by default" in classify


def test_the_data_skill_says_where_a_split_s_ex_date_is_dated() -> None:
    """An agent declaring a US split without a zone gets a UTC day, which lands the split
    inside a winter post-market; the skill is where it learns the key exists."""
    text = skill(PACKAGED, "kanso-data")
    assert "`override.info.timezone`" in text
    assert "first session that traded at the new price" in text


def test_the_data_skill_says_an_unmapped_side_is_no_aggressor() -> None:
    """The loader called every print of a file without the column a buyer's, and an agent
    writing a spec had nowhere to learn that the column decides which resting orders fill."""
    text = skill(PACKAGED, "kanso-data")
    assert "left unmapped, every print is loaded with no aggressor, never a guessed one" in text


def test_the_research_skill_names_every_status_a_card_can_carry() -> None:
    """This file ships into every workspace on `kanso init` and `kanso skills sync`, so a
    status it omits is one an operator meets in `results.tsv` with nothing to read. It
    described `redundant` as a refusal with no card and no trial for a release after that
    stopped being true, which is the worse failure: a page that is confidently wrong."""
    text = skill(PACKAGED, "kanso-research")
    for status in get_args(CardStatus):
        assert f"`{status}`" in text or f"**{status}**" in text, status
    assert "no card, no trial" not in text
    assert "so it is a trial, a `results.tsv` row and a coverage entry like any other" in text


def test_the_research_skill_names_the_minute_a_bar_s_session_reads_as_the_clock_reads_it() -> None:
    """The skill ships into every workspace beside `program.md` and states the same minutes;
    they are the clock's in winter and in summer, so a change to the clock fails here too."""
    line = next(
        line for line in skill(PACKAGED, "kanso-research").splitlines() if "`self.session`" in line
    )
    stated = (
        ((9, 30), "the bar stamped 09:30 is `pre`, minute 569", ("pre", 569)),
        ((9, 31), "the first regular bar is the one stamped 09:31, minute 570", ("rth", 570)),
    )
    for day in (date(2024, 1, 2), date(2024, 7, 2)):
        for (hour, minute), said, (segment, of_day) in stated:
            assert said in line
            stamp = int(datetime.combine(day, time(hour, minute), NEW_YORK).timestamp()) * 10**9
            assert session_of(stamp) == (segment, day, of_day)
    assert "for the last minute the point covers" in line
    assert "a `KansoModifier`, which has none, from `session_of(ctx.ts_event)`" in line


def test_the_research_skill_names_every_field_a_re_pin_clears_the_best_on() -> None:
    """An agent re-pinning a hypothesis between runs reads here whether the edit costs the
    best; the list stopped at `book` while the objective, the costs and the depth cleared it
    too, as the types each instrument is asked for and the session scope now do."""
    text = skill(PACKAGED, "kanso-research")
    for name in scope_names():
        assert name in text, name


def test_the_research_skill_states_the_exploration_default_the_package_ships() -> None:
    """The agent that reads this skill is the one an `explored` draft lands in front of.

    The skill is a symlink into the installed package, so an agent in a workspace an older
    template wrote reads it too, beside a `kanso.toml` that `init` never rewrote: it is told
    to read the key there rather than to assume the template's number."""
    default = ResearchConfig().explore_after_stalls
    text = skill(PACKAGED, "kanso-research")
    assert (
        "`[research] explore_after_stalls` — read it in the workspace's `kanso.toml`, which"
        f" decides: the template writes {default}, 0 is never, and a workspace initialised on"
        " 0.8.0 to 0.13.x states 0"
    ) in text


def test_every_page_that_says_what_a_warmup_counts_dates_a_point_by_its_market() -> None:
    """Below a bar of a day a warmup dates each point on its market's own clock and counts the
    days a name printed on while its market was open, and at a bar of a day or longer every
    UTC day one is stamped on (`kanso.nautilus.backtest.warmup_prefix`). A page that said any
    UTC day that printed would tell an agent a Friday's post-market, stamped on Saturday,
    warms a strategy a session, and a stage restart on a weeknight's evening one more; one
    that said only open days would tell it a Friday's daily bar, stamped on Saturday, does
    not."""
    workspace = prose(page("workspace.md"))
    template = prose((ROOT / "src" / "kanso" / "templates" / "hypothesis.yaml").read_text())
    assert (
        "Below a day — quotes, prints and bars shorter than one — a point belongs to the day of "
        "its market that the instant before its stamp falls in"
    ) in workspace
    assert (
        "a session is a day of the instrument's market on which it printed while that calendar "
        "holds it open"
    ) in workspace
    assert (
        "is stamped on the next UTC date — a Saturday after a Friday, the holiday after the day "
        "before one — and adds no session to it, nor to a weekday whose own session has not "
        "begun"
    ) in workspace
    assert (
        "A bar of a day or longer is the only point of what it covers, so every UTC day that "
        "holds one is a session and neither zone nor calendar is read"
    ) in workspace
    dated = "while that market was open, each point dated on the market's own clock"
    daily = "a bar of 1d or longer counts on the day it is stamped"
    hypothesis_skill = skill(PACKAGED, "kanso-hypothesis")
    assert dated in hypothesis_skill
    assert daily in hypothesis_skill
    assert dated in template.replace("# ", "")
    assert daily in template.replace("# ", "")
    cli = prose(page("cli.md"))
    assert ("a session being a day of a universe name's market on which it printed " + dated) in cli
    assert "makes no weekend, holiday or next weekday a session" in cli
    assert "or at a bar of 1d or longer every UTC day one is stamped on" in cli
    assert (
        "there a point is dated by its market's own clock, New York's for a US equity, so an "
        "evening stamped past 00:00Z is its own day's, and a day a name's market was closed on "
        "is no warmup session for it"
    ) in prose(page("concepts.md"))


def test_the_data_skill_says_what_a_gap_and_an_answer_mean() -> None:
    """An agent reading `data show` for an operator meets `empty` inside `gaps`, and told
    nothing would either ask for those days again or call a lost trading day a holiday."""
    text = skill(PACKAGED, "kanso-data")
    step = next(line for line in text.splitlines() if "`kanso data show` →" in line)
    assert "A gap always holds a day the instrument's market opened" in step
    assert "never something to explain away as a holiday" in step
    assert "which a backfill will not ask for again and which no snapshot covers" in step


def test_the_data_skill_names_funding_as_realised_and_required_of_a_perpetual() -> None:
    """An agent loading a perpetual's history reaches for whatever rate a feed publishes; the
    skill is where it learns the predicted one is not funding, and that validation needs it."""
    text = skill(PACKAGED, "kanso-data")
    assert "| funding |" in text
    assert "the **realised** rate of the period that settled" in text
    assert "must list `funding` in `data_requirements`" in text
    assert "a spot leg beside it is asked for none" in text
    assert "the runner books each settlement on what the card held before it" in text
    assert "`types: [bar, funding]`" in text


def test_the_screen_skill_names_what_the_screen_reads_and_refuses() -> None:
    """The skill an agent writes a screen from names every state a series can be in, every
    verdict floor, the three declarations an adapter it builds must make, and the field
    the followers are spelt with — never `on`, which YAML reads as true."""
    from kanso.schemas.screen import ScreenVerdict

    text = skill(PACKAGED, "kanso-screen")
    for state in ("held", "fetchable", "unresolved", "unserved"):
        assert f"`{state}`" in text, state
    for floor in ScreenVerdict.model_fields:
        assert floor in text, floor
    for member in ("timestamps", "serves", "spec_for"):
        assert f"`{member}`" in text, member
    assert "`followers`" in text and "never `on`" in text
    assert "kanso screen draft" in text and "--certify" in text


def test_the_replay_skill_says_a_forward_replay_is_a_look_and_names_its_one_exemption() -> None:
    """An agent told any window may be replayed spends looks at the forward window it cannot
    take back; told the exemption, it checks parity on a stage's own days for free."""
    text = re.sub(r"\s+", " ", skill(PACKAGED, "kanso-replay"))
    assert "Any window may be replayed" not in text
    assert "so a bare replay is a counted look at the forward window" in text
    assert "is a counted look at its lineage's forward window" in text
    assert "the look can never be taken back" in text
    assert "A replay of a certification window reads the hold-out, and nothing counts it" in text
    assert "never replay either window to choose or tune anything research does" in text
    assert "`kanso replay parity --strategy <id>@<version>`" in text
    assert "less the first day of a window that resumed past the stage's clock" in text
    assert "less any window that halted" in text
    assert (
        "less the days past the last on which the catalog served every series the version reads "
        "when its window ran" in text
    )
    assert "The `forward` line says `no look` when it was" in text
    assert "report the look beside any number taken from forward days" in text


def test_the_research_skill_says_a_forward_replay_is_a_look() -> None:
    """Row 331's scenario is a research agent reading forward data back into a seed."""
    text = re.sub(r"\s+", " ", skill(PACKAGED, "kanso-research"))
    assert "Never read the forward window to decide a seed, a filter or a program edit" in text
    assert "is a counted look at the lineage's forward window" in text


def test_the_promote_skill_reports_a_promotable_version_with_its_forward_look() -> None:
    """A version is promotable at any look, so the agent reports which look it was, and offers
    the parity that is no look rather than the replay that is one."""
    text = re.sub(r"\s+", " ", skill(PACKAGED, "kanso-promote"))
    assert "Its evidence also carries `look`, `looks` and `lineage`, a skip's too" in text
    assert 'Report a `promotable` version with "look k of n" beside it' in text
    assert "offer `kanso replay run --strategy <id>@<v>` over the paper period" not in text
    assert "`kanso replay parity --strategy <id>@<v> --from … --to …`" in text
    assert "is a counted look at the forward window" in text


def test_the_certify_skill_names_the_forward_looks_and_that_they_move_no_bar() -> None:
    """The planner is shown the forward looks; the agent reading its plan is told what they
    count, that they judge nothing and that what they do to the bar is never the agent's."""
    text = re.sub(r"\s+", " ", skill(PACKAGED, "kanso-certify"))
    assert "at its forward window (`inputs.forward_looks`)" in text
    assert "`inputs.forward_looks` counts the looks at the hypothesis's **forward window**" in text
    assert "a pinned plan is not asked again as looks accrue" in text
    assert "is the planner's answer at a plan or a replan" in text
