# Changelog

One line per user-visible change, newest release first. The format is the one
`docs/maintainers.md` §4 and the `kanso-release` skill require; versions are semver.

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
