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
envelope.yaml
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
| `hypotheses/<id>/hypothesis.yaml` | `hyp new`, `classify`, `hyp explore` and `screen draft` (a draft, in a directory each creates) | **yes**, between runs |
| `hypotheses/<id>/program.md` | `hyp new`, `hyp explore`, `screen draft` | **yes**, between runs |
| `screens/<id>/screen.yaml` | `screen new` | **yes** |
| `screens/<id>/specs/` | `screen run`, the loader specs its fetches were made with | no — a record of what was fetched |
| `screens/<id>/<sha7>-s<snap7>-v<ver7>.yaml` | `screen run` | no — a rendering of the result `state.db` records |
| `demo.yaml` and other loader specs | you (`init --demo` renders one) | **yes** |
| `mock/responses.yaml` | `init --demo` | **yes** — the mock register's scripted answers, one per task class; every `params` is a list of `{name, value}` pairs, the shape a provider constraining an answer accepts and kanso reads back into a map; every `propose` answer carries `tags` from `kanso.schemas.TAGS`, as a real model's must; the script wraps, so a second hypothesis classified against it gets the first one's answer; `{{call}}` in any string of an answer is replaced by the ordinal of the call, which is how a wrapped script still proposes bytes the loop has not carded |
| `kanso_ext/` | you | **yes** |
| `AGENTS.md`, `CLAUDE.md` | `init`, if absent | **yes** |
| `.gitignore` | `init`, `skills sync` (append only) | **yes** |
| `instruments.yaml` | `data instruments resolve` | **four fields only** — see below |
| `portfolio.yaml` | `init`, then certification, `deploy`, `promote`, `demote`, `strat retire` | **stages and limits only** |
| `hypotheses/<id>/strategy.py` | `hyp explore` or `screen draft` for a draft, then research, after every keep that moves the hypothesis's best | no — it is the best-so-far |
| `hypotheses/<id>/results.tsv` | research, rendered from state | no |
| `envelope.yaml` | `env detect` | no — `[env]` in `kanso.toml` is the override |
| `state.db` | kanso | no |
| `catalog/` | `data load`, `sync`, `backfill`, `snapshot`, `instruments resolve` — and the instrument store by `instruments resolve` alone: a validation and a registration resolve in memory, and a run reads the store | no |
| `runs/` | `research begin`, the daemon, `state prune` (its copy of `state.db`) | no |
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
is, the edit is ignored, silently reverted on the next write, refused by name, or believed at
your own risk. Each section below says which.

## What the workspace refuses

Every one of these is a refusal against workspace state, and each is explained where its file
is. Exit 2 is a precondition the workspace or the engine forbids; exit 3 is input you wrote
that is wrong; exit 4 is an operator act that is missing rather than a fault.

| you do this | you get |
|---|---|
| run a command outside any workspace | 2 · `not inside a kanso workspace` |
| `kanso init` over an existing `kanso.toml` | 2 · a workspace is scaffolded once |
| leave a typo in `kanso.toml` | 3 · `unknown key '<section>.<key>'`, from every command |
| write a ticker that is a YAML boolean — `ON`, `OFF`, `YES`, `NO`, `TRUE`, `FALSE` — bare in a loader spec | 3 · naming the field and the ticker's place in it; quote it, `"ON"` (`catalog/`) |
| run anything against a `state.db` behind the schema | 2 · *N* migration(s) behind; `kanso migrate` |
| write windows with no embargo between research and certification | 3 · at `hyp validate`, changing nothing |
| leave `costs` at its defaults on a hypothesis that does not require `quote` data | 3 · at `hyp validate`: no quotes to take a spread from, so `fixed_bps` must be set |
| put instruments whose venues carry different account currencies in one universe | 3 · at `hyp validate`; a hypothesis trades one account currency |
| put an instrument in a universe that settles in a currency other than its venue's account currency, or is booked in one — a data leg as well as a traded one | 3 · at `hyp validate`, naming the instrument, its currencies and the account's |
| hold a perpetual in a universe whose `data_requirements` does not list `funding` | 3 · at `hyp validate`, naming the instrument; a perpetual is known by its resolved definition, not its id |
| give a definition a non-zero `maker_fee` or `taker_fee`, or a perpetual `is_inverse: true` — in `override` or from the reference adapter | 3 · wherever it is built: the runner charges commission once, from the venue model, and kanso trades linear perpetuals |
| resolve a venue to an account currency the engine does not register, from `[research] currency` or `venues.<MIC>.currency` | 3 · at `hyp validate`, and again wherever a venue is funded; the engine would otherwise mint the misspelt code at a precision nobody chose |
| declare `benchmark` on a horizon under a day, or on a construct measured against its host | 3 · at `hyp validate`, on a draft too: no objective measures a hold there |
| add or remove `benchmark` on a classified file without changing `objective.id` | 3 · at `hyp validate`; the remedy names the objective to write |
| declare `book.maintenance_pct` above `100 / max_leverage`, a `reset: monthly` or a non-zero `financing_rate_bps` on a venue whose account is `cash`, or a `book` on an attached construct that is not its host's | 3 · at `hyp validate`: the floor is breached at entry, a cash account funds no restore and holds no borrowed notional, and a construct's version is deployed under the host's policy |
| declare `depth` on a hypothesis whose `data_requirements` does not list `book`, or lists `quote` | 3 · at `hyp validate`: depth is a view of a book the hypothesis holds, and level one reaches `on_quote_tick` from it alone |
| bind a name the strategy's base class owns in `strategy.py` — `self._close = 3`, `def _fund(...)`, `size = 10` in the class body | 3 · at `hyp validate`, naming the name and the line; a warning in `doctor`'s `base names`; the baseline refused at `research begin` (2), and any card that carries it a `discard` by `strategy_integrity` |
| name a `leg_edge` leg the universe does not hold | 3 · at `hyp validate`, from `constraints` or `required_constraints`; an `instrument` parameter names one of the universe's own ids |
| `hyp add` while the hypothesis has an active run | 2 · a run is pinned to the bytes it began with |
| `research begin` on a hypothesis already running | 2 · one active run per hypothesis |
| `research start` twice in one workspace | 2 · the pid file is the lock |
| `research start` or `state prune` while a lane or the monitor of a daemon that is gone still runs | 2 · naming each; `kanso research stop` ends it |
| `state prune` while a card, certification or demotion a lane or the monitor of a daemon that is gone started still runs | 2 · naming the lane or monitor that started it; `kanso research stop` waits for it to end |
| `data load` over a dataset a snapshot names | 2 · **with or without `--replace`** |
| `data load` over unpinned data | 2 · until you pass `--replace` |
| `data load --replace` or `--supersedes` over a dataset no run of whose files both hashes to the checksum its manifest recorded and holds its `row_count` — files altered or removed by hand, damaged by a replace on kanso 0.13 or earlier, come between by another write of its series, or recorded with a file another write of its series landed meanwhile, or a dataset kanso 0.13 or earlier recorded while another write ran in the workspace, whose checksum covers that write's files too — and whose span cannot say which files are its own: it is not `realtime`, a file of its series runs across an edge of its span, or a dataset of its series before it is not `realtime` | 2 · nothing is removed: a removal by span can reach the dataset beside it. Remove its manifest and its files by hand, then load again — without `--supersedes`, which names only a held dataset |
| `data snapshot` over instrument data while the store holds no definition | 2 · a run reads its definitions from the store; resolve first |
| `data instruments resolve` that would change a definition the store holds for the same date | 2 · a correction is explicit: `--refresh` |
| `research begin` after the store's definitions moved from what the newest covering snapshot pins | 2 · by name; `kanso data snapshot` pins what is held now |
| run a warmed hypothesis — `research begin`, `cert run`, `replay run`, `strat compose`, `portfolio deploy` — over a catalog holding fewer sessions before its window than `warmup` asks for | 2 · naming what it found and how far back to `kanso data load`; a snapshot has to cover the sessions too |
| `cert run` on bytes already certified under the same plan and engine | 2 · a certificate is immutable |
| `models check` or `cert plan` with no `models.yaml` | 2 · there is no default plan |
| edit a file under `strategies/<id>/impl/<version>/` | 3 · at `deploy` and at `replay`, before either runs it |
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
it is defined. The sections are `[extensions]`, `[skills]`, `[research]`, `[screen]`,
`[certify]`, `[data]`, `[env]`, `[monitor]`, `[webhook]`, and `[adapters.<id>]`; `[data]` is rendered
commented out, header included, because its two keys — `reference`, naming the adapter that
resolves instruments (default `none`), and `adjusted` (default `false`) — are the defaults
until a vendor is configured, and the table you then append is not declared twice.

`[screen] draws` is how many session sign flips a screen's null is drawn from (`docs/concepts.md`,
Screen): a precision rule like `[research] folds`, which bounds the measurement and chooses
nothing; it is recorded on every result, and a smaller p than `1 / (draws + 1)` cannot be read.
A measure over S sessions whose 2^S vectors of signs are no more than `draws` takes each of them
once instead, and its p is exact — seven sessions under the default are 128 vectors.

The two top-level keys are written by `init` and read by nothing: `kanso_version` records
the kanso that scaffolded the workspace, and `schema_version` is not the schema guard —
the version a `state.db` is at is its own `PRAGMA user_version`, which `kanso migrate`
advances and `kanso doctor` compares against the package. Any value of either key that
parses changes nothing, and deleting `schema_version` is a validation failure like any
other missing key.

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

```toml
[adapters.okx]
region = "us"             # global | eea | us: the regional host that accepts the account's key; no default
rate_per_second = 5       # the quota kanso's own requests to the public API share; the engine's own clients meter themselves
```

A broker's table is read by `kanso doctor` through that broker's model whether or not it is
there, and one the model refuses fails the `execution` check — an `api_key` pasted in
included, since no table holds a credential. `docs/adapters.md` lists every adapter's keys.

`[research] broker` is the single place the core lets a broker's name in: it says whose venue
model — account type, currency, costs — research inherits. A workspace naming a broker it has
no adapter for falls back to the two `[research]` keys below and then to the shipped venue
defaults rather than refusing.

`account` and `currency` are the account type and the **account** currency of every venue
nothing else declares. A venue's model is resolved in a fixed order of precedence — the
shipped defaults, then these two `[research]` keys, then the broker's declaration, then
`venues.<MIC>` in `portfolio.yaml`, then the hypothesis's own `costs` — and every card,
certificate and version records where each field came from as one of `default`, `config`,
`broker`, `venue_override` or `hypothesis`. So a broker's declared currency still wins over
`[research] currency`: to trade a venue the broker serves in another currency, override it
under `venues.<MIC>`. A value that restates the shipped default (`margin`, `USD`) is not a
layer and leaves the origin at `default`, which is why a workspace that never touched these
keys resolves the same model it always did. The code must be one the engine registers, as
fiat or as crypto (`USD`, `EUR`, `USDT`, ...): the grammar admits any code of two to eight
capitals and digits, and `kanso hyp validate` refuses one the engine does not register
(exit 3), naming the code and the two places to set it. Whichever layer it came from, it
is the account currency that `kanso hyp validate` checks: a universe whose instruments sit
on venues with more than one account currency is refused (exit 3), because a hypothesis
trades one. Every instrument of the universe — a data leg as well as a traded one — is
compared with its venue's account currency twice: the currency it settles in (a perpetual's
`settlement_currency`, a currency pair's quote currency, every other instrument's
`currency`) and the currency the engine books its positions, PnL and margin in (its quote
currency, a perpetual's included). Both must be the account's, and a mismatch is refused
(exit 3), naming the instrument, what it settles in, what it is booked in where the two
differ, and the account currency, with the two places to set it: a leg booked in a currency
the account holds none of is converted at a rate nothing in the workspace records, and the
engine defers the balance update when it has none. A perpetual quoted in USDT and settled in
USDC settles in one and is booked in the other, so no account currency admits it and the
remedy is to take it out of the universe. So a workspace trading USDT-quoted, USDT-settled
perpetuals sets `currency = "USDT"` here.

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

**Two `kanso migrate` at once is one migration.** The write lock serialises them, and each
migration re-reads the stamped version while it holds that lock, so the one that arrives
second applies nothing and reports `nothing pending`. That second reading is what keeps
the schema and the number moving together: a migration applied to a database that has
already moved past it would stamp its own older version over the newer one and leave a
`state.db` whose schema no `kanso migrate` could reach again.

**What grows, and the one thing kanso gives back.** Every judged card stores its book — what
it held, session by session, and the number it earned — so the redundancy rule can refuse
the next spelling of the same bets (`docs/concepts.md`). A book is read only under the pins
of the run asking, and nothing deletes one as research moves on, so the books stored under a
file since re-pinned, an earlier kanso, a snapshot a newer run moved past or a retired
hypothesis stay, read by nothing, and can come to be nearly the whole file: measured on
2026-10-02 in a live workspace, 3,025 MB of books in a 3,399 MB `state.db`, of which 250 sat
under pins a run could still be given. `VACUUM` alone gives back only free pages, and there
are none until rows are deleted. `kanso state prune` deletes the books no run can select —
with the daemon stopped, after copying the whole file to `runs/state-<instant>.db`, which
the `.gitignore` `init` writes keeps out of git — and rewrites the file; `kanso state prune --dry-run` says what it
would delete while the daemon works. The copy is yours to delete — or, if a book it held is
wanted after all, to put back: stop everything, delete `state.db-wal` and `state.db-shm`, and
put it in `state.db`'s place, which also loses whatever was recorded since the prune. No
card, no best, no trial count and no certificate is touched by a prune: a book deleted whose
pins come back is re-earned by the next run, one card at a time.

**Deleting `state.db` is not a reset — it is a loss.** The next command creates an empty
database, reports it behind by every migration this kanso ships, and after `kanso migrate`
the workspace has no
registered hypotheses, no cards, no best pointer, no certificate of record and no approvals —
nor any record of what a source answered empty, so a backfill asks a source again for a gap
it already answered. Coverage is untouched: it is read off the manifests and the market's
calendar, never off an answer.
What survives is what is file-backed: `catalog/` still serves its data, `certificates/<hyp>/`
still holds the certificate YAML and the certified `<sha7>.py`, `strategies/<id>/` still holds
`strategy.yaml` and its `impl/` directories, and `kanso strat show` and `kanso replay
--strategy` both answer from those files — a committed version replays from its `impl/` and
its committed `hypothesis.yaml` without the record. Re-running `kanso hyp add` re-registers
the hypothesis as `classified` with no best and no certificate. Two things the record kept
are enforced by the files instead: re-certifying the same bytes under the same plan and
engine is refused by the certificate already on disk, not only by the row that is gone (so
the trial count in the filename cannot be quietly reset), and a version the record no longer
knows is not deployed — `deploy` and `portfolio show` agree it is down. Back it up with the
workspace or accept that the research history is the part that does not travel.

## `hypotheses/<id>/`

Four files, and the boundary between you and kanso runs right through the directory.

`hypothesis.yaml` is **yours**. It states the idea: the thesis, the mechanism, the universe,
the horizon and resolution, the data requirements, the risk limits, the three windows, and —
once classified — the construct, the objective and the card-stage constraints. `kanso classify`
writes those last three and nothing else; you can equally write them yourself and run
`kanso hyp add`, which needs no model at all. The file's own comments are its field reference.

**`required_constraints` is the one classification cannot touch.** It holds card-stage gates
you require, in the same shape as `constraints`, and classification neither reads it as
something to rewrite nor writes it: it owns three keys of the file and this is a fourth, so it
survives a re-classification with your comments and ordering intact. Both lists are evaluated
on every card, yours first, and where both name a gate yours stands and the classifier's entry
is dropped — a model may add to what you require and may not remove or reprice it. Naming a
gate twice in your own list is refused (exit 3), because the two entries would disagree and
nothing says which wins. A draft may carry it: these are yours, so they do not wait on a
classification.

```yaml
required_constraints:
- id: position_size          # every position worth 95% to 105% of the capital, always
  params: {min_pct: 95.0, max_pct: 105.0}
- id: leg_edge               # the hedge leg's own spells clear a Sharpe of zero, every fold
  params: {leg: DEMO.SIM, min_sharpe: 0.0}
```

Before this existed, an instruction like that could only be prose in `program.md`, which
nothing enforces: `risk_limits` are three ceilings, and a strategy holding a tenth of what you
asked for satisfies every one of them.

**`sizing` is yours too, and it is not a gate.** A gate judges after a backtest has been
paid for; this moves the size of every order to the harness, so a proposal that sizes wrong
cannot be run at all:

```yaml
capital: 30000
sizing:                            # scope: adding or changing it clears `best`
  mode: full_book                  # every entry is the whole budget in one instrument, whole lots, at market;
  budget: 30000                    # one instrument held at a time; every exit the whole position; no size argument
```

Under it `submit_entry(id, side)` and `submit_exit(id)` take no `notional`, `qty` or
`price`; `self.held(id)` is the position reader; a flip is `submit_exit(old)` then
`submit_entry(new, side)` in one handler. The budget is filled in whole lots of the
instrument, and on a multiplied instrument — a future, an option — the price and the tick it
is divided by are one contract's, `price x multiplier`, as is every notional the harness
sizes, reserves or reads back on either path. Without the key, `submit_entry(id, side,
notional=…)` sizes to the smaller of what was asked and what the risk limits leave — read on the smaller
of the capital and the balance the sleeve has left, so an account that has lost money cannot
borrow to keep its size, and one that has made money does not grow past its capital — and on
both paths the limits and `self.held(id)` are read with the sleeve's own unfilled market
orders applied, so the same flip fits at leverage one either way: the exit in flight frees
the room the entry takes, and the venue settles both at one price, the exit first.
`modify_order` is the engine's and is neither cut nor refused: an entry grown or re-priced by
a modify is held to no ceiling until kanso holds it to one (`docs/backlog.md` row 123).
**An exit never goes past flat, counting the exits still working.** `submit_exit` closes the
smaller of what was asked and what is left to close: the position less the unfilled quantity
of every order of the sleeve's own on the closing side that the venue has not closed —
resting, in flight to it, or, under a stated latency, waiting on a cancel that has not
landed — and returns `None` when those already close it. Every such order counts in full,
an exit or not, so a stop that would reverse the position, or both legs of a bracket only one
of which can fill, leave that much less to close. The stop-loss and take-profit of a bracket
whose entry has filled nothing do not count: they can fill only after the entry and close
what it opens, and neither the venue nor the order emulator has them open to cancel; once the
entry has filled any of it they count in full. With no latency stated, an exit at market
is never held back by a resting one: when the sleeve's own limit or stop orders resting at
the venue on the closing side would leave it less than asked, it cancels them first, so a
stop is not blocked by a take-profit, and a take-profit left above the market cannot fill
after the stop has closed. Under a latency the cancelled orders can still fill until their
cancels land, so the exit is cut to what they leave and the rest is owed (`costs.latency_ms`,
below); an order of the sleeve's still in flight to the venue, modified or not, is not
cancelled and counts, and what it cuts from an exit at market is owed at any latency, and
paid once the venue holds that order open and the owed exit has cancelled it. An order the
venue holds whose modify has not been answered yet, one sent in the same handler among them,
counts too, and an exit at market does not cancel it at once: on a node a cancel sent in the
handler that sent the modify would overtake it, where the backtest lands the modify first.
With no latency stated it cancels it as the venue answers the modify; under a latency it
cancels it on the next point, before the sleeve's handler for it, whether or not the venue
has answered the modify by then, and the cancel still lands behind the modify, which was
stamped first and waits the same latency. So on both paths the modify lands first, filling
the order at once if it made it marketable, and then the cancel; what the order cut from the
exit is owed and paid as for any cancelled order, a sleeve that modifies that order on every
point included. With no latency stated the venue answers both before the
next point, on both paths, so the order is cancelled as the venue takes it or answers its
modify, and the owed exit goes out as soon as the order is closed — cancelled, filled or
refused — in the instant it was asked for: an exit at market asked for on a session's last
point, or on the window's, is not carried past it. Under a latency the owed exit is paid
on a later point, which for one asked on a session's last point is in the next session, and
on the window's last never. A resting order whose cancel the venue refused is cancelled again. **An order the engine's order
emulator holds** (one sent with an `emulation_trigger`) has not reached the venue: it counts
until it is cancelled, an exit at market cancels it with the resting ones, and its cancel
takes it out at once, at any latency; `cancel_orders` cancels it on its own, through the
emulator, and batches the rest. An attached exit rule closes through the same market exit.
`self.held(id)` applies the sleeve's market orders in flight and no limit or stop order, so
a sleeve whose exit rests at the ask reads the whole position there until the exit fills; an
exit sized from it is cut to what the working ones leave.
`self.balance` is what the sleeve's account is worth at that moment — the capital, less what
its fills paid and were charged, plus its positions marked at the last print — the number the
equity curve strikes at each period end, and one a strategy may size from. Reading it on every
bar costs what the sleeve's orders gained since the last read, however long they have lived,
so a resting order moved on every bar need not be re-posted to keep a card fast; re-posting
does not save memory either, since the engine keeps every order's events for the run
(`docs/concepts.md`). `strategy_integrity` discards a `strategy.py`
that names a size knob, builds an order by hand or reads `self.portfolio`, and — sized or
not — one that overrides a harness method or binds any other name its base class owns
(`docs/concepts.md`), with the line and what to write instead; what the scan cannot see — a second
instrument while one is held, the other side of a held name — is refused inside the handler,
the run stops, and the card is a `discard` whose `sizing` gate carries the rule, the
instrument, the instant and the book held. `position_size` under the rule judges entry fills
over the budget, gathered per order, and is the backstop that should never fire. Classification
never touches the key. `kanso hyp validate` refuses (exit 3) a budget above what
`max_position_pct` or `max_leverage` admits, a rule on a `filter` or an `exit`, a budgeted
overlay on an unbudgeted host or the reverse, an overlay whose host budget and own budget
together exceed what its own `capital × max_leverage` funds or whose host budget exceeds its
own position ceiling, and a filter or exit rule whose `resolution` is not its host's. An
overlay's `capital` is the whole book its cards run on — host budget and its own — and its
clips are sized to its own budget (`docs/constructs.md`).

**`fixed_params` names the numbers that are not knobs.** Every numeric field an author adds
to the strategy's `Config` is a parameter the `param_plateau` gate moves a little each way
and re-scores; a selector among rules, a clock constant or a size the hypothesis sets is a
number the strategy reads, not one it was tuned on, and moving it tests a different strategy
rather than the same one nearby. Name those in `fixed_params` and the gate leaves them where
they are:

```python
class Config(KansoConfig):
    gate: float = 5.0  # which of ten state rules admits a post
    g1: float = 1.75  # that rule's threshold: a knob
    start_minute: float = 575.0  # a clock constant
    fixed_params: tuple[str, ...] = ("gate", "start_minute")
```

A name that is not a numeric field of the class is refused when the strategy is built
(`strategy.py: Config: fixed_params: … is not a numeric field`), so a typo cannot quietly
fix nothing.

**`warmup` is yours, and it is scope.** A strategy's indicators start empty, so without it
the first sessions of every window are spent filling them and the run is measured cold;
with it the runner feeds the strategy the sessions before the window and drops every order
until the window opens:

```yaml
warmup:                            # scope: adding or changing it clears `best`
  sessions: 20                     # sessions before the window fed to the strategy before it may trade
```

A session is a calendar day on which any instrument of the universe printed at the
sleeve's grain — the host's, for an attached construct consulted on a coarser or finer
one; the runner takes the last `sessions` of them before the
window from the catalog, so the prefix is trading days rather than calendar days and a
weekend or a holiday adds nothing. Over the prefix every handler runs and every attached
overlay's `on_data` is asked, so their state warms too, but `submit_entry` and
`submit_exit` return `None` — the same answer a refused filter gives — and nothing fills:
the measured run begins at the window's first point with no position and no cold start. A
card, a certificate, a replay and a stage node all warm on the same rule; a stage restart
warms on the sessions at or before its clock, and a stage-mate's prefix warms nobody else:
a version on a stage is handed only the sessions its own file asks for, whatever the
versions beside it declare. `research begin` refuses (exit 2) when the
catalog holds fewer sessions before the window than the file asks for, naming what it
found, and the snapshot it pins must cover the prefix as well as the two windows. Adding
the key re-pins the hypothesis under a new sha and clears `best`, exactly as `sizing`
does, and a certificate earned cold refuses to compose under it. The window's own bounds
are unchanged: the upper bound of every window is what it was, and only the data fed
before it widens. An attached construct declares the same `warmup` as its host, or
`kanso hyp validate` refuses it (exit 3), because its cards run the host underneath it.

**`data_by_instrument` narrows what one instrument must carry, and it is scope.** A
universe that mixes sources can hold a type for some instruments and not others: a crypto
exchange's prints beside the quotes of the US equities that follow it, where the exchange
serves no historical quotes.
`data_requirements` lists every type the hypothesis reads, and `data_by_instrument` gives a
listed instrument its own subset:

```yaml
data_requirements: [quote, trade]
data_by_instrument:                # optional, and scope: adding or changing it clears `best`
  ETH-USD.COINBASE: [trade]        # the signal: the exchange's prints alone
```

A run's snapshot must cover each instrument for its own list only. Of the market data — bars,
quotes, prints and a book — the runner reads a listed instrument nothing else and the strategy
is subscribed to nothing else, so backtest, replay and a stage node see the same data. A custom
type is narrowed by coverage alone: the runner reads its points for every name of the universe
the catalog holds them under, and a strategy subscribes to the type rather than to an
instrument, so leaving a custom type off one instrument's list relaxes what the snapshot must
hold for it, not what the strategy is handed. Each list is a non-empty subset of `data_requirements`; an instrument outside
the universe, a type outside the list, or a type in `data_requirements` no instrument is then
asked for is refused at `hyp validate` (exit 3). `funding` is still asked of a perpetual alone.
`kanso screen draft` writes it whenever the cell's two legs read different types. An
instrument not listed is asked for every type in `data_requirements`, and so is one listed
with all of them. What each instrument is asked for is scope: a rule handed one leg's prints
and one that never saw them measured different runs over the same days, so adding, changing
or removing an entry that narrows an instrument clears `best` with a `best_cleared` event
naming `data_by_instrument`, and a certificate earned under another refuses to compose.
Listing an instrument with every required type, or reordering a list, changes nothing an
instrument is shown and clears nothing.

**`session_scope` is yours, and it is scope.** A universe of a thousand names cannot be
fed to a strategy whole at an intraday grain: the runner reads every name's bars a session
at a time, the card holds a session of them beside what it has folded, and a session over
that many names is refused by the lane's memory cap long before it trades. A scope delivers each name's market data only on the sessions a series
you built says it is in play:

```yaml
data_requirements: [bar, open_context]
session_scope:                     # scope: adding or changing it clears `best`
  series: open_context             # a custom type the hypothesis requires, one point per name per session
  flag: candidate                  # its integer field; above zero, the name's bars of that session are delivered
  always: [SPY.ARCX]               # names delivered every session, whatever their point says
```

The series is a custom type of your own (`kanso_ext/`, `docs/extensions.md`), filed under
every name of the universe with one point per session and stamped before the session's
first market point — at 09:29 New York for a regular session, say — so the rule that
admits a name is written down before the session it admits opens, from what was public
then: the previous close, the pre-market tape, a filing, a calendar. The runner reads the
series first and then loads a name's bars, quotes and trades of a session only when its
point of that session carries the flag above zero; a name in `always` is loaded every
session. What is not loaded is not in memory, so a card's memory is the names in play
rather than the pool. The series itself is delivered in full, as any requirement is, so a
strategy reads the same points the runner scoped on. A replay and a stage node subscribe
to every name and would be handed everything: the strategy base drops any bar, quote or
trade of a name on a session its flag did not admit, so the two code paths see the same
market and `kanso replay parity` holds. `series` must be one of the custom types in
`data_requirements` and `always` a subset of the universe, or `kanso hyp validate`
refuses the file (exit 3). A position held into a session its name is not admitted on is
not marked that session, exactly as an instrument that stopped printing is not. Adopting,
changing or dropping the key re-pins the hypothesis and clears `best` with a
`best_cleared` event naming `session_scope`, and a certificate earned under another scope
refuses to compose; the order of `always` is not scope.

A file could state `session_scope` from 0.12.0, and `data_by_instrument` from the build that
added it, before either was scope, so a row pinned before they joined holds neither in its
pins. Its re-pin compares against what the file it pinned states, read back from the state
store, exactly as `costs` is; only when the store no longer holds those bytes, or this kanso
cannot read them as a hypothesis, does a missing key read as unchanged. A hypothesis whose
file still scopes the same way keeps its best across the upgrade — and so does one whose
`session_scope` moved under 0.12.0 to 0.13.x, when no re-pin cleared on it, though its best
was measured under the scope before. End its run if one is active, and `kanso research begin
ID --from-workspace` starts it over under the file as it is.

**`benchmark` is yours, and it is scope.** A strategy trading one instrument can show a
healthy Sharpe by holding that instrument through a rising market. Declaring a benchmark
makes the objective the strategy's Sharpe *over* a hold of the universe's first leg:

```yaml
benchmark:                         # scope: adding or changing it clears `best`
  hold: first_leg                  # buy universe[0] on the first point it may trade on, never exit
```

The hold is not arithmetic on prices. It is a sleeve kanso ships
(`src/kanso/templates/strategy_hold.py`) run by the same runner as the strategy — the same
window, snapshot, venue model, costs, splits, capital, sizing budget, warmup and `book`
policy, with every order in the warmup dropped like the strategy's — and never gated by `max_hold`, because it
is a benchmark rather than a card. Classification then selects `wf_sharpe_vs_hold` instead
of `wf_sharpe_net` (`docs/constructs.md`): the strategy's fold-wise Sharpe minus the hold's,
fold by fold, so the keep rule's standard error is the paired one. The hold is run on every
path that measures the objective — each card of a run (once per run, in a child of the lane
as a card is, then reused), both
certification windows (a `param_plateau` perturbation moves the strategy and never the
hold), the expectation composition measures, and every window a stage node closes, where it
is stored beside the version's realised run for the paper and live gates. `kanso hyp
validate` and `kanso hyp add` refuse (exit 3), naming `benchmark`, a benchmark on a horizon
under a day, which is measured per trade — on a draft as on a classified file, so
`kanso classify` is never asked to classify one — and, on a classified file, on a construct
measured relative to its host (every attached construct but `alpha`). The leg held is scope
with the key: reordering the universe of a hypothesis that declares one clears `best`.

On a file already classified, the key is adopted in the same edit as `objective.id:
wf_sharpe_vs_hold`, its parameters unchanged — or by clearing `construct`, `objective` and
`constraints` and running `kanso classify` again. Adding the key alone is refused (exit 3):
`wf_sharpe_net` no longer applies, and the remedy names the id to write; removing the key
alone is refused the same way. The re-pin that adopts it clears `best` with a `best_cleared`
event naming both fields that moved, `objective` and `benchmark`, and `strat compose`
refuses (exit 2) a certificate earned before it, naming the same two.

**`book` is yours, and it is scope as a whole.** Without it the book is what the fills
leave: a strategy that made money compounds on its gains, one that lost keeps trading on
what is left, borrowing costs nothing and nothing floors the margin. With it the runner
applies a policy at every period end, once, in the extraction, and the harness mirrors it
so `self.balance` reads the same book:

```yaml
book:                              # scope: adding or changing any key clears `best`
  reset: monthly                   # the book returns to `capital` at the first period end of each month
  financing_rate_bps: 250          # per year, on what the book holds above its equity, shorts included
  maintenance_pct: 25              # floor for the `maintenance_margin` gate, in percent of gross
```

`reset: monthly` moves a surplus over `capital` into a cushion outside the book and restores
a deficit from that cushion while it lasts — never by borrowing — so a strategy is measured
on the same book every month. A drawdown is bounded by the month it fell in: the peak starts
again at each month's first end, so a surplus swept out is no loss, and a loss the cushion
could not restore carries on as a drawdown from `capital`. The
transfer is not a return: returns are struck before it, and the run carries the cushion
beside the equity curve. `financing_rate_bps` is charged per year on the notional held
above the book's equity — gross exposure with shorts counted, less what the account is
worth — once per return period, by the runner, in the extraction, as its own `carry`
series; `cost_stress` multiplies fill costs and leaves it alone. `maintenance_pct` is the
floor the `maintenance_margin` gate holds: each period's end-of-period holdings valued at
the period's adverse extreme — longs at the lowest low, shorts at the highest high since the
previous end — over their gross. A card is refused below it when its constraints include
`maintenance_margin` (list it in `required_constraints` to hold every card to it), a paper
window when the sleeve's do, and a composed version whenever the floor is declared: `strat
compose` refuses (exit 2) a version whose own run over the certification window falls
through it. `kanso hyp validate` refuses (exit 3) a floor above
`100 / max_leverage`, which a book levered to the ceiling breaches at entry; a reset or a
carry on a venue whose account is `cash`, which can neither fund a restore nor hold a
borrowed notional; and an attached construct whose `book` is not its host's, because its
cards run the host under its own file's policy and its version is deployed under the
host's. Classification never touches the key.

On a paper or live stage the node restarts flat at the version's capital every window, as
it always has. What carries across a restart is the policy's own state, read from the
version's newest recorded window on that stage that measured a period: the cushion it closed
with, and its last period end, against which the next window's first month turn is judged.
The first carry is charged from the instant the restart resumes trading, not from that end:
every window ends flat, so the time the version spent off the stage — a stop, or a tenure
on live before a demotion back to paper — held nothing to borrow against. A new version, or
a version on a stage it has not run on, starts with nothing set aside.

**`depth` is yours too, and scope.** A hypothesis that holds a level-two `book` hands every
change of it to the simulated venue, which keeps its queues and matches against it, and,
without the key, to the strategy's `on_order_book_deltas` as well. An account rarely sees a
book that way. Measured on 2026-10-02 against OKX's public feeds: the depth channel an
ordinary account subscribes to is a snapshot every 100 ms, the change-by-change book is
served only from the fourth fee tier, and the top of the book is free every 10 ms. A rule
that earns on every change of depth earns on data its account cannot have. `depth` hands the
strategy what the account sees, and leaves the venue every change:

```yaml
depth:                             # scope: adding or changing it clears `best`
  every_ms: 100                    # the book the strategy sees, as of each multiple of this
  levels: 3                        # of each side, from the best: 1 to 400, at most the levels loaded
```

Under the key the strategy's `on_order_book_deltas` is handed the top `levels` of each side
as they stood at each multiple of `every_ms` after the epoch — UTC-aligned — as one batch of
the changes since the view before it, stamped with that instant (`data_time` reads it), at
the first point of data published after it, and only when something it shows moved. Its
`on_quote_tick` is handed level one — the best bid and offer with their sizes — once for every
instant whose changes, all applied, left the top where it was not, never partway through
an instant and never later than it: at the change itself, stamped with it, before any later
point of the feed. Neither is delayed again: an order sent from either reaches the venue
`costs.latency_ms` after the point that carried the call, as every order does, because the
latency is the whole round trip, feed and order together. The quote is a signal and moves
nothing else: `last_quote` and `last_price` do not read it, the book marks no position, and
`spread: quotes` has no quote series to read. What else the strategy may not touch under the
key — the message bus and the engine's own book subscriptions — is in `docs/concepts.md`,
The embargo. An attached construct researched or composed under the key is handed no book
at all, neither the view nor a subscription of its own — asking for one is refused, and
the card crashes naming the call; it reads its host's prices from the context it is asked
with. A strategy that reads level one alone sends and fills the same on any grid,
since the venue is handed every change whatever the strategy is shown. The same harness code
runs on both code paths, and a deployed stage is configured with the key as its card was.
An exit the harness still owes the strategy — an exit at market a cancel in flight cut — is
asked for again on every change of the book, whether or not the strategy is shown anything
there, and one paid on a change is stamped with that change, after level one is handed,
never with the earlier grid instant of a view handed on it.

`levels` is 1 to 400, the depth of the sampled channel the key models (measured on
2026-10-02 against OKX's: 400 levels a side every 100 ms; its archives hold 5,000 a side at
one second, which no account sees live), and `kanso hyp validate` refuses (exit 3) any other
value, naming `depth.levels`. It may not usefully exceed the levels the book was loaded at
either: a loader that keeps the top K of an archive leaves a level pushed past K at its last
size rather than deleting it, so past K the book holds stale levels. `kanso hyp validate` refuses (exit 3) `depth` on a
hypothesis whose `data_requirements` does not list `book`, and one that also lists `quote`,
because a quote series would be a second source of `on_quote_tick` that neither the strategy
nor a replay could tell apart. Classification never touches the key.

`costs` is optional, with one case the scaffold's comment names: a hypothesis whose
`data_requirements` do not include `quote` has no quotes to take a spread from, so it must
set `spread: fixed_bps` and a `fixed_bps` width itself, or inherit one from
`venues.<MIC>.costs` in `portfolio.yaml`. The shipped broker declaration supplies a
commission and no spread, so under the defaults a bar-only hypothesis is refused at
`hyp validate` and `hyp add` (exit 3), naming `costs.fixed_bps`; the demo hypothesis carries
the block for exactly that reason. The block is scope: every metric is net of it, so a best selected under one schedule
is gross of what another charges, and `hyp add` clears `best` when any key of it moves,
`latency_ms` included, as it does for `sizing`. A row pinned before 0.13 recorded no costs
in its pins, so its re-pin compares against the costs the file it pinned states, read back
from the state store; only when the store no longer holds those bytes, or this kanso cannot
read them as a hypothesis, does the missing key read as unchanged.

`costs.maker_bps` charges a fill that rested on the book apart from the rest:

```yaml
costs:                             # scope: changing any key clears `best`
  commission_bps: 0.35             # every fill that took liquidity pays these three
  slippage_bps: 0.5
  spread: fixed_bps
  fixed_bps: 1.0
  maker_bps: -0.2                  # a fill that rested pays this alone; negative is a rebate
```

A fill the venue reports as a maker's — a limit that waited on the book until the market
reached it (`docs/concepts.md`, Delivery) — pays exactly `maker_bps` of its notional and
nothing else: no slippage, because it filled at its own price, and no half-spread, because
the spread is what a resting order earns rather than pays. Negative is a net rebate, the way
a per-share-priced account that pays for displayed liquidity can come out ahead on a fill
that rested. Every other fill — a market order, a limit that was marketable when it arrived
— is charged commission, slippage and half the spread exactly as before, and so is a maker's
fill under a model that states neither `maker_bps` nor `maker_per_share` (below): leave the
keys out and no number moves. It is
applied where every cost is, once, in the runner's extraction, and `self.balance` books the
same rate. `cost_stress` multiplies a charge and divides a rebate, so a stress of one or more
never lets a fill earn more. What a sleeve reserves when it sizes is the larger of the maker
rate and the others, since an order cannot know whether it will rest. A fill a broker reports
with no liquidity side is charged as a taker's. The key is inherited like the rest of the
block, and a layer can restate it but not remove it.

`costs.commission_per_share` states a commission per share beside the one in basis points:

```yaml
costs:
  commission_bps: 0.0
  commission_per_share: 0.0055     # $0.0055 a share on every share that pays commission
  slippage_bps: 0.5
  spread: fixed_bps
  fixed_bps: 2.0
  maker_bps: 0.0                   # a fill that rested pays this alone, per share included
```

A per-share-priced account charges a cheap share more of its price than a dear one — $0.0055
is 5.5 bp of a $10 share and 0.2 bp of a $300 one — and a flat rate in basis points cannot say
so over a universe that spans both. The per-share commission is charged on every share of a
fill that pays commission at all: a taker's, and a maker's under a model that states neither
`maker_bps` nor `maker_per_share`. Per share means per contract on a multiplied instrument, whose notional is the
price times the contract multiplier, and so does `sell_fee_per_share` below. A maker's fill
under a stated maker schedule — `maker_bps`, `maker_per_share` or both — pays that schedule
alone, because it is by contract the whole charge on that fill; a per-share-priced account
states its maker charge as `maker_per_share`, commission less any rebate. It is applied
where every cost is, once, in the runner's extraction; `self.balance` books the same; what a
sleeve reserves when it sizes includes it at the price it sizes at; and `cost_stress` multiplies it with the rest, since it
is part of the recorded cost of the fill. Zero unless stated, so no number moves for a model
that does not name it.

`costs.maker_per_share` charges a fill that rested on the book per share, the way an account
priced per share charges it:

```yaml
costs:
  commission_bps: 0.0
  commission_per_share: 0.0040     # a taker's fill: $0.0040 a share, on top of …
  slippage_bps: 0.5                # … its slippage and the quoted half-spread
  spread: quotes
  maker_per_share: 0.0040          # a fill that rested: $0.0040 a share and nothing else
```

A fill the venue reports as a maker's pays exactly `maker_per_share` on each share — on each
contract of a multiplied instrument — and nothing else: no commission in basis points or per
share, no slippage, no half-spread. `maker_bps` and `maker_per_share` are one maker's
schedule: a model that states either charges every maker's fill `maker_bps` of its notional
and `maker_per_share` on each share, the one it leaves unstated counting as nothing, so a
model stating both charges both, and one stating neither charges a maker's fill what any
fill pays. Like every key of the block the two are inherited key by key — a broker's
declaration, then `venues.<MIC>.costs`, then the hypothesis — so `maker_per_share` stated over
a layer that states `maker_bps` charges the two together; state `maker_bps: 0.0` beside it to
charge the share alone. Negative is a rebate. A sale pays the sell-side fees below on top,
as every sale does. It is applied where every cost is, once, in the runner's extraction;
`self.balance` books the same; what a sleeve reserves when it sizes takes the larger of it and
`commission_per_share` at the price it sizes at, since an order cannot know whether it will
rest, and a rebate reserves nothing of its own; `cost_stress` multiplies it with the rest and
divides it where it is a rebate; `cost_scenario` states it key for key. A screen's hurdle is
two taker fills, so neither maker key moves it. A fill a broker reports with no liquidity
side is charged as a taker's (`docs/backlog.md` row 94). Unset unless a layer states it, so
no number moves for a model that does not name it.

`costs.slippage_ticks` charges a fill that took liquidity in the instrument's own ticks, the way
an account that takes at the touch states what it pays to take:

```yaml
costs:
  commission_bps: 0.0
  commission_per_share: 0.004      # a taker's fill: $0.0040 a share …
  slippage_ticks: 1                # … and one tick over its fill on each share, within its limit
  maker_per_share: 0.004           # a fill that rested: $0.0040 a share and nothing else
  slippage_bps: 0.0
  spread: fixed_bps
  fixed_bps: 0.0                   # no half-spread on top of a fill at the touch
```

A fill the venue reports as a taker's — a market order, or a limit that was marketable when it
landed — pays `slippage_ticks` times the instrument's price increment on each share, read from
its definition, so one tick is a cent on a name quoted in cents, $0.0001 on a sub-dollar one
and the contract's own step on a crypto instrument; on a multiplied instrument it is per
contract, at the increment times the multiplier. A fill the venue reports as a maker's never
pays it: it filled at its own price. An order that carries a limit is never charged past it:
on each share the charge is capped at what the limit leaves above the fill's price for a buy,
or below it for a sale, so a buy limited at 10.02 that fills at 10.01 pays one cent of a
stated two ticks, and one that fills at its own limit — a limit priced at the touch, taken
there — pays none of it; a market order carries no limit and pays it whole. It is applied
where every cost is, once, in the runner's extraction, which records each fill's increment
and the limit its order carried when it filled; `self.balance` books the same; what a sleeve
reserves when it sizes includes it whole at the price it sizes at, beside the per-share
commission, and takes the larger of the two and `maker_per_share`; `cost_stress` multiplies
it with the rest; `cost_scenario` states it key for key and charges it on each recorded
fill's increment and limit — a fill recorded before v0.15.0 kept neither, so a scenario
charges it no tick, and a card is re-run before one is re-applied to it; and a screen's
hurdle charges it whole on both of its taker fills. Zero unless stated, so no number moves for
a model that does not name it. It states as a cost what the simulated venue does not do to
the price: the venue fills a taker at the touch it matched, and this key is the tick past it
the account pays, applied in the extraction like every other charge rather than by moving
the fill (`docs/backlog.md` row 157).

`costs.sell_fee_bps` and `costs.sell_fee_per_share` charge every sale on top of the rest,
whoever the venue reports the fill as:

```yaml
costs:
  commission_bps: 0.0
  commission_per_share: 0.0025
  slippage_bps: 0.5
  spread: quotes
  maker_bps: -0.2                  # a fill that rested earns this alone …
  sell_fee_bps: 0.206              # … but a sale still pays $20.60 per million of notional
  sell_fee_per_share: 0.000166     # and $0.000166 a share, as the account passes them through
```

A regulatory transaction fee is charged on sells alone, per notional, and a trading activity
fee per share sold, and an account passes both through whatever the fill's liquidity side:
a maker's sale under a stated maker schedule pays that schedule and these on top, where the
per-share commission does not. Before the keys existed the only way to state them was a
larger `maker_bps` on both sides, which charges a purchase for a fee it never pays. They
are applied where every cost is, once, in the runner's extraction; `self.balance` books the
same; what a sleeve reserves when it sizes includes half of each, since a round trip pays
them once and the reserve is struck per side; `cost_stress` multiplies them with the rest,
and `cost_scenario` states them key for key like any other. Zero unless stated.

`costs.limit_fill` is the one key of the block that is not a charge: it is how the simulated
venue fills a limit order resting on the book.

```yaml
costs:
  limit_fill: through              # touch (default) | through | print_through | print_through_whole
```

Under `touch`, the engine's own rule, a resting buy fills the moment the market reaches its
price — a bar whose low is the limit, or a print at it. Under `through` the market has to go
beyond it: a low one tick under the buy, a high one tick over the sell, or a print past
either, and the order then fills at its own price — for all that is left of it, whatever the
size of the print or quote that went past, though a point beyond two resting orders fills only
the better-priced (`docs/concepts.md`, Delivery). A print counts only from the side that can
trade with the order — a seller's print or one with no aggressor for a buy, never a buyer's
(`docs/concepts.md`, Delivery). It is deterministic either way — the venue's fill model is
asked with a probability of exactly one or exactly zero and draws nothing — and it reaches
every run of the hypothesis alike: a card, a certificate, a replay on either code path and a
stage, whose simulated exchange is built from the same venue configuration a card's is. On
quotes it withholds less than its name suggests: the engine asks it only when the order's own
side of the book is at the price, so an ask that falls exactly to a resting buy fills it under
either rule, by the size it shows and again at every quote that shows it, an ask below the buy
fills all of it (only the best-priced buy, when several rest above it), and only a market
locked at the limit is left to it (`kanso doctor` re-checks the side a print reaches from and
each of these quote behaviours as engine facts). A broker fills as it fills: the key moves
kanso's simulated venues and nothing a broker does. Like every cost it is inherited — a
broker's declaration, then `venues.<MIC>.costs`, then the hypothesis — and two versions
certified under different rules cannot share a stage venue, which is one exchange: `deploy`
refuses the pair before it writes the stage (exit 2), naming `venues.<MIC>.costs.limit_fill`.

**`print_through` fills a resting limit only on a print through it, and a taker only on a
quote.** It is a rule for the top-of-book venue fed both quotes and prints, and it moves both
kinds of fill. A resting limit fills only on a print strictly through its price — a print under
a resting buy, over a resting sell — that the venue applied after the order reached its book at
the price it rests at, and by that print's own size, shared across the orders it reaches: a buy
of 320 resting at 9.50 met by a print of 100 at 9.49 fills 100, and the next print under it
another 100; one print of 300 under buys of 200 at 9.51 and of 200 at 9.50 fills the
better-priced 200 and the other the 100 left. Nothing else fills it: not a quote however far
through its price, not a print at its price, not a bar, not a print the venue applied before the
order landed, and not one it applied before a modify moved the order to its price. That is the
reading of how much a print fills that the operator's resting rule takes; `print_through_whole`
is the same rule with the other reading, a print through filling all that is left of the order,
as a market that traded through a displayed limit would have taken it first. A taker — a market
order, or a limit marketable when it lands — fills against the quote in force, at its touch and
up to the size it shows, never against a print standing as the book: a limit fills there and no
further than its own price, so a buy limit never pays above its limit, and its rest stays on the
book at its price under the rule above; a market order's rest walks one increment past the
touch, as the engine walks any market order larger than the top level, so a rule that sizes a
taker down to the displayed size is the sleeve's to apply, from the quote it is handed. The
quote in force is the last one the venue applied, until a print trades strictly outside it —
under a bid or over an ask it shows at a size — which ends it: a market that traded there has
left the quote. With no quote in force, or one showing nothing on the side a market order takes,
a market order is refused for want of a market and a limit rests at its price; so a sleeve that
buys at market on a print over the ask is refused rather than filled at that ask — measured, the
quote before would have filled it a dollar under a session's first print when that quote was the
last session's, and nine cents under a print within one — and kanso's benchmark hold sends its
entry again on the next point. A split restates the quote in force, not the print the book
holds. Under a stated latency a command due by a print's instant lands before that print, so an
order that reached the book in time is there when the print arrives and a cancel that reached it
in time has taken the order off; a command due at a quote still lands after the quote, so a
taker fills on the first quote at or after its delay — unless a print comes first, when it fills
on the quote before, if no print since has traded outside it. The venue cannot tell one print
from another: it fills on every print the hypothesis loads, so a rule that only lit,
last-sale-eligible, round-lot prints may fill needs a trade series written with only those
(`csv_parquet`, `docs/adapters.md`), and a print outside the quote that a later report put there
ends the quote all the same. A print carrying an aggressor reaches only the orders on the side
it hit, as under the engine's own rules. A hypothesis whose resolved `limit_fill` is either
value, from whichever layer, must require `quote` and `trade` and may not require `book`; `kanso
hyp validate` refuses it otherwise (exit 3), naming `costs.limit_fill`. The tick a taker pays
over the touch is a charge, not a price: state it as `slippage_ticks`, which is never charged
past an order's limit, so a limit priced at the touch and taken there pays its commission and no
tick, and state a resting fill's charge as `maker_per_share`. An account that charges $0.0040 a
share on every fill and a tick over the touch to take, with the regulatory fees passed through
on sales, states:

```yaml
costs:
  commission_bps: 0.0
  commission_per_share: 0.004      # a taker's fill: $0.0040 a share …
  slippage_ticks: 1                # … and one tick over the touch, never past its limit
  maker_per_share: 0.004           # a fill that rested: $0.0040 a share and nothing else
  slippage_bps: 0.0
  spread: fixed_bps
  fixed_bps: 0.0                   # no half-spread on top of a fill at the touch
  sell_fee_bps: 0.206              # the transaction fee and the activity fee, on every sale
  sell_fee_per_share: 0.000195
  limit_fill: print_through        # a resting limit fills only on a later print through it
  latency_ms: 30                   # the time to decide and the route to the venue, one number
```

Four parts of a taker rule that waits for the first quote at or after its delay and sizes a
clip down to the displayed size are approximated, and `docs/backlog.md` row 158 records each: a
taker whose first point after its delay is a print fills on the quote before it (measured on
five sessions at 20 ms, 134 of 4,456 market orders, 26 of them priced otherwise, −6 to +5
ticks); a market order's rest past the displayed size walks one increment and pays
`slippage_ticks` on top; a marketable limit's rest past the displayed size is not sized down but
rests at its own limit, through the quotes that show the market under it, and a later print
through it fills it there as a maker, at a price above the market for a buy, paying
`maker_per_share`; and a taker landing after a print outside the quote, before the next, is
refused if a market order and rests at its price if a limit, where such a rule would fill it on
the next quote. A quote carried across a gap — a session's last into the next's first prints —
stays in force until a print trades outside it, so a market order sent on a print inside it
fills at its touch, at most its spread from that print. `kanso doctor` checks each engine
behaviour the rule rests on as an engine fact, and the rule itself as kanso loads it.

`costs.latency_ms` is the other key that is not a charge: how long the simulated venue
takes to see an order.

```yaml
costs:
  latency_ms: 20                   # zero unless stated: no delay, no model
```

Every order command — an insert, an update, a cancel — reaches the venue's book that many
milliseconds after the sleeve sent it, on both code paths alike, and the book carries on in
between: a print that would have filled the order in that interval finds it not there yet,
and a cancel that arrives after a fill finds the order filled. The venue acts on a command at
the first point of data after its delay has passed, and only after matching that point —
except that under `limit_fill: print_through` a command due by a print's instant lands before
that print (above) — so
the delay a run models is never shorter than the one stated and at tick resolution
exceeds it by one point. On a feed of prints, quotes or a book, and on any grain of several
names, a print, a quote or a bar reaches the sleeve through a flush marker at its instant,
and the command lands before the sleeve's handler for it on both code paths
(`docs/concepts.md`, Delivery). A change to the book is never held: the sleeve's
`on_order_book_deltas` for it runs before a command due at that change lands, and sees the
order sent and not yet on the book; the next point's handler sees it there. It models the
round trip from the strategy to the exchange's book through the account and route it will trade on, and it
is measured there, on real orders, rather than assumed. State the whole round trip: a feed
that reaches the strategy late and an order that reaches the book late add up, and a rule
that reacts to a point and posts lands the same instant either way, so one number carries
both. Availability (`ts_init`, `docs/concepts.md`) is a property of the data, when it became
public, never of the route that carries it to you. **Under a stated latency a cancel is not
instant**: it is a command like the rest, and the order it cancels can still fill until it
lands. So `submit_exit` counts an order waiting on its cancel as an exit still working, and
an exit that replaces one whose cancel is in flight is sized to what the old one cannot also
take — often nothing, in which case it returns `None`. What the cancel in flight held back is
owed, not dropped: kanso asks for that exit again, at the price given and sized to what is
left then, on every later point after the strategy's own handler has run, until it goes out
whole — so a sleeve that cancels and exits once is closed once the cancel lands, and one that
re-posts on every point replaces the owed exit with its own. An order sent on a session's
last point under a latency reaches the book on the next session's first, and one sent on the
window's last point reaches it after the window has ended and never fills, owed or not. An owed exit is forgotten when
the position is flat or has changed sides and when the sleeve asks for another exit in the
name. A cancel on that side takes back only an owed exit that has a price, as it would have
taken back the limit order itself; an owed exit at market stands for an order the venue
would have taken before any cancel that followed it, so the sleeve's later cancels leave it
owed. One an attached exit rule asked for is forgotten only when the position is flat,
whatever its host sends or cancels, so a rule that says exit once still closes. An owed exit
with a price still owed when a session ends is asked for at that price on the next session's
points if the position is still open; it never goes past flat, but the price may be the last
session's. Measured on 0.13.0, before exits
counted the ones still working: a rule that followed the ask with its exit on every change of
the book bought 50 shares in one session at 20 ms, sold 92, and was left short 42 to the
session's end, and every card of its lane at 20 ms held a position for close to a day. With
no latency stated a cancel lands before anything further is matched, so an order the venue
held open when its cancel was sent is not counted. **A cancel for an order still on its way
to the venue lands behind the order**, on every path, whatever the latency — `cancel_order`,
`cancel_orders` and `cancel_all_orders` alike; an order the engine's order emulator holds
has not been sent to the venue at all, and its cancel takes it out at once (above). The venue takes the order, filling it if it is
marketable, and then the cancel: with no latency stated before it matches anything further,
so an order sent and cancelled in one handler that does not fill when it is taken never
rests through a point; under a latency both land at the same instant, the order first. The
order counts as working until the cancel lands. A node may not yet have handed the order to
the venue when the strategy's handler cancels it, and would send the cancel ahead of it,
where it is lost and the order rests; kanso holds such a cancel back and sends it the moment
the node reports the order submitted, before the venue has matched it, so a node and a
backtest fill alike (`kanso replay parity`). A cancel the sleeve itself sends right behind a
modify of an order the venue already holds is not held back, as an exit at market's is: on a node it overtakes the
modify, where the backtest lands the modify first and fills it if it is marketable, so the
two paths can differ there (`docs/backlog.md`). Zero, the
default, configures no latency model at all, so a venue model that states none is built
exactly as it was before the key existed. Like every cost it is inherited — a broker's
declaration, then `venues.<MIC>.costs`, then the hypothesis — and it is part of the
hypothesis's scope, so a re-pin that changes it starts the search again. A stage venue
carries the latency its versions were certified under, so two versions certified under
different values cannot share one, which is one round trip: `deploy` refuses the pair
before it writes the stage (exit 2), naming `venues.<MIC>.costs.latency_ms` and both
versions.

**A print at a resting limit's price fills it by its own size**, and no more: a buy of 320
met by four sellers' prints of 100 at its price fills 100, 100, 100 and 20, one part per
print, and met by one print of 1,000 fills whole (`kanso doctor` re-checks this as an engine
fact). Under `touch` and `through` a quote beyond the price, or a print beyond it from the side
that can trade with the order, fills all that is left of it whatever its own size — the same
buy met by one seller's
print of 100 a tick under it fills 100 and then 220 — and of two orders resting beyond one
point it fills only the better-priced. Under `touch` a print at a price is credited whole to
every order resting there: two buys at 9.50 met by one seller's print of 100 there fill 100
each. So the fills a run reports are as honest as the print sizes it is fed only for an order
alone at its price and no larger than the prints that reach it (`docs/concepts.md`, Delivery).
Under `print_through` a print at the price fills nothing and one beyond it fills by its own
size, shared across the orders it reaches, so a resting fill is as honest as the prints that
made it; under `print_through_whole` one beyond fills all that is left (above).
A resting order sits on one exchange's book and is filled only by the executions that reach
that book, so the trade stream that stands in for that exchange has to be its own executions,
one print per execution: a consolidated tape fills the order with prints from venues it never
rested on, and a file that merges a run of same-price prints into one hands it their sum in one
fill. Measured on a posting strategy over one month of two Nasdaq names, the consolidated
tape's fills were an order of magnitude larger than the exchange's own executions at the same
prices and instants allowed. On a level-two book (`OrderBookDelta` data) the venue can also
track queue position — `queue_position` in the engine's venue configuration — so a limit that
joins a level showing 500 ahead fills only after those 500 have traded through; an order posted
inside the spread creates its own level and has nothing ahead of it either way.

A hypothesis that requires `book` needs the book on every UTC day its window holds a name's
bars, quotes or prints. A day that holds them and no change of that name's book — its book
archive missing, say — is refused, on a card, a replay and a certification alike, naming the
name and the days and the load that fixes it:

```
data: demo_book holds the book, and the catalog holds market data and no book change for DEMO.XNAS on 2024-01-03
remedy: load the book for DEMO.XNAS over 2024-01-03..2024-01-03 with `kanso data load`, then take a snapshot
```

The venue would otherwise match that day's prints against the book the day before left. A
card reports the refusal as a crash carrying that remedy, and `kanso research begin`
refuses a baseline that met it (exit 2). Hours of prints after a day's last change of the
book, in a day that has one, are not refused, and neither is a name whose changes all
follow another name's in what the engine is handed at once: the engine's own check counts
the first point of each batch it is handed, and refused such a name with its book on every
day — measured on 2026-10-03 on five OKX books, a chunk of six seconds held 49 changes and
18 prints of `BCH-USDT-SWAP.OKX` and none of the changes began a batch — so kanso makes the
check itself, of every point, and the engine's is off.

`kanso hyp validate PATH` says whether it is admissible and changes nothing either way:

```
$ kanso hyp validate hypotheses/demo_mr/hypothesis.yaml
error: hypotheses/demo_mr/hypothesis.yaml: windows: certification.start: 2024-12-31 overlaps
       the research window ending 2024-12-31
```

(exit 3). The embargo between the research and certification windows is enforced here rather
than trusted, because a certification window that touches the research window is not
out-of-sample and a strategy measured on it has no evidence behind it.

An id is 3 to 40 characters of `a-z`, `0-9` and `_`, and **`portfolio` is not one of them.**
A certified sleeve composes a strategy named after its hypothesis, and `portfolio` is how a
construct attached to the book names its host, so a hypothesis of that name would leave
`construct.host` meaning two things. `hyp validate` and `hyp add` refuse the id and say so;
`docs/constructs.md` has the reasoning under `allocation`.

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

A re-pin keeps `best` while the file still asks the same question. A change to the
`universe`, the `resolution`, the `data_requirements`, `data_by_instrument` (an entry that
narrows an instrument), `session_scope`, `construct.id`, `sizing`, `objective.id`,
`warmup`, `benchmark`, `book`, `costs` or `depth` clears it — stripping the classification
counts, since a draft has no construct and the best was earned as one — and the event log
records `best_cleared` naming the field that moved. `kanso classify` re-pins on the same
terms, so classifying onto another construct clears it too. The windows are not scope:
moving the research window keeps `best`, and later cards are compared with a best measured
over other days (`docs/backlog.md` row 149). The cards and their blobs stay in state, and
`strategy.py` still holds the best-so-far bytes, so the next `research begin` starts from
them.

`program.md` is yours on the same terms: it is copied into the lane and pinned at
`research begin`.

`strategy.py` is **kanso's once a keep exists.** It is the best-so-far, written atomically
from the best blob after every keep that moves the hypothesis's `best` and every re-point
of it, so the file on disk is always the current champion. A keep that moves only its run's
best — a re-seeded run's baseline, a lesser keep under new pins — leaves the file alone.
Its bytes hash to the `strategy_sha` kanso shows:

```
$ shasum -a 256 hypotheses/demo_mr/strategy.py
f729a538831e3ea8f80c46b68c5993ed4662c168bd9c56541cdf570619b6f6e9
```

which is the `f729a53` in `kanso hyp show`, `research show`, the certificate's filename and
the certified source beside it. Editing it by hand does not change what a card evaluates —
a card evaluates the **lane** copy, `runs/<lane>/<hyp>/strategy.py` — and the next keep
overwrites your edit without comment. `kanso doctor`'s `best` check warns that it diverged,
naming both shas; it never fails, because editing that file is how you prepare the next line. If you
want to start from your own code, that is what `kanso research begin --from-workspace` is
for, and it clears `best` so the history says what happened.

`results.tsv` is rendered from state after every card, so restoring a lane from the best
never loses a row. Deleting it loses nothing.

## `screens/<id>/`

`screen.yaml` is **yours**: what a screen measures, written by you or by an agent following the
`kanso-screen` skill, from the template `kanso screen new` renders. It holds the question and
nothing about the answer, so its bytes can be content-addressed, as a hypothesis's are. The
template's comments are its field reference; `kanso screen validate` is its admissibility check.

A screen is **bound** or **free**. `hyp: <id>` binds it to a registered hypothesis: its window
is that hypothesis's research window, read from the pinned bytes, its costs are the
hypothesis's, and every leg must be an instrument of its universe read at a type and grain it
researches. A free screen states `window` instead, and may state `costs` per venue.

| key | what it holds |
|---|---|
| `legs` | name → `{instrument, type, resolution?}`: a catalog instrument id, read as `bar` (with its `resolution`), `trade`, `quote` or `book` |
| `derived` | name → exactly one of `basket` (weights over legs), `spread` (`long`, `short`, `hedge: fixed` with `beta`, or `hedge: ols` with `fit: window` or `fit: first_fold`) or `gap` (`a`, `b`: one asset on two venues) |
| `groups` | name → a list of legs and derived legs, for a measure to name at once |
| `clock` | `grid`, the step a `grid` estimator samples on, and `hours`: `overlap`, or `{tz, span}` in a named time zone so daylight saving moves it |
| `measures` | each one of the measure library's: `lead_lag` (`from`, `to`, `estimator: grid` or `hy`, `lags`) or `response` (`trigger`, `followers`, `side: with` or `against`, `horizons`, `latency_ms`) |
| `verdict` | optional, never defaulted: `alpha` (family-wise, over a measure's cells), `min_sessions`, and for a `response` cell `min_margin_bp` (gross per event less the hurdle) and `min_events_per_day`; an `alpha` below 2^(1 − `min_sessions`), the smallest p that many sessions can give, is refused (exit 3) |
| `costs` | a free screen's, per venue, in the `costs` shape `hypothesis.yaml` takes: what its hurdles are struck under, as the last layer over the workspace's venue model; a bound screen is charged its hypothesis's. A `latency_ms` here is ignored: a response's latency is its measure's `latency_ms`, which `kanso screen draft` carries into the hypothesis |

Each parameter of a measure has the range the measure library declares, and `kanso screen
validate` refuses one outside it (exit 3):

| measure | parameter | from | to |
|---|---|---|---|
| `lead_lag` | `lag` | `1ms` | `1d` |
| `lead_lag` | `lags` (how many) | `1` | `64` |
| `response` | `move_bp` | `0.1` | `10000` |
| `response` | `within` | `1ms` | `1d` |
| `response` | `z` | `0.5` | `20` |
| `response` | `lookback` | `1s` | `30d` |
| `response` | `horizon` | `1ms` | `1d` |
| `response` | `horizons` (how many) | `1` | `32` |
| `response` | `latency_ms` | `0` | `60000` |

A result is rendered beside the screen as `<sha7>-s<snap7>-v<ver7>.yaml` — the screen's bytes,
the snapshot and the measure library's version, the three pins its record in `state.db` is
keyed by. It is a rendering: editing it changes nothing, and `kanso screen show` reads the
record. The loader specs a run's fetches were made with are kept under `specs/`.

A result holds one entry per cell:

| field | what it is |
|---|---|
| `key`, `params` | the cell: its measure, legs and lattice point |
| `mean`, `se`, `t`, `sessions` | across the sessions the cell held a value in. For `lead_lag`, the correlation at the lag. For `response`, a session's value is the sum of its events' drift-adjusted signal — the follower's signed move at its mid, less its session drift over the hold, in bp at one notional an event — so `mean` is bp a day of a follow, before any cost |
| `p` | family-wise over the measure's cells, by session sign-flip max-T; with 2^S sign vectors no more than `[screen] draws`, exact |
| `folds`, `folds_same_sign` | the means inside each calendar fold of the window, and how many share the sign of the whole. For `lead_lag`, the same quantity as `mean`. For `response`, **net** bp a day — gross less the hurdle — sharing the sign of `ceiling_bp_day`: so a cell can show a significant follow in `p` and every fold negative, a real move a taker cannot earn |
| `staleness` | a `grid` lead only: per leg, the share of grid intervals it did not print in; Hayashi–Yoshida and response cells have none |
| `clock_bound` | a lead under a second between legs whose timestamp kinds differ, or are undeclared: it may be the gap between two clocks |
| `in_sample_fit` | the cell reads a spread fitted on the window it is judged on |
| `response.events`, `events_per_day`, `unfilled` | events scored, a day over the sessions the cell was live, and events with no follower point to enter or leave at, counted and scored nowhere |
| `response.gross_bp`, `hurdle_bp`, `margin_bp` | per event: what a taker made (a quote or book follower buys the ask and sells the bid), the round trip charged, and the difference |
| `response.ceiling_bp_day` | the margin summed a day over the live sessions: one notional on every event and no capacity limit, so a bound a search can approach and never exceed |
| `response.hit_rate`, `drift_adjusted_bp` | the share of events whose gross was positive, and the drift-adjusted signal an event, the quantity `mean`, `t` and `p` test |
| `judged`, `reason` | `pass`, `fail` or `thin` under the verdict, and every clause the cell missed |

The summary says whether the result is `worth_a_lane` — a `response` cell passed; a lead alone
names no trade — counts the cells each way, and ranks the passing ones in `best`: responses by
`ceiling_bp_day`, then leads by |t| whichever way they point, so a negative lag, the reverse
direction, can rank beside its mirror.

Spans are `<n>(ms|s|m|h|d)` — finer than a hypothesis's grain, because a lead between two venues
is measured in milliseconds — and a lag carries a sign, positive when `from` leads `to`. The
followers of a response are `followers` and never `on`: YAML reads a bare `on` as the boolean
true. Every list of a measure is a declared, finite lattice; its cells are the cross product,
and `kanso screen validate` prints how many there are.

## `models.yaml`

The LLM register and the routing table, and yours entirely. It holds **model ids, providers,
tiers, context sizes, prices and the variable name each key is read from — never a key.** By
default that name is `KANSO_<PROVIDER>_API_KEY`; `api_key_env` overrides it with another
name, and an override replaces the standard name rather than adding to it.

`routing` maps each task class — `classify`, `certify_plan`, `propose`, `align_check`,
`explore` — to a tier, a thinking effort and an output cap. `kanso models check` prints the
register as the router reads it and then makes one minimal call to every configured model.

One call connects within fifteen seconds and then waits **seven minutes** for the answer,
and neither client retries — the router's ladder is the only retry kanso has. A model you
serve yourself, a `local` entry whose `base_url` is a process you wrote around a vendor's
CLI, should give up sooner than that: a `propose` through such a shim regularly runs for
more than four minutes, and whichever side gives up first decides what you read. The shim's
own status arrives as `<model>: the provider answered 504`; kanso giving up first arrives as
`<model>: the request did not complete (ReadTimeout)`, which names nothing the shim was
doing.

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
resolution actually changed them. `sources` is the vendor's own key, by reference adapter,
and it is what that adapter is asked for: an entry may be filed under any key and a
hypothesis may name it by its qualified id, and the vendor is still asked for its own
spelling. An entry with no key for the configured adapter is asked for as it was named.

An entry is resolved again through the adapter that resolved it, which `resolved.adapter`
records. An id nothing has resolved is asked of the one adapter that declares its venue in
`venues` (`docs/adapters.md`) — `ETH-USDT-SWAP.OKX` of the exchange's — and of `[data]
reference` otherwise, an equity under its symbol. So a workspace
may hold instruments from more than one source — a perpetual beside the equities a screen
reads it against — and change `[data] reference` between them: each entry keeps the source
that defined it, and a hypothesis on either validates. Asked of the other source instead, a
vendor refuses an instrument it does not list, and the hypothesis could not be registered.

An edit to `override` reaches the store at the next `kanso data instruments resolve` and
never before: `hyp validate` and `hyp add` build the definition in memory to check it, and a
run is priced under what the store holds. Resolved as of a date the store already holds a
definition for, an edited override is a correction of that definition, and a correction is
explicit — the plain command refuses by name (exit 2) and `--refresh` replaces it. Resolved
as of another date it is added beside what is held, since what an instrument was on each
date is its own fact.

A run asks the reference adapter nothing about an instrument the store holds. `research
begin` and every card build the venue model from the definitions a card is priced under —
the newest-dated the store holds of each instrument, which are what the run's snapshot pins
— whatever date the cache was resolved as of, so lanes starting together send the vendor no
request at all, and a vendor that is down or throttling stops no run. An edited `override`
therefore changes no run until it is resolved and snapshotted. Only an id the store holds no
definition of is resolved, in memory, and a run over one is refused by name when its
snapshot is chosen.

`manual: true` suppresses resolution entirely and requires you to supply the constructor
fields yourself. That is the path the file loaders, the synthetic loader and the demo take,
and it is why a workspace can run end to end with no reference adapter and no credential.

Tick and lot come from kanso's dated convention table where one is on file — US equities —
or from the reference provider's measured definition, and otherwise from your `override`;
they are never guessed. Two things a definition may not carry, from `override` or from what
the reference adapter resolved, and each is refused by name (exit 3): a non-zero
`maker_fee` or `taker_fee` — the runner charges commission once, from the venue model, so
state it under `venues.<MIC>.costs` or the hypothesis's `costs`, because the simulated venue
would charge the instrument's rate on every fill on top — and an inverse perpetual. A rate
your `override` states, remove from it; a rate the reference provider resolved, state as
`"0"` in the entry's `override` (`maker_fee: "0"`), which wins over the resolved field — a
`--refresh` alone would fetch the provider's rate again. `margin_init` and `margin_maint`
stay yours to correct. `kanso doctor` fails its `instruments` check on a stored definition
that carries a maker or taker rate, naming it and the
`kanso data instruments resolve ID --as-of DATE --refresh` that replaces it once the rate is
gone.

### A perpetual

A crypto perpetual swap is an entry of asset class `CRYPTOCURRENCY` with
`instrument_class: swap` in `override`, and it builds the engine's `CryptoPerpetual`. No
convention table covers one, so a manual entry states its contract as the venue publishes
it:

```yaml
BTCUSDT-PERP:
  nautilus_id: BTCUSDT-PERP.SIM
  asset_class: CRYPTOCURRENCY
  manual: true
  corporate_actions: none
  override:
    instrument_class: swap
    base_currency: BTC
    quote_currency: USDT
    settlement_currency: USDT
    multiplier: "0.01"          # one contract is 0.01 BTC
    price_increment: "0.1"
    price_precision: 1
    size_increment: "1"
    size_precision: 0
    lot_size: "1"
```

`multiplier` and `lot_size` are required, though the engine would default both to one: a
contract value of one is a claim about the contract, and most perpetuals' is a fraction of
a coin. The precisions follow from the increments when you leave them out. kanso builds the
contract linear, and a definition stating `is_inverse: true` is refused by name (exit 3) —
the runner's notional is `qty x px x multiplier` in the quote currency, and an inverse
contract's is not. `min_notional` and `max_notional`, where a venue states them, are
amounts in the settlement currency: `min_notional: 5` is five USDT. A perpetual settles in
its `settlement_currency` and is booked in its `quote_currency`, so both must be its venue's
account currency — `[research] currency = "USDT"` in `kanso.toml`, or
`venues.<MIC>.currency` in `portfolio.yaml` — or `kanso hyp validate` refuses it (exit 3);
one whose two differ fits no account. What a perpetual is to a
card, and what it is not yet, is in `docs/concepts.md`.

A perpetual the exchange lists need not be written by hand. Name the OKX package's public
reference and the regional host, and resolve it by the exchange's own id with the venue
appended:

```toml
[research]
currency = "USDT"         # or broker = "okx", whose venue OKX declares a USDT account

[adapters.okx]
region = "us"

[data]
reference = "okx"
```

```
$ kanso data instruments resolve BTC-USDT-SWAP.OKX --as-of 2026-09-30
```

The entry is written for you — `asset_class: CRYPTOCURRENCY`, `instrument_class: swap` in
`override`, and `sources: {okx: BTC-USDT-SWAP}` — and the contract's size, tick, lot and
minimum come from the exchange's listing, with no credential sent. An `override` you add to
that entry is applied over what the exchange lists. An inverse contract (`BTC-USD-SWAP`) and a
contract that is not live are refused by name (exit 3); `docs/adapters.md` lists the fields
it reads and why the definition's fees are zero.

Its history loads the same way, with no credential, once the swap is resolved — its prices
and sizes are read at the definition's precision, sizes in contracts. One spec per loader:

```yaml
loader: okx_bars                 # candles, stamped at their close
instruments: [BTC-USDT-SWAP]
start: 2026-09-28
end: 2026-09-28
resolution: 1m
```

```yaml
loader: okx_trades               # every print, from the exchange's daily archives
instruments: [BTC-USDT-SWAP]
start: 2026-09-28
end: 2026-09-28
```

```yaml
loader: okx_book                 # the book, kept exact to `levels` deep, from the daily archives
instruments: [BTC-USDT-SWAP]
start: 2026-09-28
end: 2026-09-28
levels: 3                        # required, 1 to 400; okx_book only
```

```yaml
loader: okx_funding              # the realised rate at each settlement
instruments: [BTC-USDT-SWAP]
start: 2026-09-01
end: 2026-09-28
```

```
$ kanso data load --loader okx_funding --spec funding.yaml
```

A range reaching before what the exchange serves — about three months of funding, about
six of `1s` bars — or into a UTC day that has not ended is refused naming the day to use
(exit 3); `docs/adapters.md` gives each loader's source, horizon, units and rate limits.
`okx_trades` and `okx_book` write each UTC day as a dataset of its own, with its own
manifest, so a range of them is as many datasets as days, and `data show` joins them into
one span.

**A perpetual's funding is data it requires.** A held perpetual pays or is paid funding at
every settlement, so a hypothesis whose universe holds one lists `funding` in
`data_requirements`, or `kanso hyp validate` refuses it (exit 3) naming the instrument:

```
error: data_requirements: BTCUSDT-PERP.SIM is a perpetual and funding is not required; a perpetual's P&L is not honest without the funding it paid and was paid
remedy: add funding to data_requirements and load its realised funding history
```

What makes an instrument a perpetual is its resolved definition — the engine's
`CryptoPerpetual`, the same definition the settlement check reads — never its id.

`funding` is a built-in custom type, `kanso.data.types.Funding`, whose points carry
`instrument_id` and `rate`. The rate is the **realised** rate of the period that just
settled, as a fraction of notional — `0.0001` is one basis point, paid by longs to shorts
when positive and by shorts to longs when negative. It is never the rate a venue publishes
for the period in progress: that is a prediction, which moves until the instant it settles,
and a series of predictions read as payments charges a book what it was never charged. Load
the settled history, and leave a feed's "current" or "next" rate out of the catalog. A rate
that is not a finite number — `nan`, `inf` — is refused at load, as any custom type's
decimal field is: a placeholder would be booked as a payment.
`ts_event` and `ts_init` are both the settlement instant, since that is when the rate stopped
moving and when it was paid, so a funding dataset is `realtime` and names no publication
rule. A file maps `ts_event` and `rate`, and `instrument_id` where it holds one — the entry
names the instrument otherwise — and leaves `ts_init` unmapped; a `ts_init` mapped earlier
than the settlement is refused at load (exit 3). Three settlements of a day:

```
instrument_id,rate,ts
BTCUSDT-PERP.SIM,0.0001,2024-01-01T00:00:00
BTCUSDT-PERP.SIM,-0.00005,2024-01-01T08:00:00
BTCUSDT-PERP.SIM,0.000125,2024-01-01T16:00:00
```

```yaml
loader: csv_parquet
timezone: UTC
files:
  - path: data/btcusdt_funding.csv
    instrument: BTCUSDT-PERP
    venue: SIM
    type: funding
    columns: {instrument_id: instrument_id, rate: rate, ts_event: ts}
```

`funding` is required of a hypothesis and asked of its perpetuals alone. A spot leg settles
no funding, so a basis universe — `[BTCUSDT.SIM, BTCUSDT-PERP.SIM]` with `data_requirements:
[bar, funding]` — is covered by bars for both and a funding history for the perpetual only;
which instruments are asked is read from the stored definitions, as `hyp validate` reads
them. `research begin` refuses a snapshot whose funding dataset for a perpetual does not
span the research and certification windows, whole UTC days as for every series, and a card
is handed each point of its window in `on_data`, at the settlement instant, as a `Funding`.
**The runner books each settlement once**, in its extraction, where every other cost is
applied: the rate on the quantity held before the instant — a fill stamped at it is not held
there — times the last print at or before it and the multiplier, out of cash — a long pays a
positive rate, a short receives it — and the sleeve's `balance` has booked the same amount
by the time `on_data` is handed the point. What that puts in a card's run, its trades and
its equity is in `docs/concepts.md`. A dataset without a settlement that happened is a card
that did not pay it: load the whole settled history of each window.

A workspace whose entries are all `manual` may still name a reference adapter in `[data]
reference` without setting that adapter's key: the adapter is built only once resolution
finds an id the cache and the manual entries cannot answer, and building it is what
resolves the credential. Name the vendor you will eventually resolve through; you need it
configured on the day you first ask it something. A research run is never that day for an
instrument the store already holds.

The registry of record is the catalog's instrument store, not this file — `kanso data
instruments show <ID>` reads the store and renders the definition a run would use, the
newest-dated one it holds. The file is the cache and the place your overrides live, so
deleting it costs you the overrides and a round of resolution, not the definitions.

### The split schedule

An equity's `override.info.splits` is where you declare the splits it has been through. It
and `info.timezone` beside it are the only structured overrides, and they are the one thing
that makes a window containing a corporate action researchable: kanso applies a split at
its ex-date — cancelling resting orders, rescaling every open position, resyncing the
portfolio behind it — instead of reading the restated price as a return.

```yaml
SOXS:
  nautilus_id: SOXS.ARCX
  asset_class: EQUITY
  corporate_actions: adjust_all
  override:
    info:
      timezone: America/New_York                # where the listing's sessions are dated
      splits:
        - {ex_date: 2026-07-15, ratio: 0.1}     # a one-for-ten reverse split
        - {ex_date: 2021-03-02, ratio: 4.0}     # a four-for-one split
```

`ratio` is shares held **after** per share held **before**, the same convention the
`corporate_action` data type uses: `0.1` for one-for-ten reverse, `4.0` for four-for-one.
An entry holds `ex_date` and `ratio` and nothing else — a ratio of `1.0`, a repeated
ex-date and any other key are refused by name (exit 3). In particular a schedule carries no
**cash**: money moves in exactly one place in kanso, the runner's extraction, and a cash
event has an announcement date, so it belongs in the data as a `corporate_action` point.

`ex_date` is the first session that trades at the new price, and `info.timezone` says where
that session is dated: the IANA zone the instrument trades in, `America/New_York` for a US
listing. The split holds from the first instant after the midnight that opens the ex-date
there. That one instant falls between the old share count's last point and the new count's
first for every grain at once: after the 20:00 close of a session's minute bars, and after
the daily bar kanso stamps at the following midnight, because a bar is stamped at its close
and that bar is the session before's. Without `info.timezone` the ex-date is a UTC day, as
it was before the key existed, and that is wrong for any listing whose session runs past
UTC midnight — in winter 19:00 New York is already the next UTC day, so the split lands
inside a post-market and rescales a position that session bought at a price the split
never touched. A zone the host's zone database does not know is refused by name (exit 3).
Adding the key changes the definition, so it is a correction like any schedule change: say
which date each split first traded at its new price, then `--refresh` and re-snapshot.

Two consequences worth knowing. A schedule is part of the definition, so it is part of
`definition_checksum` and therefore of the snapshot a run is pinned to: adding one to an
instrument already resolved as of that date is a *correction*, which the plain command
refuses (exit 2) and `--refresh` performs, and re-snapshotting afterwards is what a later
run reproduces. And the shares a reverse split leaves short of a whole lot are paid out in
cash at the close before the ex-date, as an issuer pays them — 1,005 shares through a
one-for-ten split keep 100 and are paid five old shares' worth — so a position under one new
lot is paid out whole and closes at the split rather than stopping the run. The close is the
midpoint the venue's book still quotes; a book whose last quote showed one side at size zero
pays at the side it shows, and one that quotes neither at the position's own last fill.

The schedule goes here rather than in the data because a split is the one corporate action
with no honest publication instant. A dividend carries the day it was declared; a split
carries only the day it takes effect, so kanso cannot say when it became knowable, a
corporate-actions dataset that has only effective dates declares `publication: unknown` and
no snapshot will rely on it. A definition is a *dated assertion you make*, which is a different kind of
claim, and the store already keeps one per date.

The schedule is applied by the simulated venue rather than by a strategy, and no researched
`strategy.py` can read it: it names every split of the instrument's life, including ones
after the window a card is judged on, so `.cache` — the one route to an instrument — is
denied by `strategy_integrity`, along with everything the engine computes against a
position's opening basis (see `docs/concepts.md`). The strategy base holds none of it either:
the venue announces each split on the bus as it applies it, which is how the harness restates
a price printed before the split and books a payment in lieu, so all a sleeve ever holds is
a split that has already happened.

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

**A ticker that is a YAML boolean is quoted.** A spec is YAML, and PyYAML reads a bare `ON`,
`OFF`, `YES`, `NO`, `TRUE` or `FALSE` — in lower, title or upper case — as a boolean, so
`instruments: [DEMO, ON]` names `true` where ON Semiconductor was meant. Write
`instruments: [DEMO, "ON"]`. Left bare, the spec is refused (exit 3), naming the field and the
ticker's place in it:

```
$ kanso data backfill --loader synthetic --spec on.yaml
error: instruments.1: true is a YAML boolean, not a string; YAML reads a bare ON, OFF, YES, NO, TRUE or FALSE as one
remedy: quote the value in the YAML, e.g. "ON" rather than ON
```

`Y` and `N` are read as strings and need no quotes. Every YAML file kanso reads refuses a
boolean the same way wherever a string belongs — a symbol under `sources` in
`instruments.yaml`, a Massive spec's `tickers` override. A free-form map — a strategy's
`config`, a gate's `params` — takes a boolean as a value like any other, so quote a word there
that you mean as text.

The synthetic loader, as `demo.yaml` drives it, generates the weekday sessions of a US equity
venue, 09:30 to 16:00 in `America/New_York`; a spec that sets `calendar: continuous`
generates every calendar day from `start` to `end` instead, in UTC, 00:00 to 24:00 — what a
round-the-clock venue looks like to the runner. Bars are stamped at their close on either
calendar, so at a resolution that divides the day the last bar of a continuous day closes
at 00:00Z of the next, and a daily bar
lands in the day after the one it summarises (`docs/concepts.md`, the card's return
periods). A continuous calendar fixes its zone and its session, so a spec that also states
`timezone`, `session_start` or `session_end` is refused (exit 3) naming the field:

```
$ kanso data load --loader synthetic --spec round_the_clock.yaml
error: timezone: 'America/New_York' conflicts with calendar 'continuous', whose every session is a UTC calendar day, 00:00 to 24:00; drop the field
```

`calendar: weekdays` is the default and is recorded in no manifest, so a dataset generated
before the field existed carries the request parameters it always did and its snapshot id
is unchanged; a continuous dataset records its calendar with the zone and session it fixed.
The loader emits bars, quotes and trades on either calendar, and on a continuous one a
perpetual's `funding` too: a settlement at 00:00, 08:00 and 16:00 UTC — each session's last
at 00:00Z of the next day, as its last daily bar — with a rate drawn from the instrument's
own seed, a whole number of hundredths of a basis point from -1 up to +2, so a card holding
a perpetual pays and is paid funding with no vendor in the loop. Adding `funding` to `types`
changes no other series of the spec, and a weekday spec that asks for it is refused (exit 3):

```
$ kanso data load --loader synthetic --spec weekday_funding.yaml
error: types: funding is settled round the clock and needs calendar 'continuous'; a weekday calendar has no settlements to generate
```

A spec may plant a lead. `leader_seed` and `leader_index` name the shocks another spec draws
for one of its instruments, and every instrument of this spec takes `coupling` of the leader's
shock `lag_steps` bars late and the rest of its own, so its returns repeat the leader's that
many bars later — `demo_lag.yaml` is the demo's (`LAGD` follows `DEMO` by a minute at 0.6). A
shock is drawn a batch of the whole span at a time, so a follower redraws its leader's own
shocks only over its leader spec's span, step for step: state the follower with the leader's
`start`, `end`, `resolution` and model. A leader and a coupling above zero are stated together
or not at all (exit 3), and a spec stating neither records neither, so every dataset generated
before a leader could be stated keeps its request parameters and its snapshot id.

Nothing else is generated.

**Coverage counts only the days a market opened.** A backfill is chunked, and a chunk edge
that falls on a weekend leaves the spans the chunks served a weekend apart. The store's
definition of the instrument names its market, and on that market's calendar
(`docs/concepts.md`, Snapshot) the weekend is closed, so the series is one. In a fresh
`kanso init --demo` workspace, after `kanso data instruments resolve`:

```
$ kanso data backfill --loader synthetic --spec demo.yaml --to 2024-04-30
loader     synthetic · demo.yaml
2024-01-02..2024-01-31 DEMO.SIM bar → written · 8580 rows
2024-02-01..2024-03-01 DEMO.SIM bar → written · 8580 rows
2024-03-02..2024-03-31 DEMO.SIM bar → written · 7800 rows
2024-04-01..2024-04-30 DEMO.SIM bar → written · 8580 rows
total      4 chunk(s) · 33540 rows written
$ kanso data show
DEMO.SIM bar 1m · 2024-01-02..2024-04-30 · 33540 rows
           DEMO.SIM-bar-1m-raw-20240131 · synthetic · 8580 rows
           DEMO.SIM-bar-1m-raw-20240301 · synthetic · 8580 rows
           DEMO.SIM-bar-1m-raw-20240329 · synthetic · 7800 rows
           DEMO.SIM-bar-1m-raw-20240430 · synthetic · 8580 rows
total      4 dataset(s) · 33540 rows
$ kanso data backfill --loader synthetic --spec demo.yaml --to 2024-04-30
loader     synthetic · demo.yaml
           DEMO.SIM bar: nothing missing before 2024-04-30
total      0 chunk(s) · 0 rows written
```

Each dataset still records what it served — its id ends on the day it served to, so the
second stops at Friday the 1st of March and the third, asked from Saturday the 2nd to Sunday
the 31st, served the 4th to Friday the 29th — and the weekends between them are no gap, so
`research begin` pins a snapshot across them and a second backfill plans nothing. A day the
market opened that nothing serves is a gap however it arose. A chunk the source answers
empty on such days is recorded in `state.db` and shown inside its gap as `answered empty`:
the source holds nothing there, which is why the gap persists, and `data backfill` does not
ask for it twice. It is never coverage, because a source that lost a trading day and a
market that shut read the same in its answer. An instrument the store does not define, and
a market with no calendar on file, are read with every day open, so their weekends are gaps
like any other.

A **snapshot** freezes what is held: the dataset checksums plus the checksum of the resolved
instruments. Every run is pinned to one, and to the instruments as much as to the data:
`research begin` hands out only a snapshot whose instrument checksum is the store's own, and
refuses by name when the definitions have moved since the newest covering one was taken.
The store is resolved before it is frozen — a snapshot over instrument data is refused while
the store holds no definition, because a run reads its definitions from the store and a
snapshot pinning none is a promise no run can keep.

**A dataset a snapshot names cannot be rewritten. At all.**

```
$ kanso data load --loader synthetic --spec demo.yaml
error: DEMO.SIM-bar-1m-raw-20250901 is named by a snapshot and cannot be rewritten
remedy: load it again with --supersedes DEMO.SIM-bar-1m-raw-20250901 to put this dataset in its
        place; every snapshot naming the old one then stops supporting a certification
```

(exit 2 — and `--replace` gives the identical refusal, which is the point: the flag lifts the
overlap check, not the pin). A run, a card, a certificate and a deployed version all reference
a snapshot; rewriting the bytes underneath one would make every result that cites it
unreproducible while leaving the citation looking fine. The successor path — a new dataset
recording `supersedes` — is what `kanso data sync` walks, so extending a series never mutates
one. A pinned mistake is corrected the same way, in the open: `--supersedes D` lets the load
take the place of the one pinned dataset `D` it names — its files and its manifest go, as a
replace's do, and the new dataset records `supersedes: D` — and every snapshot naming `D`
stops supporting a certification, because the workspace no longer describes what it pinned
(`cert run` says so by name). Any other pinned dataset the load would overlap is still
refused: the flag names one dataset, never a span.

An overlapping write into data **no snapshot pins** is a different question and gets a
different answer:

```
error: 2024-01-02..2025-09-01 overlaps the held dataset(s) DEMO.SIM-bar-1m-raw-20250901
remedy: pass --replace to delete and rewrite the overlapped span
```

(exit 2). Here the data is yours to replace; you just have to say so.

**The manifests are the record of what the catalog holds**, and `kanso data show` reads them
— with the empty answers `state.db` recorded — rather than the parquet files. Delete a
parquet by hand and `data show` keeps reporting the dataset, its span and its row count;
`kanso doctor` has no catalog check at all. The loss surfaces only when a run needs the
rows, as a baseline or card that did not run (exit 2):

```
error: the baseline card of demo_mr did not run: exception: …
kanso.errors.PreconditionError: data: the catalog holds nothing for demo_mr over 2024-01-02..2024-12-31
remedy: run `kanso data load` for the window, then take a snapshot
```

The card runs in a process of its own, and the remedy is the one the failure inside it
raised rather than one remedy for every way a baseline can fail — a baseline that fails
because the strategy raised still says to fix `strategy.py`, and to begin again with
`--from-workspace` when the strategy that raised was the best card's, since `research begin`
would otherwise take that blob again. The catalog is a directory you back up, not one you
prune.

## `runs/`

The lane directories, and the only place research edits anything.

```
runs/<lane>/<hyp>/         hypothesis.yaml, program.md, strategy.py — and nothing else
runs/<lane>/<hyp>/.card/   a card's report and output, only while the card runs
runs/daemon.pid            the supervisor's pid, and its lock
runs/<child>.<pid>.lock    held by a lane or the monitor, named for it and its pid, while it lives
runs/<child>.<pid>.work    held by it and by every card it starts, until the last of them exits
runs/daemon.log            whatever the daemon and its children write to a stream
runs/state-<instant>.db    a copy of state.db `kanso state prune` made before it deleted anything
```

`.card/` is where a card's child writes back what it measured and whatever it printed; the
lane removes the directory once it has read them. The window never goes there: the lane
streams it to the child on its standard input, a chunk at a time, while the card runs
(`docs/concepts.md`, Card). A lane killed in the middle of a card leaves those two small
files behind, and the run's next card empties the directory before it starts. Under a
running daemon that next card comes at once: the supervisor starts a dead lane again under
its name, and the lane in its place resumes the run. A lane killed in its baseline has no
run yet; the supervisor puts the hypothesis back in the queue and removes the directory. The
scope check a card passes ignores `.card/`, as it ignores every dot-file.

A lane writes no log of its own, and no file under `runs/` records what a run did. The
record of a run is in `state.db` — the run row, every card with its metric and verdict, and
the `events` table every state change appends to — and it is read back with
`kanso research show` (a card's source, or the diff between two), `kanso research status`,
`hypotheses/<id>/results.tsv` and `kanso status`. `daemon.log` is one plain stream that the
supervisor and every lane it spawns share, for whatever they print; it is not structured and
not per lane.

`kanso research begin` prints the lane directory, copies the three scoped files into it and
pins them. Exactly those three files are there, and `.card/` beside them while a card runs;
a card runs in a subprocess with its cwd set to that directory, and lanes never share files.
**One active run per hypothesis:**

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

Each lane and the monitor holds `runs/<child>.<pid>.lock` (`l1.58102.lock`,
`monitor.58105.lock`) under the same kind of lock for as long as it lives, and removes it when
it exits. That is how a child still running after its supervisor is gone is seen: `research
status` and `status` name it beside the stopped daemon, `research stop` ends it, and
`research start` refuses until it has:

```
error: still running from a daemon that is gone: lane l1 (pid 58102)
remedy: run `kanso research stop`, which ends it, then start again
```

(exit 2). `kanso state prune` refuses the same way, since such a lane still writes the store,
and so does `python -m kanso.research serve`, which a service unit runs, exiting 1 with the
same message.

Each also holds `runs/<child>.<pid>.work`, and every card, certification or demotion it starts
inherits that lock and holds it until it exits. A card leads a session of its own, so a lane
killed outright leaves its card to see the lane gone and end itself, which takes it up to half
a second once it is running and longer while it is still starting; the `.work` lock is how
that is seen rather than guessed. `research status` names a lane that is gone by what it
started for as long as that still runs, and `research stop` waits on it — returning the
moment it is let go, or, ten seconds after the last lane went, naming what still holds it. A
lane `research stop` kills itself, or with its supervisor's group, it waits for until the
kernel has let go of that lane's `.lock`, since a lane read as alive has its card's `.work`
read as its own, and a stop that read it then would return with the card still running.
Here a lane was killed outright while a child it had started through the card path — one
that never looks for its lane, so it outlasts the wait — slept on:

```
$ kanso research status
daemon     stopped · still running: what lane l1 (pid 90209) started
lanes      l1, l2, l3
restarts   none
runs       0 active
queue      0 waiting
$ kanso research stop
daemon     stopped
ending     what lane l1 (pid 90209) started · still running; it ends itself, and `kanso research status` names it until it has
runs       left open, with their lane directories
```

(exit 0 both, the stop after 11.45 s). With a child that exited eight seconds after it
started, the same `stop` returned once it had, after 5.29 s, with no `ending` line.

A certification or a demotion writes the store itself until it sees its parent gone, so
`kanso state prune` refuses while one holds the `.work` of a lane or monitor that is gone.
Here the monitor was killed outright inside a demotion that slept on:

```
$ kanso state prune
error: still running from a daemon that is gone: what monitor (pid 74578) started
remedy: run `kanso research stop`, which waits for it to end, then run this again
```

(exit 2). `research stop` then named it under `ending` after 11.39 s, and once it had exited
the same prune ran.

A child killed outright leaves its files with nobody holding them, which name nothing; the
supervisor that buries the child removes them — its `.work` once nothing the child started
holds it — and the next supervisor removes any left.

The whole directory is gitignored, and deleting it while nothing is running costs you only
the log.

## `certificates/`

```
certificates/<hyp>/plan.yaml
certificates/<hyp>/<sha7>-h<pin7>-<n_trials>-p<plan>-e<engine>.yaml
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
bytes, under the hypothesis file as the card's run pinned it (`h<pin7>`, the first seven of
that file's sha), under that plan version, on that engine — with the trial count that stood
when it was minted.

```
$ kanso cert run demo_mr
error: demo_mr already certified f729a53 under plan version 1 and nautilus_trader 1.231.0 on
       2026-09-06T00:48:13…; a certificate is immutable
remedy: research a better strategy, replan, or upgrade the engine
```

(exit 2). Change the bytes, the pinned file, the plan version or the engine and it is a
different certificate under a different name, so re-certifying an unchanged commit after an
engine upgrade is a plain `cert run` and produces a second file rather than overwriting the
first, and so is certifying the same seed again after `hyp add` re-pinned its file. A
certificate written before pins were recorded carries no `h<pin7>` in its name and no
`hypothesis_sha` in its document, and refuses a repeat under any pin.

Editing a certificate file changes nothing kanso will ever act on: the certificate of record
is in `state.db` and the YAML is a rendering of it. Change `verdict: pass` to
`verdict: fail` in the file and `kanso cert show` still prints `pass`. This is not a
tamper-check — it is that the file was never the authority.

## `strategies/`

```
strategies/<id>/strategy.yaml          the versions, their pins, their expectation, their state
strategies/<id>/impl/<version>/        the manifest and a verbatim copy of every certified source
```

`strategy.yaml` gains a version each time a construct attaches and each time the sleeve
itself is certified with different bytes; earlier versions are never rewritten, only their
`state` moves.

`kanso strat compose` writes both, and a passing certificate composes on its own, so these
are usually not commands you type. `impl/<version>/` is the **one directory a backtest, a
replay and a live node all load from**, so the exchange a version was judged against and the
one it is deployed onto cannot drift apart. Each source file is named after the module it
defines, and that module name carries a digest of the file's own bytes — which is why the
sha of that file, the `strategy_sha` in `manifest.yaml`, the sha of the certificate's
`<sha7>.py` and the `<sha7>` in the certificate's own name are all one number.

That number is checked every time the directory is used, so **what a stage runs is what was
certified or nothing at all.** Both ways out of `impl/` — loading a version into a node and
reading its sources for a replay — hash every file first and refuse the version by name when
a digest is not the one its manifest records. Edit a file under `impl/` and the next
`kanso portfolio deploy` or `kanso replay run --strategy` exits 3 having run nothing:

```
$ kanso portfolio deploy --stage paper
error: …/strategies/demo_mr/impl/1/kanso_impl_sleeve_demo_mr_f729a538831e.py hashes to
       8d18d26 and demo_mr@1 was certified with f729a53, so this file is not the sleeve
       that was certified
remedy: restore the file from …/certificates/demo_mr/f729a53.py, which holds the certified
        bytes; to run code of your own, research and certify it
```

(exit 3, and the same from `kanso replay run --strategy demo_mr`). The remedy is exact: the
certificate keeps the same bytes beside it under the sha they hash to, so copying that file
back over this one restores the version and the next deployment runs. A deleted or truncated
source is refused the same way, which matters because a module imported once in a process
would otherwise keep running out of the interpreter's cache. Treat the directory as
generated: change `strategy.py`, research it, certify it, and let composition write the next
version.

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

`speed` is validated against the execution client's clock — a `clock: wall` client needs
`1` — recorded on the stage's session and printed by `portfolio show`, and in this version
it paces nothing: a stage node is a bounded catch-up over the catalog and replays it
unpaced whatever the value says, so the demo's three months of minute bars pass in seconds
at `speed: 1`. Only `kanso replay run --speed` paces a replay. The value will take effect
when a stage node outlives the command that starts it, which the backlog tracks.

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

The live stage admits only what `promote` moved there. `kanso portfolio show` reads the same
record, so it marks the entry rather than printing it as a deployed version: it is counted in
neither the stage's `allocated` nor its P&L, the stage is not `up` for it, and `--json` gives
it `"recorded": false`.

`deploy` reads that record too, so the two never disagree. A version the record does not know
— one `strategies/<id>/strategy.yaml` marks deployed while `state.db` never travelled, which
is the state of a fresh clone — is admitted by neither: `deploy` runs no node for it (so the
monitor finds no window to judge) and `portfolio show` reports the stage down, rather than one
command trusting the file while the other trusts the record.

```
$ kanso portfolio show                    # the paper stage and the limits line are elided
live       down · exec sandbox (simulated) · data replay · speed 1 · capital 100,000
           clock never run · catalog to nothing · allocated 0 · pnl +0.00
           demo_mr@1               40,000  not deployed · in portfolio.yaml only
```

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

One directory per run of a node: `session.yaml` and the order intents that came back
(`intents.jsonl`). Replay writes one, a parity comparison writes two — one per code path —
and a deployment that actually runs a node writes one. They are the evidence behind a
`parity_replay` gate and behind a stage's realised window. The sessions a warmed target was
fed before its range are not among the points released, and `clock_ns` is never inside
them: a session claims what it was asked for, and a stage resumes into its window rather
than into its prefix.

**The points a session released are recorded, not kept.** `released` counts them and
`stream_sha256` is the sha256 of their stream: one JSON object per point — `instrument`,
`ts_event`, `ts_init`, `type`, keys sorted — and a newline. That stream is the window's
catalog points in feed order, cut where the feed stopped: an input both code paths were
handed, every point of which the catalog already holds. Written out it was most of every
session and nothing read it — measured on 2026-10-02 at 0.5 to 0.8 GB a parity pair for a
month of five-second bars on a dozen names and 1.5 GB a pair on a level-two book, the node's
copy and the engine's byte for byte the same. The digest keeps what those copies proved:
`kanso replay parity` and the `parity_replay` gate compare the two paths' counts and digests
before their orders, and two paths released different points fail whatever they submitted.
To see the points themselves, replay the session over its range again; on the same catalog
it releases the same stream, and the digest says whether it did. A book's changes of one
instrument at one instant are released as one point, so they are one line of that stream.

A replay takes the digest as it runs. It streams its range in a card's chunks
(`kanso replay run`), and each path folds what a chunk released into the digest as the
chunk is released, so a replay holds neither its stream nor its window whole, and the
digest of a range streamed a chunk at a time is the digest of the same range run whole.
Nothing is written until the replay has finished: one refused, failed or killed part-way
leaves nothing in `sessions/`.

**The intents are kept whole.** They are what parity compares — field by field, with an
instant tolerance a digest cannot honour — and what a reader opens to find where two paths
parted. They are what a session costs: 15 KB to 13 MB a session on the same two workspaces,
by how often the strategy trades.

A `session.yaml` with no `stream_sha256` was written by an earlier kanso, which kept the
stream beside it as `stream.jsonl`. Nothing reads that file, and its sha256 is the digest a
session of the same points records now — on the demo, the 40,950-line stream kanso 0.13.0
wrote for the certification window's engine replay hashes under `shasum -a 256` to the
`stream_sha256` both paths record for it — so deleting it loses nothing kanso uses. From the
workspace root:

```bash
rm sessions/*/stream.jsonl
```

A `.spool-<random>.jsonl` directly under `sessions/` was left by a development build after
0.13.0 that wrote the stream there while a replay ran and moved it into the session at the
end; one killed part-way left it behind. Nothing reads one either:

```bash
rm sessions/.spool-*.jsonl
```

**What a certificate's citation rests on.** Its `parity_replay` evidence names the two
sessions (`node`, `engine`) and carries what was compared: each path's count and digest
(`node_released`, `node_stream`, …), each path's intent count, the widest instant apart and
the first divergence. `kanso cert show` prints it. So the citation stays meaningful without
the directories — the verdict and what it was taken over are in the certificate — and what a
cited session adds is its intents, which are what lets the comparison be read again.

Sessions accumulate and nothing prunes them; the directory is gitignored. `kanso replay show`
lists what is on disk, so deleting a session directory removes it from the listing cleanly.
One no certificate names — a replay run by hand, a certification interrupted before it wrote
— is evidence for nothing kanso keeps; one a certificate names takes that certificate's
intents with it. Delete a directory only when nothing is running: a certification writes
the node's session before the engine's, and compares the two once both are on disk.

## `escalations/inbox.md`

Append-only, and kanso means it. One line per escalation — `misaligned`, `cert_failed`,
`promotable`, `demoted`, `deploy_blocked`, `explored` — carrying an id, a timestamp, the kind, its
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
`reserved_mem_gb`, `cores_per_lane` and `mem_per_lane_gb` in `kanso.toml` are read on every
detection.

`mem_per_lane_gb` **replaces** the derived memory per lane rather than raising its floor.
The derivation charges every lane 1.5× the largest baseline peak any run in the workspace
ever recorded, which is the right figure only while every hypothesis is as heavy as the
heaviest: a one-second overlay that once peaked at 4.3 GB charges every lane 6.45 GB, and a
16 GB machine then plans a single lane while the daily hypothesis actually researching peaks
at 0.25 GB. Declare what a lane costs now and the plan uses it. A figure at or below zero is
refused with the rest of `kanso.toml` (exit 3, as `cores_per_lane = 0` is), and a positive
figure under 0.5 GB — half a gigabyte is twice the smallest baseline peak measured, so
anything under it is a typo rather than a measurement — is read as 0.5 GB. An
under-declaration therefore buys more lanes than the host can feed, not fewer.

The figure is also what a card of that lane may hold: the loop kills a card child whose
resident memory passes the lane's share, and the declaration is the first way that threshold
can fall under 4 GB. It is never lowered below three times the run's *own* measured baseline
peak, so the heavy hypothesis whose recorded peak you are declaring your way out of keeps
the room its own cards need. On a 16-core, 16 GB host with a 0.25 GB baseline peak recorded,
the derived 4 GB plans three lanes and kills a card above 4 GB; `mem_per_lane_gb = 2` plans
six and kills above 2 GB; `0.5` plans seven and kills above 0.75 GB, which is the floor
rather than the declaration.

A stall's certification is held to the same figure. The lane certifies in a child, and a
child whose resident memory passes what a card of the judged run may hold is killed and the
certification refused, with a remedy naming this key and `kanso cert run`, which certifies
in your own process instead. What each certification cost is on its `stalled` event
(`cert_peak_mem_gb`), so declare at least that if you want the daemon to certify on its own:
a share sized for cards alone is a share no certification of a heavier window fits in.

Because it measures *this* host, the rendered `.gitignore` excludes it: `init` writes it and
`env detect` rewrites it, but it is not committed, so a clone of the repository on another
machine detects its own rather than inheriting one that describes a machine it never ran on.

A missing envelope is a `warn` in `kanso doctor` and leaves `research status` reporting
`lanes none (run kanso env detect)`. A clone therefore detects its own once, with
`kanso env detect` (or `kanso init`), rather than reading a foreign measurement.

## `kanso_ext/`

Optional, and yours. Every package or single-module file **directly under** each
`[extensions] paths` entry (default `kanso_ext`) is imported at startup, and what its
module-level `PROVIDES` table declares — constructs, loaders, adapters, execution clients,
custom data types — is collected. A file at `kanso_ext/house.py` is an extension; a
`kanso_ext/__init__.py` is not, because the directory is a search path and not a package.
A gate or an objective is not on that list and declaring one is refused: the toolbox a plan
is drawn from and judged by is the package's own. `docs/extensions.md` is how to write one.

`kanso ext show` says what is there and whether it is actually in play:

```
$ kanso ext show
paths      kanso_ext
house           loaded · kanso_ext/house.py
                loaders      house_bars          absent · the module's LOADERS table yields no loader under that id
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

`kanso init <dir> --demo` fills in what a plain `init` leaves as a placeholder and adds five
files, and between them they are the reason the demo runs end to end with no credential of any
kind:

| file | plain `init` | `--demo` |
|---|---|---|
| `models.yaml` | a commented skeleton with `<provider>` placeholders | the shipped `mock` protocol listed for every tier, so classification, proposal, alignment and planning cost nothing and reach nothing |
| `instruments.yaml` | `{}` plus the field reference in comments | two `manual: true` entries, `DEMO.SIM` and `LAGD.SIM`, so no reference adapter is needed |
| `mock/responses.yaml` | — | the scripted answers that register reads, one per task class, with every `params` written as the list of `{name, value}` pairs a real model answers with and every `propose` answer carrying `tags` |
| `demo.yaml` | — | a synthetic loader spec: a seeded mean-reverting series spanning the research, certification and forward windows |
| `hypotheses/demo_mr/` | — | a hypothesis that ships already classified, with its `program.md` and the sleeve stub |
| `demo_lag.yaml` | — | a second synthetic spec: `LAGD`, on its own seed and `demo.yaml`'s span, taking 0.6 of `DEMO`'s shock one bar late (`leader_seed`, `leader_index`, `lag_steps`, `coupling`) |
| `screens/demo_lag/screen.yaml` | — | a free screen of `LAGD` against `DEMO` over the first quarter of 2024: a lead-lag and a response, with a declared verdict |

Everything else `init` writes is the same either way. Delete the five added files and replace
the two rendered ones and you have an ordinary empty workspace.

## Moving, copying and backing up a workspace

A workspace is relocatable. Copy or rename the directory and everything keeps working:
`kanso doctor` is green, and a run in the copy resolves the same pinned snapshot and
reproduces the same baseline metric. (`state.db` does record the absolute path each snapshot
was written at, but that column is written and never read — a snapshot is found by its id
under `catalog/snapshots/`.) Two things are worth knowing before you copy one.

**The skill links point at the installed package**, not into the workspace, so a copy taken to
another machine has dangling links until `kanso skills sync` runs there. `envelope.yaml`
measures the host, and the rendered `.gitignore` keeps it out of the repository, so a clone
on another machine has none until `kanso env detect` runs there — a filesystem copy carries
it, but it then describes the machine it came from, which is why re-detecting is the honest
step on a new host.

**`state.db` is the half that does not travel, and a clone is a fresh workspace that
inherits your certified work.** Copy the directory and you have everything. Clone the
repository and you have the data, every certificate, the composed implementations and the
source of every hypothesis — reproduced to the digit — but not the record: no research
history, no `best` pointer, no trial count, no approvals, no version or session index, and no
record of what a source answered empty, so a backfill asks again for a gap its source
already answered; coverage is read off the manifests and the market's calendar, which do
travel. That is
the design rather than an accident: the record is what one machine did, and approvals in
particular must never travel, because real capital always needs a person to say so again on
the machine that will trade. `kanso doctor` names the situation when it meets it — the
`record` check — and what a clone does next is small: `kanso hyp add
hypotheses/<id>/hypothesis.yaml` re-registers a hypothesis from its committed best-so-far, a
certificate on disk stays the certificate of record and a repeat is refused, and
`kanso promote … --as NAME` is asked again by whoever is present.
