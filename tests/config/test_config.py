"""`kanso.toml` parsing and rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError as PydanticValidationError

from kanso import __version__
from kanso.config import Config, load_config, render_config
from kanso.errors import Exit, PreconditionError, ValidationError

MINIMAL = 'kanso_version = "0.1.0"\nschema_version = 1\n'


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kanso.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_render_fills_the_version_placeholder() -> None:
    text = render_config(__version__)
    assert f'kanso_version = "{__version__}"' in text
    assert "{{" not in text


def test_rendered_template_parses_with_the_documented_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, render_config("9.9.9")))
    assert config.kanso_version == "9.9.9"
    assert config.schema_version == 1
    assert config.extensions_paths == ["kanso_ext"]
    assert config.skills_targets == [".claude/skills", ".cursor/skills", ".codex/skills"]
    assert config.research.capital == 100_000
    assert config.research.account == "margin"
    assert config.research.currency == "USD"
    assert config.research.return_period == "1d"
    assert config.research.annualisation == "auto"
    assert config.research.align_every == 10
    assert config.research.stall_k == 30
    assert config.research.context_cards == 20
    assert config.research.folds == 4
    assert config.research.max_lines_per_keep == 40
    assert config.research.baseline_budget_s == 1800
    assert config.certify.n_fail == 3
    assert config.data.reference == "none"
    assert config.data.adjusted is False
    assert config.env.reserved_cores is None
    assert config.env.reserved_mem_gb is None
    assert config.env.cores_per_lane is None
    assert config.monitor.interval == "5m"
    assert config.webhook.url is None
    assert config.adapters == {}


def test_the_template_names_the_broker_research_inherits(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, render_config("0.1.0")))
    assert config.research.broker  # the template supplies it; the model has no default
    assert Config(kanso_version="0.1.0", schema_version=1).research.broker is None


def test_omitted_sections_take_their_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL))
    assert config.research.folds == 4
    assert config.data.reference == "none"
    assert config.monitor.interval == "5m"


def test_a_workspace_directory_resolves_to_its_config(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    assert load_config(tmp_path).schema_version == 1


def test_adapter_tables_are_free_form(tmp_path: Path) -> None:
    text = MINIMAL + '[adapters.some_vendor]\nplan = "pro"\nregions = ["us", "eu"]\n'
    config = load_config(write(tmp_path, text))
    assert config.adapters["some_vendor"] == {"plan": "pro", "regions": ["us", "eu"]}


def test_an_unknown_top_level_key_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, MINIMAL + "capital = 5\n"))
    assert "unknown key 'capital'" in caught.value.message
    assert caught.value.code is Exit.VALIDATION


def test_an_unknown_section_key_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, MINIMAL + "[research]\nfolds = 4\nfollds = 5\n"))
    assert "unknown key 'research.follds'" in caught.value.message


@pytest.mark.parametrize(
    ("section", "key"),
    [("extensions", "path"), ("skills", "target")],
)
def test_an_unknown_key_in_a_flattened_section_is_named(
    tmp_path: Path, section: str, key: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, MINIMAL + f'[{section}]\n{key} = ["x"]\n'))
    assert f"unknown key '{section}.{key}'" in caught.value.message


def test_a_flattened_section_that_is_not_a_table_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, MINIMAL + "extensions = 5\n"))
    assert "[extensions] must be a table" in caught.value.message


def test_the_flat_field_names_are_accepted_directly() -> None:
    config = Config(
        kanso_version="0.1.0",
        schema_version=1,
        extensions_paths=["ext"],
        skills_targets=[".claude/skills"],
    )
    assert config.extensions_paths == ["ext"]
    assert config.skills_targets == [".claude/skills"]


@pytest.mark.parametrize(
    "body",
    [
        '[research]\ncapital = "lots"\n',
        "[research]\nfolds = 1\n",
        "[research]\ncapital = 0\n",
        '[research]\naccount = "spot"\n',
        '[research]\nreturn_period = "1 day"\n',
        '[monitor]\ninterval = "soon"\n',
        '[data]\nadjusted = "yes"\n',
        "[certify]\nn_fail = 0\n",
        "[env]\ncores_per_lane = 0\n",
        '[skills]\ntargets = "a"\n',
    ],
)
def test_a_value_the_template_does_not_describe_is_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValidationError):
        load_config(write(tmp_path, MINIMAL + body))


def test_schema_version_must_be_at_least_one(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, 'kanso_version = "0.1.0"\nschema_version = 0\n'))
    assert "schema_version" in caught.value.message


def test_a_missing_required_key_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, "schema_version = 1\n"))
    assert "kanso_version" in caught.value.message


def test_numeric_annualisation_is_accepted(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, MINIMAL + "[research]\nannualisation = 252\n"))
    assert config.research.annualisation == 252


def test_malformed_toml_is_a_validation_failure(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        load_config(write(tmp_path, "kanso_version = \n"))
    assert "not valid TOML" in caught.value.message


def test_undecodable_bytes_are_a_validation_failure(tmp_path: Path) -> None:
    path = tmp_path / "kanso.toml"
    path.write_bytes(b'kanso_version = "\xff\xfe"\n')
    with pytest.raises(ValidationError):
        load_config(path)


def test_an_absent_config_is_a_precondition_failure(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError) as caught:
        load_config(tmp_path / "kanso.toml")
    assert caught.value.code is Exit.PRECONDITION
    assert caught.value.remedy


def test_an_unreadable_config_is_a_precondition_failure(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL)
    path.chmod(0o000)
    try:
        with pytest.raises(PreconditionError):
            load_config(path)
    finally:
        path.chmod(0o600)


@given(key=st.from_regex(r"\A[a-z][a-z_]{0,12}\Z"))
def test_every_unknown_top_level_key_is_named_in_the_message(
    tmp_path_factory: pytest.TempPathFactory, key: str
) -> None:
    if key in Config.model_fields or key in ("extensions", "skills"):
        return
    path = tmp_path_factory.mktemp("ws") / "kanso.toml"
    path.write_text(MINIMAL + f"{key} = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_config(path)
    assert key in caught.value.message


def test_an_already_parsed_config_revalidates(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, render_config("0.1.0")))
    assert Config.model_validate(config) == config


def test_an_unfilled_placeholder_in_the_template_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kanso.config._template", lambda: 'k = "{{unknown}}"\n')
    with pytest.raises(ValidationError) as caught:
        render_config("0.1.0")
    assert "placeholder" in caught.value.message


def test_a_document_that_is_not_a_table_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        Config.model_validate(["kanso_version", "0.1.0"])
