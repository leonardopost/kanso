# The workspace

A **workspace** is a plain directory holding `kanso.toml`. Everything one experiment needs
lives in it — the configuration, the credentials, the market data, the hypotheses, the
research history, the certificates, the composed strategies and the book — and nothing
outside it is read or written except the process environment and the installed package.

`kanso init <dir>` scaffolds one. Every command afterwards finds it by walking up from the
current directory to the nearest `kanso.toml`, so commands run from anywhere inside. Outside
one the answer is `not inside a kanso workspace: no kanso.toml at or above <dir>` (exit 2),
naming the directory it started from. `--workspace PATH` (`-w`) names one explicitly.
Running a command from inside a lane directory (`runs/<lane>/<hyp>/`) resolves to the
workspace that owns the lane rather than to the lane, because the interactive research loop
works with its cwd set there and a lane is not a workspace.

**Two workspaces on one host share nothing.** Not the state, not the catalog, not the
credentials — an adapter resolves its key from the workspace it is acting on, at the moment
it acts. That is the unit of separation: a strategy on one account and a strategy on another
belong in two directories, not in two sections of one file.

This page is about the files. `docs/concepts.md` is the vocabulary they are written in,
`docs/cli.md` is every command and every exit code, and `docs/adapters.md` is what a vendor
or a broker adds to a workspace.

## kanso never runs git

No command in kanso invokes git — not `init`, not `commit`, not a branch, a tag, a worktree
or a read. A workspace may sit inside a repository, in a subdirectory of one, or nowhere near
one, and the three are identical to kanso. `kanso doctor` reports an enclosing repository
(`enclosed by a repository at …`) by looking for a `.git` entry on the filesystem and does
nothing with the answer.

The consequence is that **committing is yours**. kanso versions the files it owns by content
instead: `hypothesis.yaml` is pinned by the sha256 of its bytes, every card's `strategy.py` is
stored as a blob under that sha, certificates carry the certified bytes beside them and
`impl/<version>/` holds verbatim copies. That is enough to answer "which code produced this
number" without a repository, and it is not a substitute for one. A tool that committed on
your behalf would be deciding for you what a revision means and when a piece of work is
finished, which are the two decisions least worth automating.

`init` writes `.gitignore` if there is none and **appends** to one that exists, under a
`# kanso` header, never adding an entry already present. The entries are:

```
.env
state.db
state.db-journal
state.db-wal
state.db-shm
runs/
sessions/
strategies/*/impl/*/__pycache__/
hypotheses/*/results.tsv
catalog/.cache/
# Uncomment to keep market data out of git (recommended for anything beyond demos):
# catalog/
```

`catalog/` is deliberately left commented for you to decide: a demo's synthetic bars are
worth committing and a decade of vendor minute bars is not. `kanso skills sync` appends a
second `# kanso` block for the skill links. `kanso doctor` counts the entries it finds and
never edits the file.

## Who writes what

| path | written by | yours to edit |
|---|---|---|
| `kanso.toml` | `init` | **yes** — the whole file |
| `.env` | `init` (empty, mode 600) | **yes** — kanso reads it at each use and writes it never |
| `models.yaml` | `init` | **yes** |
| `hypotheses/<id>/hypothesis.yaml` | `hyp new`, `classify` | **yes**, between runs |
| `hypotheses/<id>/program.md` | `hyp new` | **yes**, between runs |
| `demo.yaml` and other loader specs | you (`init --demo` renders one) | **yes** |
| `kanso_ext/` | you | **yes** |
| `AGENTS.md`, `CLAUDE.md` | `init`, if absent | **yes** |
| `.gitignore` | `init`, `skills sync` (append only) | **yes** |
| `instruments.yaml` | `data instruments resolve` | **four fields only** — see below |
| `portfolio.yaml` | `init`, then certification, `deploy`, `promote`, `demote`, `strat retire` | **stages and limits only** |
| `hypotheses/<id>/strategy.py` | research, after every keep | no — it is the best-so-far |
| `hypotheses/<id>/results.tsv` | research, rendered from state | no |
| `envelope.yaml` | `env detect` | no — `[env]` in `kanso.toml` is the override |
| `state.db` | kanso | no |
| `catalog/` | `data load`, `sync`, `backfill`, `snapshot`, `instruments resolve` | no |
| `runs/` | `research begin`, the daemon | no |
| `sessions/` | replay, parity, stage nodes | no |
| `certificates/` | `cert plan`, `cert run` | no |
| `strategies/` | `strat compose` | no |
| `escalations/inbox.md` | `init`, then kanso, append-only | no |
| `<skills target>/kanso-*` | `skills sync` | no (symlinks) |

Nothing in that table is enforced by a file mode, and there is no lock. What makes the split
hold is that **every fact has exactly one authority, and for the facts that matter it is not
the file you would be tempted to edit.**

`state.db` is the authority for which hypotheses are registered and at what pin, every card
ever run and its bytes, the `best` pointer, certification plans and certificates, approvals,
which escalations have been read, model spend and the queue. The files are the authority for
what the catalog holds (the manifests), what a composed version is and what state it is in
(`strategy.yaml`), how a stage is configured (`portfolio.yaml`), the lane plan
(`envelope.yaml`) and which sessions exist (the directories). Where the two overlap — a
certificate, a deployed version — the file is a rendering and state is the record.

So editing a file kanso owns is not a way to change kanso's mind. Depending on which file it
is, the edit is ignored, silently reverted on the next write, or believed at your own risk.
Each section below says which.

## What the workspace refuses

Every one of these is a refusal against workspace state, and each is explained where its file
is. Exit 2 is a precondition the workspace or the engine forbids; exit 3 is input you wrote
that is wrong; exit 4 is an operator act that is missing rather than a fault.

| you do this | you get |
|---|---|
| run a command outside any workspace | 2 · `not inside a kanso workspace` |
| `kanso init` over an existing `kanso.toml` | 2 · a workspace is scaffolded once |
| leave a typo in `kanso.toml` | 3 · `unknown key '<section>.<key>'`, from every command |
| run anything against a `state.db` behind the schema | 2 · *N* migration(s) behind; `kanso migrate` |
| write windows with no embargo between research and certification | 3 · at `hyp validate`, changing nothing |
| `hyp add` while the hypothesis has an active run | 2 · a run is pinned to the bytes it began with |
| `research begin` on a hypothesis already running | 2 · one active run per hypothesis |
| `research start` twice in one workspace | 2 · the pid file is the lock |
| `data load` over a dataset a snapshot names | 2 · **with or without `--replace`** |
| `data load` over unpinned data | 2 · until you pass `--replace` |
| `cert run` on bytes already certified under the same plan and engine | 2 · a certificate is immutable |
| `models check` or `cert plan` with no `models.yaml` | 2 · there is no default plan |
| `deploy` a stage whose `kill_switch` is on | 2 · the switch is yours |
| `deploy` a `clock: wall` client, or feed one `data: replay` | 2 · no stage node runs one in 0.1.0 |
| `deploy` a real-capital client on the paper stage | **4** · it may be configured only on live |
| `promote --live` without `--as NAME` | **4** · the approval is the name |

## `kanso.toml`

The one file whose presence makes a directory a workspace, and the operator's throughout.
`init` renders it once and refuses to render it twice:

```
$ kanso init w1 --demo
error: …/w1/kanso.toml already exists; a workspace is scaffolded once
remedy: run `kanso skills sync` and `kanso env detect` to refresh this workspace
```

(exit 2). Refreshing an existing workspace is `skills sync` and `env detect`; there is no
`init --force`, because the file it would overwrite is the one you have been editing.

The rendered file is the reference for the keys and their defaults: each is commented where
it is defined. The sections are `[extensions]`, `[skills]`, `[research]`, `[certify]`,
`[env]`, `[monitor]`, `[webhook]`, and `[adapters.<id>]`. Two keys the template does not
render but the parser accepts belong to `[data]`: `reference`, naming the adapter that
resolves instruments (default `none`), and `adjusted` (default `false`).

**An unknown key is a validation failure, not a shrug.**

```
$ kanso data show
error: …/kanso.toml: unknown key 'research.fold'
```

(exit 3, and the same from `status`, `doctor`, `hyp show` and everything else that opens the
workspace, because the file is parsed on the way in). A typo that is tolerated is a setting
that silently does nothing, which is worse than a stopped command: you would spend the next
week reading results produced under the default you thought you had changed, and nothing in
the output would say so. `[adapters.<id>]` is the exception at the top level only — each
adapter validates its own table with its own model, just as strictly, which keeps every
vendor key out of a kanso-owned schema.

`[research] broker` is the single place the core lets a broker's name in: it says whose venue
model — account type, currency, costs — research inherits. A workspace naming a broker it has
no adapter for falls back to the shipped venue defaults rather than refusing.

## `.env`

`KEY=VALUE` lines, one credential each, created empty at mode 600 and **never written by
kanso**. An `.env` that already exists when you `init` over a directory is left exactly as it
is, contents and mode.

Each name is resolved at the moment of use: this file first, then the process environment,
first non-empty value wins, and nothing is injected into the process environment for anything
else to read. `kanso doctor` prints, per credential, the variable name and where it resolved
from — `.env`, `environment`, or `unset` — and never a value:

```
ok   credentials   0/0 required credentials resolve
                   KANSO_WEBHOOK_URL: .env · escalation webhook (optional)
```

Which names a workspace needs depends on what it is configured with: `kanso doctor` lists
them, and `docs/adapters.md` says what each vendor and broker asks for. A workspace with none
set is the ordinary state of a fresh one and everything in the demo still runs.

A credential that resolves in neither place fails its step with exit 2, naming the variable
and both places searched. Card subprocesses start with an allow-listed environment rather
than the parent's — no catalog path, no credential, no workspace variable survives — so a
strategy under evaluation cannot reach a key even though the command that launched it could.

## `state.db`

SQLite in WAL mode, and the only database kanso keeps. It holds hypothesis registration,
status and pins; runs and every card ever run; the blob store the cards' `strategy.py` bytes
live in; certification plans and the certificates of record; **approvals**; escalations and
which of them have been acknowledged; the model spend ledger; the session and strategy-version
indexes; and the queue.

It is kanso's alone. There is no supported way to edit it and no need to: everything in it
is reachable through a command, and every command that writes it does so through one code
path.

**A database behind the shipped schema is refused rather than migrated behind your back.**

```
$ kanso hyp show
error: …/state.db is 1 migration(s) behind this kanso
remedy: run `kanso migrate`
```

(exit 2, from every command that needs state; `kanso doctor` grades it `warn`). Upgrading
kanso and running a command is not consent to rewrite the record of your research, so
`kanso migrate` is a separate act you take when you are ready to take it.

**Deleting `state.db` is not a reset — it is a loss.** The next command creates an empty
database, reports it behind by every migration this kanso ships, and after `kanso migrate`
the workspace has no
registered hypotheses, no cards, no best pointer, no certificate of record and no approvals.
What survives is what is file-backed: `catalog/` still serves its data, `certificates/<hyp>/`
still holds the certificate YAML and the certified `<sha7>.py`, `strategies/<id>/` still holds
`strategy.yaml` and its `impl/` directories, and `kanso strat show` still answers from them.
Re-running `kanso hyp add` re-registers the hypothesis as `classified` with no best and no
certificate. Back it up with the workspace or accept that the research history is the part
that does not travel.

## `hypotheses/<id>/`

Four files, and the boundary between you and kanso runs right through the directory.

`hypothesis.yaml` is **yours**. It states the idea: the thesis, the mechanism, the universe,
the horizon and resolution, the data requirements, the risk limits, the three windows, and —
once classified — the construct, the objective and the card-stage constraints. `kanso classify`
writes those last three and nothing else; you can equally write them yourself and run
`kanso hyp add`, which needs no model at all. The file's own comments are its field reference.

`kanso hyp validate PATH` says whether it is admissible and changes nothing either way:

```
$ kanso hyp validate hypotheses/demo_mr/hypothesis.yaml
error: hypotheses/demo_mr/hypothesis.yaml: windows: certification.start: 2024-12-31 overlaps
       the research window ending 2024-12-31
```

(exit 3). The embargo between the research and certification windows is enforced here rather
than trusted, because a certification window that touches the research window is not
out-of-sample and a strategy measured on it has no evidence behind it.

`kanso hyp add` pins the file under the sha256 of its bytes. Edit it afterwards and kanso
says so rather than silently working from either version:

```
$ kanso hyp show demo_mr
sha        eb6db7b2cda1bb5626029df3e63c9b948ccaf49721e74580b673baff6c53703f
pinned     no (the workspace file has moved)
```

Re-pinning is `hyp add` again — **unless a run is active**:

```
error: demo_mr has an active run (7e4b3a48…), so it cannot re-pin
remedy: end the run with `kanso research end demo_mr` first
```

(exit 2). A run is pinned to the bytes it began with, which is what makes its cards
comparable to each other; re-pinning underneath it would silently change the question the
cards were answering.

`program.md` is yours on the same terms: it is copied into the lane and pinned at
`research begin`.

`strategy.py` is **kanso's once a keep exists.** It is the best-so-far, written atomically
from the best blob after every keep and every re-point of `best`, so the file on disk is
always the current champion. Its bytes hash to the `strategy_sha` kanso shows:

```
$ shasum -a 256 hypotheses/demo_mr/strategy.py
f729a538831e3ea8f80c46b68c5993ed4662c168bd9c56541cdf570619b6f6e9
```

which is the `f729a53` in `kanso hyp show`, `research show`, the certificate's filename and
the certified source beside it. Editing it by hand does not change what a card evaluates —
a card evaluates the **lane** copy, `runs/<lane>/<hyp>/strategy.py` — and the next keep
overwrites your edit without comment. Nothing in 0.1.0 warns you that it diverged; if you
want to start from your own code, that is what `kanso research begin --from-workspace` is
for, and it clears `best` so the history says what happened.

`results.tsv` is rendered from state after every card, so restoring a lane from the best
never loses a row. Deleting it loses nothing.

## `models.yaml`

The LLM register and the routing table, and yours entirely. It holds **model ids, providers,
tiers, context sizes, prices and the variable name each key is read from — never a key.** By
default that name is `KANSO_<PROVIDER>_API_KEY`; `api_key_env` overrides it with another
name, and an override replaces the standard name rather than adding to it.

`routing` maps each task class — `classify`, `certify_plan`, `propose`, `align_check` — to a
tier, a thinking effort and an output cap. `kanso models check` prints the register as the
router reads it and then makes one minimal call to every configured model.

A workspace with no register is refused where a model is actually needed:

```
$ kanso models check
error: no models.yaml at …/models.yaml
remedy: write models.yaml, or run `kanso init` in a fresh directory
```

(exit 2; `kanso cert plan` gives the same answer). What does **not** need a register is worth
knowing: `research begin`, `research card` and `cert run` against a pinned plan all run to
completion with `models.yaml` moved out of the way. The model is in the proposer's seat, not
in the measurement.

## `instruments.yaml`

The resolved-instrument cache, its provenance and your corrections, in one file with a
field-level split down the middle.

**Yours:** `override` (fields applied after resolution and before the instrument is
constructed — a correction, not a note), `attributes` (free-form facts strategies and gates
may read), `corporate_actions`, and `manual`. **kanso's:** `nautilus_id`, `asset_class`,
`resolved` and `sources`, rewritten by `kanso data instruments resolve` and only when a
resolution actually changed them.

`manual: true` suppresses resolution entirely and requires you to supply the constructor
fields yourself. That is the path the file loaders, the synthetic loader and the demo take,
and it is why a workspace can run end to end with no reference adapter and no credential.

The registry of record is the catalog's instrument store, not this file — `kanso data
instruments show <ID>` reads the store. The file is the cache and the place your overrides
live, so deleting it costs you the overrides and a round of resolution, not the definitions.

## `catalog/`

The market data, and the only directory in the workspace measured in gigabytes.

```
catalog/data/          the engine's own ParquetDataCatalog tree, including instrument definitions
catalog/manifests/<dataset_id>.yaml
catalog/snapshots/<snapshot_id>.yaml
catalog/.cache/        an adapter's scratch space
```

A **dataset** is one instrument, one data type, one resolution and one adjustment basis served
over one span of dates, and its id is derived from exactly those dimensions rather than
invented — `DEMO.SIM-bar-1m-raw-20250901`. The id carries the span's end but not its start, so
re-loading the same series to the same end reuses the id and is a replacement, while a `sync`
that extends the end and a `backfill` that reaches further back both mint fresh ids and record
the dataset they follow in `supersedes`. The manifest records the span that was **served**,
never the span that was asked for, because a source may answer a five-year request with two
years, HTTP 200 and no warning.

A **snapshot** freezes what is held: the dataset checksums plus the checksum of the resolved
instruments. Every run is pinned to one.

**A dataset a snapshot names cannot be rewritten. At all.**

```
$ kanso data load --loader synthetic --spec demo.yaml
error: DEMO.SIM-bar-1m-raw-20250901 is named by a snapshot and cannot be rewritten
remedy: write a successor dataset recording supersedes=<dataset_id>
```

(exit 2 — and `--replace` gives the identical refusal, which is the point: the flag lifts the
overlap check, not the pin). A run, a card, a certificate and a deployed version all reference
a snapshot; rewriting the bytes underneath one would make every result that cites it
unreproducible while leaving the citation looking fine. The successor path — a new dataset
recording `supersedes` — is what `kanso data sync` walks, so extending a series never mutates
one.

An overlapping write into data **no snapshot pins** is a different question and gets a
different answer:

```
error: 2024-01-02..2025-09-01 overlaps the held dataset(s) DEMO.SIM-bar-1m-raw-20250901
remedy: pass --replace to delete and rewrite the overlapped span
```

(exit 2). Here the data is yours to replace; you just have to say so.

**The manifests are the record of what the catalog holds**, and `kanso data show` reads them
rather than the parquet files. Delete a parquet by hand and `data show` keeps reporting the
dataset, its span and its row count; `kanso doctor` has no catalog check at all. The loss
surfaces only when a run needs the rows, as a baseline or card that did not run (exit 2):
`data: the catalog holds nothing for <id> over <window>` — whose remedy line suggests fixing
`strategy.py`, which is not what went wrong. The catalog is a directory you back up, not one
you prune.

## `runs/`

The lane directories, and the only place research edits anything.

```
runs/<lane>/<hyp>/   hypothesis.yaml, program.md, strategy.py — and nothing else
runs/daemon.pid      the supervisor's pid, and its lock
runs/daemon.log      whatever the daemon and its children write to a stream
```

`kanso research begin` prints the lane directory, copies the three scoped files into it and
pins them. Exactly those three files are there; a card runs in a subprocess with its cwd set
to that directory, and lanes never share files. **One active run per hypothesis:**

```
error: demo_mr already has an active run (d7220ee4…)
remedy: end it with `kanso research end demo_mr`
```

(exit 2).

`kanso research end` removes the lane directory and nothing else — the cards, the blobs and
the best all stay in state, and `results.tsv` still renders. `kanso research stop` removes
neither: it signals the daemon, waits for it to go and leaves every active run and every lane
directory exactly where they are, so the next `start` resumes them. Stopping is meant to be
cheap.

The pid file is also the lock:

```
error: a daemon is already running in this workspace (pid 72797)
remedy: run `kanso research stop` first
```

(exit 2). A pid file naming a process that is gone reads as "not running" and the next
`start` overwrites it. `daemon.log` survives a stop; `daemon.pid` does not.

The whole directory is gitignored, and deleting it while nothing is running costs you only
the log.

## `certificates/`

```
certificates/<hyp>/plan.yaml
certificates/<hyp>/<sha7>-<n_trials>-p<plan>-e<engine>.yaml
certificates/<hyp>/<sha7>.py
```

The plan is what would count as proof for this hypothesis — the cert, paper and live gates
with their parameters and a rationale each — pinned once, re-minted only by `cert plan
--replan`. A certificate is the verdict, its evidence and its pins. The `.py` beside it is
the certified source, byte for byte, so a certified subject travels with the files even where
`state.db` does not:

```
$ shasum -a 256 certificates/demo_mr/f729a53.py
f729a538831e3ea8f80c46b68c5993ed4662c168bd9c56541cdf570619b6f6e9
```

**A certificate is immutable**, and the filename says what it is a certificate *of*: these
bytes, under that plan version, on that engine — with the trial count that stood when it was
minted.

```
$ kanso cert run demo_mr
error: demo_mr already certified f729a53 under plan version 1 and nautilus_trader 1.231.0 on
       2026-09-06T00:48:13…; a certificate is immutable
remedy: research a better strategy, replan, or upgrade the engine
```

(exit 2). Change the bytes, the plan version or the engine and it is a different certificate
under a different name, so re-certifying an unchanged commit after an engine upgrade is a
plain `cert run` and produces a second file rather than overwriting the first.

Editing a certificate file changes nothing kanso will ever act on: the certificate of record
is in `state.db` and the YAML is a rendering of it. Change `verdict: pass` to
`verdict: fail` in the file and `kanso cert show` still prints `pass`. This is not a
tamper-check — it is that the file was never the authority.

## `strategies/`

```
strategies/<id>/strategy.yaml          the versions, their pins, their expectation, their state
strategies/<id>/impl/<version>/        the manifest and a verbatim copy of every certified source
```

`kanso strat compose` writes both, and a passing certificate composes on its own, so these
are usually not commands you type. `impl/<version>/` is the **one directory a backtest, a
replay and a live node all load from**, so the exchange a version was judged against and the
one it is deployed onto cannot drift apart. Each source file is named after the module it
defines, and that module name carries a digest of the file's own bytes — which is why the
sha of that file, the `strategy_sha` in `manifest.yaml`, the sha of the certificate's
`<sha7>.py` and the `<sha7>` in the certificate's own name are all one number.

Nothing checks that at deploy time. Edit a file under `impl/` and the next
`kanso portfolio deploy` runs your edit and reports success. The digest in the filename and
the `strategy_sha` in the manifest are what make the divergence *findable* — `shasum -a 256`
against either — but they are a record, not a guard. Treat the directory as generated: change
`strategy.py`, research it, certify it, and let composition write the next version.

`__pycache__` appears inside it the first time a version is imported. It is the interpreter's,
not the version's, and the template gitignores it.

## `portfolio.yaml`

The one file you and kanso both write, so it is worth being precise about which half is
which.

**Yours:** the two stages' `exec`, `data`, `speed`, `capital` and `kill_switch`, the `limits`
block, and the optional `venues` overrides. **kanso's:** `stages.<name>.strategies`, appended
on certification and rewritten by `deploy`, `promote`, `demote` and `strat retire`. kanso
rewrites the whole file when it writes it, so **your comments do not survive** the first
deployment. Keep your notes elsewhere.

Everything the file can say about a stage's execution reduces to one id, and everything that
matters about that id is the pair of declarations behind it: `capital` is `simulated`,
`broker_paper` or `real`, and `clock` is `replay` or `wall`. `kanso portfolio clients` prints
them with what `deploy` would refuse each stage for, and reaches nothing to say so.

**Editing this file by hand can never move real money.** Four independent refusals stand
between it and a broker, and every one of them can be provoked in a demo workspace with no
credential set:

*A real-capital client is refused off the live stage.*

```
$ kanso portfolio deploy --stage paper
error: stages.paper.exec: 'alpaca' trades real capital and may be configured only on the live stage
remedy: move it to stages.live
```

(exit 4 — a missing act, not a broken precondition, which is why it has its own code;
`portfolio clients` reports the same sentence without deploying.)

*`promote` is the only command that can put a version on real capital, and it requires a
person.*

```
$ kanso promote demo_mr@1 --live
error: promote: moving demo_mr onto the live stage is a named operator act
remedy: kanso promote demo_mr --live --as NAME
```

(exit 4, having changed nothing). `--as NAME` is the whole of the approval: no environment
fallback, no default. The approval is recorded in `state.db` against that exact version
before anything moves, and `deploy --stage live` checks, per version, that one is on record
before it funds a real-capital client.

*What deploys is read from `state.db`, not from this file.* Add a strategy to
`stages.live.strategies` by hand and deploy the stage: it admits nothing.

```
$ kanso portfolio deploy --stage live
deployed   live · 0 version(s) · 0
```

The live stage admits only what `promote` moved there. (`kanso portfolio show` does read the
file, so a hand-added entry appears there and nowhere else — a good reason not to add one.)

*And a stage whose kill switch is on stays halted.*

```
error: stages.live.kill_switch is on, so the stage is halted and nothing deploys to it
remedy: clear stages.live.kill_switch in portfolio.yaml, then deploy again
```

(exit 2). The switch is yours; a deployment that cleared it by starting a node would make it
advisory. `kanso demote` still works with the switch on and leaves the halted stage alone.

In 0.1.0 a `clock: wall` execution client is refused outright (exit 2) — a stage node here is
a bounded replay of the catalog into kanso's own simulated venue, and running a broker's
client through it would record a simulated fill as the broker's. So the demo's `sandbox`
client is the only one a stage can actually run, and the refusals above are what will still
be standing when the long-running node arrives.

## `sessions/`

One directory per run of a node: `session.yaml`, the points released (`stream.jsonl`) and the
order intents that came back (`intents.jsonl`). Replay writes one, a parity comparison writes
two — one per code path — and a deployment that actually runs a node writes one. They are the
evidence behind a `parity_replay` gate and behind a stage's realised window.

They accumulate and nothing prunes them; the directory is gitignored. `kanso replay show`
lists what is on disk, so deleting a session directory removes it from the listing cleanly —
the certificate that cites it still stands, it just no longer has the stream to show you.

## `escalations/inbox.md`

Append-only, and kanso means it. One line per escalation — `misaligned`, `cert_failed`,
`promotable`, `demoted`, `deploy_blocked` — carrying an id, a timestamp, the kind, its
subject, a summary and the commands that kind offers.

`kanso inbox ack <id>` marks one read, and **the line in the file does not change**: it stays
an unchecked `- [ ]` forever, because the file is never rewritten and read-state lives in
`state.db`. Acking twice is acking once (exit 0 both times). Acknowledging is never an
approval — it writes one timestamp and stands for no decision, so every action the entry
offers is still yours to take.

## `envelope.yaml`

Generated by `kanso env detect`: what the host is (cores, memory, disk, power, engine wheel
compatibility) and the lane plan derived from it. Its first line says `do not edit`, and the
reason is that `env detect` rewrites the file wholesale with no merge — so an edit survives
exactly until the next detection.

It is honest about what it is: a hand-edited `lanes: 9` **is** believed, and
`kanso research status` will show nine lanes. The file is a measurement, not a claim to be
validated, which is why the durable override lives somewhere else: `[env] reserved_cores`,
`reserved_mem_gb` and `cores_per_lane` in `kanso.toml` are read on every detection.

A missing envelope is a `warn` in `kanso doctor` and leaves `research status` reporting
`lanes none (run kanso env detect)`.

## `kanso_ext/`

Optional, and yours. Every package or single-module file **directly under** each
`[extensions] paths` entry (default `kanso_ext`) is imported at startup, and what its
module-level `PROVIDES` table declares — gates, objectives, constructs, loaders, adapters,
execution clients, custom data types — is collected. A file at `kanso_ext/house.py` is an
extension; a `kanso_ext/__init__.py` is not, because the directory is a search path and not a
package. `docs/extensions.md` is how to write one.

`kanso ext show` says what is there and whether it is actually in play:

```
$ kanso ext show
paths      kanso_ext
house           loaded · kanso_ext/house.py
                objectives   always_one          absent · no registry reads it: …
1/1 loaded · 0 registered · 0 shadowed · 1 absent
```

An extension is operator code, so importing one is expected to fail sometimes. A failure is
recorded and never raised: a broken extension degrades the workspace to the ones that load,
and `kanso doctor` says which and why.

```
warn extensions    1/2 loaded
                   broken: ModuleNotFoundError: No module named 'nope_not_a_module'
                   house: ok
                   → fix or remove the extension; a shadowed id resolves to the packaged one
```

## `AGENTS.md`, `CLAUDE.md`, and the skill links

`init` writes both instruction files and **never touches an existing one.** Where `AGENTS.md`
already exists, the kanso instructions go to `AGENTS.kanso.md` and you are told the line to
add:

```
AGENTS.md exists and was left untouched; the kanso instructions are in AGENTS.kanso.md —
add this line to AGENTS.md: @AGENTS.kanso.md
```

Where `CLAUDE.md` exists, the notice names the line `@AGENTS.md`. Your house rules are yours;
kanso asks to be imported, not to be obeyed instead.

`kanso skills sync` links the eleven packaged skills into every `[skills] targets` entry as
symlinks into the installed package, so upgrading kanso upgrades the skills without
re-linking — and moving or reinstalling the package breaks the links until you sync again.
`kanso doctor` counts them.

## What `--demo` adds

`kanso init <dir> --demo` fills in what a plain `init` leaves as a placeholder and adds three
files, and between them they are the reason the demo runs end to end with no credential of any
kind:

| file | plain `init` | `--demo` |
|---|---|---|
| `models.yaml` | a commented skeleton with `<provider>` placeholders | the shipped `mock` protocol listed for every tier, so classification, proposal, alignment and planning cost nothing and reach nothing |
| `instruments.yaml` | `{}` plus the field reference in comments | one `manual: true` entry, `DEMO.SIM`, so no reference adapter is needed |
| `mock/responses.yaml` | — | the scripted answers that register reads, one per task class |
| `demo.yaml` | — | a synthetic loader spec: a seeded mean-reverting series spanning the research, certification and forward windows |
| `hypotheses/demo_mr/` | — | a hypothesis that ships already classified, with its `program.md` and the sleeve stub |

Everything else `init` writes is the same either way. Delete the three added files and replace
the two rendered ones and you have an ordinary empty workspace.

## Moving, copying and backing up a workspace

A workspace is relocatable. Copy or rename the directory and everything keeps working:
`kanso doctor` is green, and a run in the copy resolves the same pinned snapshot and
reproduces the same baseline metric. (`state.db` does record the absolute path each snapshot
was written at, but that column is written and never read — a snapshot is found by its id
under `catalog/snapshots/`.) Two things are worth knowing before you copy one.

**The skill links point at the installed package**, not into the workspace, so a copy taken to
another machine has dangling links until `kanso skills sync` runs there.

**`state.db` is the half that does not regenerate.** Copy the directory and you have
everything; copy everything *but* `state.db` and you have the data, the certificate files, the
composed implementations and the source of every hypothesis — but no research history, no
best pointer, no certificate of record and no approvals. If you are choosing what to commit,
that is the trade the rendered `.gitignore` makes for you by default, and it is the one line
of it worth reconsidering deliberately rather than by habit.
