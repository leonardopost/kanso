# kanso — Technical Specification

Version 0.1.0 · 2026-09-04 · status: approved for build.

## 1. Introduction

### 1.1 Purpose
This document specifies kanso, a minimal, agent-first quantitative research workbench built on NautilusTrader: requirements, structure, contracts, behaviour, verification and delivery. It is the sole normative input to the build and governs nothing after it: this file is deleted at the 0.1.0 release, after which `docs/` and the tests are the reference, so no artefact that outlives the build (code, docstrings, `docs/`, skills, templates) refers to it. There is no decision log and no deviation log; when the engine or the host makes a clause impossible, the clause is amended here in the same commit as the code. Maintenance: `docs/maintainers.md`.

### 1.2 Scope
kanso takes a market hypothesis stated in chat, classifies it as a quant construct, researches it autonomously and indefinitely, certifies the result, composes it into a strategy, deploys it to paper and — on operator approval — to live execution, and watches it there. Version 0.1.0 ships the primitives plus four first-party adapters — three data vendors and one broker for paper and live execution — each behind the interfaces fixed here, isolated so the core knows none of them and works with none configured (D14). Every further vendor or broker is an extension against the same interfaces.

### 1.3 Conventions
MUST, MUST NOT, SHOULD, MAY per RFC 2119. Identifiers: `D` directives (§3.1), `C` constraints (§3.2), `F` functional (§3.3), `N` non-functional (§3.4). Any "e.g." is illustrative, never normative (D7). *default* marks a shipped configuration default. Tables are normative unless titled *example*. The framework states no numeric research or certification opinion: every threshold is chosen by an agent at runtime inside the ranges the toolbox declares (D16).

### 1.4 Structure
§2 definitions · §3 requirements · §4 context · §5 architecture · §6 data model · §7 interfaces · §8 behaviour · §9 cross-cutting · §10 verification · §11 delivery · §12 extension points · §13 non-goals · appendices A (constructs), B (criteria toolbox), C (configuration), D (traceability).

## 2. Definitions

| term | definition |
|---|---|
| hypothesis | A testable market thesis captured in `hypothesis.yaml`; the unit of research. CLI noun `hyp`. |
| construct | What a hypothesis *is* in portfolio-construction terms — a sleeve, an alpha, a filter, an overlay, an exit rule, an execution tactic or an allocation rule (Appendix A). Assigned by classification. |
| host | The existing strategy (or the portfolio) a non-sleeve construct attaches to. |
| run | One research session on one hypothesis in one lane: id `<hyp>/<tag>`, its own lane directory, pinned inputs. |
| run tag | The `<yyyymmdd>-<n>` suffix of a run id; a plain string in the state store, never a git tag. |
| card | One experiment in a run: store the `strategy.py` blob → backtest → evaluate → `keep` / `discard` / `crash`. |
| blob | The stored bytes of one scoped file (`strategy.py`, `hypothesis.yaml`, `program.md`), keyed by its sha256 in the state store (table `blobs`). `best` names the blob of the best keep. |
| strategy_sha | sha256 of the bytes of a `strategy.py`; the identity of a card, of a certificate's subject and of a strategy version's sleeve. `sha7` is its first 7 hex characters, used in `results.tsv` and certificate file names; where 7 characters collide within a hypothesis the shortest unique prefix is used. |
| lane | One concurrent research worker with its own lane directory `runs/<lane>/<hyp>/`: daemon lanes `l1..lN`, interactive lane `op`. |
| objective | The one scalar a run optimises. `absolute` objectives score the construct alone; `relative` objectives score its marginal effect on its host. |
| gate | A pass/fail test with implementation, hint and parameter ranges, staged `card`, `cert`, `paper` or `live`; the set of gates is the *toolbox* (Appendix B). |
| constraints | A hypothesis's card-stage gates, chosen at classification. |
| certification plan | A hypothesis's cert/paper/live gates, parameters and rationale, produced at runtime by the planner and validated against structural invariants. |
| certificate | Immutable record of one cert-stage evaluation of one `strategy_sha` on one data snapshot under one plan version. |
| strategy | A versioned composition: a sleeve plus the constructs attached to it; `impl` is the generated Nautilus `Strategy`. |
| portfolio | Strategy versions deployed per stage (`paper`, `live`) with capital and limits; rendered into Nautilus node configurations. |
| envelope | Auto-detected host-machine capabilities and the derived lane plan. |
| register | `models.yaml`: LLM models, tiers, costs and the task-class routing table. |
| snapshot | Immutable, content-addressed set of catalog datasets referenced by runs and certificates. |
| session | One persisted node stream (data and order events), replayable through the backtest engine. |
| workspace | An operator project directory scaffolded by `kanso init`, owning all state as plain files plus `state.db`. It MAY live inside a git repository; kanso never writes to git (D18). |
| extension | A gate, objective, construct, loader or custom data type provided by a workspace or package through the built-in interfaces. |

## 3. Requirements

### 3.1 Directives (D)
| id | directive |
|---|---|
| D1 | Anti-slop: tiny, manageable build; no learnings, notes or summary artefacts anywhere. |
| D2 | Anti-leak: provider specifics stay inside `models/`; vendor and broker specifics stay inside exactly one adapter package — `data/adapters/<vendor>/` for data and reference, `nautilus/adapters/<broker>/` for execution and live feeds. No module outside an adapter package names a vendor, an endpoint, a vendor field or a vendor symbology; mapping to Nautilus types and kanso schemas happens at the adapter boundary. Skills, prompts, templates and the criteria library contain no vendor-specific commands. The import direction is one-way and tested (§10). |
| D3 | Autoresearch runs indefinitely until the operator stops it; it never pauses to ask. |
| D4 | Token-conscious, not token-capped: route by task class (tier, thinking effort, output cap), minimise context, skip LLM calls a deterministic rule can answer; no spend caps. |
| D5 | The machine envelope is auto-detected and determines concurrency. |
| D6 | Model-agnostic: any operator agent; any LLM provider behind two wire protocols plus a shipped mock. |
| D7 | Every example is an example, never a constraint. |
| D8 | Test ⇄ live parity: one strategy class, one data model, one evaluation path. |
| D9 | Escalate to the operator only on misalignment, repeated certification failure, promotability, live demotion, blocked deployment. |
| D10 | Only `strategy.py` is mutable by the research loop; everything fixed lives in the package. |
| D11 | A run optimises exactly one scalar; constraints are gates, never objectives. |
| D12 | The certification window is embargoed from research by code, not by prompt. |
| D13 | Real-capital promotion requires an explicit, named, logged operator approval; paper promotion and demotion are automatic. |
| D14 | Adapter isolation: 0.1.0 ships four first-party adapters (`massive`, `edgar`, `finra` data; `alpaca` execution and live market data) and the core knows none of them. Every adapter is discovered through the same registry a `kanso_ext/` extension uses; no framework behaviour, test or milestone acceptance depends on an adapter being configured; the full test suite, `kanso doctor` and the demo are green with every vendor credential unset. |
| D15 | kanso ships no compiled artefact of its own and writes no Rust in 0.1.0. The only Rust it executes is NautilusTrader's, reached through `nautilus_trader.core.nautilus_pyo3`: adapter network I/O uses `HttpClient` / `WebSocketClient` with `Quota` rate limits. A `kanso-core` crate remains §12. |
| D16 | Agents decide, the framework evaluates: which gates, which thresholds, which constraints and which construct are chosen at runtime by an agent from catalogues that declare capabilities and ranges, never defaults; the framework's only opinions are structural invariants (§8.4.2) and the domain taxonomy of Appendix A. |
| D17 | Unbounded cardinality of hypotheses, runs, cards, certificates, plans, strategies, versions, snapshots, sessions; only concurrency and deployment are bounded. |
| D18 | kanso is a Python package, not a git actor: it scaffolds and writes files inside the workspace and never invokes git — no `git init`, commit, branch, tag, worktree, reset or object write — by itself or on the operator's behalf. Committing workspace files is the operator's business; versioning of the scoped files is kanso's own, content-addressed in the state store. |
| D19 | Availability, not observation: every catalog point's `ts_init` is the instant its information became public and `ts_event` is its economic reference time; delayed-publication data is refused at write unless its `ts_init` comes from a declared publication rule (§8.9, §9). |

### 3.2 Constraints (C)
| id | constraint |
|---|---|
| C1 | Python ≥ 3.12; `uv`. |
| C2 | `nautilus_trader>=1.231.0,<1.232`, one requirement for every host. The installed wheel MUST be compatible with the host: its platform tag's minimum OS version ≤ the host's and its architecture equal to the host's — a newer host running an older tag is correct, equality is not required. `doctor` verifies. |
| C3 | Runtime dependencies are exhaustive: `nautilus_trader`, `pydantic>=2`, `typer`, `pyyaml`, `httpx`, `numpy`; no optional dependencies. The adapters of §5.1 add none: they use `nautilus_pyo3.HttpClient` / `WebSocketClient` (D15) and the standard library. There are no extras; an adapter is enabled by the presence of its credentials (§7.5), never by installation. An adapter that would require a new runtime dependency is not admissible in 0.1.0. `httpx` serves `models/` only. |
| C4 | Hosts: macOS 26+ arm64, Linux x86_64 (glibc ≥ 2.35). Earlier macOS is out of scope: the pinned engine publishes no wheel for it and would be built from source. |
| C5 | A workspace is a directory; it MAY live inside a git repository. kanso never invokes git; `doctor` detects an enclosing repository by the presence of a `.git` directory (a filesystem check) and only reports it; every write kanso performs is a plain file or `state.db` write (D18). |
| C6 | Apache-2.0. Package versions follow Semantic Versioning 2.0.0 (N5 maps change classes to levels; `criteria_version` = package version); conventional commits. |

### 3.3 Functional requirements (F) — the system MUST
| id | requirement |
|---|---|
| F1 | Register and validate hypotheses (§6.1, §8.1). |
| F2 | Classify each hypothesis as a construct of Appendix A, attach it to a host where the construct requires one, and set its objective parameters and constraints before research (§8.3.1). |
| F3 | Research a classified hypothesis indefinitely in isolated runs, one experiment per card, on research-window data only (§8.3). |
| F4 | Detect and revert drift from the thesis (§8.3.6). |
| F5 | Plan certification per hypothesis at runtime and run it on embargoed data (§8.4). |
| F6 | Compose certified hypotheses into versioned strategies whose generated implementation is the one class used everywhere (§8.5). |
| F7 | Deploy versions per stage under capital and risk limits; promote to live only on recorded approval; demote automatically (§8.6). |
| F8 | Monitor paper and live versions with the plan's paper- and live-stage gates (§8.7). |
| F9 | Replay catalog data through the engine for any version or `strategy_sha` over any range, in the live and research code paths, and compare them (§8.8). |
| F10 | Keep all data in one catalog with manifests and immutable snapshots; load from files and a synthetic generator; register custom types (§8.9). |
| F11 | Route every LLM call through the register by task class and ledger its spend (§7.4, §9). |
| F12 | Detect the host envelope and derive the lane plan (§8.10). |
| F13 | Keep all state in the workspace behind one store with versioned migrations (§6.12). |
| F14 | Escalate through an append-only inbox and an optional webhook (§8.11). |
| F15 | Ship operator skills and templates in the package and link them into any agent's skills directory (§5.2). |
| F16 | Discover workspace extensions and support moving them upstream (§5.5). |

### 3.4 Non-functional requirements (N)
| id | requirement |
|---|---|
| N1 | Determinism: same snapshot and configuration → byte-identical card metrics; every automatic decision reproducible from recorded inputs. |
| N2 | Parity: identical strategy class, data types and evaluation path across backtest, paper, live; versions pin the engine version they were certified under. |
| N3 | Token consciousness per §9; no caps. |
| N4 | Coverage ≥ 85% of lines in `src/kanso`; `ruff` and `mypy --strict` clean. |
| N5 | Migration-required changes are minor releases; incompatible changes are major; `doctor` guards schema mismatches. |
| N6 | Observability: `status`, JSON-lines logs per lane, `--json` on every command; no UI. |
| N7 | Secrets only in environment variables, resolved by standard name from the workspace `.env` (gitignored by the template) and then the ambient environment (§7.5); never in any file kanso writes, nor in logs, prompts, certificates or `doctor --report`. A credential MUST be sent in a request header, never in a URL query string or path, because URLs reach logs, manifests and `doctor --report`. |
| N8 | Portability across operator agents (Agent Skills format), providers (two protocols plus mock) and hosts (C4). |
| N9 | State and catalog handle millions of rows without configuration (D17). |

## 4. System context
| actor / system | role | interface |
|---|---|---|
| operator | states theses, approves live capital, answers escalations | chat via the operator agent, or the CLI directly |
| operator agent | any coding or chat agent (out of scope) | shipped skills; CLI with `--json` |
| kanso CLI and daemon | every operation; the daemon runs research lanes and the monitor | `kanso …` (§7.1) |
| NautilusTrader | backtest engine, sandbox execution, live node, data catalog | `kanso.nautilus` (§7.3) |
| LLM providers | classify, propose, plan certification, check alignment | register and clients (§7.4) |
| data sources | CSV/Parquet files, synthetic generator, first-party vendor adapters (§5.1), vendor extensions | `Adapter`, `Loader`, `InstrumentProvider` (§7.3) |
| brokers | paper and live execution behind an exec client id | `nautilus/adapters/<broker>/` (§6.7, §7.3) |
| host machine | CPU, memory, power, OS | §8.10 |

kanso owns workspace state, the research protocol, criteria and plans, composition, portfolio configuration, sessions and LLM routing. It does not own the operator agent, the engine, data provenance, broker connectivity or any UI. Deployment is a single host: short-lived CLI processes; one daemon with a subprocess per lane plus the monitor loop; a subprocess per card; one process per deployed stage node and per replay session. On macOS the daemon runs under `caffeinate -i`.

## 5. Architecture

### 5.1 Package (`src/kanso/`)
| module | responsibility |
|---|---|
| `cli/` | typer application; `--json`, `--workspace`; exit codes §7.2 |
| `schemas/` | pydantic v2 models for §6 |
| `hyp/` | registry, scaffolding, validation |
| `classify/` | construct catalogue `classify/constructs/*.yaml` + impl; classification; objective and constraint selection |
| `research/` | runner, driver, scheduler, lanes, alignment, results rendering |
| `criteria/` | objectives, gate toolbox `criteria/library/*.yaml`, applicability, plan validation |
| `certify/`, `strategy/`, `portfolio/`, `monitor/`, `replay/` | §8.4–§8.8 |
| `nautilus/` | engine binding: catalog, backtest runner, strategy API, `ReplayDataClient`, sandbox, nodes, sessions |
| `data/` | `Adapter` registry, `Loader`, `InstrumentProvider`, reference loaders, instrument resolution, tick and lot conventions, publication rules, manifests, snapshots, custom data types |
| `data/adapters/{massive,edgar,finra}/` | per vendor: client (`HttpClient`, header auth, quotas), reference resolution, bulk and incremental loaders, vendor-introduced custom types, static calendars, entitlement and history-floor probing |
| `data/adapters/<vendor>/objectstore.py` | where a vendor serves bulk history from an object store: request signing (standard library only, with an injectable clock so the published signature vectors are an offline oracle), listing pagination, ranged and streaming object GET through `nautilus_pyo3.http_download`. No new runtime dependency (C3) |
| `nautilus/adapters/alpaca/` | `config`, `factory`, `LiveExecutionClient`, `LiveMarketDataClient` (the *default* live feed for a stage whose execution client runs on a wall clock, since the broker that fills the order is the least surprising source of the price that triggered it; any adapter declaring a live data client may replace it), the venue model it declares (account, currency, costs) for the venues it serves, provider, `parsing` (every pure mapping, so tests need no network) |
| `models/` | register, router, clients (`anthropic`, `openai_compat`, `mock`), spend ledger |
| `env/`, `state/`, `inbox/` | envelope; `StateStore` + SQLite + `migrations/`; escalations and webhook |
| `skills/<name>/SKILL.md`, `templates/` | shipped operator skills (Agent Skills format); workspace templates, `templates/demo/` |
`learn/` is reserved (§12).

### 5.2 Workspace
| path | content | written by |
|---|---|---|
| `kanso.toml` | configuration (Appendix C) | `init`, operator |
| `AGENTS.md`, `CLAUDE.md` | operator-agent instructions; `CLAUDE.md` = `@AGENTS.md` | `init` |
| `<skills target>/kanso-*` | links to the package's skills, one per `[skills] targets` entry | `skills sync` |
| `.gitignore`, `.env` | the Appendix C entries, created or appended whether or not a repository encloses the workspace; `KEY=VALUE` credentials — LLM providers, vendors and brokers under the §7.5 scheme — read at each use and never written by kanso | `init`; operator |
| `envelope.yaml`, `models.yaml`, `portfolio.yaml` | §6.8, §6.9, §6.7 | `env detect`; operator; kanso |
| `instruments.yaml` | §6.10: the resolved-instrument cache, provenance and operator overrides | `data instruments resolve`; operator (`override`, `attributes`, `corporate_actions`, `manual`) |
| `hypotheses/<id>/` | `hypothesis.yaml`, `program.md`, `strategy.py` (the best-so-far: rewritten after every keep and every re-point of `best`; kanso-owned once `best` exists), `results.tsv` (rendered from state; gitignored by the template) | `hyp new`, `classify`, research |
| `strategies/<id>/` | `strategy.yaml`, `impl/<version>/` | `strat compose` |
| `certificates/<hyp>/` | `plan.yaml`, the certificates named in §6.5, `<sha7>.py` (the certified `strategy.py` bytes, so certified subjects travel with the files) | `cert plan`, `cert run` |
| `catalog/` | Nautilus `ParquetDataCatalog`, including the instrument definitions a snapshot pins; `manifests/<dataset_id>.yaml`; `snapshots/<snapshot_id>.yaml`; `.cache/` for adapter downloads | `data load`, `data sync`, `data snapshot`, `data instruments resolve` |
| `sessions/<session_id>/` | persisted node streams and `session.yaml` | nodes, replay |
| `state.db` | SQLite (WAL); the only write path is `StateStore` | kanso |
| `escalations/inbox.md` | append-only | kanso |
| `runs/<lane>/` | `<hyp>/` lane directory of the active run (exactly `hypothesis.yaml`, `program.md`, `strategy.py`); `<hyp>-<tag>.jsonl` logs beside it, never removed by `research end` | research |
| `kanso_ext/` | optional extensions | operator |

### 5.3 Concurrency
One active run per hypothesis. `lanes` daemon lanes plus the interactive lane `op`, each with its own lane directory; lanes never share files. Each card runs in a subprocess with cwd = the lane directory and no catalog environment. Each stage node and replay session is its own process. `state.db` has one writer per process; the daemon serialises scheduler writes.

### 5.4 Files, versioning and git
kanso is a package (D18). It scaffolds the workspace, writes its own files and `state.db`, and never invokes git: no `git init`, commit, branch, tag, worktree, reset or object write, and no read either. `init` always writes the workspace `.gitignore` (creating it or appending the Appendix C entries); `doctor` detects an enclosing repository by the presence of a `.git` directory. Whether and when workspace files are committed is the operator's business. Versioning of the scoped files is kanso's own: `hyp add`/`classify` pin `hypothesis.yaml` by the sha256 of its bytes; `research begin` copies `hypothesis.yaml` and `program.md` into the lane directory `runs/<lane>/<hyp>/`, stores both as blobs (`hypothesis_sha`, `program_sha`), and takes `strategy.py` from the hypothesis's `best` blob if one exists, else from the workspace (`--from-workspace` forces the workspace copy and clears `best`); every card stores the lane's `strategy.py` as a blob under its `strategy_sha`; `results.tsv` rows, certificates and strategy versions reference that sha. After every keep and every re-point of `best`, kanso writes the `best` blob atomically (write, then rename) to `hypotheses/<id>/strategy.py`, so the workspace copy is always the best-so-far and is kanso-owned once `best` exists (`doctor` warns when its sha differs from `best`). Certificates carry the certified bytes beside them (`<sha7>.py`) and `impl/<version>/` holds verbatim copies, so certified subjects travel with the files even where `state.db` does not.

### 5.5 Extensions
Packages under `[extensions] paths` (*default* `kanso_ext`) are discovered at startup and MAY provide gates, objectives, constructs, loaders and custom data types through §7.3. `doctor` lists them and warns when one shadows a built-in id. Upstreaming is a copy into the framework checkout plus tests and a pull request (`docs/maintainers.md`, skill `kanso-upstream`); those git operations happen in the kanso source repository, are performed by the operator or their agent, and are outside D18, which governs the workspace only.

## 6. Data model
Fields are required unless marked `?`. YAML files carry `schema: 1` (except the id-keyed `instruments.yaml`). Durations match `^[0-9]+(s|m|h|d|w)$`.

### 6.1 `hypothesis.yaml` (*example values are the demo's*)
```yaml
schema: 1
id: demo_mr                  # ^[a-z0-9_]{3,40}$, unique
title: "Demo: mean reversion on a synthetic series"
thesis: "≤2 sentences, falsifiable."
mechanism: mean_reversion    # mean_reversion|momentum|microstructure|stat_arb|event|carry|vol|other
universe: [DEMO]             # ids from instruments.yaml; venues derive from their nautilus_id
horizon: 30m                 # holding period
resolution: 1m               # duration | tick | quote | trade
data_requirements: [bar]     # bar|quote|trade|<registered custom type id>
costs: ?                     # optional override of the venue's costs (§6.7); same shape, any subset
capital: ?                   # backtest starting balance; default [research] capital
risk_limits: {max_position_pct: 20, max_drawdown_pct: 15, max_leverage: 1}          # *_pct of capital
windows:
  research:      {start: 2024-01-02, end: 2024-12-31}
  certification: {start: 2025-01-06, end: 2025-05-30}   # start ≥ research.end + embargo
  forward:       {start: 2025-06-02}                      # no end; paper/live replay starts here
construct: ?                 # set by `classify`: {id: <construct id>, host?: strategy id, params?, rationale: ≤240}
objective: ?                 # set by `classify`: {id, params: {min_delta, k_se}}
constraints: ?               # set by `classify`: card-stage gates [{id, params}]
```
Validation: `embargo = max(5 × horizon, 1d)`; windows ordered and non-overlapping; every universe id resolves to a Nautilus instrument as of `windows.research.start` — from `instruments.yaml` when its entry is `manual`, else through the configured reference adapter, whose result is cached in `instruments.yaml` and written to the catalog's instrument store; an id that is unknown, ambiguous across venues, delisted before or listed after that date fails validation (exit 3) naming the id and the reason; when set, `construct.id` exists in the catalogue, `host` is present iff the construct needs one and names a certified strategy, `objective` is applicable and its params are inside ranges, every constraint is a card-stage gate with params inside its ranges, and `strategy_integrity` is present; at `research begin`, `data_requirements` ⊆ types in the pinned snapshot for the universe. Status, pins and the hypothesis-level `best` (§6.12) live in the state store.

### 6.2 run record (state)
`{run_id, hyp_id, tag, lane, dir, base_sha, hypothesis_sha, program_sha, snapshot_id, criteria_version, host_version?, card_budget_s, baseline_wall_s, baseline_peak_mem_gb, best_sha?, best_metric?, started_at, ended_at?}`

### 6.3 card
`results.tsv` row (tab-separated, header, append-only, rendered from state): `sha7 · metric(6dp; 0 on crash) · metric_se · n_trials · n_trades · wall_s · peak_mem_gb · status(keep|discard|crash) · desc(≤120)`. State also holds `run_id, lane, strategy_sha, aligned, gate_results, crash_tail?, venue_model, created_at`; the bytes of every card's `strategy.py` and of the run's pinned `hypothesis.yaml` and `program.md` are blobs (§2). Commands taking a sha accept any unique prefix of a sha that belongs to the named hypothesis (ambiguous or foreign → exit 3).

### 6.4 certification plan (`certificates/<hyp>/plan.yaml`)
`{hyp_id, plan_version: int, planned_at, planned_by: <model id>, inputs: {hypothesis_sha, construct, data_availability: {types, spans}, n_trials}, gates: [{id, stage: cert|paper|live, params, rationale: ≤200}], excluded: [{id, reason: ≤200}]}`

### 6.5 certificate (`certificates/<hyp>/<sha7>-<n_trials>-p<plan_version>-e<nautilus_version>.yaml`, immutable; the certified `strategy.py` bytes are written beside it as `<sha7>.py`)
`{hyp_id, strategy_sha, nautilus_version, venue_model, snapshot_id, criteria_version, plan_version, construct, objective: {id, value, se}, gates: [{id, stage, params, evidence, pass, skipped?: reason}], n_trials, verdict: pass|fail, created_at}`

### 6.6 `strategy.yaml`
`{id, versions: [{version: int, sleeve: {hyp_id, strategy_sha}, attached: [{hyp_id, strategy_sha, construct, params?}], config, pins: {kanso_version, nautilus_version, criteria_version, plan_version, snapshot_id, venue_model}, expectation: {objective_id, value, ci90: [lo, hi], mdd_p95, window}, state, created_at}]}`. `pins` record what the version was certified under.

### 6.7 `portfolio.yaml`
```yaml
schema: 1
stages:
  paper: {exec: sandbox, data: replay, speed: 1, capital: 100000, kill_switch: false, strategies: [{id, version, capital, joined_at}]}
  live:  {exec: sandbox, data: replay, speed: 1, capital: 0,      kill_switch: false, strategies: []}
limits: {max_gross_pct: 100, max_net_pct: 100, per_strategy_max_pct: 40, daily_loss_pct: 3}
venues: ?                        # optional per-venue overrides, e.g. XNAS: {account: margin, currency: USD, costs: {...}}
```
`exec` names an execution client: `sandbox` (simulated fills), `alpaca_paper` (broker paper), `alpaca` (real capital); `data` names a data client: `replay` (*default*, catalog replay), `massive`, or any adapter declaring a live data client. Each execution client declares `capital: simulated|broker_paper|real` and `clock: replay|wall`; a client declaring `capital: real` MAY be configured only on the `live` stage, and moving a deployed version onto one requires `promote --live --as NAME` (D13, §8.6). A stage whose execution client declares `clock: wall` MUST pair it with a live data client and `speed: 1`; `deploy` refuses otherwise (exit 2), because a historical replay feeding a broker that matches against current prices fills at unrelated prices. On a broker execution client the stage `capital` is a reconciliation target, not an authority. `*_pct` are percentages of the stage `capital`; `speed` applies to replay data only (`0` = as fast as possible).

**Venues carry the trading model, and a broker defines it.** Every venue kanso simulates or trades has an account type, an account currency and a cost model, and all three are inherited rather than invented: the broker behind the configured execution client declares them for the venues it serves, so a backtest is costed the way the account that would trade it is costed. `[research] broker` (Appendix C, *default* `alpaca`) names the broker research inherits from; a stage inherits from its own `exec` client. `venues.<MIC>` overrides any field for one venue, and a hypothesis's optional `costs` overrides the cost model for that hypothesis alone — the path for stressing an idea against costs worse than the broker's. Shipped defaults where a broker declares nothing: `account: margin` with `default_leverage = risk_limits.max_leverage`, so `max_leverage: 1` still forbids borrowing while short sales remain possible (a cash account rejects every short, which no reasonable default should do); `currency: USD`; costs `{commission_bps: 0, slippage_bps: 1.0, spread: quotes}` when quotes are available and `fixed_bps` otherwise. The resolved model — broker, account, currency, costs, with each field's origin — is recorded on every card, certificate and strategy version, so a number is always traceable to the venue that produced it. A universe whose resolved instruments span more than one currency is refused at `hyp validate` (exit 3) rather than silently funded in one; multi-currency accounts are §12.

Costs are applied once, by the runner, in the single extraction that feeds cards, certification gates, `expectation` and the realised paper and live objectives, rather than inside the simulated venue. One application means one number, identical across backtest, replay and broker paper, so `cost_stress` recomputes from recorded fills without re-running a backtest, two strategies with different cost models can share a stage node, and `fill_quality_drift` measures exactly the gap between the modelled cost and the broker's realised fill.

### 6.8 `envelope.yaml` (generated)
`{schema: 1, detected: {os, os_version, arch, chip, cores_perf, cores_eff, cores_total, mem_gb, disk_free_gb, on_ac_power, python, nautilus_version, nautilus_wheel_ok}, plan: {live_colocated, reserved_cores, reserved_mem_gb, cores_per_lane, mem_per_lane_gb, lanes}, detected_at}`

### 6.9 `models.yaml`
`models: [{id, provider, protocol: anthropic|openai_compat|mock, base_url?, api_key_env?, tier: cheap|mid|frontier | [..], local, ctx, cost_in, cost_out, tools, script?}]` (`api_key_env` overrides the standard credential name `KANSO_<PROVIDER>_API_KEY` of §7.5; it is never a value) (`tier` MAY list several tiers); `routing: {<task class>: {tier, effort: none|low|medium|high, max_output?: int}}` for §7.4; an absent entry or field takes that class's §7.4 default. Every tier MUST have at least one model (checked at `classify`, `research begin`, `cert plan`).

### 6.10 `instruments.yaml`
`{<id>: {nautilus_id: "<SYMBOL>.<VENUE>", asset_class, resolved?: {adapter, as_of, at, checksum}, override?: {<Instrument field>: value}, manual?: bool, corporate_actions: adjust_all|none, attributes?, sources?: {<vendor>: <symbol>}}}`.

The file is a cache with operator overrides, written by `data instruments resolve` and edited only for `override`, `attributes`, `corporate_actions` and `manual`. The registry of record is the catalog's instrument store; this file is its human-readable pin. `manual: true` suppresses resolution and MUST carry the full constructor field set for the asset class — the path the file loaders, the synthetic loader and `init --demo` use, so a workspace needs no vendor. `override` is applied after resolution and before construction; `doctor` lists every override and every manual entry. The venue part of `nautilus_id` is the canonical venue for every stage — backtest, sandbox, broker paper and live all use it, and a broker is never a venue, since a forked `InstrumentId` would break positions, risk checks, reconciliation and D8. Tick and lot defaults come from kanso's dated convention table (`data/conventions.py`, keyed by asset class, venue, price and date), never from a vendor: no vendor publishes them and a US equity tick size becomes a dated per-security fact. `attributes` is a free-form map strategies and gates MAY read; `sources` maps a vendor or broker id to its symbol for that instrument and is written by the adapter that resolved it. Resolved definitions are content-addressed into the snapshot (§8.9), so a run reproduces the instruments it was pinned to and a later tick-size reassignment never rewrites a completed card.

### 6.11 catalogue items
**Construct** (`classify/constructs/*.yaml`, Appendix A):
```yaml
id: filter
description: "conditioning rule that gates a host's entries by time, regime or instrument"
needs_host: sleeve            # none | sleeve | portfolio
objective_mode: relative      # absolute | relative
params?: {scope: [time, instrument]}
runnable: true                # false = classification only in 0.1.0 (§12)
impl: kanso.classify.constructs.filter   # harness(hyp, host?), compose(strategy, hyp_id, strategy_sha, params), host_run (relative)
```
**Criteria item** (`criteria/library/*.yaml`, Appendix B):
```yaml
id: embargoed_window
kind: gate                    # objective|gate
stage: cert                   # gates: card|cert|paper|live
required: true                # gates: structural invariant, every plan includes it
applies:                      # objectives only: selection predicate over mechanism, objective_mode, horizon, resolution, universe_size, data_requirements, history_days (min inclusive, max exclusive)
priority: 10                  # objectives only: lowest applicable wins
meaningful_when: "≤200 chars for the planner: what the test assumes and when it is informative"
params: {min_fraction: float, length: duration}                 # names and types only — values are chosen by the agent
ranges: {min_fraction: [0.0, 1.0], length: ["1 * horizon", "100 * horizon"]}   # expressions "<number> * <attr>", attr ∈ {horizon, resolution, history_days, folds}
impl: kanso.criteria.gates.embargoed_window
```
`history_days` = research window length in days; `folds` = `[research] folds`. Gates carry no `applies` and no default values: which gates and which values is the planner's decision (§8.4). A gate lacking its context (no numeric params, no deployed strategies) evaluates `pass` with `skipped: <reason>`. Relative constructs: items referring to the objective use the relative objective (§8.3.4); items referring to trades, positions, drawdown or returns use the combined run; `book_correlation` excludes the host.

### 6.12 session and state
`sessions/<id>/session.yaml`: `{session_id, mode: node|engine|paper|live, target, instruments, from, to, speed, exec, started_at, ended_at?}` plus the persisted stream. `StateStore` tables: hypotheses (status, pins, `best_sha?`, `best_metric?`, `best_run_id?`), runs, cards, blobs (content-addressed bytes of scoped files), plans, certificates, strategies, strategy_versions, approvals, escalations, spend, snapshots, sessions, events. SQLite in WAL mode; migrations `NNNN_name.sql` applied by `migrate`; `schema_version` in `kanso.toml`; `doctor` refuses a workspace newer than the package.

## 7. Interfaces

### 7.1 Command-line interface
Global: `--json` on every command; `--workspace PATH` (*default* cwd). `ID` is a hypothesis id; `STRATEGY[@V]` a strategy id with optional version; `show` without an argument lists.

| command | effect |
|---|---|
| `kanso init [DIR] [--demo]` | scaffold a workspace directory (§5.4; kanso never invokes git); write `.gitignore` (create or append the Appendix C entries); `skills sync`; `env detect`; `--demo` renders a mock-only register (mock listed for all tiers), `demo.yaml`, `instruments.yaml` with the synthetic `DEMO`, and `hypotheses/demo_mr/` |
| `kanso doctor [--report]` | versions; install mode (`editable`/`package`, path); engine wheel vs OS; schema version; envelope freshness; repository (filesystem check: does a `.git` directory enclose the workspace? are the Appendix C `.gitignore` entries present?); workspace `strategy.py` vs `best`; certificates and versions whose `strategy_sha` has neither blob nor `.py` file; skills links; credentials (per required variable: its standard name, and whether it resolved from `.env`, from the environment, or nowhere — never the value); adapters (which are configured — credential variables present, never values — their capabilities and quotas); instrument registry (every universe id resolvable, overrides and manual entries listed, drift between the resolved definitions and those the newest snapshot pins); lane directory health; extensions and shadowing; `--report` prints a redacted block for upstream issues. `doctor` makes no network call unless `--check-adapters` is passed |
| `kanso migrate` | apply pending migrations |
| `kanso skills sync` | link the package's skills into `[skills] targets` |
| `kanso env detect` | write and print the envelope |
| `kanso models check` | print the register; one minimal call per model with latency and status |
| `kanso data load --loader ID --spec FILE` · `kanso data show` · `kanso data snapshot` | run a loader over the range its spec names; list datasets, their served spans and the gaps between them; freeze a snapshot |
| `kanso data backfill --loader ID --spec FILE [--from DATE] [--to DATE] [--dry-run]` | fill history for the spec's universe and types, from the source's history floor (or `--from`) up to the earliest data already held (or `--to`), and close any gap inside an existing span; chunked, checkpointed and idempotent, so an interrupt resumes and a repeat fetches only what is missing; prefers the adapter's bulk transport where it declares one; clamps to the history floor and reports the clamp rather than failing; `--dry-run` prints the chunks, the request or object count and the estimated bytes without fetching |
| `kanso data sync [--loader ID] [--dataset D] [--to DATE]` | incremental load from each manifest's served `span.end` to `--to` (*default* now) into a successor dataset (`supersedes`); same chunking, checkpointing and idempotence as `backfill`; never mutates a dataset a pinned snapshot references |
| `kanso data instruments resolve [ID…] [--as-of DATE] [--refresh]` · `kanso data instruments show [ID]` | resolve universe ids through the configured reference adapter, write the definitions to the catalog's instrument store and the cache to `instruments.yaml`; show a resolved definition or list them. `--refresh` is refused (exit 2) while a run is active or while the instrument is referenced by a snapshot a deployed version depends on |
| `kanso data adapters [--check]` | id, kind (`data`\|`reference`\|`exec`), whether its credentials resolve, declared capabilities and quota; `--check` makes one minimal authenticated request per configured adapter. Without `--check` the command performs no network I/O |
| `kanso hyp new ID` · `kanso hyp validate PATH` · `kanso hyp add PATH` · `kanso hyp show [ID]` · `kanso hyp retire ID` | scaffold; validate; register or re-pin (§8.1); show or list; retire |
| `kanso classify ID` | construct, host, objective params, constraints → `hypothesis.yaml` (pinned); render `strategy.py` from the construct's stub iff the file's sha256 equals a rendered stub's |
| `kanso research begin ID [--tag T] [--from-workspace]` · `kanso research card ID --desc TEXT` · `kanso research run ID [--cards N]` · `kanso research end ID` · `kanso research show ID [--sha S] [--diff S2]` | interactive lane `op`: start a run (lane directory, copies, pins, baseline; prints the lane directory path; `--from-workspace` starts from the workspace `strategy.py` and clears the hypothesis's `best`); evaluate the lane directory's `strategy.py`; autonomous driver (`begin` if needed, then propose → card until `N` or forever); end the run (cards, blobs and `best` remain in state; only the lane directory is removed); print a card's stored `strategy.py` (*default* `best`) or the unified diff between two blobs |
| `kanso research start` · `kanso research stop` · `kanso research status` | daemon with `lanes` workers over the queue; `stop` leaves active runs in place and `start` resumes them first; `status` shows lanes, runs (with `lane_sha`, `best_sha`, `base_sha`) and the queue |
| `kanso research queue add ID [--priority P]` | enqueue (`P` descending, then FIFO) |
| `kanso align check ID` | alignment check now (§8.3.6) |
| `kanso cert plan ID [--replan]` · `kanso cert run ID [--sha S]` · `kanso cert show ID` | create or replace the plan; plan if absent then run cert gates; show the latest certificate |
| `kanso strat compose ID` · `kanso strat show [STRATEGY[@V]]` · `kanso strat retire STRATEGY[@V]` | compose; show or list; retire |
| `kanso portfolio show` · `kanso portfolio deploy --stage paper\|live` | show; validate limits, render the node configuration, (re)start the stage node |
| `kanso promote STRATEGY[@V] --live --as NAME` · `kanso demote STRATEGY[@V]` | approval (D13; exit 4 without `--as`) and stage move, then redeploy both stages |
| `kanso replay run (--strategy STRATEGY[@V] \| --hyp ID [--sha S]) [--from D] [--to D] [--speed N] [--mode node\|engine]` · `kanso replay parity (…)` · `kanso replay show [SESSION]` | §8.8; show a session or list them |
| `kanso monitor run` | one pass of the paper/live gate loop (the daemon runs it continuously) |
| `kanso inbox [ack ID]` · `kanso status` | list unread escalations / acknowledge (never an approval); lanes, cards per hour, best metric per hypothesis, spend today, unread escalations, `baseline_failed` hypotheses |
| `kanso ext show` | list extensions and the built-in ids they shadow |

### 7.2 Exit codes
`0` success · `1` error · `2` precondition failed · `3` validation failed · `4` approval missing.

### 7.3 Python interfaces (`kanso.*`, stable within a major version)
| interface | contract |
|---|---|
| `kanso.nautilus.strategy.KansoConfig` | subclass of the Nautilus `StrategyConfig`; numeric fields are the parameters `param_plateau` perturbs |
| `kanso.nautilus.strategy.KansoStrategy` | the sleeve class: subclass of the Nautilus `Strategy`; class attribute `config_cls`; universe, resolution and costs injected from the hypothesis; cost-aware order helpers; hooks consulted for attached constructs — `before_entry(ctx) -> bool` (filters), `size(ctx, qty) -> qty` (overlay scaling), `before_exit(ctx) -> bool` (exit rules), `hedges(ctx) -> [order]` (overlay hedge legs) |
| `kanso.nautilus.strategy.KansoModifier`, `Decision` | the attached-construct class: subclass of the Nautilus `Actor`; `construct`, `config_cls`, `evaluate(ctx) -> Decision`; `Decision` fields per construct: `filter → allow: bool`; `overlay → scale: float ∈ [0, 1], hedges?: [{instrument, qty}]`; `exit → exit: bool`; `Decision.neutral(construct)` |
| `kanso.classify.Construct` | `harness(hyp, host?) -> Runnable`, `compose(strategy, hyp_id, strategy_sha, params) -> StrategyVersion`, `host_run(...)` for relative constructs |
| `kanso.criteria.Objective`, `Gate` | `compute(run: CardRun, folds: int) -> (metric, se)`; `evaluate(ctx: GateContext) -> GateResult{pass, evidence, skipped?}`; `CardRun` = per-period net returns, trades with fills, positions, equity, extracted by the runner |
| `kanso.data.Loader`, `register_custom_type` | `discover(spec) -> list[DatasetRef]`, `load(ref, window) -> Iterable[Data]`, optional `load_arrow(ref, window) -> Iterator[RecordBatch]` (the catalog writer prefers it: measured an order of magnitude faster than the per-object path), `manifest(ref) -> Manifest`; every yielded point satisfies the availability invariant of §8.9; `register_custom_type(type_id, py_class, arrow_schema)` |
| `kanso.data.Adapter` | `id`, `kind: data|reference|exec`, `capabilities` (datasets offered, entitlement per class, history floor per class, whether a bulk transport is available), `credentials` (variable names, never values), `client()`; the registry's only entry point, identical for package adapters and `kanso_ext/` (D14) |
| `kanso.data.InstrumentProvider` | `resolve(ids, as_of) -> dict[str, Instrument | ResolveError]`, `sources(id)`; synchronous and venue-discovering, unlike the engine's own provider, which requires a fully qualified `InstrumentId` |
| `kanso.state.StateStore` | §6.12 |
| `kanso.learn.Learner` | reserved (§12) |

### 7.4 LLM task classes
Every call goes through the router with the class's tier, thinking effort and output cap (*defaults* below), requests structured JSON, retries once on malformed or invalid output with the validation errors, escalates one tier once, then fails the calling step (exit 2). No task class has a non-LLM fallback: without a configured model (or the mock) the step refuses. Prompts MUST NOT contain secrets. `classify` and `certify_plan` prompts MUST NOT contain card metrics, certificates or `strategy.py`. Diffs are unified diffs between two blobs, computed in-package with the standard library; the driver applies a `propose` diff to the lane copy in-package (never with git); a diff that does not apply cleanly, or touches anything but `strategy.py`, is invalid output and takes the retry path above.

| task class | inputs (nothing else) | output | routing: tier · thinking · max output | skipped when |
|---|---|---|---|---|
| `classify` | hypothesis; construct catalogue (id, description, `needs_host`, params, runnable); certified strategies' specs; objective catalogue with applicability results and ranges; card-stage gate catalogue (id, `meaningful_when`, params, ranges, required) | `{construct: {id, host?, params?}, objective_params: {min_delta, k_se}, constraints: [{id, params}], rationale ≤240}` | frontier · high · 1024 | never |
| `propose` | stable prefix: `program.md`, `hypothesis.yaml`, objective definition; dynamic: current `strategy.py`, last `context_cards` cards, last diff, crash tail ≤ 50 lines, failing cert gates ≤ 10 lines | `{desc ≤120, diff}` touching only `strategy.py` | mid · medium · 4096 | never |
| `align_check` | thesis, mechanism, universe, horizon, current `strategy.py`, diffs since last check | `{aligned, reason ≤200}` | cheap · none · 256 | deterministic pre-checks fail |
| `certify_plan` | hypothesis; toolbox catalogue (id, stage, `meaningful_when`, params, ranges, required); data availability; construct; `n_trials`; invariants | plan (§6.4) | frontier · high · 4096 | a valid plan exists |

The routing defaults are the token-consciousness policy of D4 made concrete, and each is chosen from how often the class is called against what a wrong answer costs. `classify` runs once per hypothesis and its answer directs every run that follows, so it takes the best model at full thinking: the cheapest possible place to spend the most. `certify_plan` is the same shape — once per hypothesis and per `--replan` — and decides what counts as proof, so it is not economised either. `propose` runs on every card and therefore dominates lifetime spend, so it takes a mid tier with moderate thinking and pays for its quality in context discipline instead: a byte-stable prefix, diffs rather than files, the last `context_cards` cards only. `align_check` is a yes-or-no with a short reason, reached only after the deterministic checks already passed, so thinking is pure waste there and the default is none. Each class caps its own output, because every output is a small structured object and an uncapped one is a runaway. A model or protocol with no thinking control ignores the effort; a class routed to `none` MUST NOT be sent a thinking budget at all.

### 7.5 Files, environment and credentials
The workspace `.env` holds `KEY=VALUE` lines (blank lines and `#` comments ignored, an optional `export ` prefix accepted, matching quotes stripped). kanso never writes it and the template gitignores it.

Every credential kanso needs has a **standard variable name** `KANSO_<SUBJECT>_<PURPOSE>`, where `<SUBJECT>` is the id under which its consumer is configured — a model's `provider` (§6.9), a data or execution client id (§6.7), a loader id (§8.9) — upper-cased with every non-alphanumeric character replaced by `_`, and `<PURPOSE>` is `API_KEY` unless the consumer declares others (`API_SECRET`, `ACCESS_KEY_ID`, …). A consumer MAY override the name (`api_key_env` in §6.9, the equivalent field in a client or loader spec); an override replaces the standard name rather than adding to it. `KANSO_WEBHOOK_URL` below is an instance of the scheme.

kanso resolves a name at the moment of use: the workspace `.env` first, then the process environment (where a shell profile's `export`s arrive — kanso never reads or executes shell files); the first non-empty value wins; nothing is injected into the process environment. A required credential that resolves in neither place fails its step with exit 2, naming the variable and both places searched. Resolved values reach only the processes that need them (stage and replay nodes, `data load`); card subprocesses are started with a scrubbed environment (§9).

Inbox entry: `- [ ] <id> <ts> <kind> <hyp|strategy> — <summary ≤200> · actions: …`. Webhook: JSON POST of each entry to `[webhook] url`, else `KANSO_WEBHOOK_URL`.

## 8. Behaviour

### 8.1 Hypothesis lifecycle
`draft → classified → researching → candidate → certified | failed | retired`

| transition | trigger | precondition |
|---|---|---|
| ∅ → draft | `hyp add` | §6.1 validation. On an already registered id (no active run, else exit 2), `hyp add` re-pins and keeps the status iff `construct`/`objective`/`constraints` still validate, else resets to `draft`; a change of `universe`, `resolution` or `data_requirements` clears the hypothesis's `best` (event `best_cleared`) |
| draft → classified | `classify` | no active run; construct, objective params and constraints written and pinned; a change of `construct.id` clears `best` (event `best_cleared`); a non-runnable construct is recorded here and refused at `research begin` with the §12 seam named |
| classified / candidate / researching / certified → researching | `research begin` or scheduler | no active run; lane free; snapshot pinned; construct runnable; the sha256 of the workspace `hypothesis.yaml` equals the registered pin (else exit 2: `hyp validate`, `hyp add`) |
| researching → candidate | stall with ≥ 1 keep (§8.3.5) or `cert run` | `best` exists and differs from the latest certificate's `strategy_sha` |
| candidate → certified | verdict pass | plan exists; every non-skipped cert gate passes |
| candidate → researching | verdict fail | fewer than `n_fail` consecutive fails |
| candidate → failed | `n_fail` consecutive fails | escalation `cert_failed` |
| any → retired | `hyp retire` | no active run |

### 8.2 Strategy version lifecycle
`composed → paper → promotable → live → retired`. At most one version per strategy per stage; a replaced version becomes `retired`.

| transition | trigger | precondition |
|---|---|---|
| ∅ → composed | certification → `strat compose` (automatic) | per the construct's `compose`: a new strategy v1 (sleeve) or the host's version n+1 (attached construct) |
| composed → paper | `portfolio deploy --stage paper` (automatic) | replaces the strategy's previous paper version (inheriting capital) or receives capital within limits, else escalation `deploy_blocked` |
| paper → promotable | all paper-stage gates of the plan pass (monitor) | escalation `promotable` |
| promotable → live | `promote --live --as NAME` | approval recorded; live capital assignable; engine pin matches |
| live → paper | any live-stage gate fails (monitor) or `demote` | escalation `demoted` |
| any → retired | `strat retire` or replacement | — |

### 8.3 Research protocol

#### 8.3.1 Classification
`classify` computes deterministic features (universe overlap, horizon and resolution match, mechanism, which constructs can attach given the certified strategies), evaluates objective applicability, and calls the `classify` task class (§7.4). kanso validates the result (§6.1) and writes `construct`, `objective` (id from applicability, params from the call) and `constraints` (the call's choice, which MUST include `strategy_integrity`) into `hypothesis.yaml`. The objective set is total over `{mechanism} × {objective_mode} × {horizon < 1d, ≥ 1d}`. Operator override: edit, `hyp validate`, `hyp add`.

#### 8.3.2 Run setup
Tag `<yyyymmdd>-<n>`; lane directory per §5.4: `hypothesis.yaml` and `program.md` copied from the workspace and stored as blobs (`hypothesis_sha`, `program_sha`), `strategy.py` from the hypothesis's `best` blob if present and `--from-workspace` is absent, else from the workspace (`base_sha` = its `strategy_sha`); pin per §6.2 (constructs with a host: `host_version` = latest certified host version); scope is `hypothesis.yaml`, `program.md`, `strategy.py`. Baseline card on the unmodified `strategy.py` under `[research] baseline_budget_s` with memory uncapped: sets `card_budget_s = max(60, 3 × baseline_wall_s)` and `baseline_peak_mem_gb`; status `keep` iff all constraints pass, else `discard`; `best` is unset until the first keep. Baseline timeout or exception → exit 2, no run record, lane directory removed.

#### 8.3.3 Card
(1) store the lane directory's `strategy.py` as a blob (`strategy_sha`); the lane directory MUST hold exactly the three scoped files (transient artefacts such as `__pycache__` and dot-files excepted) with `hypothesis.yaml` and `program.md` equal to the blobs `hypothesis_sha` and `program_sha`; (1a) evaluate the static half of `strategy_integrity` — the import and identifier rules and the scope rule — **before** anything runs: a failure is `discard` with no backtest, metric 0, and the lane copies restored from their blobs, so code that violates the embargo never executes; (2) run the backtest in a subprocess on research-window data of the pinned snapshot under `card_budget_s` and the lane memory cap — exceeding either kills the run → `crash`; an exception → `crash` with the traceback tail recorded; (3) any constraint fails → `discard`; (4) otherwise the keep rule; (5) record the card; `keep` → the run's and the hypothesis's `best_sha` = the card's sha, `best_metric` updated, the blob written atomically to `hypotheses/<id>/strategy.py`; `discard`/`crash` → restore the lane directory's `strategy.py` from the `best` blob, else from `base_sha`. `results.tsv` and every blob live in state, so history survives restores.

#### 8.3.4 Keep rule
Objective over `[research] folds` contiguous folds of the research window from one backtest; `metric = mean(folds)`, `metric_se = std(folds)/√folds`. Relative objectives: the construct's `host_run` folds are computed once per run (cached by snapshot and host version); each card's folds are differences. Keep ⇔ all constraints pass and (`best` unset or `metric − best_metric > max(min_delta, k_se × metric_se)`), both from `objective.params`; equal or worse → discard. A keep growing `strategy.py` by more than `[research] max_lines_per_keep` lines requires twice `k_se`.

#### 8.3.5 Stall and scheduling
`[research] stall_k` consecutive non-keeps end the run: if `best` exists and differs from the latest certificate's `strategy_sha` → `candidate` and `cert run`; else requeue at priority −1. Certificate fail → `researching`, requeued at priority −1, failing gates fed to `propose`. Certificate **pass** → `certified`, composed and deployed per §8.5 and §8.2, and requeued at priority −1 as well: only `failed` and `retired` leave the queue, because D3 makes research indefinite and a certificate is a milestone in a hypothesis's life rather than its end. Churn is bounded by the keep rule's noise floor, by the refusal to re-certify an unchanged `strategy_sha`, and by priority decay. The scheduler serves the queue by priority then FIFO; a `begin` failure requeues at priority −2 with event `baseline_failed`; `stop` leaves active runs and lane directories in place and `start` resumes them before new items; lane `op` never blocks daemon lanes.

#### 8.3.6 Alignment
Every `[research] align_every` cards: deterministic AST checks (universe ids, resolution constants, data types), then `align_check` only if they pass. Aligned → cards since the last check marked `aligned`. Drift → those cards marked not aligned; the lane directory's `strategy.py` restored from the last aligned keep of this run (else from `base_sha`), `best_sha` re-pointed there (else unset) and `best_metric` set to that card's metric (else unset), the re-pointed `best` written to `hypotheses/<id>/strategy.py`, event `drifted`, escalation `misaligned`, research continues.

#### 8.3.7 Interactive path and trial accounting
Any coding agent follows `program.md`: edit `strategy.py` in the printed lane directory, run `research card`; evaluation is identical to the driver's. `n_trials` = all cards of all runs of the hypothesis, including baselines and crashes; recorded on every card and certificate.

### 8.4 Certification

#### 8.4.1 Planning
On the first `cert run` (or `cert plan`), `certify_plan` returns the plan of §6.4 with every included gate's parameters chosen inside its ranges; kanso validates it and retries once with the errors; a second invalid plan fails the step (exit 2). There is no default plan. The plan is pinned; it changes only through `cert plan --replan`, which re-runs the planner on the same closed inputs and produces a new `plan_version`.

#### 8.4.2 Invariants (D16)
A valid plan has every id in the toolbox with the stated stage; every included gate's params inside `ranges`; every `required` gate; at least one cert gate evaluated on `windows.certification`; at least one paper gate; at least one live gate. Nothing else is mandated.

#### 8.4.3 Running
Cert gates of the plan run for `best` (or `--sha S`, a card of this hypothesis) on the data snapshot pinned by the run that produced the card, with `windows.certification` (and research where a gate needs both). Refused (exit 2) only when `strategy_sha`, `plan_version` **and** `nautilus_version` all equal an existing certificate's, and refused (exit 2) if the target file already exists, since a certificate is immutable. A certificate therefore records the engine it was certified under, and moving to a new engine range is a plain `cert run` on the same commit — no replan, no frontier planner call, and `deploy`'s engine-pin refusal has a path out of it. A snapshot containing a `publication: unknown` or a vendor-adjusted dataset produces a recorded `fail` verdict rather than an exit code, so it counts toward `n_fail` and reaches the operator through the normal escalation. Verdict `pass` ⇔ every non-skipped gate passes; the certificate is written with the certified `strategy.py` bytes beside it (`<sha7>.py`). `n_fail` consecutive fails → `failed` and escalation `cert_failed`.

### 8.5 Composition
On certification the construct's `compose` produces the version: a sleeve → a new strategy v1 `{sleeve: hyp@strategy_sha}`; an attached construct → the host's version n+1 with the hypothesis appended to `attached`. `impl/<version>/` is generated from the sleeve's `strategy.py` blob plus each attached construct's `KansoModifier`, whose decisions the sleeve consults through its hooks (§7.3). Composition runs the impl over the sleeve hypothesis's certification window and stores `expectation` (objective, bootstrap `ci90`, `mdd_p95`) and `pins`. The generated impl is the single class loaded by backtest, paper and live nodes (D8).

### 8.6 Deployment, promotion, demotion
`deploy --stage S` validates `limits`, renders the node configuration (strategies, capital, `RiskEngineConfig.max_notional_per_order` from `per_strategy_max_pct`), and (re)starts the stage node with the stage's `exec`, `data`, `speed`. Capital: a new version of a deployed strategy inherits its predecessor's; otherwise `min(per_strategy_max_pct × stage capital, unallocated)`, and `0` → escalation `deploy_blocked`. A stage keeps one **session clock** (its persisted replay position); restarts resume from it and a joining version records `joined_at`. `deploy` refuses (exit 2) a stage whose `kill_switch` is on, a version whose `pins.nautilus_version` differs from the installed engine, or a stage with no catalog data at or after `forward.start`. A node flattens — cancels open orders and closes positions — before every stop, so a stage always restarts flat; simulated execution keeps no position across a restart, so the alternative is losing positions silently, and each redeploy therefore realises its P&L into the record the paper and live gates read (stated in `paper_forward`'s `meaningful_when`, since it shortens the useful window).

**Kill switch.** `stages.<s>.kill_switch: true` halts a stage: its node cancels open orders, flattens and stops, and only the operator clears the flag. The flag is written by the operator or by the monitor — never by a gate, which is a pure evaluator in another process — and a running node observes it from the state store on its own timer, within one `[monitor] interval`, rather than instantaneously. A halted stage is left halted: `demote` still performs its state move but redeploys only stages whose switch is off, so the kill switch and the automatic demotion it triggers cannot deadlock each other, and the `demoted` entry names the halted stage and the command that resumes it.

`promote --live --as NAME` requires `promotable`, records `{strategy, version, operator, ts}`, moves the version to the live stage (retiring the previous live version) and redeploys both stages; `demote` is symmetric. Moving any version onto an execution client that declares `capital: real` requires such a record: `deploy --stage live` refuses (exit 4) an entry that has none, so editing `portfolio.yaml` by hand can never move real money. Agents pass `--as` only on explicit operator instruction; there is no environment fallback.

### 8.7 Monitoring
Every `[monitor] interval`, for each deployed version, run the paper- or live-stage gates of its sleeve hypothesis's plan with bands from the version's `expectation`. Paper: all pass → `promotable` and escalation. Live: any fail → `demote` and escalation `demoted`, except `daily_loss_kill`, which sets the stage `kill_switch` and escalates without demoting — halting the stage is the stronger action and demoting into a halted stage would achieve nothing.

The monitor is also where stage-level exposure is enforced, because it is the only component that sees every deployed version at once: each pass computes gross and net exposure per stage from the persisted positions and compares them with `limits.max_gross_pct` and `max_net_pct`; a breach sets the stage `kill_switch` and escalates `deploy_blocked`. The engine's own risk configuration is a per-order, per-instrument backstop only and cannot express a per-strategy or a net limit, so `per_strategy_max_pct` is enforced where the size is chosen — the version's injected `capital` and the strategy's sizing helpers — with that backstop underneath.

### 8.8 Replay and parity
Targets: `--strategy` (generated impl; *default* latest version) or `--hyp` (*default* `best`; attached constructs with their pinned host). Range: `--from/--to` (*default* `forward.start` to the last catalog timestamp); instruments = the target's universe; `--speed` (*default* 0 for `replay run`; a stage node uses its stage `speed`). Modes (*default* `node`): `node` = `TradingNode` + `ReplayDataClient` + the stage's `exec` (0.1.0: sandbox) — the live code path; `engine` = `BacktestNode` on the same data — the research code path. Both produce a session. `replay parity` runs `node`, then `engine` over the persisted session, and compares order-intent sequences `(ts_event, instrument, side, qty, order_type, price?)`, reporting the first divergence; the `parity_replay` gate is `replay parity` over the certification window. Replay always uses the simulated execution client, whatever a stage is configured with: replay feeds historical data, a broker's paper account fills against current prices, and §6.7 refuses that pairing — so were replay to use a broker client, the required `parity_replay` gate could never pass on a workspace configured for one. Replay is evaluation only: it never creates cards, updates `best` or certifies, and the research runner cannot invoke it.

### 8.9 Data
The store is the Nautilus `ParquetDataCatalog`; all data and all instrument definitions pass through it. Reference loaders: `csv_parquet` (column mapping to `Bar`, `QuoteTick`, `TradeTick`, `CorporateAction`) and `synthetic` (seeded GBM/OU with spread and volume; the fixture for tests and demos). Vendor adapters (§5.1) provide further loaders and reference resolution through the same interfaces. Custom types: `register_custom_type` plus the built-in `CorporateAction`; any registered type is accepted in `data_requirements`.

Manifests `{dataset_id, source, instrument, type, span, adjusted, row_count, checksum, vendor?, vendor_dataset?, request_params? (credentials removed), publication: realtime|delayed|unknown, publication_rule?, as_of?, adjustment_basis?, supersedes?}`; snapshots list dataset ids and checksums **and the checksum of the resolved instrument definitions**, `snapshot_id = sha256(sorted checksums)`. `research begin` pins the newest snapshot covering the universe's `data_requirements` over the research and certification windows (exit 2 if none). Datasets referenced by a pinned snapshot are immutable: overlapping writes are refused (exit 2) and `backfill` and `sync` write successor datasets (`supersedes`) instead.

**Three verbs, one mechanism.** `load` writes exactly the range its spec names. `backfill` walks history from the source's floor forward to what is already held, and `sync` walks from each dataset's served end to now; both also close gaps inside an existing span, which `data show` reports. Backfill and sync are chunked and checkpointed per chunk, so an interrupted run resumes where it stopped and a repeated run fetches only what is missing — history for a real universe is measured in years of files and a run that must restart from the beginning is a run that never finishes. Both prefer an adapter's bulk transport when it declares one, because one object per trading day costs far less than the paginated request path for the same bytes, and both honour the adapter's quota. Reaching the history floor is a normal outcome that ends the backfill and is reported, never an error.

**Coverage is what was served, never what was asked.** A source MAY return less than the requested range with a success status and no warning, so a loader MUST compare the served span against the requested span, record the served span in the manifest, and surface the difference. Recording a requested span would claim coverage the dataset lacks, and `research begin` pins snapshots by coverage. An adapter records a **history floor** per source and class — the earliest date it can serve — probed rather than assumed; a request whose range lies wholly before the floor is refused (exit 2) naming the floor, and is never reported as an entitlement failure. Entitlement, history floor, an empty result and a malformed request are four distinct outcomes that a source MAY report with one indistinguishable message; an adapter MUST separate them by probing, not by parsing that message, and MUST probe at the grain the source gates on, which is not always the asset class.

**Availability (D19).** For every point kanso writes, `ts_init` is the instant the information first became publicly available and `ts_event` is its economic reference time, with `ts_init ≥ ts_event`; the engine's own ordering is by `ts_init`, so it delivers exactly that availability and nothing earlier. A dataset declaring `publication: delayed` is refused at write (exit 3) if any point has `ts_init == ts_event` or if its `ts_init` was not derived from a declared publication rule. Publication rules are keyed by data class, not by vendor. Restatements and revisions are additional points with a later `ts_init`, never overwrites, so "the value known at time t" is the latest point at or before t. A dataset's publication class is declared by the adapter that produced it, per dataset — the adapter knows whether its feed is real time, delayed by a plan tier, or published on a schedule — and defaults to `realtime` when an adapter declares nothing, which is what the file and synthetic loaders produce. A `publication: unknown` dataset MAY be loaded but is refused by `research begin`, and at `cert run` yields a recorded `fail` verdict rather than an exit code.

**Windows are primed, not truncated.** Loading a window for a series that changes only on publication also loads the last point published before the window opens, otherwise no value exists at the window's first instant and "the value known at time t" is unanswerable for as long as the publication interval — a quarter, for fundamentals. The primer is an input to evaluation, never a point inside the window.

An overlapping `load` into unpinned data is refused (exit 2) and needs `--replace` to delete and rewrite the overlapped span; a destructive default on a command that reads like an import is the wrong default.

**Adjustment.** kanso loads unadjusted prices plus a `CorporateAction` dataset and applies `instruments.yaml: corporate_actions` at load (`adjust_all`) or leaves event handling to the strategy (`none`). A vendor-adjusted series is adjusted as of its request date and is therefore mutable: an `adjusted: true` dataset MUST record `adjustment_basis`, and a snapshot containing one is marked `reproducible: false` and refused by `cert run`.

### 8.10 Envelope
Detection runs at `init`, at daemon startup and on demand; `research begin` refuses without an envelope; `doctor` flags one older than 7 days or with changed hardware. Sources: macOS `sysctl` (`hw.perflevel0/1.logicalcpu`, `hw.memsize`, `machdep.cpu.brand_string`), `sw_vers`, `pmset -g batt`; Linux `/proc/cpuinfo`, `/proc/meminfo`, `/sys/class/power_supply`; Python and engine versions. Plan: `live_colocated` = live stage has strategies; `reserved = {cores: 2, mem_gb: 8}` if colocated else `{1, 4}` (overridable in `[env]`); `cores_per_lane = 2`; `mem_per_lane_gb = max(4, 1.5 × max baseline_peak_mem_gb over runs)`; `lanes = max(1, min(⌊(cores_total − reserved_cores)/cores_per_lane⌋, ⌊(mem_gb − reserved_mem_gb)/mem_per_lane_gb⌋))`.

### 8.11 Escalation
Kinds: `misaligned`, `cert_failed`, `promotable`, `demoted`, `deploy_blocked` (D9). Each appends one inbox entry (§7.5), records an event and posts the webhook if configured. `inbox ack` marks an entry read; it is never an approval.

## 9. Cross-cutting concerns
- **Anti-overfitting.** Research loads only `windows.research`; certification loads `windows.certification`; `forward` is never backtested. Fold-wise objective with a noise floor and a complexity rule (§8.3.4); trial counts on every card and certificate; the planner never sees research results (§7.4); the instrument definitions a run is pinned to are frozen in its snapshot, so a tick-size or lot-size reassignment never rewrites a completed card.
- **Availability and delayed publication (D19).** The engine orders, filters and merges by `ts_init`, so a card receives exactly the availability timestamps the loader stamped. kanso therefore stamps `ts_init` with the publication instant and `ts_event` with the economic reference time, enforces that at write (§8.9), and never sets `ts_init` from `ts_event` or from ingest time. A datum whose `ts_event` falls in the research window but whose publication falls in the certification window is thereby excluded from research with no extra machinery: D12's embargo extends to delayed data for free. The required cert gate `publication_lag` (Appendix B) is the runtime backstop, and publication rules live in one data-class-keyed module rather than in each adapter, because they are regulator facts.
- **Parity.** One generated impl per version loaded unchanged everywhere; one catalog and one set of data types; `parity_replay` compares the two code paths on identical data; engine pins prevent running a version on an engine it was not certified with.
- **Token consciousness (D4).** Route strictly by task class: tier, thinking effort and output cap per §7.4, spending where a wrong answer is dearest and thinking nothing where a rule already decided; escalate at most one tier once, at the same effort. Byte-stable prompt prefix before dynamic context so provider caches hit; diffs rather than files the model already saw; last `context_cards` cards; crash tails ≤ 50 lines; structured JSON and short descriptions; skip calls a rule can answer (§7.4). Ledger every call `{ts, lane, task_class, model, tokens_in, tokens_out, cost, cache_hit?}`; `status` reports spend per lane and day; nothing pauses research.
- **Integrity and isolation.** `strategy_integrity` (Appendix B) restricts imports, forbids I/O and new dependencies, and requires that a card changes only `strategy.py` (the lane directory holds exactly the three scoped files and the copies of `hypothesis.yaml` and `program.md` equal the run's pinned blobs). Cards run in a subprocess with no catalog environment; the research runner refuses any window but research; `hypothesis.yaml` is immutable within a run; approval for live capital is a named, logged CLI act with no environment fallback.
- **Secrets.** Only in environment variables, resolved per §7.5 from the workspace `.env` and then the ambient environment; configurations name variables, never values; card subprocesses are started with a scrubbed environment; logs, prompts, certificates and `doctor --report` never contain values.
- **Versioning.** Semantic versioning; `schema_version` with migrations applied by `migrate`; engine range bumps only in minor releases after the e2e and parity tests pass (`docs/maintainers.md`).

## 10. Verification
`pytest` with `pytest-cov`; property tests with `hypothesis`; no network; the `synthetic` loader and the `mock` protocol are the fixtures; every CLI command has an integration test; every §6 schema a property test; every §11 acceptance criterion a named test. CI: macOS arm64 and Linux x86_64 × Python 3.12 and 3.13; coverage ≥ 85%, `ruff`, `mypy --strict`. The workflow lands in M0's first commit, since every milestone from M0 is gated on it. CI runs the **offline** suite only, which is the whole suite except `tests/live/`: no credential is available to it and none is added, so a green run proves the property D14 asserts — that everything works with every vendor credential unset. The credentialed half of an adapter milestone's acceptance is run by the maintainer and its result recorded in the pull request. `uv.lock` is committed so every job resolves identically. Tags `vX.Y.Z` publish to PyPI through trusted publishing: the workflow requests an OIDC token and carries no stored credential, so the publishing identity is registered once on the index against this repository and workflow, and nothing can upload without a tag.

| area | required test |
|---|---|
| determinism | same snapshot and configuration → byte-identical card metrics |
| embargo | a research card reading the certification window exits 2; `strategy_integrity` rejects `nautilus_trader.persistence` and any I/O |
| classification | every construct of Appendix A is representable in `hypothesis.yaml`; non-runnable constructs are accepted by `classify` and refused by `research begin` with the seam named; objective totality over `{mechanism} × {objective_mode} × {horizon < 1d, ≥ 1d}` |
| plans | a plan missing a required gate, a paper/live gate, or a parameter outside its range is rejected, retried once, then fails; the `classify` and `certify_plan` prompts contain no card metrics, certificates or `strategy.py`; no step has a non-LLM fallback |
| loop | a 30-card mocked run exercises keep, discard and crash; a `hypothesis.yaml` edit attempt is rejected; history survives a discard restore; drift reverts, re-points `best` and writes an inbox entry; stall → candidate and requeue |
| content addressing | blob round-trip by `strategy_sha`; unique-prefix resolution and an ambiguous or foreign prefix error; every keep and every re-point of `best` rewrites `hypotheses/<id>/strategy.py` atomically; discard/crash restores from `best` else `base_sha`; a lane copy of `hypothesis.yaml` or `program.md` differing from its pin → `discard` without a backtest and restored copies; `research begin` refuses a workspace `hypothesis.yaml` whose sha differs from the pin and honours `--from-workspace`; certificate files use `sha7` and carry `<sha7>.py` |
| credentials | a standard name resolves from `.env` when present there, from the environment when absent from `.env`, and `.env` wins when both hold it; an `api_key_env` override replaces the standard name; a missing required credential exits 2 naming the variable; no value ever reaches a log, a prompt, a certificate, `doctor` or `doctor --report`; a card subprocess sees no credential variable |
| files and git | two lanes never touch each other's files; `init` in a fresh directory, an existing repository and a monorepo subdirectory writes only inside the workspace and writes `.gitignore`; no kanso command invokes `git` at all (asserted by a subprocess spy) and an enclosing repository's `git status` shows only the workspace files kanso wrote; `hyp add` on a registered id keeps the status iff still valid |
| certification | re-certifying an unchanged `strategy_sha` under the same plan is refused; a planned `deflated_sharpe` consumes `n_trials`; `n_fail` fails → `failed` and inbox |
| deployment | `deploy` refuses an engine-pin mismatch, a kill-switched stage, and a stage without forward data; capital inheritance and `deploy_blocked` |
| e2e | synthetic data with forward data: hypothesis → classified (sleeve) → 30 mocked cards → candidate → certified (incl. `parity_replay`) → paper → paper gates → promotable → `promote --live` (exit 4 without `--as`) → live → injected drift → demoted and inbox; then a `filter` hypothesis attached to it → certified → host version 2 in paper |
| adapter isolation | an import-graph test and a source scan prove no vendor name appears outside its adapter package, `skills/`, `templates/` and `criteria/library/`; the full suite, `doctor` and the demo are green with every vendor credential unset |
| adapter contracts | one recorded-fixture test per dataset asserting the exact field names and types the loader reads; fixtures are raw response bytes with credentials redacted, replayed through an injected fake HTTP client; no network |
| publication lag | a `delayed` dataset with `ts_init == ts_event` is refused at write; `publication_lag` fails such a snapshot and any `unknown` one; a research card never receives a point whose `ts_init` is at or after the research window's end; a restated fact arrives as a second point in publication order |
| instrument resolution | unresolvable, ambiguous, pre-listing and post-delisting ids exit 3 naming the id; all five instrument classes round-trip catalog ↔ `instruments.yaml`; `override` wins; `manual` is never resolved; re-resolving produces a new snapshot and leaves the old replayable |
| execution safety | exit 4 for a `capital: real` execution client off the live stage or without a recorded approval; exit 2 for a `clock: wall` client paired with replay data or `speed ≠ 1`; the kill switch cancels and flattens through the broker path; no credential appears in any log, `--json` output, session file or `doctor --report` |
| live smoke | `tests/live/`, marked `live`, deselected by default, excluded from the coverage gate and from pull-request CI |
| demo | the §11 M8 sequence succeeds on a fresh machine with the mock register and no vendor credential |

## 11. Delivery plan
| milestone | scope | acceptance |
|---|---|---|
| M0 | package skeleton; schemas; state store and migrations; CLI plumbing; `init` (fresh, existing, monorepo); `doctor`; `skills sync`; `env detect`; credential resolution (§7.5); extension discovery; verification of every engine API claim in §7.3 and §8.8 against the installed package (facts in module docstrings) | `kanso init && kanso doctor` green on both hosts in all three repository situations; coverage ≥ 85% |
| M1 | catalog, loaders, custom types, manifests, snapshots, instruments, windows; backtest runner and `CardRun`; strategy API (`KansoStrategy`, `KansoModifier`); construct catalogue with the four runnable constructs; criteria library (objectives, toolbox, applicability, plan validation); `hyp`; `research begin/card/end` with lane directories; the instrument-resolution pipeline and the `InstrumentProvider` interface with a vendor-free manual/synthetic provider; the tick and lot convention table; the `Manifest` publication fields, the availability write invariants and the publication-rule table; `load_arrow` | determinism, embargo, totality and integrity tests; baseline card on synthetic data; two lane directories isolated; blob round-trip and restore tests; the instrument-resolution and publication-lag tests pass with no adapter configured |
| M2 | register, clients, router, ledger; `classify`; driver; scheduler and lanes; alignment; stall; `status` | classification and loop tests |
| M3 | certification planner; runner and certificates; `cert`; failure feedback; the required `publication_lag` gate joins the toolbox | plan and certification tests |
| M4 | composition and expectations; `ReplayDataClient`; sessions; sandbox nodes; `replay`; portfolio deployment; monitor; promote/demote; inbox and webhook | deployment and e2e tests |
| M5 | **Massive data adapter.** `HttpClient` with `Quota` and header auth; entitlement and history-floor probing in `discover`, `data adapters` and `doctor`, at the grain the source gates on; reference resolution for stocks, options, futures, forex and indices; bar, trade and quote loaders; object-store bulk history (signer, listing, ranged and streaming GET) for the entitled classes; splits and dividends into `CorporateAction`; the financials endpoint carrying a true acceptance instant; the filings index; `data sync` | one instrument resolves into the catalog per class the key entitles, probed at the grain the source gates on; load → backfill → sync → snapshot, over both the request and the bulk path, with an interrupted backfill resuming and a repeat fetching nothing; a card runs on vendor bars; the bar-close, timestamp-unit (millisecond and nanosecond epochs), zero-sided-quote, futures-year and multiplier fixtures pass; entitlement failure, history floor, empty result and malformed request are four distinct outcomes, the first two non-fatal; a silently truncated range is detected and the manifest records the served span; the signer passes the applicable subset of the published signature vectors offline under a frozen clock, the test module recording which cases are structurally inapplicable and why; the offline suite, `doctor` and the demo stay green with no credential set |
| M6 | **EDGAR and FINRA adapters.** Filing submissions and company facts stamped with the acceptance instant joined on the accession; bulk backfill through `data backfill`; frames refused; daily short-sale volume, consolidated short interest and OTC weekly summaries; the dissemination calendar shipped as package data | the publication-lag and restatement tests; a backfill interrupted mid-history resumes without refetching; a fundamentals-conditioned `filter` hypothesis certifies; a short-interest dataset stamped at its settlement date is refused; a daily file whose trailer disagrees with its row count fails the load |
| M7 | **Alpaca execution and live market data adapter.** `alpaca_paper` and `alpaca` execution ids; `LiveExecutionClient` with the five report generators; the unsupported-order deny table; the deterministic trade id; the tradability overlay; `capital` and `clock` declarations with the D13 guard; `fill_quality_drift` | sandbox versus broker-paper order-intent parity on a recorded session; a restart produces zero duplicate fills; every `deploy` refusal; the credential-leak scan; `promote --live --as NAME` remains the only path to a `capital: real` client |
| M8 | README, `docs/` (concepts, CLI, workspace, constructs, adapters, extensions and upstreaming, maintainers, service-unit recipe), `templates/demo/`, `ext show`, CI, PyPI `0.1.0`; `SPEC.md` deleted (§1.1) | fresh machine, no vendor credential, installing the locally built wheel so the test does not depend on the release it precedes: `uv tool install kanso && kanso init demo --demo && cd demo && kanso data load --loader synthetic --spec demo.yaml && kanso data snapshot && kanso hyp add hypotheses/demo_mr/hypothesis.yaml && kanso classify demo_mr && kanso research run demo_mr --cards 3` succeeds with the mock register |

Each milestone ends with its work committed on a `feat/m<N>-<slug>` branch and pushed to the origin remote, so CI runs the milestone's acceptance on both hosts (§10); a milestone is not done until that run is green, and only then is the branch merged and the next milestone branched from the result. This is the build's own procedure, not a property of the package.

Order: M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8. Adapters follow M4 so the core is provably complete and offline-green before any vendor lands, the e2e and demo stay synthetic, and the only milestone that can move money has composition, replay, sessions and deployment behind it.

## 12. Extension points and reserved seams
| seam | contract |
|---|---|
| constructs not runnable in 0.1.0 | `alpha` (canonical wrapper for evaluation; combination into an alpha-combining sleeve), `execution` (Nautilus execution algorithms on the host's orders; objective = net P&L delta from fill quality), `allocation` (portfolio-level capital/risk allocation across sleeves; `host: portfolio`; objective relative to the current allocation), and `overlay` with `host: portfolio`. Each becomes runnable by implementing `Construct` for it; classification already recognises them |
| additional adapters | any vendor or broker through `Adapter` + `Loader` + `InstrumentProvider` (data) or a `LiveExecutionClient` factory registered against an exec id (execution), in the package or in `kanso_ext/`; NautilusTrader's own adapters are reachable through the same exec and data id mechanism. A third-party distribution declares its own dependencies; the kanso distribution declares none (C3) |
| unentitled bulk classes | object-store bulk history ships in 0.1.0 for the classes a key entitles (§5.1). The seam is the rest: classes that answer `403` under any key kanso can test become reachable by entitlement alone, with `Loader` and `Adapter` unchanged |
| universe selectors | a `universe` declared as a predicate (an option chain, a futures curve) rather than a list; changes universe cardinality over time and interacts with snapshot pinning, D12 and `book_correlation` |
| non-US venues and calendars | the convention table, session calendars and publication rules are US-shaped in 0.1.0 |
| gates and objectives | criteria items (§6.11) with `impl`, in the package or in `kanso_ext/` |
| local models | any server speaking the OpenAI-compatible protocol via `openai_compat` and `base_url` |
| Rust (`kanso-core`) | a maturin/PyO3 crate (name prefixed; `kanso` is taken on crates.io) added only for a profiled hotspot outside the engine or a Nautilus component that must be Rust; an optional accelerator behind an unchanged Python API |
| learning (`kanso.learn`, reserved) | `strategy.py` MAY declare a `Learner` — `fit(train: CardRun slice) -> artifact`, `predict(state) -> signal`; the runner fits inside each fold's train span and predicts on its test span so the keep rule and gates hold unchanged; artifacts content-addressed per card; reinforcement learning uses the backtest engine as environment; no fitting on certification or forward data (D12); fit time counts against `card_budget_s`; out of scope for 0.1.0 |

## 13. Non-goals (0.1.0)
UI; multi-host scheduling; Redis; asset-class-specific models; machine learning; notes or learnings artefacts; spend caps; out-of-band approval channels; service-unit management beyond a documented recipe; default plans, default thresholds, or any non-LLM fallback for agent decisions. Also: vendor-computed ratios and other derived fundamental quantities, which carry no derivable publication instant and are typically served only as a current snapshot, so they cannot be evaluated point-in-time — kanso computes them in-strategy from the underlying statements and prices instead; bulk history for classes a key does not entitle; universe selectors; non-US market calendars; macro series carrying no release vintage; options and crypto order classes; broker-side portfolio margining or tax lots; greeks and factor computation inside the framework; fractional shares; and vendor-adjusted price series as a reproducible input.

## Appendix A. Construct catalogue
The domain taxonomy `classify` assigns a hypothesis to: what a thesis *is* in portfolio construction. It is a library (extensions may add constructs); these are the constructs practice uses, not examples of them.

| id | construct | attaches to | objective | 0.1.0 | notes |
|---|---|---|---|---|---|
| `sleeve` | A self-contained strategy — signal, entries, exits, sizing — with its own book and P&L attribution (also called a sub-strategy or book). | portfolio, as a new strategy | absolute | runnable | `KansoStrategy` |
| `alpha` | A return forecast (signal) that does not trade by itself; combined with other alphas inside an alpha-combining sleeve. | an alpha-combining sleeve | absolute, on a canonical wrapper | classification only (§12) | wrapper and combination rule are the seam |
| `filter` | A conditioning rule that gates a host's entries: regime, trend, volatility, calendar or liquidity conditions (`scope: time`), or which instruments the host may trade (`scope: instrument`). | a sleeve | relative | runnable | `Decision.allow` via `before_entry` |
| `overlay` | An exposure modification layered on a host without changing its signal: scaling (volatility targeting, drawdown control) and/or hedge legs (beta, tail, currency, delta hedges). | a sleeve, or the portfolio | relative | runnable on a sleeve; portfolio host is a seam | `Decision.scale` via `size`; `Decision.hedges` via `hedges` |
| `exit` | Exit logic added to or replacing a host's: stops, targets, time exits, trailing rules. | a sleeve | relative | runnable | `Decision.exit` via `before_exit` |
| `execution` | How a host's orders are worked: order types, passive/aggressive tactics, slicing — implementation shortfall. | a sleeve | relative | classification only (§12) | Nautilus execution algorithms are the seam |
| `allocation` | Capital or risk allocation across sleeves: risk parity, regime switching, a strategy of strategies. | portfolio | relative | classification only (§12) | — |

## Appendix B. Criteria toolbox
What kanso can evaluate. Objectives are selected by `applies` (the one deterministic domain rule, chosen by the operator). Gates carry no applicability, no defaults and no thresholds: which gates run and with which values is the planner's runtime decision inside the declared ranges; `required` marks the structural invariants.

| id | kind / stage | `applies` (objectives) · required (gates) | computes | params (ranges) |
|---|---|---|---|---|
The quantities every row below shares are defined once, as shipped defaults that `[research]` overrides (Appendix C). A **return** is the mark-to-market equity change over one `return_period` (*default* `1d`) containing at least one data event, equity being cash plus positions marked at the period's last price, computed by kanso from its own fills rather than read from the engine's analyser. **Annualisation** is `auto`: the observed number of periods per year in the window, not a fixed constant, so a 24/7 series and a five-session week are each scaled by their own calendar. **Drawdown** is peak-to-trough equity over the window as a percentage of starting capital, the same base `risk_limits.max_drawdown_pct` is expressed in. Sample statistics use `ddof = 1`. A **trade** is a closed position; positions open at a window's end keep their unrealised P&L in equity but are not counted.

| `wf_sharpe_net` | objective, p=20 | horizon min 1d, absolute | fold-wise annualised Sharpe of net returns, zero risk-free rate | `min_delta` [0, ∞), `k_se` [0.5, 3] |
| `net_edge_bps` | objective, p=10 | horizon max 1d, absolute | fold-wise mean net PnL per trade in bps | `min_delta` [0, ∞), `k_se` [0.5, 3] |
| `marginal_wf_sharpe` | objective, p=20 | horizon min 1d, relative | `wf_sharpe_net(combined) − wf_sharpe_net(host_run)` fold-wise | `min_delta` [0, ∞), `k_se` [0.5, 3] |
| `marginal_net_edge_bps` | objective, p=10 | horizon max 1d, relative | relative `net_edge_bps` fold-wise | `min_delta` [0, ∞), `k_se` [0.5, 3] |
| `strategy_integrity` | gate / card | required | AST, evaluated before the backtest (§8.3.3). Imports are matched by full dotted path against an allow-list of **exact leaves**, never package roots: `nautilus_trader.model.*`, `nautilus_trader.trading.*`, `nautilus_trader.indicators.*`, the pure leaves of `nautilus_trader.core` (`datetime`, `uuid`, `message`, `data`, `math`, `stats`, `correctness`, `fsm`), `kanso.nautilus.strategy`, `numpy`, and the standard-library set `math`, `statistics`, `collections`, `dataclasses`, `typing`, `decimal`, `datetime`. Every other `nautilus_trader` path is denied, `nautilus_trader.core.nautilus_pyo3`, `.core.rust`, `.persistence` and `.backtest` explicitly, because each reaches the catalog and so the certification window. Identifiers are rejected wherever they appear, as a name, an attribute or an import alias: the builtins `open`, `eval`, `exec`, `compile`, `__import__`, `getattr`, `setattr`, `vars`, `globals`, `locals`, `input`, `breakpoint`; the modules `os`, `sys`, `subprocess`, `socket`, `pathlib`, `importlib`, `builtins`, `ctypes`, `time`; the introspection dunders (`__subclasses__`, `__globals__`, `__code__`, `__class__`, `__mro__`, `__bases__`, `__dict__`); the bridge names `nautilus_pyo3`, `capsule_to_list`; numpy's file functions (`load`, `save`, `savez`, `fromfile`, `tofile`, `loadtxt`, `genfromtxt`, `savetxt`, `memmap`); and `self.clock` with the timer API, since a strategy reads data time (§7.3) and wall-clock logic cannot survive replay parity. `Bar.open` and the other OHLC attributes are unaffected: the ban is on the builtin name, not the attribute. No new dependencies; the lane directory contains exactly `hypothesis.yaml`, `program.md`, `strategy.py`, and the first two equal the run's pinned blobs. This is a guardrail against the loop's own proposer reward-hacking the embargo, layered with data isolation of the card subprocess — not a sandbox against a hostile actor, which §4 and D15 exclude | — |
| `min_trades` | gate / card | — | `n_trades ≥ min` and every fold has ≥ 1 trade | `min` [1, 10000] |
| `max_drawdown` | gate / card | — | `mdd ≤ risk_limits.max_drawdown_pct` | — |
| `embargoed_window` | gate / cert | required | objective on the certification window > 0 and ≥ `min_fraction` × research metric | `min_fraction` [0, 1] |
| `parity_replay` | gate / cert | required | §8.8, identical order intents within `ts_ns` | `ts_ns` [0, 10⁹] |
| `publication_lag` | gate / cert | required | per `publication: delayed` dataset in the pinned snapshot, `min(ts_init − ts_event)` meets its data class's documented minimum; fails on any `publication: unknown` dataset | `tolerance_s` [0, 86400] |
| `capacity_vs_adv` | gate / cert | — | peak daily traded notional ≤ `participation` × average daily volume over `adv_days`; `skipped` without volume data | `participation` [0.0001, 0.2], `adv_days` [5, 250] |
| `fill_quality_drift` | gate / live | — | rolling realised slippage minus `costs.slippage_bps` ≤ `max_excess_bps` over ≥ `min_fills`; `skipped` on a simulated execution client | `max_excess_bps` [0, 50], `min_fills` [10, 10000] |
| `walk_forward_consistency` | gate / cert | — | ≥ `min_positive_folds` research folds positive; the certification fold is not the worst | `min_positive_folds` [1, folds] |
| `deflated_sharpe` | gate / cert | — | the deflated Sharpe ratio of the candidate's **research-window** objective — the estimate selection actually acted on, which is what `n_trials` counts — against an expected maximum built from `n_trials` and the variance of the non-crash cards' own metrics, with `T`, skew and kurtosis from that window's return series; ≥ `min_dsr`. `skipped` with a reason when the objective is not a Sharpe family, since no card computed a Sharpe to deflate, or when fewer than two non-crash cards exist; the planner is told so through `meaningful_when` and does not plan it on a per-trade objective | `min_dsr` [0.5, 0.999] |
| `param_plateau` | gate / cert | — | ±`perturb_pct` on each numeric `Config` field keeps ≥ `keep_fraction` of the metric; `skipped` if none | `perturb_pct` [1, 50], `keep_fraction` [0, 1] |
| `cost_stress` | gate / cert | — | metric > 0 at `mult_a` × costs; ≥ 0 at `mult_b` × | `mult_a` [1, 10], `mult_b` [1, 10] |
| `bootstrap` | gate / cert | — | `n` trade-sequence bootstraps; evidence `objective_ci90`, `mdd_p95`; pass iff `mdd_p95 ≤ risk_limits.max_drawdown_pct` | `n` [100, 100000] |
| `book_correlation` | gate / cert | — | correlation of returns with each deployed strategy ≤ `max_corr`; `skipped` if none deployed | `max_corr` [0, 1] |
| `paper_forward` | gate / paper | ≥ 1 paper gate | elapsed stage-clock time since `joined_at` ≥ `max(min_duration, horizon_mult × horizon)`; realised objective inside `expectation.ci90`; and of the sleeve's card-stage constraints only `max_drawdown` is evaluated — `min_trades` is `skipped`, because a research-window trade count cannot be met in a paper window and applying it makes `promotable` unreachable, and trade sufficiency in paper is already the planner's decision through `min_duration` and `horizon_mult`. `meaningful_when` states that a redeploy flattens the stage (§8.6), so the window is measured from the current `joined_at` | `min_duration` [1d, 365d], `horizon_mult` [1, 1000] |
| `live_drift` | gate / live | ≥ 1 live gate | rolling objective (window = paper duration) below `expectation.ci90[0]` → fail | — |
| `daily_loss_kill` | gate / live | — | stage day PnL ≤ −`limits.daily_loss_pct` × stage capital → fail; the monitor then sets the stage `kill_switch` and escalates without demoting (§8.6, §8.7). The gate itself writes nothing: it is a pure evaluator | — |

## Appendix C. Configuration (`kanso.toml`)
The template `src/kanso/templates/kanso.toml` is normative for keys and defaults: top-level `kanso_version`, `schema_version`; `[extensions]` `paths`; `[skills]` `targets`; `[research]` `capital`, `align_every`, `stall_k`, `context_cards`, `folds`, `max_lines_per_keep`, `baseline_budget_s`; `[certify]` `n_fail`; `[data]` `reference` (*default* `none`, the adapter id that resolves instruments), `adjusted` (*default* `false`); `[research]` also `broker` (*default* `alpaca`, the broker whose venue model research inherits), `account` (*default* `margin`), `currency` (*default* `USD`), `return_period` (*default* `1d`), `annualisation` (*default* `auto`, derived from the observed periods per year rather than a fixed constant); `[env]` `reserved_cores`, `reserved_mem_gb`, `cores_per_lane`; `[monitor]` `interval`; `[webhook]` `url`; `[adapters.<id>]` — a free-form table validated by that adapter's own pydantic model, so no vendor key appears in a kanso-owned schema; credentials never appear here (§7.5 covers them). The template `src/kanso/templates/gitignore` is normative for the `.gitignore` entries `init` writes: `.env`, `state.db`, `state.db-journal`, `state.db-wal`, `state.db-shm`, `runs/`, `hypotheses/*/results.tsv`, `catalog/.cache/` (the commented `catalog/` line is the operator's choice); `doctor` checks their presence.

## Appendix D. Traceability
| directive | sections |
|---|---|
| D1 | §5.2, §7.4, §13 |
| D2 | §5.1, §7.3, §7.4, §10, §12 |
| D3 | §8.3.5 |
| D4 | §7.4, §9 |
| D5 | §8.10 |
| D6 | §5.2, §6.9, §7.4 |
| D7 | §1.3, §6.1, §6.10, Appendix A |
| D8 | §8.5, §8.8, §9 |
| D9 | §8.11 |
| D10 | §8.3.3, §9, Appendix B |
| D11 | §8.3.1, §8.3.4 |
| D12 | §8.3.3, §9, §10 |
| D13 | §8.6 |
| D14 | §5.1, §7.3, §10, §12 |
| D15 | §5.1, C3, §12 |
| D16 | §7.4, §8.3.1, §8.4, §13, Appendices A–B |
| D17 | §5.3, N9 |
| D18 | C5, N7, §2, §5.2–§5.5, §6.1–§6.3, §6.12, §7.1, §7.4, §8.1, §8.3.2–§8.3.6, §8.4.3, §8.5, §9, §10, §11 |
| D19 | §6.11, §7.3, §8.9, §9, §10, Appendix B |
