"""The core knows a protocol name and nothing else about any provider.

A vendor detail that escapes this package is not a style problem: it is a second place a
provider's wire has to be changed, and a second place a credential could be read. These
two checks are cheap, and they fail on the commit that would have started the leak rather
than on the release that discovers it.
"""

from __future__ import annotations

from pathlib import Path

import kanso

PACKAGE = Path(kanso.__file__).resolve().parent
MODELS = PACKAGE / "models"
TEMPLATES = PACKAGE / "templates"
ADAPTERS = PACKAGE / "data" / "adapters"

FORBIDDEN = (
    "api.anthropic.com",
    "api.openai.com",
    "anthropic-version",
    "x-api-key",
    "chat/completions",
    "/v1/messages",
    "reasoning_effort",
    "output_config",
    "response_format",
    "input_schema",
    "cache_control",
    "Bearer ",
)
"""Endpoints, headers and request fields that belong to one provider's wire.

The protocol *names* — `anthropic`, `openai_compat`, `mock` — are not on this list: they
are the register's own vocabulary, and the schema that parses the register must name them.
"""


def modules() -> list[Path]:
    """Every shipped module outside this package, templates excluded.

    A template is rendered into an operator's workspace rather than imported, so what it
    says about a provider is documentation for them, not a dependency of the core.
    """
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if not path.is_relative_to(MODELS) and not path.is_relative_to(TEMPLATES)
    ]


def test_httpx_is_imported_by_the_model_layer_and_by_nothing_else() -> None:
    """It is on the dependency list for this one purpose."""
    leaked = [p for p in modules() if "import httpx" in p.read_text(encoding="utf-8")]
    assert leaked == []
    importers = {
        p.name for p in MODELS.glob("*.py") if "import httpx" in p.read_text(encoding="utf-8")
    }
    assert importers == {"wire.py", "anthropic.py", "openai_compat.py"}


def test_no_provider_wire_detail_appears_outside_the_model_layer() -> None:
    """This list is one provider's wire, so a vendor adapter is not what it is about.

    A data vendor's own auth header and endpoints belong in its adapter package and
    nowhere else, which is a different rule with its own test; scanning an adapter for the
    model layer's tokens would fail a module for doing exactly what that rule requires. The
    httpx rule above is not relaxed the same way: `httpx` serves the model layer alone, and
    an adapter reaching for it would be a new dependency on an old name.
    """
    found: list[str] = []
    for path in modules():
        if path.is_relative_to(ADAPTERS):
            continue
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(PACKAGE)}: {token}" for token in FORBIDDEN if token in text]
    assert found == []


def test_the_model_layer_reaches_credentials_only_through_the_standard_resolver() -> None:
    """Names come from `kanso.creds`; no module here reads the environment for a key."""
    readers = [
        path.name
        for path in MODELS.glob("*.py")
        if "os.environ" in path.read_text(encoding="utf-8")
    ]
    assert readers == ["router.py"], "only the prompt guard enumerates variables"
