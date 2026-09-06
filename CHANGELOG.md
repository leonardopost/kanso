# Changelog

One line per user-visible change, newest release first. The format is the one
`docs/maintainers.md` §4 and the `kanso-release` skill require; versions are semver.

## Unreleased

- `kanso doctor` has a `record` check: certified files on disk the record has no memory of — a clone, or a removed `state.db` — are named once as a fresh workspace that inherits certified work, with what did not travel and what re-establishes it.
- `kanso doctor` fails the `engine facts` check when a binding kanso relies on no longer holds on the installed engine, naming the claim and its evidence; the design constraints the package records are listed `by design`, where every gap alike used to print `does not hold` under `ok`.
- `kanso doctor` grades four more things: `best` — `hypotheses/<id>/strategy.py` against the hypothesis's best blob, a warning and never a failure, because editing that file is how an operator prepares `research begin --from-workspace`; `certificates` — every certificate and strategy version whose `strategy_sha` is held neither as a blob in state nor as the `<sha7>.py` beside the certificate, a failure naming the copy still in the workspace when one exists; `instruments` — every manual entry and override listed, every universe id of every registered hypothesis resolved as of its research start without calling a vendor, and drift between the store's definitions and what the newest snapshot pinned, graded by the same comparison `research begin` pins a run by; `lanes` — an open run whose lane directory is gone fails, a lane directory departing from its three pinned files or with no open run behind it warns. Seventeen checks, each remedy a command.
- `kanso data instruments resolve` refuses (exit 2), by name, a resolution that would change a definition the store holds for the same date, and `--refresh` replaces it: the held file is removed before the write, every write is read back, and a write the engine skipped is an error rather than a line on stdout. A definition dated otherwise is added beside the held ones. The store used to keep the old definition while the command reported the new one.
- `kanso data instruments show ID` renders the one definition a run would use — the newest-dated the store holds — rather than every dated copy.
- `kanso hyp validate` and `kanso hyp add` no longer write the catalog's instrument store or `instruments.yaml`; `kanso data instruments resolve` alone does. A run resolves its universe for the venue model the same way, so `research begin` and a card never move the store under the snapshot the run is pinned to.
- `kanso data snapshot` refuses (exit 2) to freeze an empty instrument store over datasets that name instruments, and `kanso research begin` pins the newest covering snapshot whose instrument checksum is the store's own — refusing (exit 2) by name, with the snapshot, what it pins and what the store holds, when the definitions moved since, and refusing a universe id the store holds no definition for. The README's demo runs `kanso data instruments resolve --as-of 2024-01-02` before `kanso data snapshot`.
- `kanso cert run` refuses to re-certify bytes whose certificate is already on disk for the same plan and engine, even when `state.db` has no record of it (a fresh clone): the refusal reads the certificate files, matching the subject, plan and engine and ignoring the trial count in the filename, so the count can no longer be reset by re-certifying from a clone.
- The workspace `.gitignore` excludes `envelope.yaml`: it is a measurement of the host, so a clone on another machine detects its own (a missing envelope is a `kanso doctor` warning whose remedy is `kanso env detect`) rather than inheriting a committed one that describes a different machine.
- `kanso replay run --strategy` and `kanso replay parity --strategy` resolve a composed version's hypothesis from the committed `hypotheses/<id>/hypothesis.yaml` when the registry has no pin, so a committed, certified version replays from the files in a fresh clone without re-registering the hypothesis first.
- `kanso portfolio deploy` admits only versions the state record knows, the same record `kanso portfolio show` reads, so in a workspace whose `state.db` did not travel it deploys nothing for a version the record has forgotten instead of running a node whose window `show` would then call not-deployed.
- `kanso hyp add` and `kanso classify` clear the hypothesis's best when `construct.id` changes — stripping the classification included — and the `best_cleared` event names the field that moved (`construct changed from 'sleeve' to 'filter'`); a filter no longer inherits a sleeve's champion through the strip-and-reclassify round trip, and a re-pin with nothing to clear no longer records a clearing. `hyp show --json` lists `construct` among the pins.
- A baseline that fails when the run started from `best` names `kanso research begin <id> --from-workspace` in its remedy, since plain `begin` would take the same blob again.
- `kanso inbox` pads the kind column past the longest kind, so a `deploy_blocked` entry no longer runs into its subject.
- `kanso init` renders a commented `[data]` table (`reference`, `adjusted`) and an `[adapters.<id>]` hint into `kanso.toml`, so the rendered file is the reference for every key the parser reads; the header is commented too, so a `[data]` table you append is not declared twice.
- The `hypothesis.yaml` scaffold, the `portfolio.yaml` template and the `kanso-hypothesis` and `kanso-promote` skills say that a hypothesis without `quote` data must set `fixed_bps`; the `kanso.toml` template states the currency refusal as the account-currency check it is; `portfolio.yaml`'s `speed` comment says a stage node replays unpaced in this version.
- Docs: `doctor`'s checks are listed by name, `engine facts` included; `research begin` needs no register; `research start` does not re-detect the envelope; a lane writes no log and the record is `state.db`; the paper gate is two-sided and a short window fails; the provider protocol's import path; `mock/responses.yaml` in the who-writes-what table; `README.md` no longer states a backlog count.
- The unused `live` pytest marker and its default deselection are gone; credentialed acceptance is a maintainer-driven CLI run recorded in the pull request (`docs/maintainers.md`).

## v0.1.0 — 2026-09-06

First release.

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
