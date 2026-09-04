"""Validation: one hypothesis per rule, each breaking exactly that rule.

Every failure is a validation failure (exit 3) whose message names the field and says why,
so the test asserts both rather than only that something went wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from kanso import hyp
from kanso.errors import Exit, KansoError, ValidationError
from kanso.workspace import Workspace
from tests.hyp.conftest import (
    DOCUMENT,
    FILTER_CLASSIFICATION,
    HOST_ID,
    SLEEVE_CLASSIFICATION,
    document,
    write_hypothesis,
    write_instruments,
    write_portfolio,
    write_strategy,
)


def refused(ws: Workspace, doc: dict[str, Any], hyp_id: str | None = None) -> ValidationError:
    """Validate a document that must be refused, and hand back the failure."""
    path = write_hypothesis(ws, doc, hyp_id)
    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, path)
    assert failure.value.code is Exit.VALIDATION
    assert isinstance(failure.value, ValidationError)
    return failure.value


def accepted(ws: Workspace, doc: dict[str, Any]) -> Any:
    return hyp.validate(ws, write_hypothesis(ws, doc))


# -- the admissible one --------------------------------------------------------


def test_the_demo_hypothesis_is_admissible(ws: Workspace) -> None:
    assert accepted(ws, DOCUMENT).id == "demo_mr"


def test_a_classified_sleeve_is_admissible(ws: Workspace) -> None:
    parsed = accepted(ws, document(**SLEEVE_CLASSIFICATION))

    assert parsed.construct is not None
    assert parsed.construct.id == "sleeve"


def test_a_construct_attached_to_a_certified_host_is_admissible(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID)

    parsed = accepted(ws, document(**FILTER_CLASSIFICATION))

    assert parsed.construct is not None
    assert parsed.construct.host == HOST_ID


# -- the duration grammar and the windows --------------------------------------


def test_a_horizon_outside_the_duration_grammar_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(horizon="30x"))

    assert "horizon" in failure.message


def test_a_certification_window_inside_the_embargo_is_refused(ws: Workspace) -> None:
    # A one-day horizon embargoes five days; research closes 2024-12-31.
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2025-01-02", "end": "2025-05-30"},
    }

    failure = refused(ws, document(horizon="1d", windows=windows))

    assert "windows.certification.start" in failure.message
    assert "embargo" in failure.message


def test_the_embargo_is_five_horizons_or_a_day_whichever_is_longer(ws: Workspace) -> None:
    # 30m x 5 is under a day, so the floor applies and the day after research closes is legal.
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2025-01-01", "end": "2025-05-30"},
    }

    assert accepted(ws, document(windows=windows, horizon="1m")) is not None


def test_a_long_horizon_pushes_certification_further_out(ws: Workspace) -> None:
    # 10d x 5 is 50 days, so a certification window opening two days later is refused.
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2025-01-02", "end": "2025-05-30"},
    }

    failure = refused(ws, document(windows=windows, horizon="10d"))

    assert "50d" in failure.message


def test_a_certification_window_overlapping_research_is_refused(ws: Workspace) -> None:
    windows = {
        **DOCUMENT["windows"],
        "certification": {"start": "2024-12-01", "end": "2025-05-30"},
    }

    failure = refused(ws, document(windows=windows))

    assert "certification.start" in failure.message
    assert "overlaps the research window" in failure.message


def test_a_forward_window_overlapping_certification_is_refused(ws: Workspace) -> None:
    windows = {**DOCUMENT["windows"], "forward": {"start": "2025-05-01"}}

    failure = refused(ws, document(windows=windows))

    assert "forward.start" in failure.message
    assert "overlaps the certification window" in failure.message


def test_a_window_that_ends_before_it_starts_is_refused(ws: Workspace) -> None:
    windows = {
        **DOCUMENT["windows"],
        "research": {"start": "2024-12-31", "end": "2024-01-02"},
    }

    failure = refused(ws, document(windows=windows))

    assert "end" in failure.message


# -- resolution, data requirements and costs -----------------------------------


def test_a_bar_resolution_without_bars_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(resolution="1m", data_requirements=["quote"]))

    assert "data_requirements" in failure.message
    assert "bar" in failure.message


def test_a_tick_resolution_without_ticks_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(resolution="tick", data_requirements=["bar"]))

    assert "data_requirements" in failure.message
    assert "'trade' or 'quote'" in failure.message


def test_a_grain_resolution_absent_from_the_requirements_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(resolution="quote", data_requirements=["trade"]))

    assert "data_requirements" in failure.message
    assert "'quote'" in failure.message


def test_a_data_requirement_naming_no_known_type_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(data_requirements=["bar", "orderbook"]))

    assert failure.message.startswith("data_requirements:")
    assert "orderbook" in failure.message


def test_a_repeated_data_requirement_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(data_requirements=["bar", "bar"]))

    assert "data_requirements" in failure.message
    assert "repeats" in failure.message


def test_a_spread_read_from_quotes_needs_quotes(ws: Workspace) -> None:
    costs = {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "quotes"}

    failure = refused(ws, document(costs=costs))

    assert "costs.spread:" in failure.message
    assert "quote" in failure.message


def test_a_fixed_spread_needs_its_width(ws: Workspace) -> None:
    costs = {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "fixed_bps"}

    failure = refused(ws, document(costs=costs))

    assert "costs.fixed_bps: required when spread is fixed_bps" in failure.message


def test_a_cost_model_that_cannot_be_completed_is_refused(ws: Workspace) -> None:
    # No override, no broker declaration and no quotes to take a spread from.
    failure = refused(ws, document(costs=None))

    assert failure.message.startswith("costs.fixed_bps:")
    assert "no quotes" in failure.message


def test_quotes_complete_the_cost_model_without_a_width(ws: Workspace) -> None:
    doc = document(costs={"spread": "quotes"}, data_requirements=["bar", "quote"], resolution="1m")

    assert accepted(ws, doc) is not None


# -- the universe --------------------------------------------------------------


def test_a_repeated_universe_id_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["DEMO", "DEMO"]))

    assert "universe" in failure.message
    assert "repeats" in failure.message


def test_an_unknown_universe_id_is_refused_naming_it(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["NOPE"]))

    assert failure.message.startswith("NOPE:")
    assert "no entry in instruments.yaml" in failure.message


def test_an_instrument_delisted_before_the_research_window_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["GONE"]))

    assert failure.message.startswith("GONE:")
    assert "delisted 2023-06-30, before 2024-01-02" in failure.message


def test_an_instrument_listed_after_the_research_window_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["LATE"]))

    assert failure.message.startswith("LATE:")
    assert "listed after 2024-01-02" in failure.message


def test_every_failing_universe_id_is_reported_together(ws: Workspace) -> None:
    failure = refused(ws, document(universe=["GONE", "LATE"]))

    assert "GONE" in failure.message
    assert "LATE" in failure.message


def test_a_universe_spanning_two_account_currencies_is_refused(ws: Workspace) -> None:
    write_portfolio(ws, {"XETR": {"currency": "EUR"}})

    failure = refused(ws, document(universe=["DEMO", "EURO"]))

    assert failure.message.startswith("universe:")
    assert "more than one account currency" in failure.message


def test_one_account_currency_across_two_venues_is_admissible(ws: Workspace) -> None:
    assert accepted(ws, document(universe=["DEMO", "EURO"])) is not None


def test_the_resolved_instruments_reach_the_catalog_store(ws: Workspace) -> None:
    from kanso.data.instruments import read_store

    accepted(ws, DOCUMENT)

    assert [str(held.id) for held in read_store(ws).values()] == ["DEMO.SIM"]


# -- where the file lives ------------------------------------------------------


def test_a_hypothesis_lives_in_the_directory_its_id_names(ws: Workspace) -> None:
    failure = refused(ws, DOCUMENT, hyp_id="somewhere_else")

    assert failure.message.startswith("id:")
    assert "somewhere_else" in failure.message


def test_a_file_outside_the_hypotheses_tree_is_judged_on_its_content_alone(
    ws: Workspace,
) -> None:
    import yaml

    path = ws.path("elsewhere.yaml")
    path.write_text(yaml.safe_dump(DOCUMENT), encoding="utf-8")

    assert hyp.validate(ws, path).id == "demo_mr"


def test_a_file_that_cannot_be_read_is_a_validation_failure(ws: Workspace) -> None:
    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, ws.path("hypotheses", "absent", "hypothesis.yaml"))

    assert failure.value.code is Exit.VALIDATION
    assert "cannot be read" in failure.value.message


def test_a_file_that_is_not_utf8_is_a_validation_failure(ws: Workspace) -> None:
    path = ws.path("hypotheses", "demo_mr", "hypothesis.yaml")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"schema: 1\nid: \xff\xfe\n")

    with pytest.raises(KansoError) as failure:
        hyp.validate(ws, path)

    assert failure.value.code is Exit.VALIDATION
    assert "not UTF-8" in failure.value.message


# -- the classification --------------------------------------------------------


def test_a_construct_outside_the_catalogue_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "construct": {"id": "invented"},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct:")
    assert "not in the catalogue" in failure.message


def test_a_sleeve_that_names_a_host_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "construct": {"id": "sleeve", "host": HOST_ID},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.host:")
    assert "attaches to nothing" in failure.message


def test_an_attached_construct_with_no_host_is_refused(ws: Workspace) -> None:
    classification = {
        **FILTER_CLASSIFICATION,
        "construct": {"id": "filter", "params": {"scope": "time"}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.host:")
    assert "attaches to a sleeve" in failure.message


def test_a_host_that_is_not_a_certified_strategy_is_refused(ws: Workspace) -> None:
    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert failure.message.startswith("construct.host:")
    assert "not a certified strategy" in failure.message


def test_a_host_file_declaring_another_strategy_is_refused(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID, declared_id="other_sleeve")

    failure = refused(ws, document(**FILTER_CLASSIFICATION))

    assert failure.message.startswith("construct.host:")
    assert "other_sleeve" in failure.message


def test_a_portfolio_construct_attaches_to_the_book(ws: Workspace) -> None:
    classification = {
        "construct": {"id": "allocation", "host": "portfolio"},
        "objective": {"id": "marginal_net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
        "constraints": [{"id": "strategy_integrity"}],
    }

    assert accepted(ws, document(**classification)) is not None


def test_a_portfolio_construct_named_onto_a_sleeve_is_refused(ws: Workspace) -> None:
    write_strategy(ws, HOST_ID)
    classification = {
        "construct": {"id": "allocation", "host": HOST_ID},
        "objective": {"id": "marginal_net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
        "constraints": [{"id": "strategy_integrity"}],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("construct.host:")
    assert "attaches to the book" in failure.message


def test_an_objective_outside_the_toolbox_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {"id": "invented", "params": {"min_delta": 0.5, "k_se": 1.0}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("objective.id:")
    assert "not an objective in the toolbox" in failure.message


def test_an_objective_that_does_not_apply_is_refused(ws: Workspace) -> None:
    # wf_sharpe_net wants a holding period of a day or more; this one holds 30 minutes.
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.5, "k_se": 1.0}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("objective.id:")
    assert "does not apply" in failure.message
    assert "net_edge_bps" in failure.message


def test_a_relative_objective_on_a_sleeve_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {
            "id": "marginal_net_edge_bps",
            "params": {"min_delta": 0.5, "k_se": 1.0},
        },
    }

    failure = refused(ws, document(**classification))

    assert "does not apply" in failure.message


def test_an_objective_parameter_outside_its_range_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "objective": {"id": "net_edge_bps", "params": {"min_delta": 0.5, "k_se": 0.1}},
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("objective.params.k_se:")
    assert "outside the range" in failure.message


def test_a_constraint_that_is_not_a_gate_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "strategy_integrity"}, {"id": "net_edge_bps"}],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("constraints.net_edge_bps:")
    assert "not a gate" in failure.message


def test_a_constraint_from_another_stage_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [
            {"id": "strategy_integrity"},
            {"id": "embargoed_window", "params": {"min_fraction": 0.5}},
        ],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("constraints.embargoed_window:")
    assert "runs at the cert stage" in failure.message


def test_a_constraint_parameter_outside_its_range_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 0}}],
    }

    failure = refused(ws, document(**classification))

    assert failure.message.startswith("constraints.min_trades.min:")
    assert "outside the range" in failure.message


def test_constraints_without_the_integrity_gate_are_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "min_trades", "params": {"min": 30}}],
    }

    failure = refused(ws, document(**classification))

    assert "constraints" in failure.message
    assert "strategy_integrity is required" in failure.message


def test_a_repeated_constraint_is_refused(ws: Workspace) -> None:
    classification = {
        **SLEEVE_CLASSIFICATION,
        "constraints": [{"id": "strategy_integrity"}, {"id": "strategy_integrity"}],
    }

    failure = refused(ws, document(**classification))

    assert "constraints" in failure.message
    assert "repeats" in failure.message


@pytest.mark.parametrize("dropped", ["construct", "objective", "constraints"])
def test_a_half_written_classification_is_refused(ws: Workspace, dropped: str) -> None:
    classification = {k: v for k, v in SLEEVE_CLASSIFICATION.items() if k != dropped}

    failure = refused(ws, document(**classification))

    assert failure.message.startswith(f"{dropped}:")
    assert "together" in failure.message


def test_an_unclassified_hypothesis_needs_no_classification(ws: Workspace) -> None:
    parsed = accepted(ws, DOCUMENT)

    assert parsed.construct is None
    assert parsed.objective is None
    assert parsed.constraints is None


# -- extensions ----------------------------------------------------------------


def test_a_custom_type_an_extension_registers_is_a_usable_requirement(ws: Workspace) -> None:
    extension = ws.root / "kanso_ext"
    extension.mkdir()
    (extension / "sentiment.py").write_text(
        "from nautilus_trader.core.data import Data\n"
        "from nautilus_trader.model.custom import customdataclass\n"
        "from kanso.data import register_custom_type\n\n"
        "PROVIDES = {'data_types': ('sentiment',)}\n\n\n"
        "@customdataclass\n"
        "class Sentiment(Data):\n"
        "    score: float = 0.0\n\n\n"
        "register_custom_type('sentiment', Sentiment)\n",
        encoding="utf-8",
    )
    write_instruments(ws, "DEMO")

    parsed = accepted(ws, document(data_requirements=["bar", "sentiment"]))

    assert "sentiment" in parsed.data_requirements


def test_a_workspace_without_a_portfolio_has_no_venue_overrides(ws: Workspace) -> None:
    ws.path("portfolio.yaml").unlink()

    assert accepted(ws, document(universe=["DEMO", "EURO"])) is not None
