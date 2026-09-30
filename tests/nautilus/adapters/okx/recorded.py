"""The exchange's recorded answers, served back through a transport that opens no socket.

Every body under `fixtures/` was recorded from the public API on 2026-09-30 with no
credential; `fixtures/provenance.json` gives each one's url, host, status and UTC instant,
and says which page was trimmed to the rows the tests read. A test asks for a recording by
its file name and is served its bytes and its recorded status unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kanso.nautilus.adapters.okx.reference import Response

FIXTURES = Path(__file__).parent / "fixtures"

PROVENANCE: dict[str, Any] = json.loads((FIXTURES / "provenance.json").read_text("utf-8"))


def recorded(name: str) -> Response:
    """One recorded answer: its body as served and its status as recorded."""
    return Response(
        status=int(PROVENANCE["files"][name]["status"]),
        body=(FIXTURES / name).read_bytes(),
    )


def row(inst_id: str) -> dict[str, Any]:
    """One recorded listing row, as the exchange served it."""
    rows: list[dict[str, Any]] = json.loads(recorded(f"swap_{inst_id}.json").body)["data"]
    return rows[0]


BY_ID: dict[str | None, str] = {
    None: "swap_page.json",
    "BTC-USDT-SWAP": "swap_BTC-USDT-SWAP.json",
    "ETH-USDT-SWAP": "swap_ETH-USDT-SWAP.json",
    "BTC-USD-SWAP": "swap_BTC-USD-SWAP.json",
    "NOPE-USDT-SWAP": "swap_unknown.json",
    "btc-usdt-swap": "swap_malformed.json",
}
"""Which recording answers which `instId`, exactly as each was asked of the exchange."""


@dataclass
class Replay:
    """A transport serving the recording for each request, and remembering what it sent."""

    answers: Mapping[str | None, str] = field(default_factory=lambda: dict(BY_ID))
    asked: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        self.asked.append((url, dict(params)))
        return recorded(self.answers[params.get("instId")])
