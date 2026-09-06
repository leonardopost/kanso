# kanso — instructions for changing this package

This repository is the `kanso` framework: a released Python package, versioned by semver.
It is its own reference. `docs/` states what every part of it guarantees and refuses, the
tests state it again in a form that fails when it stops being true, and the two are changed
together or not at all. There is no design document, no decision log and no build plan; if
something is true of kanso and is written down nowhere, that is a gap in `docs/` or in the
suite, and the fix is to close it rather than to write a note.

`docs/maintainers.md` is the other side of this file: repositories, the development loop,
promoting an extension upstream, releases, and running the daemon as a service. Maintainer
skills live in `skills/` — link that directory into your tool's skills path.

## The standing invariants

These are the properties the rest of the package is built on. Each one would be easy to
lose in a change that looks local, so each is enforced somewhere you can read.

**kanso never invokes git.** No `git` subprocess, in a workspace or anywhere else — no
`init`, commit, branch, tag, worktree, reset or object write, by itself or on an operator's
behalf. Committing a workspace is the operator's business; versioning the file research
mutates is kanso's own, content-addressed in the state store. `tests/workspace/test_no_git.py`
and `tests/cli/test_no_git.py` spy on every process launch and fail the moment one is git.

**The core knows no vendor and no broker.** Every outside party lives in exactly one
package — `data/adapters/<vendor>/` for data and reference, `nautilus/adapters/<broker>/`
for execution and live feeds — and nothing outside it names a vendor, an endpoint, a vendor
field or a vendor symbology. Mapping to engine types and kanso schemas happens at the
adapter boundary. Provider specifics stay inside `models/` the same way. The isolation
tests (`tests/data/adapters/`, `tests/nautilus/adapters/`, `tests/models/`) are a source
scan and an import-graph check, and they read the names from the adapter directories rather
than from a list, so a new adapter is scanned without anyone remembering to add it.

**An adapter is enabled by its credentials, never by installation.** There are no extras.
The full suite, `kanso doctor` and the demo are green with every vendor and broker
credential unset, and CI has no credential at all, which is what proves it. A test that
genuinely needs a real key carries the `live` marker, is deselected by default and never
runs in CI.

**Availability, not observation.** Every catalog point's `ts_init` is the instant its
information became public and `ts_event` is its economic reference time; `ts_init >=
ts_event` always, and the engine orders by `ts_init`. Never derive `ts_init` from
`ts_event` or from ingest time. A delayed dataset whose `ts_init` does not come from a
declared publication rule is refused at write (`data/publication.py`).

**Costs are applied once, by the runner, in the extraction.** Commission, slippage and half
the spread each side are deducted per fill in `nautilus/backtest.py` and nowhere else. One
application means one number: a card, a certification gate, a composition expectation and a
realised paper objective all read the same arithmetic, and a cost model can be re-applied to
recorded fills without re-running anything. The simulated venue charges nothing to keep it
that way — and it charges nothing because kanso's resolved instruments leave their maker and
taker rates at zero, not because the venue is configured fee-free, so an instrument that
arrives with a non-zero rate double-counts silently.

**The embargo is code.** A backtest request may name only a window the hypothesis declares,
and the card path accepts only the research window. A card runs in a child process with no
path to any catalog — the parent reads the window and serialises the points across, and the
child re-checks them against the window it was asked for. Never add a route by which
research could reach the certification window, and never replace the refusal with an
instruction to a model.

**Only `strategy.py` is mutable by the research loop.** Everything fixed lives in the
package. A proposal that names another file, does not apply, or changes nothing is a wrong
answer that takes the retry ladder; it never becomes a card.

**Real capital moves only on a named, recorded approval.** `promote --live --as NAME` is the
only path, `--as` has no default and no environment fallback, and the approval is recorded
against the exact version before anything moves. Paper promotion and demotion are automatic;
this one is not, and no convenience is worth making it so.

**One strategy class, one data model, one evaluation path.** Backtest, replay, paper and
live run the same generated implementation, and a version pins the engine version it was
certified under. `kanso replay parity` compares the two code paths element by element and
exists to be run at a tolerance of zero.

**A run optimises exactly one scalar.** Constraints are gates, never terms in an objective.

**Agents decide, the framework evaluates.** Which gates, which thresholds, which constraints
and which construct are chosen at runtime by a model from catalogues that declare
capabilities and ranges. There are no default plans, no default thresholds and no non-LLM
fallback for a decision an agent is supposed to make: with no model configured the step
exits 2. The framework's own opinions are structural invariants and the construct taxonomy,
and nothing else.

**Runtime dependencies are exhaustive.** `nautilus_trader`, `pydantic`, `typer`, `pyyaml`,
`httpx`, `numpy`, and the standard library. `pyarrow`, `pandas` and `msgspec` are reachable
only as the engine's own transitive dependencies and only where its API hands them to you.
`httpx` serves `models/` only. Adding a runtime dependency is a deliberate change to
`pyproject.toml` argued in the commit body; an adapter that would need one is not
admissible.

**Pure Python.** kanso ships no compiled artefact of its own. The only Rust it executes is
NautilusTrader's, through `nautilus_trader.core.nautilus_pyo3` — adapter network I/O uses
its `HttpClient` and `WebSocketClient` with `Quota` rate limits.

**Secrets live only in environment variables**, resolved by standard name from the
workspace `.env` and then the ambient environment, sent in a request header and never in a
URL path or query string, because URLs reach logs, manifests and `doctor --report`. No
credential appears in anything kanso writes.

**No notes, learnings, summaries or progress reports** — not in this repository, not in a
workspace. The tests, the diff, the cards, the certificates and the state store are the
record.

## Toolchain

`uv` for everything.

```bash
uv sync
uv run pytest -n auto --cov-fail-under=85    # the gate CI runs; drop the flag for a partial run
uv run pytest --durations=40                 # serial: the form a durations reading is taken from
uv run ruff format
uv run ruff check
uv run mypy src
```

Python ≥3.12; `nautilus_trader>=1.231.0,<1.232`, one requirement for every supported host
(macOS 26+ arm64, Linux x86_64). Tests use `pytest`, `pytest-cov`, `pytest-xdist` and
`hypothesis`, make no network call and are deterministic; the `synthetic` loader and the
`mock` model protocol are the fixtures.

## Conventions

- Every CLI command takes `--json` and prints exactly one object under it, exits with the
  codes `docs/cli.md` lists, and has at least one integration test.
- Every schema is a pydantic v2 model in `schemas/` with one property test.
- Docstrings state the contract a module implements in words, and record the
  NautilusTrader facts it relies on with the engine version. They never point at a document
  outside this repository.
- `from __future__ import annotations` everywhere. No `print()` outside `cli/`. Raise
  `kanso.errors.{PreconditionError, ValidationError, ApprovalError}` — each carries the
  message and, wherever there is one to give, a remedy that is a command the reader can run.
- A new state table ships as a new file in `src/kanso/state/migrations/`, never as an edit
  to an existing one, with the `schema_version` bump beside it.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`) on a branch named for
  the change. Semver per `docs/maintainers.md`.

## Two rules paid for in defects

Both were learned the expensive way during the build and are cheap to keep.

**A fixture is a claim about the world, and is worth what its evidence is worth.** Eight
defects in the first data adapter reached a suite of 3,492 tests at 100% line coverage,
because every fixture recorded what its author expected the vendor to do rather than what
the vendor was measured doing: an option key asked of the wrong reference endpoint, a
listing requested under a version prefix that answers 404, a page four times wider than the
endpoint serves, a signed `Host` forwarded to a transport that sets its own, an empty page
read as an entitlement refusal — which tells an operator to buy what they already own. Every
one was invisible offline and obvious in one live call. So: record the measured response,
say where it came from, and drive an adapter against its real source before calling the work
done. Coverage tells you which lines ran, never whether the fixture resembles the source.

**A test that recomputes a timestamp the product already took is asserting that no clock
moved between them.** Five did, and one failed CI at 00:13 UTC when a definition was
resolved either side of midnight and the two checksums differed. Pin the input, freeze the
clock, or accept either instant the call spanned — never leave it to the hour the suite
happens to run at.

## Definition of done

1. `uv run ruff format --check`, `uv run ruff check` and `uv run mypy src` clean.
2. `uv run pytest` green. The enforced floor is 85% of lines in `src/kanso`; the tree is at
   100%, and a change that lowers it has shipped lines nothing runs.
3. `kanso doctor` green in a fresh workspace, and the demo sequence in `README.md` runs end
   to end with every `KANSO_*` and vendor variable unset.
4. `docs/` changed in the same commit as any operator-visible behaviour — a command, a
   workspace file, a refusal, a skill — and the tests with it. The PR body says why.
5. CI green on macOS arm64 and Linux x86_64, on both Python versions, before it merges.

## Working style

- Small commits, each green. Prefer deleting code to adding it. If a component can be a
  function, it is not a class.
- When the engine's API is unclear, read the installed source rather than guessing:
  `uv run python -c "import nautilus_trader, inspect; ..."`. Check
  `src/kanso/nautilus/facts.py` first — it asserts what this build relies on, and
  `kanso doctor` re-checks every claim against the installed engine and names the ones that
  no longer hold.
- **A fixture is a claim about an outside party, and it is worth what its evidence is
  worth.** Eight defects reached a green suite at 100% line coverage during the build
  because the fixtures encoded what an author expected instead of what the vendor did.
  Coverage says which lines ran, never whether the fixture resembles the source. Drive an
  adapter against the real thing before trusting it, and record the measured response.
- Prose fails the same way. Every command a document shows must have been run, and if the
  code and the explanation disagree, the code is what happened.

## Where things are

| directory | what it holds |
|---|---|
| `cli/` | every command, its `--json` object and its exit codes |
| `schemas/` | a pydantic model for every file kanso reads or writes |
| `state/` | the store and its migrations |
| `data/` | the catalog, manifests, snapshots, loaders, instruments, `adapters/<vendor>/` |
| `nautilus/` | the strategy base, the runner, the venue, the node, replay, `facts.py`, `adapters/<broker>/` |
| `classify/` | the construct catalogue and the classifier |
| `criteria/` | objectives and gates, with the declared toolbox in `library/` |
| `hyp/`, `research/` | registration, the loop, the driver, the daemon, alignment |
| `certify/`, `strategy/` | the planner, certificates, composition and versions |
| `portfolio/`, `monitor/`, `replay/` | stages, deployment, promotion, surveillance, the two code paths |
| `models/` | the register, the router, the two wire protocols and the mock |
| `inbox/` | the five escalation kinds and the append-only file |
| `skills/`, `templates/` | what ships into an operator's workspace |
