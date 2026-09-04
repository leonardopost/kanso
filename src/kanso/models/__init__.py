"""The model layer: the register, the router, the clients and the spend ledger.

Every LLM call kanso makes enters here and leaves through one of three clients. The
package holds three properties the rest of the system depends on and cannot check for
itself.

**One call path.** `route` is the only way a call is made. It resolves the task class's
tier, thinking effort and output cap from the workspace register, builds the prompt,
climbs the retry-and-escalate ladder and records every attempt. There is no second path,
so there is no attempt that escapes the ladder or the ledger.

**One place for provider specifics.** The two wire protocols each ask for a
schema-constrained JSON answer in their own way and map kanso's four efforts onto their
own notion of thinking. Both live in this package. Nothing outside it names a provider, a
header, an endpoint or a parameter, and `httpx` is imported here and nowhere else.

**Spend is spend.** A rejected answer was generated and billed, so a failed attempt is
ledgered exactly like a successful one. Nothing here can pause or refuse research over
cost; the ledger reports, and the operator decides.
"""

from __future__ import annotations

from kanso.models.call import Answer, Call, CallInputs, Client
from kanso.models.jsonschema import validate
from kanso.models.ledger import NO_LANE, LedgerEntry, Spend, cost_of, ledger, spend
from kanso.models.mock import MockClient
from kanso.models.mock import reset as reset_mock
from kanso.models.register import (
    REGISTER_NAME,
    escalation,
    first_for_tier,
    read_register,
    route_for,
    tiers_covered,
)
from kanso.models.router import ModelCheck, check, client_for, route
from kanso.models.tasks import ANSWER_SCHEMAS, INSTRUCTIONS, build, canonical

__all__ = [
    "ANSWER_SCHEMAS",
    "INSTRUCTIONS",
    "NO_LANE",
    "REGISTER_NAME",
    "Answer",
    "Call",
    "CallInputs",
    "Client",
    "LedgerEntry",
    "MockClient",
    "ModelCheck",
    "Spend",
    "build",
    "canonical",
    "check",
    "client_for",
    "cost_of",
    "escalation",
    "first_for_tier",
    "ledger",
    "read_register",
    "reset_mock",
    "route",
    "route_for",
    "spend",
    "tiers_covered",
    "validate",
]
