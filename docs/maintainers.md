# kanso — maintainer guide

Audience: whoever owns the framework repository. Operator-side procedures are the shipped skills; this is the other side.

## 1. Repositories and roles
| repository | contains | agent instructions | skills |
|---|---|---|---|
| **framework** (`kanso`) | package, tests, docs, shipped skills and templates | `AGENTS.md` (build/maintain) | `skills/` (maintainer: `kanso-release`) |
| **project** (any directory, optionally inside a git repository, holding a workspace) | `kanso.toml`, hypotheses, data, state; optional `kanso_ext/` | rendered `AGENTS.md` | linked from the installed package (`kanso skills sync`), incl. `kanso-upstream` |

A project depends on the framework with `uv add kanso` (a release) or `uv add --editable <path-to-checkout>` (local development). `kanso doctor` reports which (`install.mode`).

## 2. Development loop
1. Change the framework in its checkout; `uv run pytest` (coverage ≥85%), `uv run ruff check`, `uv run mypy src`.
2. Try it from a project with an editable install; the demo workspace (`kanso init demo --demo` and the demo sequence in the README) is the smoke test.
3. Conventional commit on a `feat/`, `fix/`, `docs/` or `chore/` branch; PR; CI (macOS arm64 + Linux x86_64, Python 3.12/3.13) must be green.

## 3. Prototype in a project, promote to the framework
Capabilities are prototyped as **workspace extensions** (`kanso_ext/`, same interfaces as built-ins: `Gate`, `Objective`, `Construct`, `Loader`, `register_custom_type`) and moved upstream with the `kanso-upstream` skill (copy, real tests, branch, PR with the workspace evidence). Built-in additions land in `src/kanso/criteria/library/` (gate/objective YAML + `impl`), `src/kanso/classify/constructs/`, `src/kanso/data/loaders/`, or `src/kanso/data/types/`, each with tests and a line in `docs/`. Anything found in use without a checkout goes to the issue tracker with `kanso doctor --report`.

## 4. Releases (skill `kanso-release`)
- Semver. Patch: fixes. Minor: features, new gates/loaders, `nautilus_trader` range bumps, schema migrations (additive). Major: incompatible schema/CLI changes.
- A release = version bump (`pyproject.toml`, `kanso.__version__`), `CHANGELOG.md` entry (one line per change, no prose), tag `vX.Y.Z`; CI publishes to PyPI on the tag.
- Publishing is by **trusted publishing**: `publish.yml` requests an OIDC token and stores no credential. The index-side registration is done once and must match the workflow exactly — repository `leonardopost/kanso`, workflow filename `publish.yml`, environment `pypi`. A mismatch in any of the three fails the upload with an authentication error that looks like a missing secret; check the registration before touching the workflow. A failed publish uploaded nothing: fix and re-run the job, never retag.
- `nautilus_trader` range: bump in a minor release only after the demo e2e and the parity tests pass on the new version; note wheel/OS constraints in the changelog. Strategy versions pin the engine they were certified under; operators re-certify to move.
- Schema changes ship with a migration `src/kanso/state/migrations/NNNN_name.sql` and a `schema_version` bump; `kanso migrate` applies them; `kanso doctor` refuses to run a workspace at a newer schema than the package.
- Skills and templates ship inside the wheel; a release changes them for every workspace at the next `kanso skills sync` / `kanso init`.

## 5. Documentation governance
`docs/` and the tests are the reference. Changing behaviour = changing the relevant `docs/` page and the tests in the same PR; the PR body states why. No decision logs, deviation logs or design notes are kept in the repository.
