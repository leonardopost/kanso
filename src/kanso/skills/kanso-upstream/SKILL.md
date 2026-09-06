---
name: kanso-upstream
description: Move a capability developed inside a kanso workspace (a gate, objective, loader, custom data type, or a framework fix) into the kanso framework repository as a tested pull request, or file it as an upstream issue when the framework is not checked out locally. Use when the operator says upstream, contribute, send to the framework, promote to kanso, open a PR against kanso, or when a workspace extension has proven itself.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-upstream

## Where is the framework?
`kanso doctor --json` carries an `install` check whose `detail` is `<mode> · <directory>`: `editable · /…/kanso/src/kanso` is a local checkout, `package · /…/site-packages/kanso` an install from an index.

## Path A — extension → framework (editable or a known checkout)
1. `kanso ext show` → the workspace extensions and their kinds. Pick the one the operator named.
2. Copy its module and YAML into the framework checkout at the location for its kind (`docs/maintainers.md` §3: `src/kanso/criteria/library/`, `src/kanso/classify/constructs/`, `src/kanso/data/loaders/`, `src/kanso/data/types/`) and add a test next to the existing ones, starting from the extension's workspace tests.
3. In the framework checkout: `uv run pytest`, `uv run ruff check`, `uv run mypy src`. Coverage floor is 85%.
4. Branch `feat/<name>`, conventional commit, `gh pr create` with a body that states the workspace evidence (what it gated or loaded, over which data), and, if it changes a schema or the CLI, the semver consequence and a `docs/` update.
5. Tell the operator the PR URL. Leave the workspace extension in place until the framework release that contains it is installed; then delete it (`kanso doctor` warns about shadowed built-ins).

## Path B — framework fix from a workspace
Same as A from step 3, working directly in the checkout: fix, test, branch, PR. Do not patch the installed package in a virtualenv; if the framework is not checked out locally, ask the operator to `git clone` it (and optionally `uv add --editable <path>` in the project) before continuing.

## Path C — no checkout, or not sure it belongs upstream
`kanso doctor --report` → paste the block plus the proposed change into an issue on the framework repository. The workspace keeps working with its extension meanwhile.

## Rules
- Extensions live under `kanso.toml [extensions] paths` and use the same interfaces as built-ins; anything else is not upstreamable as-is.
- Never remove a workspace extension before the framework version containing it is installed.
- Never open a PR without green tests in the framework checkout.
