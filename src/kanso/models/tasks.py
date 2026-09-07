"""The four task classes: what each one is asked, and the shape of the answer it owes.

A task class is a call site with a fixed job, a fixed answer shape and a fixed place in
the routing table. There are four, they are named in the register's routing table, and a
fifth would be a call site the package does not have — so this module is closed, and the
schemas here are the whole of what any model in any workspace is ever asked to produce.

Three rules shape every prompt built here.

**The stable half carries the subject, the dynamic half carries the moment.** The system
turn is the instruction, the answer schema and the facts that do not move for one subject,
serialised from sorted keys so the same subject renders the same bytes on every call of
that class. The user turn is what changed. Providers cache by exact prefix bytes, so this
is not tidiness: a clock, a call counter or a previous answer in the system turn costs the
cache on every call of a run.

**A prompt states facts, never secrets.** Nothing here reaches a credential, and the
router refuses to send a prompt in which one appears anyway.

**A parameter map travels as a list of pairs.** A gate's and a construct's parameters are
keyed by names kanso does not own, and a free-form object is precisely what a provider
constraining an answer to a schema refuses — measured, not assumed; `PARAM_PAIRS` records
what each candidate shape cost. The pairs are undone by `collapse` before any step sees
them, so the encoding lives in this module and the router and nowhere else.

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

__all__ = ["ANSWER_SCHEMAS", "INSTRUCTIONS", "PARAM_PAIRS", "build", "canonical", "collapse"]

PARAM_PAIRS: Final[dict[str, object]] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "value": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "boolean"}]},
        },
        "required": ["name", "value"],
        "additionalProperties": False,
    },
}
"""A gate's or a construct's parameters on the wire: a list of `{name, value}` pairs.

The keys belong to the gate or the construct rather than to kanso, so the shape this
wants to be is a free-form object — and a free-form object is the one shape a
schema-constrained answer cannot carry. Measured against the Anthropic messages API on
2026-09-07, one request per shape, with the schema in `output_config.format`:

* `{"type": "object"}` — 400, `additionalProperties` must be explicitly set to false.
* `{"type": "object", "additionalProperties": true}` — 400, not supported.
* `{}` — 400, an empty schema that accepts any JSON value is not supported.
* `{"type": "object", "additionalProperties": false}` — 200, and the model answered
  `"params": {}`. It is accepted because it is a closed object with no properties, so
  every parameter the model chose is dropped on the way out. A gate would then run on
  its defaults and a construct would be classified with nothing, silently.
* this shape — 200, and the model answered
  `[{"name": "lookback", "value": 20}, {"name": "threshold", "value": 1.5},
  {"name": "scope", "value": "entries"}, {"name": "enabled", "value": true}]`, every
  name and every type intact.

OpenAI-compatible structured outputs demand `additionalProperties: false` on every
object too, so this one shape is admissible on that wire as well — read, not measured,
and `models/openai_compat.py` records the second requirement of that protocol which the
`classify` schema does not meet and which nothing here has been driven against. The three
value types are `kanso.schemas.ParamValue` exactly — `bool | int | float | str` — so the
encoding loses nothing the data model could have held.

`collapse` reads the pairs back into the mapping every step inland receives, so the list
never leaves this layer. Anything that turns it back into an object earns the 400 above
or, worse, the silent drop below it.
"""


ANSWER_SCHEMAS: Final[dict[TaskClass, dict[str, object]]] = {
    "classify": {
        "type": "object",
        "properties": {
            "construct": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "host": {"type": "string", "minLength": 1},
                    "params": PARAM_PAIRS,
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
                    "properties": {"id": {"type": "string", "minLength": 1}, "params": PARAM_PAIRS},
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
                        "params": PARAM_PAIRS,
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
        "Every `params` is a list of `{name, value}` pairs — one entry per parameter "
        "you choose, each name given once, and an empty list where a construct or a gate "
        "takes none. A parameter you leave out is a parameter left at its default.\n\n"
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
        "Every gate's `params` is a list of `{name, value}` pairs — one entry per "
        "parameter you choose, each name given once, and an empty list where a gate takes "
        "none. A parameter you leave out is a parameter left at its default.\n\n"
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


def collapse(
    answer: Mapping[str, object], schema: Mapping[str, object]
) -> tuple[dict[str, object], list[str]]:
    """`answer` with every parameter list read back as a mapping, and what that cost.

    The pairs of `PARAM_PAIRS` are a transport encoding and nothing else: this is where
    they stop, so `classify` and `plan` receive the `{name: value}` mapping they have
    always received and no step inland knows the wire has a shape at all. Every other
    value is carried through untouched, and a node the schema says nothing about is
    carried through too rather than dropped.

    Call it on an answer that has already satisfied `schema` — the router does — so each
    pair here is a `{name, value}` object. What that does not settle is whether the names
    are distinct, and a name given twice is a wrong answer: kanso has no rule saying the
    first wins or the last does, the model has said two things about one parameter, and
    repairing that quietly would put a value nobody chose into a gate. It earns a
    complaint instead, and a complaint is the retry ladder, where the model is told which
    name to say once.
    """
    complaints: list[str] = []
    collapsed = {
        name: _collapsed(value, _sub(schema, name), f"the answer.{name}", complaints)
        for name, value in answer.items()
    }
    return collapsed, complaints


def _collapsed(value: object, schema: Mapping[str, object], where: str, out: list[str]) -> object:
    """`value` under `schema`, with any parameter list at or below it made a mapping."""
    if schema == PARAM_PAIRS:
        return _mapped(value, where, out)
    if isinstance(value, dict):
        return {
            name: _collapsed(item, _sub(schema, name), f"{where}.{name}", out)
            for name, item in value.items()
        }
    if isinstance(value, list):
        items = schema.get("items")
        each = items if isinstance(items, Mapping) else {}
        return [
            _collapsed(item, each, f"{where}[{index}]", out) for index, item in enumerate(value)
        ]
    return value


def _mapped(value: object, where: str, out: list[str]) -> object:
    """One list of pairs as the mapping it encodes, or the value as it came.

    A value that is not a list of named pairs cannot have satisfied `PARAM_PAIRS`, so
    this is reached only by a caller that skipped the schema; it hands the value back
    rather than inventing a mapping, and the schema is where that is reported.
    """
    if not isinstance(value, list):
        return value
    mapping: dict[str, object] = {}
    for pair in value:
        if not isinstance(pair, Mapping) or not isinstance(pair.get("name"), str):
            return value
        name = str(pair["name"])
        if name in mapping:
            out.append(f"{where}: {name!r} is named twice; give each parameter once")
            continue
        mapping[name] = pair.get("value")
    return mapping


def _sub(schema: Mapping[str, object], name: str) -> Mapping[str, object]:
    """The schema for one property, or an empty one when the schema declares none."""
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        found = properties.get(name)
        if isinstance(found, Mapping):
            return found
    return {}


def _plain(value: Mapping[str, object]) -> dict[str, object]:
    return dict(value)
