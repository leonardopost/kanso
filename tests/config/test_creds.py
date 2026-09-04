"""Credential resolution: standard names, `.env` parsing, precedence, and silence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.creds import (
    origin,
    parse_env_file,
    read_env_file,
    require,
    resolve,
    scrub,
    standard_name,
)
from kanso.errors import Exit, PreconditionError, ValidationError

SECRET = "sk-not-a-real-key-9d41f"
NAME = "KANSO_TESTVENDOR_API_KEY"


def dotenv(workspace: Path, text: str) -> None:
    (workspace / ".env").write_text(text, encoding="utf-8")


# --- standard names -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "purpose", "expected"),
    [
        ("acme", "API_KEY", "KANSO_ACME_API_KEY"),
        ("acme-paper", "API_KEY", "KANSO_ACME_PAPER_API_KEY"),
        ("some.vendor", "API_SECRET", "KANSO_SOME_VENDOR_API_SECRET"),
        ("acme_compat", "API_KEY", "KANSO_ACME_COMPAT_API_KEY"),
        ("a--b", "API_KEY", "KANSO_A_B_API_KEY"),
        ("-edge-", "API_KEY", "KANSO_EDGE_API_KEY"),
        ("s3 store", "ACCESS_KEY_ID", "KANSO_S3_STORE_ACCESS_KEY_ID"),
        ("Vendor2", "api key", "KANSO_VENDOR2_API_KEY"),
    ],
)
def test_the_standard_name_is_derived_from_the_id(
    subject: str, purpose: str, expected: str
) -> None:
    assert standard_name(subject, purpose) == expected


def test_the_purpose_defaults_to_the_api_key() -> None:
    assert standard_name("acme") == "KANSO_ACME_API_KEY"


def test_the_webhook_url_is_an_instance_of_the_scheme() -> None:
    assert standard_name("webhook", "URL") == "KANSO_WEBHOOK_URL"


@pytest.mark.parametrize(
    ("subject", "purpose"),
    [("", "API_KEY"), ("---", "API_KEY"), ("x", ""), ("\u00b9", "API_KEY")],
)
def test_a_name_with_nothing_to_derive_from_is_refused(subject: str, purpose: str) -> None:
    with pytest.raises(ValidationError) as caught:
        standard_name(subject, purpose)
    assert caught.value.code is Exit.VALIDATION


@given(
    subject=st.text(min_size=1, max_size=24).filter(
        lambda s: any(c.isascii() and c.isalnum() for c in s)
    )
)
def test_every_derived_name_is_a_shell_variable_name(subject: str) -> None:
    name = standard_name(subject)
    assert name.startswith("KANSO_")
    assert name.endswith("_API_KEY")
    assert "__" not in name
    assert all(c.isalnum() or c == "_" for c in name)
    assert name.upper() == name


# --- .env parsing ---------------------------------------------------------------------


def test_dotenv_conventions() -> None:
    parsed = parse_env_file(
        "\n".join(
            [
                "# a comment",
                "   # an indented comment",
                "",
                "PLAIN=value",
                "  SPACED  =  padded  ",
                "export EXPORTED=exported",
                "export   ODD_SPACING = odd ",
                'DOUBLE="double quoted"',
                "SINGLE='single quoted'",
                "MIXED=\"unmatched'",
                "EQUALS=a=b=c",
                "URL=https://example.test/path?x=1#frag",
                "EMPTY=",
                'EMPTY_QUOTED=""',
                "no equals sign here",
                "=novalue",
                "DUP=first",
                "DUP=second",
            ]
        )
    )
    assert parsed == {
        "PLAIN": "value",
        "SPACED": "padded",
        "EXPORTED": "exported",
        "ODD_SPACING": "odd",
        "DOUBLE": "double quoted",
        "SINGLE": "single quoted",
        "MIXED": "\"unmatched'",
        "EQUALS": "a=b=c",
        "URL": "https://example.test/path?x=1#frag",
        "EMPTY": "",
        "EMPTY_QUOTED": "",
        "DUP": "second",
    }


def test_a_hash_inside_a_value_is_part_of_the_secret() -> None:
    assert parse_env_file("K=abc#def")["K"] == "abc#def"
    assert parse_env_file('K="abc # def"')["K"] == "abc # def"


def test_an_absent_dotenv_reads_as_nothing(tmp_path: Path) -> None:
    assert read_env_file(tmp_path) == {}


def test_a_dotenv_directory_reads_as_nothing(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()
    assert read_env_file(tmp_path) == {}


def test_undecodable_bytes_do_not_stop_the_rest_of_the_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"BROKEN=\xff\xfe\nGOOD=fine\n")
    assert read_env_file(tmp_path)["GOOD"] == "fine"


@given(
    key=st.from_regex(r"\A[A-Z][A-Z0-9_]{0,12}\Z"),
    value=st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=32),
)
def test_a_written_pair_reads_back(key: str, value: str) -> None:
    if value[:1] in ("'", '"', "#"):
        return
    assert parse_env_file(f"{key}={value}") == {key: value}


# --- resolution -----------------------------------------------------------------------


def test_a_name_resolves_from_the_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv(tmp_path, f"{NAME}={SECRET}\n")
    assert resolve(NAME, tmp_path) == SECRET
    assert origin(NAME, tmp_path) == ".env"


def test_a_name_resolves_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    assert resolve(NAME, tmp_path) == SECRET
    assert origin(NAME, tmp_path) == "environment"


def test_the_dotenv_wins_when_both_hold_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NAME, "from-the-environment")
    dotenv(tmp_path, f"{NAME}={SECRET}\n")
    assert resolve(NAME, tmp_path) == SECRET
    assert origin(NAME, tmp_path) == ".env"


def test_a_name_in_neither_place_resolves_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv(tmp_path, "OTHER=value\n")
    assert resolve(NAME, tmp_path) is None
    assert origin(NAME, tmp_path) is None


def test_an_empty_dotenv_entry_falls_through_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NAME, SECRET)
    dotenv(tmp_path, f"{NAME}=\n")
    assert resolve(NAME, tmp_path) == SECRET
    assert origin(NAME, tmp_path) == "environment"


def test_an_empty_environment_entry_resolves_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NAME, "")
    assert resolve(NAME, tmp_path) is None


def test_resolution_injects_nothing_into_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv(tmp_path, f"{NAME}={SECRET}\n")
    assert resolve(NAME, tmp_path) == SECRET
    assert NAME not in os.environ
    assert SECRET not in "".join(os.environ.values())


def test_resolution_reads_the_file_at_each_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv(tmp_path, f"{NAME}=first\n")
    assert resolve(NAME, tmp_path) == "first"
    dotenv(tmp_path, f"{NAME}=second\n")
    assert resolve(NAME, tmp_path) == "second"


def test_a_required_credential_that_is_missing_fails_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv(tmp_path, f"UNRELATED={SECRET}\n")
    with pytest.raises(PreconditionError) as caught:
        require(NAME, tmp_path)
    message = f"{caught.value.message} {caught.value.remedy}"
    assert caught.value.code is Exit.PRECONDITION
    assert NAME in message
    assert str(tmp_path / ".env") in message
    assert "environment" in message
    assert SECRET not in message


def test_a_required_credential_that_resolves_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv(tmp_path, f"{NAME}={SECRET}\n")
    assert require(NAME, tmp_path) == SECRET


def test_an_unreadable_dotenv_is_a_precondition_failure(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{NAME}={SECRET}\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(PreconditionError) as caught:
            resolve(NAME, tmp_path)
        assert SECRET not in caught.value.message
    finally:
        path.chmod(0o600)


# --- scrubbing ------------------------------------------------------------------------


def test_a_scrubbed_environment_carries_no_credential() -> None:
    given_env = {
        "PATH": "/usr/bin",
        "KANSO_ACME_API_KEY": SECRET,
        "KANSO_WEBHOOK_URL": "https://example.test",
        "VENDOR_TOKEN": SECRET,
        "HOME": "/home/op",
    }
    scrubbed = scrub(given_env, extra=["VENDOR_TOKEN"])
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/home/op"}
    assert SECRET not in "".join(scrubbed.values())
