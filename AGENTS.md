# kanso — build agent instructions

You are building the `kanso` package. `SPEC.md` is the single source of truth for the build and for nothing else: it is deleted at the 0.1.0 release, after which `docs/` and the tests are the reference, so nothing that outlives the build (code, docstrings, docs, skills, templates) may refer to it. `docs/maintainers.md` is how this repository is maintained and released (maintainer skills live in `skills/`; link that directory into your tool's skills path). Read `SPEC.md` before writing code.

## Ground rules
- Follow `SPEC.md` §11 build order. Finish a milestone (green acceptance tests, coverage ≥85%) before starting the next.
- Deviating from the spec is allowed only when the spec is impossible or contradicts NautilusTrader's pinned API; then amend `SPEC.md` in the same commit as the code and state the reason in the commit body. Do not silently reinterpret; there is no deviation log.
- Directives in `SPEC.md` §3.1 are binding. In particular: D1 (no learnings/notes artefacts), D2 (provider specifics only in `models/`; vendor and broker specifics only in `data/adapters/<vendor>/` and `nautilus/adapters/<broker>/`), D10/D12 (mutable surface and embargo enforced by code), D14 (adapter isolation: the core knows no vendor, and the suite, `doctor` and the demo are green with every vendor credential unset), D19 (availability timestamps), D18 (kanso never invokes git in a workspace — no `git init`, commit, branch, tag, worktree or reset; the §10 subprocess spy test enforces it). The commit rules below apply to this framework repository, not to workspaces.
- Pure Python. No Rust, no Cython, no compiled extensions in v0 (§12 governs when that changes). The only Rust kanso executes is NautilusTrader's, through `nautilus_pyo3` — adapter network I/O uses its `HttpClient` and `WebSocketClient` with `Quota` rate limits.
- Runtime dependencies are exhaustive in `pyproject.toml`; adding one requires amending C3 in `SPEC.md` in the same commit.

## Toolchain and conventions
- `uv` for everything: `uv sync`, `uv run pytest`, `uv run ruff check`, `uv run mypy src`.
- Python ≥3.12. `ruff` (format + lint) and `mypy --strict` clean on `src/`.
- Tests: `pytest`, `pytest-cov`, `hypothesis`. No network in tests; the `synthetic` loader and the `mock` model protocol are the fixtures.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). Semver; `0.1.0` is the M8 release.
- Every CLI command: `--json`, exit codes per `SPEC.md` §7.2, one integration test.
- Every schema: pydantic v2 model, one property test.
- Docstrings state the contract a module implements in words, and record the NautilusTrader facts it relies on with the engine version; never a `SPEC.md` section reference.

## Definition of done (per milestone)
1. Acceptance tests from `SPEC.md` §11 pass on macOS arm64 and Linux x86_64.
2. Coverage ≥85% lines for `src/kanso`.
3. `kanso doctor` green in a fresh workspace.
4. `docs/` updated for anything operator-visible (CLI, workspace files, skills).
5. The milestone is committed on a `feat/m<N>-<slug>` branch, pushed to origin, and its CI run is green on macOS arm64 and Linux x86_64 before the next milestone starts.

## Working style
- Small commits, each green. No WIP commits on `main`.
- Prefer deleting code to adding it. If a component can be a function, it is not a class.
- When NautilusTrader's API is unclear, read the installed package source (`uv run python -c "import nautilus_trader, inspect; ..."`) rather than guessing; note the version in the docstring.
- Do not write summaries, progress reports, or learnings files. The tests and the diff are the report.
