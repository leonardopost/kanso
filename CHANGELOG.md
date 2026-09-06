# Changelog

One line per user-visible change, newest release first. The format is the one
`docs/maintainers.md` §4 and the `kanso-release` skill require; versions are semver.

## Unreleased

- `kanso doctor` grades a state database written by a later kanso as a failure rather than reporting it up to date, so it no longer disagrees with every other command about whether a workspace is usable.

- `kanso data sync` extends only the **newest** dataset of each series, so a series with a chunked backfill behind it can be continued at all; `--dataset` still names one directly, newest of its series or not.
- `kanso data instruments resolve` builds the `[data] reference` adapter only once an id is actually left unresolved, so a wholly manual universe resolves with that adapter's credential unset.
- A baseline that did not run reports the remedy of the failure that ended it — rows the catalog no longer holds send an operator to `kanso data load` — where every cause alike used to say `fix hypotheses/<id>/strategy.py`.
- `kanso hyp validate` and `kanso hyp add` refuse a construct parameter the construct does not declare, or a value outside its declared set (exit 3); the refusal used to arrive inside a run's first card.
- `kanso hyp validate` and `kanso hyp add` refuse `portfolio` as a hypothesis id (exit 3): it is how a construct attached to the book names its host.
- `kanso portfolio deploy` and `kanso replay run --strategy` hash every file under `strategies/<id>/impl/<version>/` against the `strategy_sha` its manifest records, and refuse an edited, truncated or deleted source by name (exit 3), naming the certified copy to restore it from.
- `kanso portfolio show` reads `portfolio.yaml` against the record: a stage entry no deployment wrote prints as `not deployed · in portfolio.yaml only`, carries `"recorded": false` under `--json`, and counts towards neither the stage's allocation nor its P&L nor whether its node is up.
- `kanso cert plan` warns when the paper window the plan implies is under a tenth of the certification window the hypothesis declares, and pins the plan either way; `warnings` under `--json`.
- A `kanso_ext` module declaring `gates` or `objectives` in `PROVIDES` is refused where it is declared: no registry reads either kind, and a criterion is written in the package.
- `kanso doctor`'s shadow check reads one registry per kind a declaration may carry — the packaged constructs, the custom data types and the framework's own `sandbox` execution client included — where it read three kinds out of seven.
- `[adapters.alpaca] poll_interval_s` sets the live feed's sweep cadence, `1` to `3600` seconds and still 15 by default, refused out of range when the table is read; it was a constructor argument no workspace could reach.

## v0.1.0 — 2026-09-06

First release.

- `kanso init [--demo]`: a workspace is a plain directory; `--demo` runs end to end with no credential of any kind.
- `kanso doctor [--report] [--check-adapters]`: thirteen checks over the workspace, the install, the engine, the credentials, the adapters, the execution clients and the lanes.
- Data: `data load`, `show`, `snapshot`, `backfill`, `sync`, `instruments resolve|show`, `adapters [--check]`; one catalog, manifests recording the span a source *served*, immutable snapshots.
- Availability: every point carries `ts_init` (public) and `ts_event` (economic), `ts_init >= ts_event`; a delayed dataset with no declared publication rule is refused at write.
- Hypotheses: `hyp new|validate|add|show|retire` and `classify`; a hypothesis is pinned by the sha256 of its bytes and its progress lives in `state.db`.
- Constructs: seven in the catalogue, four runnable — `sleeve`, `filter`, `overlay` on a sleeve, `exit`; `alpha`, `execution` and portfolio-hosted `allocation` classify and refuse at `research begin` naming their seam.
- Research: `research begin|card|run|end|show`, the keep rule with its noise floor and complexity clause, `align check`, and the daemon (`research start|stop|status|queue`).
- The embargo is code: a card runs in a child process with no path to any catalog, and the `strategy_integrity` syntax-tree check runs before the backtest.
- Certification: `cert plan|run|show`; no default plan and no non-LLM fallback, gates chosen at runtime inside the toolbox's declared ranges, certificates immutable and pinned to plan, snapshot and engine version.
- Strategies and the portfolio: `strat compose|show|retire`, `portfolio show|clients|deploy`, `promote --live --as NAME`, `demote`, `monitor run`; one generated `impl/<version>/` loaded by backtest, replay and node alike.
- Real capital moves only on a named, recorded approval; `deploy` refuses six things with exit 2 and two with exit 4.
- Replay: `replay run|parity|show`, comparing the live and research code paths element by element at a tolerance meant to be zero.
- Models: `models check`, a register routed by task class and tier, every call ledgered including a retry's failed attempts.
- Operating: `status`, `inbox [ack]`, `migrate`, `skills sync`, `env detect`, `ext show`.
- Adapters: one first-party data vendor and one first-party broker, both enabled by their credentials and never by installation; the core knows neither.
- Extensions: `kanso_ext/` provides loaders, constructs, custom data types, data adapters and execution clients; a packaged id always wins.
- Eleven operator skills and the workspace templates ship inside the wheel.
- Python ≥3.12 on macOS 26+ arm64 and Linux x86_64; `nautilus_trader>=1.231.0,<1.232`; no compiled artefact of kanso's own.
- A state database this kanso cannot correctly write is refused in both directions, by the command layer and by the daemon entry point a service unit starts: behind the package names the migration to run, ahead of it names how far, and neither is silently written to.
