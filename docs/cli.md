# The command line

Every command takes `--json` and prints exactly one object under it — the result on
success, `{"error", "code", "remedy"?}` on failure — so a caller parses one document and
branches on the exit code. Without `--json` the same result is a few terse lines. Both
`kanso --json <command>` and `kanso <command> --json` work.

`--workspace PATH` (`-w`) names the workspace a command acts on; the default is the
current directory, from which discovery walks up to the nearest `kanso.toml`.

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | an unexpected fault |
| 2 | a precondition failed: workspace or engine state forbids the action |
| 3 | validation failed: operator-authored input is malformed or wrong |
| 4 | a named operator approval is required and absent |

## Workspace

| command | what it does |
|---|---|
| `kanso init [DIR] [--demo]` | scaffold a workspace, link the skills, detect the envelope, apply the migrations. `--demo` renders the mock-only register, the synthetic loader spec, the `DEMO.SIM` instrument and `hypotheses/demo_mr/`. kanso never invokes git; a `.gitignore` is written or appended |
| `kanso doctor [--report] [--check-adapters]` | one line per check, by name: `versions` (kanso and the engine), `install` (package or editable, and from where), `engine wheel`, `schema` (`state.db` against the package's migrations, behind or ahead), `envelope` (the lane plan, its age and whether the host changed), `repository`, `gitignore`, `best` (`hypotheses/<id>/strategy.py` against the best blob kanso last wrote there), `base names` (every `hypotheses/<id>/strategy.py` that binds a name its base class owns, with the name and the line — a warning never a failure, read from the files alone), `certificates` (every certified subject against the bytes still held for it), `record` (certified files on disk the record has no memory of — a clone, or a removed `state.db` — named once as a fresh workspace that inherits certified work, a warning never a failure), `skills`, `credentials`, `adapters`, `execution` (each broker's `[adapters.<id>]` table read through its own model, whether its accounts are configured, what each stage's client declares and what `deploy` would refuse), `instruments` (the registry's entries, the universes it must resolve, drift against the newest snapshot, and — failing — any stored definition carrying a non-zero maker or taker rate, named with the `kanso data instruments resolve ID --as-of DATE --refresh` that replaces it once the rate is gone), `lanes` (every open run against its lane directory, and the reverse), `extensions` (what loaded, and what shadows an id that ships — one table with `ext show`), and `engine facts` — the claims about NautilusTrader that kanso binds to, a broker package's claims about the engine's adapter for that broker among them, re-verified against the installed `nautilus_trader`: claims the package records as design constraints are listed `by design`; any other that does not hold fails, with its evidence; and the check warns when the installed engine is not the version the facts were verified against. Exits 2 when a check fails. `--report` redacts paths for pasting upstream. Makes no network call unless `--check-adapters`, which probes what each configured adapter reaches — a dataset a plan excludes is reported and not graded down; a credential that does not authenticate fails, and so does a host that does not answer, with a network remedy rather than a credential's (the `okx` data adapter sends none) — and asks each broker what its account says of the terms its venue declaration states. For `okx`, when `KANSO_OKX_API_KEY`, `KANSO_OKX_API_SECRET` and `KANSO_OKX_PASSPHRASE` resolve and `[adapters.okx]` states a region, two signed read-only requests read the real account's fee tier (`GET /api/v5/account/trade-fee?instType=SWAP`: `takerU`/`makerU`, else `taker`/`maker`, and `level`) and its configuration (`GET /api/v5/account/config`: `acctLv`, `posMode`); the `adapters` check lists the declared rates, any `venues.OKX.costs` rate in `portfolio.yaml`, what the two charge, the account's `level`, `acctLv` and `posMode` and what it pays, and — where they differ — the exact `venues.OKX.costs` lines to state. An account paying more than is charged on either side fails, one paying less on both warns, a spot-mode account (`acctLv` 1) fails as one that trades no perpetual, and an answer that is not the API's success fails naming it. With any of the three unset it lists the declaration and sends nothing; with no region it warns and sends nothing (`docs/adapters.md`, *The account's own tier*). |
| `kanso ext show` | every extension this workspace carries: whether it imported, and per id its `PROVIDES` declares, what the registry for that kind did with it — `registered` (it hands the id out), `shadowed` (it hands out the packaged one instead) or `absent`, with the reason. Exits 0 whatever it finds, because a broken extension degrades a workspace rather than stopping it and `doctor` is where that is graded. Opens nothing and resolves no credential — `docs/extensions.md` |
| `kanso migrate` | apply the pending state migrations. Every other command refuses a database behind the schema rather than migrating it behind your back |
| `kanso state prune [--dry-run]` | delete the books the redundancy rule stored that no run can select again, and give their space back to the disk. A stored book is read only under the pins of the run asking — the hypothesis, its file, the snapshot and the criteria version — so a book is kept while an open run carries its pins, or while a hypothesis that is not retired has them on the newest of its runs pinned to the file it is registered under now and to the criteria this kanso judges by — the pins a run begun now is given, unless a snapshot has been taken since, which leaves those books kept and errs the safe way. Every other book is deleted — under a file since re-pinned, under an earlier kanso, under a snapshot a newer run moved past, or of a retired hypothesis — and every reading under a kept pin stays. Nothing already recorded depends on a book — no card, trial count, best or certificate; only the redundancy check of cards still to come reads them — and one deleted whose pins come back — a file pinned back to its earlier bytes, an earlier kanso installed, a retired hypothesis resumed — is re-earned by the next run one card at a time. Refused (exit 2) while a daemon runs, and it holds the daemon's lock until it is done, so a `research start` meanwhile exits at once; any other command that writes the store while the prune is writing waits up to five seconds and then fails, so run it with nothing else working the workspace. Before it deletes anything it copies `state.db` whole to `runs/state-<instant>.db` (UTC): to have every book back, stop everything, delete `state.db-wal` and `state.db-shm` and put the copy in `state.db`'s place — which also loses whatever was recorded since — and delete the copy when you no longer want it. It refuses (exit 2) unless the disk `state.db` is on can hold that copy and twice what is kept, which is what SQLite needs to rewrite the file. Then it rewrites the file with `VACUUM` and checkpoints the write-ahead log into it. A prune refused before the rewrite has deleted and copied, and the next prune deletes nothing and rewrites. `--dry-run` counts what would go and writes nothing, and runs beside the daemon. Measured on a copy of a live workspace's store on 2026-10-02, under a kanso newer than the one its runs were begun under: the 93 books of its five open runs stayed, 17,012 holding 3,025 MB went, and the file fell from 3,399 MB to 349 MB in 36 seconds |
| `kanso skills sync` | link the packaged skills into every `[skills] targets` entry |
| `kanso env detect` | detect the host, derive the lane plan, write `envelope.yaml` |

## Data

| command | what it does |
|---|---|
| `kanso data load --loader ID --spec FILE [--replace] [--supersedes D]` | run a loader over the range its spec names. An overlapping write into unpinned data is refused (exit 2) and needs `--replace`; one into a dataset a snapshot pins is refused outright unless `--supersedes` names that dataset, and then the new one takes its place as its recorded successor, the old files go, and every snapshot naming the old dataset stops supporting a certification (`cert run` says the workspace no longer describes it). A loader that declares `chunk_days` (`okx_trades` and `okx_book` declare one) is written a dataset per that many days, each with its own manifest, in order, streamed to the catalog in batches; `--supersedes` is recorded on the one day that overlaps the named dataset (else that series' first day). A failure on a later day leaves the days before it written and is refused with the loader's exit code, naming the day and the `kanso data backfill --loader ID --spec FILE --to DATE`, `DATE` the spec's last day, that writes that day and the rest. A `--replace` or `--supersedes` write that fails — a refused point, a loader that raises part-way through a day, an interrupt — keeps the dataset it was replacing: the files a replace removes are linked aside under `catalog/.replaced/` until the new dataset is recorded, and put back with their manifest when it is not |
| `kanso data show` | every series the store holds: its datasets, the spans they **served**, the gaps between them and the row counts, each series read on the calendar the store's definition of its instrument is filed under (`docs/concepts.md`, Snapshot). Spans with only closed days between them — a weekend, a holiday at a chunk edge — are shown as one, so every gap holds a day the market opened; an instrument the store does not define, or whose market has no calendar on file, is read with every day open. The part of a gap its source was asked for and **answered empty** is listed as well (`empty`; `answered empty` in the text form): it is why the gap persists, and it closes nothing. The answers are read from `state.db`, so the command refuses a database behind the schema like every command that needs state |
| `kanso data snapshot` | freeze what is held — the dataset checksums and the checksum of the resolved instruments — into `catalog/snapshots/<snapshot_id>.yaml`. A run is pinned to one of these. Refused (exit 2) while the instrument store holds no definition and the datasets name instruments: a run reads its definitions from the store, so resolve the universe first |
| `kanso data backfill --loader ID --spec FILE [--from DATE] [--to DATE] [--dry-run]` | fill history from the source's floor (or `--from`, clamped up to the floor and reported) to the earliest day already held (or `--to`), and close the gaps inside what is held: each is asked for again, day for day. A day the instrument's market was closed is no gap and is never asked for, so the weekend or holiday a chunk edge falls on, which the chunks' own spans break at, needs no second pass. Chunked; the manifest each chunk writes is its checkpoint, and a chunk answered empty is recorded in `state.db` instead, so an interrupt resumes, a repeat fetches nothing, and a range answered empty is never planned again, `--dry-run` included — except a chunk after the last day its series serves, whose empty answer says only that the source has not published it yet: that answer is recorded nowhere, and the next backfill or sync asks again. An empty answer on days the market opened closes nothing: it stays a gap, and no snapshot covers across it. A chunk that served data and stopped short leaves the days it did not serve a gap, which the next backfill asks for. `--dry-run` prints the chunks, the request count and the estimated bytes and fetches nothing. The transport is the loader the spec names — an adapter's bulk loader is chosen by naming it, never for you. A chunk is 30 days, or the `chunk_days` a loader declares |
| `kanso data sync [--loader ID] [--dataset D] [--to DATE]` | extend each held series from the served end of its **newest** dataset towards `--to` (default today) into a successor dataset recording `supersedes`, so a dataset a pinned snapshot references is never mutated. Only the newest is extended, because a request beginning after an interior dataset ends runs over the one behind it; `--dataset` names one instead, newest or not, which is how the dataset in front of a hole is extended on purpose. It extends in chunks of 30 days, or of the `chunk_days` the series' loader declares, each a successor of the one before. A chunk its source answers empty is past the series' last served day, so the answer means "not published yet": it is not recorded, and the next sync asks for it again, which is what keeps a sync to today over a source that publishes a day or two late (`okx_trades`, `okx_book`) from leaving those days a hole |
| `kanso data instruments resolve [ID…] [--as-of DATE] [--refresh]` | resolve ids (default: every id `instruments.yaml` names) into the catalog's instrument store and the cache. A definition the store already holds is left alone and one dated otherwise is added beside it. `--refresh` resolves again rather than answering from the cache and **replaces** a definition the store holds for the same date — without it, a resolution that would change one is refused (exit 2) by name — and is itself refused (exit 2) while a run is active or while a deployed version depends on a snapshot that pins one of these instruments. A definition carrying a non-zero `maker_fee` or `taker_fee`, or an `is_inverse: true` perpetual, from `override` or from the reference adapter, is refused by name (exit 3) and nothing is written |
| `kanso data instruments show [ID]` | the definition a run would use for one instrument — the newest-dated the store holds — as its canonical fields, or the ids the catalog holds. The id is the catalog's own, qualified or as its bare symbol, never an `instruments.yaml` key: the cache's keys are what `resolve` takes |
| `kanso data adapters [--check]` | what is registered here — the package's loaders, the manual instrument provider and every data adapter, whether a vendor's package ships it, a broker's package ships it beside that broker's clients (`okx`, the exchange's public reference and history: kind `data`, no credential, loaders `okx_bars`, `okx_book`, `okx_funding` and `okx_trades`, configured by an `[adapters.okx]` table that states a `region`), or an extension provides it; a broker's execution clients are `kanso portfolio clients`' — with id, kind (`data` or `reference`; an extension's adapter declares its own), the credential names each needs and where each resolves from (never a value), its capabilities, its loader ids and its quota. An `[adapters.<id>]` table in `kanso.toml` that neither a data adapter nor a broker provides is named in a note, by the same rule `kanso doctor` applies; a broker's table is configuration and is not. Without `--check` it performs no network I/O. `--check` probes each **configured** adapter for what its key actually reaches: entitlement per dataset at the grain the source gates on, and the measured history floor of each entitled price series; an adapter that needs no key, such as `okx`, is probed once its table is present and names a host, with one public request — `docs/adapters.md`. A key that does not authenticate exits 2; a host that does not answer at all exits 1 with a network remedy, and for `okx`, which sends no key, that is the only way its probe fails |

Dates are written `YYYY-MM-DD`; anything else is a validation failure (exit 3).

## Screens

A screen measures, before any lane is spent, whether a relationship a hypothesis rests on is in
the data and how large it is against the hurdle a trade would clear (`docs/concepts.md`,
Screen). No command here calls a model, so none exits 2 for want of one.

| command | what it does |
|---|---|
| `kanso screen new ID [--hyp H]` | scaffold `screens/<id>/screen.yaml` from the template — bound to the registered hypothesis `H` when it is named, free otherwise. Refused (exit 2) when the directory exists, because a screen is scaffolded once; an id that is not one is refused (exit 3) |
| `kanso screen validate PATH` | say whether the file is admissible and what it would measure — its form, the window it reads, its legs, the cells of every measure, which is the family that measure's correction is over, and the data plan: every series it reads, `held` when the catalog serves it on every day of the window its market opened, `fetchable` when it does not and a registered adapter declares that it serves it — naming the adapter, its loader, whether it is configured and the days missing — `unresolved` when no definition of its instrument resolves, with the resolution's refusal, or `unserved` when it resolves and no adapter serves it, naming why and that the remedy is an adapter built as a workspace extension. An instrument the store does not define is resolved as `hyp validate` resolves one, recording nothing. For a screen with a `response` measure, the venue model every traded leg's hurdle is struck under, and where its costs came from — `screen` for a free screen's own `costs`, `hypothesis` for a bound screen's hypothesis, or the layer below that stated them. Fetches nothing and changes nothing. Refused (exit 3): a traded leg whose venue model has no spread to charge it — a model that takes its spread from quotes over a leg read as bars or prints, or a model with no quotes and no `fixed_bps` — naming where to state one; a file that breaks a rule of its own (a name declared twice or referenced and never declared, a derived leg that is not exactly one of a basket, a spread or a gap, lag zero, a value twice in a lattice, a grid finer than a bar leg it samples, a trigger that is neither a move nor a z-score), a parameter outside the range its measure's library entry declares, a file in another screen's directory, and a bound screen's leg that is outside its hypothesis's universe or read at a type or grain the hypothesis does not research. A bound screen whose hypothesis is not registered is refused (exit 2). A bound screen's window is its hypothesis's research window, read from the pinned bytes |
| `kanso screen run PATH` | validate; refuse a free window that meets a registered hypothesis's certification span on a shared instrument (exit 2); fetch every fetchable series through the adapter that serves it, resolve an undefined instrument, take a snapshot — and take one too when every series is held and no snapshot pins it yet; pin the newest snapshot covering every series over the window; measure every cell one session at a time, judge each measure's cells by its sign-flip max-T null and, when the file declares one, against the verdict — a `response` cell's margin over its hurdle and its events a day against their floors as well; record the result in `state.db` and render it beside the screen. The same bytes, snapshot and measure library already recorded return the stored result (`stored: true`), and nothing is fetched or read. Refused (exit 2): an instrument no definition resolves, with the resolution's refusal and a remedy naming `kanso data instruments resolve` and the ids' symbols — never an adapter to build, since the instrument may be served already; a series no registered adapter serves, naming the venue and type to build an adapter for; a fetch the source refuses, with the loader's own remedy; no snapshot covering every series; a covering snapshot whose definitions moved. Everything `validate` refuses (exit 3). A verdict of fail, or `worth_a_lane: false`, exits 0: the result is what the command produces, and a low one is evidence |
| `kanso screen show [ID] [--cell C]` | every screen with its newest result; one screen's results, newest first, every one it has ever had; or one cell of its newest result in full. An id with no result, or a cell its newest result does not hold, is refused (exit 3) |
| `kanso screen draft ID --cell C --as NEW --certify A..B` | write `hypotheses/<NEW>/` as a draft from one `response` cell of the screen's newest result, with no model call and nothing registered: the universe is the cell's trigger and follower, the grain and data requirements theirs (`funding` beside them for a perpetual), the horizon the cell's in whole seconds, the costs the screen's for the follower's venue, the research window the window the screen read and the certification window `--certify`, which has no default; the mechanism follows from the cell (a fade `mean_reversion`, a follow of the same name `momentum`, of another `stat_arb`) and the risk limits are the template's. `program.md` is the template with a `Measured` block of the cell's numbers; `strategy.py` is the cell's own rule as a sleeve, so the baseline card re-measures the cell through the runner. Appends `screen_drafted`. Refused (exit 2): an id registered, reserved or already a directory. Refused (exit 3): no recorded result, a cell the newest result does not hold, a `lead_lag` cell, a bound screen, a derived or book trigger or follower, legs at two bar sizes, a missing or malformed `--certify`, and one starting inside the embargo after the screened window, naming the first day it may start |

## Hypotheses

| command | what it does |
|---|---|
| `kanso hyp new ID` | scaffold `hypotheses/<id>/` with `hypothesis.yaml`, `program.md` and a `strategy.py` stub |
| `kanso hyp validate PATH` | say whether the file is admissible — the id, windows, embargo, universe resolution, construct, its parameters, objective and constraints, the `warmup` and the `book` an attached construct shares with its host, a `book` the venue's account and the leverage ceiling can hold, an account currency the engine registers (from `[research] currency` or `venues.<MIC>.currency`; a code it does not register is refused, exit 3), every instrument of the universe, a data leg included, settling and booked in its venue's account currency (settled in a perpetual's `settlement_currency`, a pair's quote currency, any other's `currency`; booked in its quote currency; a mismatch is refused, exit 3, naming the instrument, its currencies and the account's), `funding` among the data requirements of a universe holding a perpetual (known by its resolved definition, not its id; refused otherwise, exit 3, naming it), a `benchmark` only on a horizon of a day or more (checked on a draft too) and where the objective measures one, and a certification window that, with the embargo before it, meets no window a recorded screen read for one of the universe's instruments (refused, exit 3, naming the screen, its window and the day to certify from: data a screen read helped choose an idea and may not judge one) — and change nothing either way: not the file, not the catalog's instrument store, not `instruments.yaml`. It also reads the `strategy.py` beside the file and refuses one that binds a name its base class owns (exit 3), naming each name and line as `strategy_integrity` would at the baseline card; `hyp add` and `classify` pin the hypothesis file alone and do not ask |
| `kanso hyp add PATH` | register it, or re-pin an already registered one, under the sha256 of its bytes. Refused while a run is active (exit 2), because a run is pinned to the bytes it began with. A re-pin that changes the `universe`, the `resolution`, the `data_requirements`, `construct.id`, `sizing`, `objective.id`, `warmup`, `benchmark` (under a benchmark, the universe's first name too), `book` or `costs` (`latency_ms` included) — stripping the classification included — clears the hypothesis's best and records `best_cleared` naming the field, because a card's metric means nothing across any of them; the cards and their blobs stay in state. A row pinned before `costs` joined the scope is compared by the costs its pinned file states, read back from the state store, and reads as unchanged only when those bytes are gone |
| `kanso hyp show [ID]` | one registration — status, pin, construct, objective, best — or all of them |
| `kanso hyp retire ID` | end a hypothesis. Its cards, blobs and certificates stay in state, and it is the only way research ends: no verdict and no run of failures ends one |
| `kanso hyp resume ID` | undo an ending — a hypothesis you retired, or one an older kanso turned `failed` — back to `researching`, clearing the consecutive-failure count. Refused (exit 2) on a hypothesis research has not ended. Give it a lane with `kanso research queue add ID` |
| `kanso hyp explore ID` | write one **new** hypothesis from what the research of `ID` learned, in one call to the best model on the register (the `explore` task class): the pinned `hypothesis.yaml` and `program.md`, the best `strategy.py`, the coverage of its cards by tag, its keeps and their scores, its stalls, and each certificate's verdict with the ids of its failing gates — never a number measured on a certification window. The candidate is judged on the ladder — an id that is not registered, reserved or already a directory; a file that parses with that id and carries no classification; a strategy the static alignment checks accept and whose bytes the workspace has never stored; windows that research no later than the parent's research window ends, certify no earlier than its certification window starts, and certify no sooner than the candidate's own embargo (`max(5 x horizon, 1d)`) counted from the latest research end of any pin the parent's runs held, the last day the idea saw — and written as a **draft** to `hypotheses/<id>/`, a directory that did not exist. Nothing is registered: an `explored` escalation offers `hyp validate` and `hyp add`. Refused (exit 2) for an id not registered and for a hypothesis never researched; past those two it is an attempt, and it also exits 2 with no model configured, as every model step does, and when no answer survives the ladder — each such failure an `explored_failed` event under `ID` which, like a candidate's `explored` event, starts a lane's count of `explore_after_stalls` over |
| `kanso classify ID` | decide what the hypothesis **is** — construct, host, the keep rule's two parameters and the card-stage constraints — in one call to the best model on the register, and write the three keys into `hypothesis.yaml`, re-pinning it. The objective is not asked for: it follows from the hypothesis and the construct. A construct this build cannot run is recorded honestly and refused at `research begin`. `strategy.py` is replaced by the construct's stub only while the file is still one kanso wrote. A classification onto another construct clears the hypothesis's best on the same terms as `hyp add` |

Editing `construct`, `objective` and `constraints` by hand and running `kanso hyp add` is
the override path, and needs no model at all.

## Research

| command | what it does |
|---|---|
| `kanso research begin ID [--tag T] [--from-workspace]` | start a run and **print the lane directory**, which is where an agent works. Copies the three scoped files there, pins them, and runs the baseline card. Pins the newest snapshot that covers the universe — served on every day of both windows its market opened (`docs/concepts.md`, Snapshot) — and whose instrument checksum is the store's own, and refuses (exit 2) by name — the snapshot, what it pins, what the store holds — when the definitions have moved since; `kanso data snapshot` pins the current ones. Needs no model and opens no register: the register is checked where a call is about to be made — `classify`, `cert plan`, `research run` — and a run begun by hand is measurement only. `--from-workspace` starts from the workspace `strategy.py` and clears the hypothesis's best. A baseline that does not run is refused (exit 2) with no run recorded; when the strategy that failed was the best card's, the remedy names `--from-workspace`, since beginning again would take that blob again. A hypothesis declaring a `warmup` has both prefixes resolved here, before any run exists: a catalog holding fewer sessions before a window than the file asks for is refused (exit 2) naming what it found and how far back to load, and the snapshot pinned has to cover the sessions as well as the windows. After `[research] reseed_after_stalls` consecutive stalls on the same best the scheduler re-seeds the hypothesis — a `reseed` event, carried on the `queued` passage — and the next `begin`, by hand or by a lane, starts from that blob — the highest-scoring other keep still aligned under the pins, else the stalled run's base — instead of the best without clearing the best: the run climbs its own ancestry and the hypothesis's best moves only when a keep beats it (`run_begun` records `reseed_from`). `--from-workspace` outranks it |
| `kanso research card ID --desc TEXT` | evaluate the lane directory's `strategy.py` as one card: the static integrity rules first, then the backtest on the research window in a child process under the run's time and memory budgets — the time counted from the child's start, while the lane is still streaming it the window, so the budget covers the read the run did not overlap — then the constraints and the keep rule. Under a `sizing` rule an order the harness refused stops the run and is recorded as a `discard` carrying a `sizing` gate with the rule, the instrument, the instant and the book held — not a crash, and not a trial. A window whose data carries a `corporate_action` split that the instrument's definition does not schedule — or schedules at another ratio — is refused (exit 2) in the parent, before the child exists, naming the instrument, the ex-date and the ratio; `kanso research begin` refuses the same way on its baseline card. Declare the split in `override.info.splits` (`docs/workspace.md`), re-resolve and re-snapshot. After the keep rule, a card that did not keep is compared by what it **held** — for each session, the instruments and sides it held and whether each was still open at the session's end, read from what it held at each period end and from the spans of the positions it opened and closed, since a hypothesis that closes its positions before the session does is flat at every end — against every strategy judged under the run's pins **and measured the way this card was** — the pins fix the question and the data, while `[research] capital`, `folds`, `return_period`, the venue model, an attached construct's host version, what the sleeve sizes to, the bar grains it loads and the warmup sessions it is fed fix the arithmetic, and a number measured under one of those changed is no anchor — the last three are resolved rather than declared, so an attached run's budget and grain move when its host's `hypothesis.yaml` is registered again, and the warmup prefix moves when a `kanso data load` adds a printed day inside the lookback — and one matching on at least `[research] redundant_pct` percent of their shared sessions **and** scoring within that hypothesis's noise floor, `max(min_delta, k_se x se)`, of what that strategy scored is **redundant**: recorded as a card of that status carrying the metric it measured — a trial, and a row in `results.tsv` like any other — the lane copy restored, a `redundant` event carrying the card it repeats and both numbers, and the command refused (exit 2) with a remedy naming the floor to move the result past and `kanso research show ID --sha` for that card. The same bytes twice are the plainest case. A book already held that earned a number more than the floor from it is not a repeat but an ordinary discard: the refusal says the result is known, and two numbers that far apart say it is not. It appends a `same_book` event all the same, carrying the book it matched and both numbers, because which book a card matched is what no column of a card holds and what the next proposal is shown A card that beats the best is a keep whatever it resembles, and the baseline is never redundant |
| `kanso research run ID [--cards N]` | the same loop with the model in the agent's seat: begin a run if there is none, then propose → apply → evaluate until `N` cards or until the run stalls. `--cards` counts what **this** invocation proposed, so the baseline and everything a previous invocation left behind are not in it. A proposal is a unified diff over `strategy.py`, applied in-package; one that does not fit, names another file, changes nothing, or reproduces bytes the hypothesis already carded under the run's pins (the result is known, and it would count as a trial) is a wrong answer and takes the retry ladder rather than becoming a card, the last with the card it repeats. A ladder that runs out having judged a repeat anywhere in it is a **miss**: the model had no new change to make, which counts toward the stall exactly as a discard does, without a card — recorded as a `repeated` event and reported as `missed` — and `--cards` counts it, so an exhausted proposer cannot keep a bounded call running. A ladder that runs out without ever proposing a repeat still fails the step, and the failure is recorded with the remedy that says what the last answer was rejected for. The proposer is shown the last `[research] context_cards` cards of the hypothesis under those pins, across runs, so a run that begins after a stall does not re-walk the last run's discards, and the reasons this run was rewound, so a rewound run does not walk back into the drift it was rewound for. Every proposal carries `tags` — one or more of the vocabulary `kanso.schemas.TAGS` fixes, saying what the change reads, holds, filters, exits and sizes — recorded on the card, and the proposer is shown `coverage`: every card under the pins read back by tag, as a count, the best metric and its status, and the newest card, so a corner of the search already walked reads as walked however far back `context_cards` reaches. A run that stalls on a keep that scored above zero and that nothing has certified certifies it before returning, and the command reports that certificate; a best at or below zero showed no edge in research and is not certified. A proposal that applied and ran but held the same book as a strategy already judged under the pins, and scored what it scored to within the hypothesis's noise floor, is **redundant** — the loop refuses the turn after the backtest as it refuses a repeat before one, but not the record: the card is written with that status and the metric it measured, so it is a trial and it enters the coverage, and it counts once toward the stall, once by `--cards` and once toward the alignment cadence, reported as `redundant`. The change that produced it is what the next proposal is shown as its `last_diff`, like any other card's. The `redundant` and `same_book` events under the run's pins, across runs, are shown to the next proposals in one list, each saying which kind it is, because which card a book was already measured against is the one thing the card itself does not say — and a card that moved the number has no status to say it by either. One window of ten for both kinds and not one each: they are one fact to a proposer, that this book has been held, so the newest ten are the newest ten of both and an older repeat gives way to a newer book of either kind. Under the pins and not under the reading a card was measured with, because an entry is the record that an idea has been tried and not an anchor to be compared against — `records.matched_book` is what reads the reading. A card that crashed is the one card whose idea was never judged, so the next turn is a **repair**: the proposer is shown the traceback and the change that produced it, as a diff over the file the lane was restored to, and asked for the same idea with the fault fixed. Two repairs per idea, then it is dropped and the next turn asks for a new one. The search has a **phase**, set by the misses since the last keep and stated to the proposer as a fact: `local` for the first `[research] local_cards`, when a parameter, a threshold or a respelling is what is asked for; `structural` for the next `structural_cards`, when a change that moves no structure — the syntax tree of `strategy.py` unchanged once every constant is blanked — is refused on the ladder, and a ladder that runs out on such answers is a miss recorded as `repeated`; then `local` again. A run that stalls on the same best as the `reseed_after_stalls − 1` stalls before it re-seeds the next run from the best other keep under the pins still aligned — never one a drift check marked — else from the stalled run's own base; `best_sha` is untouched and the outcome's certificate is unaffected |
| `kanso research end ID` | end the run and remove the lane directory, and nothing else: the cards, the blobs and the best stay in state |
| `kanso research show ID [--sha S] [--diff S2]` | print a card's stored `strategy.py` (default: the best), or the unified diff between two of them. A sha is any unique prefix of one belonging to this hypothesis; a foreign or ambiguous prefix is refused (exit 3) |

A card is `keep`, `discard`, `crash` or `redundant`. On a keep the run's best moves; the
hypothesis's best moves when the keep beats it or is already this run's, and only then is
the blob written to `hypotheses/<id>/strategy.py`, which is always the hypothesis's best;
on anything else the lane's `strategy.py` is restored from the best, else from the run's base. `results.tsv` is
rendered from state, so the history survives every restore.

Every `align_every` cards a run is asked whether it still tests its own idea; `stall_k`
consecutive non-keeps — discards, crashes, redundant cards and repeated misses alike — end it;
`redundant_pct` is the share of shared sessions on which two strategies holding the same
book are one book — one experiment as well when their numbers agree to within the
hypothesis's own noise floor, which is the rest of the rule and is not configured here;
`local_cards` and `structural_cards` are the lengths of the two
phases; `reseed_after_stalls` is the spell of stalls on one best after which the next run
starts elsewhere; `explore_after_stalls` (0, never, in the template) is the spell of stalls
on one best, since the last exploration, after which a daemon lane runs `hyp explore` on the
stalled hypothesis once the driver has returned — a failure there is an `explored_failed`
event and never the lane's. All are `[research]` keys of `kanso.toml`, and all are framework
search rules rather than decisions an agent makes: they bound the search, they do not
choose within it.

## The daemon and the queue

| command | what it does |
|---|---|
| `kanso research start` | detach a supervisor and start one worker per lane the envelope allows, plus the monitor. First it puts back in the queue any hypothesis a dead lane claimed and dropped before recording its run, or was holding between a stall and the scheduler's decision — each at the priority it held — and never one whose run you ended, one you took out of the queue, or one you retired. Prints the pid, the lanes and the log. The lane count is read from `envelope.yaml` as last detected — `start` does not re-detect, so a host that changed keeps its old plan until `kanso env detect` runs again. A second `start` in the same workspace is refused (exit 2): the pid file is also the lock. While the daemon runs, a lane or the monitor that ends without a stop — the kernel's OOM killer on a large card, a crash — is recorded as a `lane_died` or `monitor_died` event (its pid, the exit status or the signal that took it, how long it had run, how long it waits) and started again under the same name. The lane in a dead one's place resumes the open run the dead one left, and that run's next card reclaims what the killed card left in `.card/`; what the dead lane held with no run yet goes back in the queue at the priority it held, and the directory its baseline was running in is removed. A child that had run a minute or more comes back at once; one that had not waits two seconds first, doubling with each such death in a row up to five minutes |
| `kanso research stop` | signal the daemon and wait for it to go. The supervisor signals every lane and the monitor at once and gives them one grace of five seconds between them, then kills whichever is left — so a stop takes about that long however many lanes are busy. Once signalled a lane begins no further proposal, card, alignment check or exploration: a proposal still being answered when the signal arrives is dropped, not carded. A card in flight is killed with its lane and not recorded, and a card whose lane was killed outright ends itself, so none is ever left running on its own; a stall's certification in flight is killed the same way, and the hypothesis goes back in the queue to be certified at its next stall. A demotion the monitor is making is not: it is left to finish, since one killed between the move and the redeploys would leave a version off the live stage with no escalation saying so, and a monitor still making one when the grace runs out is killed and the demotion ends with it. A lane answers the signal at its next safe point whatever it ran before it, a replay on a trading node included. A daemon that has not gone after twenty seconds is killed with every process in its group — its lanes and its monitor — and a lane whose supervisor is gone, however it went, stops as though it had been signalled. A lane or the monitor that ends because the daemon is stopping is not started again. **Nothing is ended and nothing is cleaned up** — active runs and their lane directories stay exactly where they are, and the next `start` resumes them before it takes anything new — so stopping is a cheap act |
| `kanso research status` | the daemon, its lanes, every child the running daemon started again after it died — how many times, and for the newest death when, the exit status or the signal that took it, how long it had run and how long it waited (`restarts`) — every active run with its `lane_sha`, `best_sha` and `base_sha`, and the queue |
| `kanso research queue add ID [--priority P]` | put a hypothesis in the queue, or raise the priority of one already in it. Served by priority descending, then by arrival. A lane claims a hypothesis by removing it from the queue, so two lanes that reach for the same head at once cannot both start it: one has it and the other takes the next row. A hypothesis a lane has claimed and not yet begun is refused (exit 2) rather than queued behind itself, since a second row would be claimed by a second lane; one with a run open is queued and waits its turn |
| `kanso research queue remove ID` | the inverse: take a hypothesis out of the queue, or out of a lane's hands if one claimed it and its run has not begun. That lane lets it go at its next catalog read — the warmup sessions, the benchmark's hold and the baseline's window are all read in the lane's own process before the run exists — or within a second when the baseline card is already running, which is killed. It begins no run, leaves no lane directory, records no failure, and claims the next hypothesis at once. A lane that fails for another reason meanwhile does not bring the hypothesis back, a stall's certification in flight returns it nowhere, and neither does the next `start`. Refused (exit 2) when it is neither queued nor held; a run that had already begun is ended with `research end` |
| `kanso align check ID` | run the alignment check now: the deterministic syntax-tree checks first, the model only when they pass and the lane holds bytes nothing has answered for. **The run's base is never judged**: the bytes a run was handed are not its proposal, and no rewind can go behind them. Nor is the file the last check left the lane on — the one it passed or the one it rewound to — which the loop returns to only when nothing has kept since. The syntax tree still reads either, since what that finds is a fact rather than an opinion, and one that fails it is drift like any other file; one that passes is reported aligned with `judged: false`, no model call and no escalation. The check is recorded so the next one differences from there — up to, and never past, a keep no check has answered for, which is there only when an operator has carded a file and then written older bytes back into the lane by hand — and no card's mark changes. Drift is not an error and does not exit like one — a check that finds the run has wandered has already rewound the lane to the last aligned keep (never onto the bytes it found drifted), re-pointed `best`, marked the cards since the last check and written an escalation, and reports that with exit 0 |

## Certification

| command | what it does |
|---|---|
| `kanso cert plan ID [--replan]` | decide what would count as proof for this hypothesis — the cert, paper and live gates, each with parameters chosen inside the toolbox's ranges and a rationale — in one call to the best model on the register, and pin it at `certificates/<id>/plan.yaml`. The planner is shown the hypothesis, its construct, the toolbox, what data the workspace holds and the trial count, and never a card metric, a certificate or the strategy source. Reading a pinned plan costs nothing; `--replan` re-runs the planner on the same closed inputs and mints the next `plan_version`. There is no default plan: with no model configured the step exits 2. Warns (`warning`, and `warnings` under `--json`) when the paper window the plan implies — the longer of a paper gate's `min_duration` and its `horizon_mult` × horizon — is under a tenth of the certification window the hypothesis declares, because a paper objective that much noisier than the band measured over that window reads as drift; the plan is pinned either way |
| `kanso cert run ID [--sha S]` | run the plan's cert gates for the hypothesis's best card (or the one `--sha` names, as any unique prefix) over the embargoed certification window, on the data snapshot the run that produced that card pinned, and write `certificates/<id>/<sha7>-<n_trials>-p<plan>-e<engine>.yaml` with the certified `strategy.py` beside it as `<sha7>.py`. Plans first if there is no plan. Both windows are run warmed on the sessions the hypothesis's `warmup` asks for before each, and every rerun of a window — the host's, a perturbed parameter's — on the same span; the lags and the volume the evidence gates read are the certification window's own, each day's volume struck as `volume x close x multiplier` of the resolved definition, so a contract and a share are both notional. `param_plateau` is judged after every other cert gate, and once `embargoed_window` has failed it is recorded as skipped with the reason and none of its perturbation backtests is run. A certificate is immutable: certifying the same bytes again under the same plan **and** the same engine is refused (exit 2), so re-certifying an unchanged commit after an engine upgrade is a plain `cert run` |
| `kanso cert show ID` | the newest certificate: the verdict, then each gate with its evidence or the reason it judged nothing |

**A failing verdict is not an error.** `cert run` exits 0 and says `fail`, because the
certificate is what the command produces and a fail is evidence: it counts toward the
`[certify] n_fail` run, its failing gates are fed back into the next proposal, and the run
that reaches a multiple of the allowance writes an inbox entry and returns the hypothesis
to research; nothing about a verdict ends it. A
snapshot holding a dataset of unknown publication, or a vendor-adjusted one, is a recorded
fail for the same reason — it reaches the operator the way every other failure does.

A run that stalls with a keep nothing has certified certifies it there and then, so the
autonomous loop reaches a certificate without an operator — when that keep scored above zero
on its objective. Every objective in the library measures an edge whose zero is none, so a
best at or below zero showed no edge and no certificate can show one: the stall records what
it scored (`best_metric` on the `stalled` event) and requeues the hypothesis without spending
the planner's call or the certification window's backtests on it. The certification is made
in a child of the lane, exactly as `cert run` makes it, held to what a card of the judged run
may hold (`docs/concepts.md`, Run, lane and the envelope), and the `stalled` event records
what it cost (`cert_peak_mem_gb`, `cert_wall_s`); one killed over that share is refused, and
the lane records the refusal and puts the hypothesis back. "Certified" means a certificate of
those bytes under the pinned plan and the installed engine, whatever came after it: a best
certified before other bytes were is not certified again, because a certificate is immutable
and the repeat would be refused. Either verdict returns the
hypothesis to the queue at priority −1; only a hypothesis the operator has retired leaves
it, and `kanso hyp resume` brings that one back.

**A plan that names `parity_replay` makes `cert run` replay.** That gate is the comparison
of the two code paths over the certification window, so the runner replays the subject on
the node path and on the engine path and hands the gate what the comparison found; the two
sessions it wrote stay in `sessions/` to be read, and the gate's evidence carries what each
path was released — its count and its stream's digest — beside the intents it compared, so
the certificate states what both paths were fed whatever becomes of the sessions. Each path
streams the certification window in a card's chunks (`kanso replay run`), so the replay holds
a read and a chunk of it rather than the window, and folds each chunk into its digest as it
is released rather than writing it out. A replay that cannot be set up at all leaves the gate
without its evidence, and the certificate records that nothing compared the paths rather
than claiming that they agreed. Neither path throttles what a strategy submits: the
engine's risk engine would deny the hundred-and-first order inside one second of its clock,
and a denied order is closed and leaves the room for the next, so a sleeve re-posting through
a flickering quote would size its next entry from a fuller room on the engine path than on
the node path. Both paths run at a rate no replay reaches.

**A passing verdict composes and deploys by itself.** The construct's version is made and
the paper stage is offered it, because both acts follow from the certificate with no
decision left in them and a loop that runs indefinitely cannot stop at every certificate to
ask for a command with only one possible form. A version that cannot be composed — its book
falls below the `book.maintenance_pct` its hypothesis declares — or a stage that cannot take
it — it is halted, the engine has moved, the catalog has no forward data, the limits leave
no capital — escalates `deploy_blocked` and the certificate still stands. What is never automatic is the
next step: paper to live needs `promote --live --as NAME`.

## Strategies

A strategy is composed, never written. A passing certificate composes the version it
implies and offers it to the paper stage on its own, so these commands are the hand-driven
form of an automatic act; running `strat compose` on bytes already composed under the same
engine returns the version that exists rather than a second copy of it, and a sleeve certified
with different bytes, or the same bytes under a new engine, composes its strategy's next
version. The certificate composed is the hypothesis's newest passing one, under the hypothesis
as the registry pins it now: one earned under another universe, resolution, data requirement,
construct, sizing rule, warmup, benchmark or book policy is refused (exit 2) by the field that
moved, and so is a construct
whose host's latest sleeve is not the one its run was pinned to. A version whose own book, run
over the sleeve's certification window, falls below the `book.maintenance_pct` its hypothesis
declares is refused (exit 2) with the worst ratio and the floor, whether or not any card was
held to `maintenance_margin`: no version is written to `strategy.yaml`, and the implementation
generated to measure it stays in `impl/<version>/` until the next compose replaces it.

`STRATEGY[@V]` is the notation every command below shares: a strategy id, optionally a
version. Leaving the version out means the one the command's own rule picks — the latest
for a read, the version on the stage for a move.

| command | what it does |
|---|---|
| `kanso strat compose ID` | turn this hypothesis's newest passing certificate into a version: version 1 of a new strategy for a sleeve composed the first time and its own strategy's version n+1 when it is certified again, the host's version n+1 for a construct attached to one. A sleeve's new version puts every idle hypothesis attached to it back in the queue (`host_composed`), so its next run pins the new version. Writes `strategies/<id>/strategy.yaml` and generates `strategies/<id>/impl/<version>/` — a verbatim copy of every certified source plus a manifest naming the classes — which is the one directory a backtest, a replay and a live node all load. Runs that implementation over the sleeve's certification window — warmed on the sessions before it when the hypothesis declares a `warmup`, as certification was — to measure the version's `expectation`: the objective, a ninety-percent interval and the ninety-fifth-percentile drawdown |
| `kanso strat show [STRATEGY[@V]]` | with no argument, every composed strategy and the state of each version; with a strategy, its versions and their bands; with a version, what it is made of, what is expected of it and what it was certified under |
| `kanso strat retire STRATEGY[@V]` | end a version: take it off whatever stages hold it and mark it retired, then restart the stages whose kill switch is off. A stage a kill switch has halted is named and left halted |

## Portfolio

| command | what it does |
|---|---|
| `kanso portfolio show` | both stages: how each is configured — including whose money its execution client trades and which clock it runs on — whether its node has consumed everything the catalog holds, what each deployed version holds and what it has realised over the windows its stage has closed. The file's entries are read against the record: one no deployment wrote — a stage entry added to `portfolio.yaml` by hand — prints as `not deployed · in portfolio.yaml only`, is `"recorded": false` under `--json`, and is counted in neither the stage's allocation nor its P&L. Writes nothing |
| `kanso portfolio clients` | every execution client a stage may name: what each declares (`capital`, `clock`), which adapter provides it, which stages it may be configured on, and per credential the variable name and where it resolves from — never a value. Then, per stage, what `deploy` would refuse its configuration for, or `ok`. Opens nothing and reaches nothing |
| `kanso portfolio deploy --stage paper\|live` | admit what composition produced, apply the capital rule, validate what the stage's execution client declares, render the node configuration and (re)start the node. The stage's simulated venue is the card's venue, built by the same function from the venue model each version was certified under: the same latency, book type, queue position and fill model, with the fee model left unset as on a card, so the exchange charges the instruments' zero rates. Two versions on one venue must therefore have been certified under one `costs.latency_ms` and one book requirement (`book` in `data_requirements`, or not); a stage that already holds a version certified under one latency refuses a version certified under another (exit 2, naming both) before it writes anything — `portfolio.yaml`, the strategy files and the state record stand as they were — whether or not the stage has new data to run, and a version the capital rule would leave unfunded still counts. The way out is `kanso strat retire STRATEGY@V` on one of the two, or a re-certification under one. A node flattens before every stop, so a stage always restarts flat and each redeploy realises its window into the record the paper and live gates read |
| `kanso promote STRATEGY[@V] --live --as NAME` | move a `promotable` version onto the live stage under a named operator's recorded approval, retiring whatever was live, then redeploy both stages. A version the live stage's venues could not hold beside what is already live — any of the venue disagreements `deploy` refuses — is refused (exit 2) before the approval is recorded or anything moves. The check is of the live stage; a paper stage that already held a disagreement from before this release still refuses its own redeploy after the promotion, and `kanso strat retire` on one of the pair clears it |
| `kanso demote STRATEGY[@V]` | take a live version off the live stage — back to paper, or retired when a newer version is already there — then redeploy the stages that are not halted. A demotion never waits on agreement: the version leaves live first, so a demoted version that paper cannot hold beside what is there — one of the venue disagreements `deploy` refuses — ends the paper redeploy at exit 2 with the move already made; `kanso strat retire STRATEGY@V` on one of the two clears it |

**`deploy` refuses eleven things with exit 2**, one with exit 3 and two with exit 4, and blocks one more with a `deploy_blocked` escalation: a share below the version's budgets — the sleeve's `sizing` budget and its sized overlays' together — because a sized version deployed at less than its budgets would size every order over the money it has; raise `limits.per_strategy_max_pct` or the stage's capital.

With exit 2: a stage whose `kill_switch` is on, because the switch is the operator's and a
deployment that cleared it by starting a node would make it advisory; an execution client id
nothing in the workspace provides; a version whose `pins.nautilus_version` differs from the
installed engine, because running it under another engine is running something that was
never measured — the way out is a plain `cert run` on the same commit; a stage whose catalog
holds nothing at or after the forward window's start, because that stage has nothing to
trade and nothing to be judged on; a `clock: wall` execution client paired with replay data
or with any speed but one, because a broker matches against current prices; a `clock: wall`
client at all, because in this version a stage node cannot run one — see below; two versions
on one venue whose venue models differ in `account` type or in `currency`, because one venue
is one account; two certified under different `costs.limit_fill`, because one exchange fills
a touched limit one way; two certified under different `costs.latency_ms`, because one venue
is one round trip and the stage's venue carries the latency its versions were measured
under; and a version whose hypothesis requires `book` beside one whose does not, because one
venue keeps one book — a level-two book with queue position or the top of the book — and
each version was certified on its own. Each of the last five names both versions and the
key; for the account type and currency the way out is one value under `venues.<MIC>` in
`portfolio.yaml` and a re-certification, and for the last three it is
`kanso strat retire STRATEGY@V` on one of the two, or one `limit_fill`, one latency and one
book requirement in both hypotheses and a re-certification. All five are refused before the
stage is written, so a refused deployment leaves `portfolio.yaml`, the strategy files and
the state record as they were.

With exit 3: a version whose implementation is not the code it was certified with. Every
file under `strategies/<id>/impl/<version>/` is hashed against the `strategy_sha` its
manifest records before anything is imported, so an edited, truncated or deleted source is
named — with both digests and the certified copy to restore it from — rather than run.
`kanso replay run --strategy` checks the same thing for the same reason: the directory is
the one a stage loads, so a replay of it has to be a replay of what would trade.

With exit 4: a `capital: real` client configured anywhere but the live stage, and a version
on a `capital: real` client with no approval on record. Both are a missing act rather than a
broken precondition, which is why they carry their own code.

**Execution clients, and what each declares.** A stage names one in `portfolio.yaml`, and
the id is all the file carries; what matters is the pair of declarations behind it. `capital`
is `simulated`, `broker_paper` or `real`, and `clock` is `replay` or `wall`. Those two are
what forbid real money off the live stage, what forbid replayed history feeding a broker,
and what make a promotion the only way a version reaches real capital. `kanso portfolio
clients` prints them; `kanso doctor` grades them.

**A `clock: wall` stage needs a live data client and `speed: 1`.** A wall-clock client fills
against the price the market is showing now, so pairing it with `data: replay` would fill
orders at prices unrelated to the data that triggered them, and any speed but real time
would compress a market that is not compressible. `data` therefore names a live data client
an adapter provides — for a broker's own feed, usually the client id of the same account —
and `speed` is 1.

**In this version `deploy` refuses a `clock: wall` client outright (exit 2).** A stage node
here is a bounded run: it releases whatever the catalog holds that the stage has not
replayed, into kanso's own simulated venue, flattens and returns. That is exactly what a
`clock: replay` client declares it is executed by. A wall-clock client needs a node that
outlives the command that started it, and running one through this node would fill every
order in simulation while the stage record — and the paper and live gates reading it —
called the money the broker's. The declarations, the refusals and the promotion path are all
live; the long-running node is the piece that is not, and it is tracked in the backlog.

The same bounded node is why a stage's `speed` paces nothing yet: the value is validated
against the client's clock, recorded on the session and printed by `portfolio show`, and the
catch-up itself runs unpaced, so a stage at `speed: 1` replays months of minute bars in
seconds. `kanso replay run --speed` is the one place a speed is honoured in this version.

**`promote` is the only command in kanso that can put money at risk, and the only one that
requires a person.** `--as NAME` is the whole of the approval: there is no environment
fallback and no default, the approval is recorded against that exact version before
anything moves, and without `--as` the command exits 4 having changed nothing. Editing
`portfolio.yaml` by hand can therefore never move real money — the file says what is
deployed and the record says what was allowed. Agents pass `--as` only on an explicit
operator instruction; acknowledging an inbox entry is not one.

## Replay

| command | what it does |
|---|---|
| `kanso replay run (--strategy STRATEGY[@V] \| --hyp ID [--sha S]) [--from D] [--to D] [--speed N] [--mode node\|engine]` | replay one target over the catalog and write `sessions/<id>/`: the record — with the count of points released and the sha256 of their stream, which is not itself written — and the order intents that came back. `node` is the live code path — a trading node, kanso's replay data client, a simulated execution client — and `engine` is the research one. The range defaults to the target's forward window through the last day the catalog serves. A target whose hypothesis declares a `warmup` is fed the sessions before the range on both paths, with every order dropped until the range opens; the session records the range's points and nothing it warmed on. Both paths read the range as a card's child is streamed its window — an hour at a time for prints, quotes or a book and a day otherwise, cut into chunks of at most 250,000 points between instants — and run a chunk before reading the next, so a replay holds one read and one chunk of its range, never the whole of it, and folds each chunk into the stream's digest as it is released. Nothing is written until the replay has finished: one refused or failed part-way leaves nothing in `sessions/` |
| `kanso replay parity (…)` | replay on both code paths over the same days and compare what each was released — the count, then the stream's digest, a difference in either being the first divergence at any tolerance — then the order intents element by element — instant, instrument, side, quantity, order type and, for an order that names one, price — reporting the first divergence with its index and its field, or that the two agreed. `--ts-ns` is the instant tolerance in nanoseconds, and it exists to be set to zero |
| `kanso replay show [SESSION]` | one session — what ran, over which range, the points released and their stream's digest, the intents that came back — or every session this workspace holds |

**Replay always executes against kanso's own simulated venue**, whatever a stage is
configured with: a replay feeds history and a broker fills against current prices, so the
pairing would fill orders at prices unrelated to the data that triggered them. It is the
same venue a stage node attaches — one piece of code, so the exchange a version is judged
against and the exchange it is deployed onto cannot drift apart. Replay is evaluation
only — it writes no card, moves no `best` and certifies nothing — and the window it runs is
the one nothing may backtest.

## Monitoring

| command | what it does |
|---|---|
| `kanso monitor run` | one pass of the watch every deployed version lives under. The daemon runs it on `[monitor] interval`; this runs it once |

A pass judges each deployed version against the paper or live gates of its **sleeve
hypothesis's** plan, with the bands from the version's own `expectation`, and acts on the
verdicts: a paper version whose gates all pass becomes `promotable` and reaches the inbox;
a live version that fails one is demoted; a live version that fails the daily loss halts its
stage instead, since halting is the stronger act and demoting into a halted stage would
change nothing about the money. The pass also sums gross and net exposure per stage — the
two limits only a whole-stage view can see — and halts a stage on a breach.

A pass demotes in a child of its own process, exactly as `kanso demote` does in yours, so
the trading nodes the redeploys run leave nothing behind in a monitor that runs all day;
the child writes every record itself, and its refusal is the pass's, recorded against the
version.

Every action is taken once, on the transition, so the command is safe on a timer and safe
to run twice. It exits 0 whatever the verdicts are: a failing gate is a fact about a
deployment, not a failure of the pass that found it.

**The paper gate is two-sided, and a short window is a fail.** `paper_forward` requires the
version to have been on the stage for the longer of the plan's `min_duration` and
`horizon_mult` × the hypothesis's horizon, measured on the stage clock; a version that joined
more recently fails with `elapsed_s` and `required_s` in its evidence rather than being
skipped. It then requires the objective the stage realised to fall **inside** the
ninety-percent interval composition measured — a result above the band fails exactly as one
below it does, because a stage that out-performs its certification is not reproducing the
model that was certified, and promoting on it would promote an unexplained difference. Of
the sleeve's card-stage constraints the drawdown limit and, when the sleeve holds cards to
it, `maintenance_margin` are judged on the stage's own run, and a breach of either fails;
`min_trades` is recorded as skipped, since a research-window count cannot be met in a paper
window.

## Models

| command | what it does |
|---|---|
| `kanso models check` | print the register as the router reads it — which model serves which tier, which task class routes where, at what thinking effort and output cap — then make one minimal call to every configured model. A tier with no model behind it is refused before any call is paid for. A model that does not answer is reported rather than raised; the command exits 2 when any failed and 0 when they all answered |

Every call is ledgered, including the failed attempts of a retry, because a rejected answer
was still generated and billed.

## Operating

| command | what it does |
|---|---|
| `kanso inbox` | the escalations nobody has acknowledged, oldest first, each with the commands its kind offers over its subject |
| `kanso inbox ack ID` | mark one entry read. **Never an approval**: it writes one timestamp and stands for no decision, so the actions the entry offers are still yours to take. Acknowledging twice is acknowledging once. `escalations/inbox.md` is append-only and is never rewritten, so the file keeps every line and the rows are what say which are unread |
| `kanso status` | the one screen: what the lanes are doing, cards per hour over the trailing hour, the best metric per hypothesis, today's spend broken out by lane, unread escalations, and any hypothesis whose baseline would not run. Writes nothing, so it is safe against a workspace a daemon is working in and safe to run in a loop |
