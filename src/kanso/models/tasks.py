"""The four task classes: what each one is asked, and the shape of the answer it owes.

A task class is a call site with a fixed job, a fixed answer shape and a fixed place in
the routing table. There are four, they are named in the register's routing table, and a
fifth would be a call site the package does not have — so this module is closed, and the
schemas here are the whole of what any model in any workspace is ever asked to produce.

Two rules shape every prompt built here.

**The stable half carries the subject, the dynamic half carries the moment.** The system
turn is the instruction, the answer schema and the facts that do not move for one subject,
serialised from sorted keys so the same subject renders the same bytes on every call of
that class. The user turn is what changed. Providers cache by exact prefix bytes, so this
is not tidiness: a clock, a call counter or a previous answer in the system turn costs the
cache on every call of a run.

**A prompt states facts, never secrets.** Nothing here reaches a credential, and the
router refuses to send a prompt in which one appears anyway.

`classify` and `certify_plan` decide what a hypothesis is and what would count as proof
of it. Neither is shown a card metric, a certificate or the strategy source, because a
planner that has seen the results is choosing the test that its results already pass.
Enforcing that is the calling step's business — it assembles the inputs — but the
instructions below say so, so a model asked for one of them anyway knows the answer is
not to be conditioned on it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from kanso.models.call import Call, CallInputs
from kanso.schemas.models import Route, TaskClass

__all__ = ["ANSWER_SCHEMAS", "INSTRUCTIONS", "build", "canonical"]

_PARAMS: Final[dict[str, object]] = {"type": "object"}
"""A free-form parameter map: the keys belong to the gate or construct, not to kanso."""


ANSWER_SCHEMAS: Final[dict[TaskClass, dict[str, object]]] = {
    "classify": {
        "type": "object",
        "properties": {
            "construct": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "host": {"type": "string", "minLength": 1},
                    "params": _PARAMS,
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            "objective_params": {
                "type": "object",
                "properties": {
                    "min_delta": {"type": "number", "minimum": 0},
                    "k_se": {"type": "number", "minimum": 0},
                },
                "required": ["min_delta", "k_se"],
                "additionalProperties": False,
            },
            "constraints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "minLength": 1}, "params": _PARAMS},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
            "rationale": {"type": "string", "maxLength": 240},
        },
        "required": ["construct", "objective_params", "constraints", "rationale"],
        "additionalProperties": False,
    },
    "propose": {
        "type": "object",
        "properties": {
            "desc": {"type": "string", "minLength": 1, "maxLength": 120},
            "diff": {"type": "string", "minLength": 1},
        },
        "required": ["desc", "diff"],
        "additionalProperties": False,
    },
    "align_check": {
        "type": "object",
        "properties": {
            "aligned": {"type": "boolean"},
            "reason": {"type": "string", "maxLength": 200},
        },
        "required": ["aligned", "reason"],
        "additionalProperties": False,
    },
    "certify_plan": {
        "type": "object",
        "properties": {
            "gates": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "stage": {"type": "string", "enum": ["cert", "paper", "live"]},
                        "params": _PARAMS,
                        "rationale": {"type": "string", "maxLength": 200},
                    },
                    "required": ["id", "stage", "params", "rationale"],
                    "additionalProperties": False,
                },
            },
            "excluded": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "reason": {"type": "string", "maxLength": 200},
                    },
                    "required": ["id", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["gates", "excluded"],
        "additionalProperties": False,
    },
}
"""The answer each task class owes, as the schema both sent on the wire and checked here."""


INSTRUCTIONS: Final[dict[TaskClass, str]] = {
    "classify": (
        "You classify a trading hypothesis for an automated research system.\n\n"
        "Decide three things from the hypothesis and the catalogues below.\n"
        "1. Which construct the hypothesis is, in portfolio-construction terms. Choose an "
        "id from the construct catalogue. A construct whose `needs_host` is not `none` "
        "modifies a deployed strategy rather than trading on its own, so name a `host` "
        "from the certified strategies; a construct that stands alone takes no host.\n"
        "2. The keep rule's parameters. `min_delta` is the smallest improvement in the "
        "objective worth keeping and `k_se` how many standard errors an improvement must "
        "clear, so both are noise floors: set them from how noisy this hypothesis's "
        "objective will be, not from ambition.\n"
        "3. The constraints every card of this hypothesis must satisfy. Choose ids from "
        "the card-stage gate catalogue and stay inside each parameter's stated range. "
        "Include every gate the catalogue marks required.\n\n"
        "Judge the idea as stated. You are shown no result, no certificate and no strategy "
        "source, and you must not ask for any: a classification conditioned on results is "
        "a classification fitted to them. Keep the rationale under 240 characters."
    ),
    "propose": (
        "You improve one trading strategy for an automated research system, one edit at a "
        "time.\n\n"
        "Propose the single next change to `strategy.py` and return it as a unified diff "
        "against the file given below. The diff must apply cleanly to exactly those bytes "
        "and must touch no other file: a diff that does not apply, or that names another "
        "path, is discarded and the attempt is wasted.\n\n"
        "One idea per change. The objective and its keep rule are stated below and are the "
        "only measure of success; a change that cannot plausibly move the objective is not "
        "worth a card. Read the recent cards for what has already been tried: repeating a "
        "discarded change, or reverting a kept one without a reason, wastes the run. When a "
        "crash tail is given, the crash is the change to make. Keep the description under "
        "120 characters and make it say what changed, not that something changed."
    ),
    "align_check": (
        "You check that a strategy still tests the hypothesis it was written for.\n\n"
        "Deterministic checks have already passed, so the question is one of meaning: does "
        "the code below still trade the stated mechanism, on the stated universe, at the "
        "stated horizon? Answer `aligned: false` only for a real drift — the code now "
        "exploits something the thesis does not claim, trades instruments the universe does "
        "not name, or works at a horizon the hypothesis does not state. Refactoring, "
        "parameter changes and added defensive code are aligned. Give the reason in under "
        "200 characters."
    ),
    "certify_plan": (
        "You decide what would count as proof for one trading hypothesis.\n\n"
        "Choose the gates that must pass before this hypothesis may hold capital, from the "
        "toolbox below, and say why each is in the plan. Every gate you include must exist "
        "in the toolbox, must be used at a stage the toolbox allows, must have parameters "
        "inside their stated ranges, and must be meaningful for this hypothesis — the "
        "toolbox states when each one is. Include every gate the toolbox marks required. "
        "The plan must reach all three stages: at least one `cert` gate, at least one "
        "`paper` gate and at least one `live` gate. Name each gate at most once.\n\n"
        "List in `excluded` every toolbox gate you deliberately left out, with the reason. "
        "A gate that is neither included nor excluded is an oversight, not a decision.\n\n"
        "You are shown no result, no card metric, no certificate and no strategy source, "
        "and you must not ask for any: a plan written against the results it will judge "
        "proves nothing. Keep every rationale and reason under 200 characters."
    ),
}
"""The fixed half of each task class's system turn."""


def canonical(value: object) -> str:
    """`value` as JSON that is the same bytes for the same content, every time.

    Keys are sorted so a mapping built in a different order renders identically, and a
    value JSON has no type for — a date, a `Decimal` — becomes its string form rather
    than failing the call, since a prompt is text and the model reads it as text.
    """
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=str)


def build(task: TaskClass, route: Route, inputs: CallInputs) -> Call:
    """The call one task class makes for one subject.

    The system turn is the instruction, the answer schema and the stable facts, in that
    order and from sorted keys, so it is identical across every call of this class for
    this subject. The user turn is the dynamic facts alone.
    """
    schema = ANSWER_SCHEMAS[task]
    system = "\n\n".join(
        (
            INSTRUCTIONS[task],
            "Answer with a JSON object matching this schema exactly, and nothing else:\n"
            + canonical(schema),
            f"These facts do not change while you work on {inputs.subject}:\n"
            + canonical(_plain(inputs.stable)),
        )
    )
    user = (
        canonical(_plain(inputs.dynamic))
        if inputs.dynamic
        else "Answer for the facts already given."
    )
    return Call(
        task_class=task,
        tier=route.tier,
        effort=route.effort,
        max_output=route.max_output,
        system=system,
        user=user,
        schema=schema,
    )


def _plain(value: Mapping[str, object]) -> dict[str, object]:
    return dict(value)
