---
name: kanso-release
description: Cut a kanso framework release from the framework repository — version bump, changelog line(s), tag, and the CI-driven PyPI publish — including the checks for NautilusTrader range bumps and schema migrations. Use in the kanso framework checkout when the maintainer says release, tag, publish, bump version, or ship.
license: Apache-2.0
metadata:
  version: "0.1"
  spec: "docs/maintainers.md §4"
---

# kanso-release

## Preconditions
- On the default branch, clean tree, CI green on the merge commit.
- Decide the semver level from the merged changes: patch (fixes) · minor (features, gates/loaders, `nautilus_trader` range bump, additive migration) · major (incompatible schema or CLI). If unsure, list the merged commits to the maintainer and ask once.

## Steps
1. If a migration was added since the last tag: confirm it is numbered after the newest (the package's schema version follows from the newest migration file; nothing is bumped by hand, and the `schema_version` key in `kanso.toml` is not the guard) and `uv run pytest tests/state` passes: it rebuilds `tests/state/fixtures/state_0_7_0.sql` — a `state.db` the 0.7.0 demo wrote, with rows in hypotheses, runs, cards and events — and applies every migration after its version over it, so a migration that cannot run over a populated earlier workspace fails there before it ships. It is the one fixture the suite reads; its header records the command that emitted it, and a re-recording runs that command rather than editing a row.
2. If the `nautilus_trader` range changed: confirm the demo e2e and parity tests passed on the new upper bound in CI, and note the wheel/OS constraint in the changelog line.
3. Bump the version in `pyproject.toml` and `src/kanso/__init__.py` (same string).
4. Add the `CHANGELOG.md` section `## vX.Y.Z — YYYY-MM-DD`: one line per user-visible change, imperative, no prose; skills/templates changes listed since they affect every workspace.
5. Commit `chore(release): vX.Y.Z`, tag `vX.Y.Z`, push branch and tag. CI publishes to PyPI on the tag through trusted publishing (no stored credential); wait for the workflow and report the PyPI URL.
6. If the publish fails on authentication, it is almost always a registration mismatch rather than a code problem: the index-side trusted publisher must name repository `leonardopost/kanso`, workflow `publish.yml` and environment `pypi` exactly. Nothing was uploaded — correct the registration and re-run the failed job.
7. If the publish fails for any other reason, do not retag: fix forward with a patch release.

## Rules
- Never publish from a local machine; only the tag workflow publishes.
- Never rewrite a pushed tag.
