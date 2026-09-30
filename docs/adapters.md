# Adapters

An **adapter** is one outside party's whole presence in kanso. There are two kinds and the
rules are the same for both. A **data adapter** offers datasets, the credential names it
needs, the loaders that fetch through it and the provider that resolves its instruments. A
**broker adapter** offers execution clients, the live market data client that goes with
them, the venue model it declares for the venues it serves, and the credential names each of
its accounts needs. Either is the only way the rest of kanso reaches that party, and it is
the same interface for an adapter this package ships and one you write in `kanso_ext/`.

Two rules hold for every adapter and are worth stating before any particular one.

**The core knows no vendor and no broker.** No module outside an adapter's own package names
one, no framework behaviour requires one, and the whole test suite, `kanso doctor` and the
demo are green with every credential unset. Adapters are discovered from their adapter
directory rather than listed anywhere, so nothing in kanso has to be edited when one lands.
The single exemption is the rendered `kanso.toml`, which names a broker under
`[research] broker` as the operator's default; the core itself defaults it to nothing, and a
workspace naming a broker it has no adapter for falls back to the shipped venue defaults
rather than to a refusal.

**An adapter is enabled by its credentials, never by installation.** There are no extras to
install and no switch to flip. A registered adapter with nothing set is the ordinary state
of a fresh workspace: `kanso data adapters` lists it, says which variables it would need
and where each resolves from, and reaches nothing to say so.

## What a command tells you

| command | what it answers |
|---|---|
| `kanso data adapters` | what is registered: id, kind, capabilities, quota, loader ids, and per credential the name and where it resolves from — never a value. No network I/O |
| `kanso data adapters --check` | what your key *actually reaches*: one authenticated lookup first, then one entitlement probe per dataset and one history-floor measurement per entitled price series. It reports the number of requests it made, and exits 2 if a configured key does not authenticate |
| `kanso doctor` | the same registration facts, graded. Green whether or not an adapter is configured; each broker's `[adapters.<id>]` table is read through that broker's own model, and one it refuses fails the `execution` check |
| `kanso doctor --check-adapters` | the same probe, graded. A dataset your plan excludes is reported and never graded down — it is a subscription, not a fault in the workspace; a credential that does not authenticate is the one failure |
| `kanso portfolio clients` | the execution half: every client a stage may name, what each declares, which adapter provides it, which stages it may be configured on, and where each credential resolves from. Then what `deploy` would refuse each stage for. No network I/O |

`--check` asks a different question from the plain command, and the difference is the whole
design. What an adapter *offers* is a constant. What your key *reaches* — whether a dataset
is included, and how far back it goes — is a fact about a subscription on a day, so it is
measured every time rather than declared once. A constant would eventually tell you that
you are not entitled to something you pay for, which is the most expensive wrong answer an
adapter can give.

## Entitlement and the history floor are two different answers

A vendor may state several quite different conditions with one sentence: *this dataset is
not in your plan*, *this range is older than your plan's window*, *this ticker carries the
wrong market prefix*, *this key shape is not one we recognise*. kanso never reads that
sentence. It reduces the wire to a signal — rows, no rows, refused, rejected — and then
establishes meaning by asking a second question whose answer separates the cases:

- a **recent** window, where a plan's rolling history window cannot be the reason;
- a **control** endpoint that is not gated the same way, which says whether the vendor
  recognises the key at all;
- the **same question asked as widely as the endpoint admits**, where the recent window
  came back empty. For a listing that is the request with its dates taken off; for a price
  series the range is part of the address and cannot be taken off, so the widest form is
  the whole of history.

**A refusal is evidence about the plan; an empty page is evidence about the window.** The
two are never read as the same thing. A refusal at a recent date cannot be about the range,
so it is about the plan unless the vendor does not carry the key at all. An empty page is
about the fortnight it was asked over and nothing else — statements are quarterly and
filings episodic, so a fortnight of either holds nothing in the ordinary case — so the
question is asked again as widely as the endpoint admits. A series that answers *that* with
rows is included, whatever the fortnight held.

There is one refusal that is *not* about the plan, and it follows from the same rule. Where
a quiet fortnight is answered `200` with no rows and the whole-of-history form of the same
question is refused, what the source declined was a range starting at the epoch — below
every plan's history window. A series the plan excludes is refused at every date, this
fortnight included; this one was not, so the plan is not what refused it. It is reported
`ok` and its floor decides the range, rather than being reported as a subscription to buy.

**The control question is asked where the vendor keeps the key.** Which endpoint that is
depends on the class: an option contract is not in the generic ticker reference, which
rejects an option key outright, so asking it about one answers "unrecognised" for a
contract the vendor defines perfectly well. The result would be `malformed` reported for a
series the plan genuinely excludes — an operator sent to correct a ticker that was already
right. The same rule decides where a key is *resolved*, for the same reason.

Four outcomes come out of that, and they are reported and raised separately:

| outcome | what it means | fatal? | what to do |
|---|---|---|---|
| `not_entitled` | the plan does not include this dataset for this key | no — it ends one dataset and leaves the run alone | change the plan, or drop the series from the spec |
| `below_floor` | the source holds nothing that old | no — a backfill that reaches the floor has finished | ask for a range at or after the floor; `data backfill` clamps to it |
| `empty` | entitled, above the floor, and nothing is there | no — it ends one dataset and leaves the run alone | widen the range, or check the instrument traded over it |
| `malformed` | the request is not one the vendor honours | yes — every request of that shape fails the same way | fix the key or the prefix its class carries |

The three non-fatal ones are the three that are true of one series and say nothing about
the next, so a walk over a universe skips that series and keeps going. Only `malformed` is
fatal, because it is a statement about a request *shape*: every name behind it would be
asked the same broken way.

**Entitlement is probed at the grain the source gates on.** For most classes that is the
endpoint: one answer covers the class. For indices it is the *ticker*, because the source
gates them by the feed behind each one — one index returns bars and the next does not, on
the same endpoint over the same range. What carries from one key to the next is the *plan*
answer, and only that: `not_entitled` and `ok` are facts about a subscription, while "this
key holds nothing" and "this key does not exist" are facts about one name and are
established for every name that asks. There is no feed allowlist anywhere in kanso, because
feeds are entitled in part and a name filter silently drops keys your plan does serve.

**The floor is measured per series, not per class.** For an instrument listed after your
plan's window opens, the floor is its listing date — which is the number a backfill wants
either way. A floor records the day it was probed, because a rolling window moves it.

A floor is read off the oldest row of a whole-of-history request, or found by halving the
start date where the source refuses such a request instead of truncating it. Halving needs
a start date known to serve; where there is none — the fortnight was quiet, or the rows
carry no readable date — the floor is reported **unmeasured**, at the epoch. That clamps
nothing, which is the safe answer: the alternative is a floor of a fortnight ago on a series
with twenty years behind it, and a backfill that fetches a fortnight and reports success.

**A reference listing is asked a question it can answer** — splits, dividends, financials,
filings. Those are sparse event series, and two things follow. No floor is measured for
one: a year holding nothing is an issuer's silence, not the source's edge, and the search
that finds a floor in a continuous price series returns an arbitrary year in a sparse one.
And each is probed **market-wide and with no date window at all**, because a fortnight of a
quarterly statement holds nothing in the ordinary case and a fortnight of one issuer holds
nothing nearly always. Only a listing the source **refuses** is `not_entitled`.

## A range straddling the floor is served truncated, silently

This is the one behaviour to keep in mind when reading a manifest.

Ask for 2021 to 2026 on a series whose history begins in 2024 and the source answers **HTTP
200 with a short series beginning at its floor**, with no warning and no error. A short row
count is therefore never evidence of anything, and no probe can see it.

kanso's answer is that **coverage is what was served**. Every loader compares what arrived
against what was asked for, records the *served* span in the manifest — never the requested
one — and reports the difference as a shortfall. `kanso data show` prints the served spans
and the holes between them, snapshots are pinned by coverage, and `data backfill` clamps to
the measured floor and says that it did. A hole is a day the instrument's market opened and
nothing holds: a weekend or a holiday no source could serve is never one, read off the
market's calendar (`docs/concepts.md`, Snapshot) rather than off the range a request asked
for. Nor does the source's own answer close one — neither a request answered with nothing,
which `data show` lists inside the hole it failed to fill, nor a truncated answer, whose
missing days stay a hole until a backfill asks for them again.

So: if a load returns fewer days than the spec named, read the manifest's span. It is the
truth about what you hold.

## The Massive adapter

### Credentials

Three names, each resolved on its own, from the workspace `.env` and then the process
environment. kanso never writes one to a file and never prints a value.

| variable | what it is for |
|---|---|
| `KANSO_MASSIVE_API_KEY` | the REST API. Sent in a request header, never in a URL |
| `KANSO_MASSIVE_ACCESS_KEY_ID` | the flat-file object store's access key id |
| `KANSO_MASSIVE_SECRET_KEY` | the flat-file object store's secret |

Two of them may hold the same value under some plans. Nothing in kanso relies on that: an
operator whose plan issues distinct keys, or who rotates one, must not have to discover
that kanso assumed otherwise.

A key that expires — an option or a futures contract — is discovered from the reference
endpoints at one request each rather than hard-coded, because a stale key comes back
refused and would be reported as a plan that excludes the class. Where no contract is
listed to probe with, that class is reported `unprobed`: nothing was asked, so nothing was
established, which is a fifth answer and not one of the four above.

### What it offers

| class | datasets | entitlement grain |
|---|---|---|
| stocks | reference, bars, trades, quotes, corporate actions, financials, filings | endpoint |
| options | reference, bars, trades, quotes | endpoint |
| futures | reference, bars | endpoint |
| forex | reference, bars, quotes | endpoint |
| indices | reference, bars | **ticker** |

That is the offer, not your plan. Run `kanso data adapters --check` for the second.

### Loaders

| loader | serves | transport |
|---|---|---|
| `massive_bars` | aggregate bars for any class | REST |
| `massive_trades` | trade prints | REST |
| `massive_quotes` | top-of-book quotes | REST |
| `massive_bulk` | `1d` and `1m` bars for the classes the object store lays out | flat files over a signed object store |
| `massive_corporate_actions` | splits and dividends, as `CorporateAction` | REST |
| `massive_financials` | periodic statements, as the `financial_statement` type (usable in a hypothesis's `data_requirements`) | REST |

The bulk path is worth reaching for over long history where the store carries the class:
whether it does is a fact about the store's *layout*, never about a plan, and whether your
key can read it is measured with a one-byte ranged GET rather than by dragging a
multi-gigabyte day across to find out. Only a read proves entitlement there — the listing
is not scoped by product, so a prefix can list a decade cleanly and refuse every object in
it. Nothing chooses for you: the ids never collide, so a spec picks a transport by naming
one, and `data sync` continues a series over whichever transport wrote its newest dataset.

A `massive_bulk` spec names no session and no zone. The file's `window_start` and the API's
`t` are the same instant in different units — nanoseconds in the file, milliseconds over
the API — and that instant is the start of the vendor's calendar day, not the session's
open. A bar closes one resolution after its window opens on both transports, so there is
nothing about a session left for you to declare, and a daily bar's `ts_event` falls on the
UTC day after the one its window opened in. `start` and `end` are UTC days of `ts_event`
here as everywhere, so one range means the same days whichever transport serves it — which
is what lets a `backfill` over the bulk path and a `sync` over the request path extend one
series rather than interleave two conventions in it.

Two things do *not* follow from that, and both are yours to keep straight.

- **Prices.** Flat-file objects are unadjusted, so every `massive_bulk` dataset is
  `adjusted: false`. The catalog files an adjusted and an unadjusted series of the same
  bars in one place, so filling history over the bulk path and extending it with an
  `adjusted: true` request-path spec joins two price bases into one series with nothing
  marking the seam. Use the same basis on both, or keep them in separate workspaces.
- **Availability.** `publication` and `publication_rule` mean the same thing in both specs
  and are read the same way, because a delayed tier is delayed whichever way the day was
  fetched. Declare them on both halves or on neither; declaring them on one is a series
  with two conventions for when its bars became known.

### Reference resolution

Set `[data] reference = "massive"` in `kanso.toml` and `kanso data instruments resolve`
resolves ids through the adapter, one authenticated lookup per key, into the catalog's
instrument store. Options and futures resolve from the reference endpoints, which carry no
history window at all — a contract that expired long before the aggregate floor still
resolves, even though its prices cannot be read.

Nothing is completed by a guess. A missing tick size, contract size, underlying, listing
date or expiry fails *that key*, by name, rather than being filled in: a guessed activation
date lets a card trade a contract before it existed, and that error is invisible in a
result.

Write a one-digit futures year as two (`ESZ26`, not `ESZ6`) if you care about stability. A
one-digit year is resolved against the date you asked as of, and the same code read on two
dates would otherwise name two contracts under one instrument id; kanso checks the digit
against the contract's own expiry and refuses a disagreement rather than picking one.

### Publication, and one refusal worth knowing about

`ts_init` is when information became public and `ts_event` is its economic reference time.
A dataset that cannot honestly state the first is refused rather than stamped with a guess,
and that has three visible consequences here.

- **Bars, trades and quotes** are `realtime` by default, which is what a real-time
  entitlement serves. On a delayed tier, declare `publication: delayed` in the spec and
  name the `publication_rule` availability comes from; every point is then stamped from
  it. Both transports read the two fields, because the delay belongs to the plan and not
  to the way a day was fetched.
- **Corporate actions**: a spec asking for dividends *alone* is stamped at the day each was
  declared and the dataset is `realtime`. Any spec that includes **splits** has only an
  effective date to work from, which is not an announcement, so that dataset declares
  `publication: unknown` — loadable, usable for price adjustment, and refused by
  `research begin`. If a hypothesis requires `corporate_action` data, write
  `kinds: [dividend]`; the sleeve is handed each declaration in `on_data` the moment it
  was made. Such a hypothesis cannot yet be *pinned* over a real universe: a dataset is
  covered from its first declaration to its last, and an issuer that declared none over
  the range is refused as empty — measured, AAPL asked for 2006-07-03..2026-09-05 is
  covered 2012-10-25..2026-07-30, and AMZN is refused. `docs/backlog.md` row 92.
- **Financials** are `delayed` under the `fundamental` publication rule, stamped from the
  source's own acceptance instant or from the filings index joined on the accession
  number — the rule derives no lag of its own and requires the source to state the instant.
  A row carrying neither is refused, and so is a range old enough that the source stamps no
  acceptance instants at all. Being refused is the point: the alternative is a statement
  that appears to have been public before it was filed.

### Configuration

`[adapters.massive]` in `kanso.toml` is validated by the adapter's own model, which
accepts these keys and no others — a key it does not know is a typo, and a typo that is
tolerated is a setting that silently does nothing. It holds no credential.

| key | default | what it does |
|---|---|---|
| `base_url` | the vendor's API host | where REST requests go |
| `requests_per_second` | `90` | the rate limit every request in a command shares |
| `timeout_s` | `30` | per-request timeout |

The object store's host and bucket are not configurable: they are measured constants of the
layout, and a wrong one is a mis-signed request rather than a redirect.

## The Alpaca adapter

A broker this package ships, for US equities, in two accounts: a paper one and a real one.
It provides both halves of a live stage — the execution client that places the orders and
the live market data client that feeds the strategy placing them — and declares the venue
model kanso costs a backtest with, so a card is measured the way the account that would
trade it is charged.

### Credentials

Four names under the standard scheme, two per account, resolved independently at the moment
of use:

| variable | what it opens |
|---|---|
| `KANSO_ALPACA_PAPER_API_KEY` · `KANSO_ALPACA_PAPER_API_SECRET` | the paper account |
| `KANSO_ALPACA_API_KEY` · `KANSO_ALPACA_API_SECRET` | the real account |

The adapter knows these four spellings and no others. It never reads, mentions or falls back
to whatever else a machine happens to export, because a fallback is how a key intended for
one tool ends up trading through another. No value reaches a log, an error message, a repr,
a manifest, a session or a commit.

**A key belongs to an account, and the adapter checks that before it opens a socket.** A
paper key carries a prefix a real key does not, so a paper key configured for the real
account is refused, and a key without the prefix configured for the paper account is refused
too. The message names the variable and the account, never the value or the prefix. The
broker answers `401` to the mismatch it can see; this refuses it earlier and more clearly.

### The two execution clients

| client | `capital` | `clock` | may be configured on |
|---|---|---|---|
| `alpaca_paper` | `broker_paper` | `wall` | `paper`, `live` |
| `alpaca` | `real` | `wall` | `live` only, and only behind `promote --live --as NAME` |

There is no switch anywhere in the adapter that turns one into the other: the id chooses the
account, the variables and the host together. A stage that trades real money says so by
naming the client that declares it.

### The feed is declared, never defaulted

The market data host serves two tapes and they are **different series for the same day**.
Measured for one session of one US equity: the consolidated tape reported an open of 309.58,
a close of 303.42 and 75,314,280 shares; the single-venue tape reported 309.765, 303.41 and
3,242,233. A card researched on one and traded on the other is not the same strategy.

So `feed` has no default. Set it in `[adapters.alpaca]`, matching the tape the strategy was
researched on, and the data client refuses to open without it (exit 2, naming both). The tape
is part of the engine client id, so every log line and every response says which one is being
read. Nothing in this version records the tape a stage ran on, so nothing compares one run's
declaration with the last: that check belongs to the long-running stage node that
`docs/backlog.md` tracks.

### Configuration

`[adapters.alpaca]` in `kanso.toml` is validated by the adapter's own model, which accepts
these keys and no others, and `kanso doctor` reads it through that model whether or not
the table is there — an unknown key or a value outside its range fails the `execution`
check. It holds no credential.

| key | default | what it does |
|---|---|---|
| `feed` | *none* | `sip` or `iex`; the tape the data client reads. Required before a data client opens |
| `requests_per_minute` | `190` | the rate limit every request through this adapter shares, under the published 200 |
| `timeout_s` | `30` | per-request timeout |
| `poll_interval_s` | `15` | seconds between the live feed's sweeps; `1` to `3600`, and a value outside that is refused when the table is read |
| the six URLs | the broker's own hosts | the paper, live and market-data REST hosts and their stream endpoints |

**The cadence and the quota together bound the universe a stage may trade.** The live feed
sweeps every subscribed series on each tick of `poll_interval_s`, one request per series, so
`requests_per_minute` divided by the sweeps in a minute is how many series it can carry: at
the defaults, 190 a minute at four sweeps a minute is 47. One subscription past that is
refused when the stage starts — a bound rather than a throttle, because a sweep the quota
cannot finish drops the series at the end of it and a stage that silently stopped seeing half
its universe is worse than one that refused to start. Set the cadence to the resolution the
strategy trades: a bar reaches the strategy at worst one sweep after it closes, which is a
third of a minute bar's life and nothing at all on a daily one.

**One connection is meant to carry all of it.** The rate limit belongs to the account, not to
any one caller, so the execution client, the live data client and the tradability overlay
each take a transport rather than building one, and are meant to be handed the same object:
three connections would be three times the limit. Nothing in this version builds more than
one of the three in a process — the stage node that would is the piece that is not wired —
so the sharing is a contract the components honour and not yet a thing the code enforces.
It is entry 24 in `docs/backlog.md`, to be closed with that node.

### What it will and will not do

**Order shapes it cannot honour are refused by name, before anything is sent.** A deny table
names the field that produced the refusal — a venue on the order, post-only, reduce-only, a
display quantity, a quote quantity, a contingency, a trigger type or a trailing-offset type
the wire has no field for — and the parser's own tables refuse a side, an order type or a
time in force the same way. A denied order never reaches the wire.

**A restart produces no duplicate fill.** The client order id is kanso's own, sent verbatim
and returned verbatim, and the trade id is derived from it and the cumulative filled quantity
alone, so the same fill read twice is the same trade id twice and reconciliation skips it. A
genuinely new fill reports the difference under a different id.

**The tradability overlay says what the broker will actually allow.** Whether an asset is
tradable, shortable and easy to borrow is a fact about the account on a day, so it is fetched
and held with its age, and a flag missing from a row the broker sent is not a flag with a
default: the instrument is recorded as undescribed with the field named, and every permission
that flag would have granted is withheld. The flags live beside the instrument definition and
never inside it — a definition is content-addressed, and a daily-changing borrow flag inside
one would re-key the instrument every day.

### What is not wired yet

The adapter, its declarations, its refusals and the promotion path are all live and tested. A
stage node that could actually run one of these clients is not: a node here is a bounded
replay of the catalog into kanso's own simulated venue, so `deploy` refuses a `clock: wall`
execution client (exit 2) rather than fill its orders in simulation and record them as the
broker's. `docs/backlog.md` tracks the long-running node, and the fills reach the engine by
polling rather than by an order stream, which is recorded there too.

## The OKX adapter

A crypto exchange, for perpetual swaps, in two accounts: a demo-trading one and a real one.
In this version the package holds the broker's **declarations** — the two execution clients,
the data client id, the six credential names, `[adapters.okx]` and the venue model — and the
exchange's **public data**, a data adapter with id `okx` that resolves a listed swap into an
instrument and loads its bars, trade prints and realised funding into the catalog, with no
credential at all. The execution clients arrive in a later version on top of what is
declared here; nothing the package ships can place an order.

### Credentials

Six names under the standard scheme, three per account — the exchange signs every private
request with a key, a secret and a passphrase — resolved independently at the moment of use:

| variable | what it opens |
|---|---|
| `KANSO_OKX_DEMO_API_KEY` · `KANSO_OKX_DEMO_API_SECRET` · `KANSO_OKX_DEMO_PASSPHRASE` | the demo-trading account |
| `KANSO_OKX_API_KEY` · `KANSO_OKX_API_SECRET` · `KANSO_OKX_PASSPHRASE` | the real account |

An account counts as configured only when all three of its names resolve. The package knows
these six spellings and no others. That matters more here than for any other broker,
because **the engine falls back on its own names**: measured on `nautilus_trader 1.231.0`,
its OKX HTTP client and its credentialed stream read `OKX_API_KEY`, `OKX_API_SECRET` and
`OKX_API_PASSPHRASE` from the process environment for any credential they are handed as
`None`. So the rule the client factory will follow is fixed now: resolve all three of the
client's own `KANSO_OKX_*` names, refuse if one is unset, hand every one to the engine
explicitly, and never call a `from_env` constructor. `kanso doctor` re-checks the fallback
among its engine facts, in a child process whose environment holds the engine's three
variables set to a marker and none of yours, so the check neither reads nor changes an
`OKX_*` key you export for another tool; setting those variables configures nothing.

The engine does not choose a stream's url for kanso either: its credentialed stream handed
no url connects to `wss://ws.okx.com:8443/ws/v5/public`, the global host's public stream,
whatever the account's region. The client factory will pass the private url for the
account's environment and declared region, as the engine's own execution client does, and
`kanso doctor` re-checks that default too.

### The two execution clients

| client | `capital` | `clock` | may be configured on |
|---|---|---|---|
| `okx_demo` | `broker_paper` | `wall` | `paper`, `live` |
| `okx` | `real` | `wall` | `live` only, and only behind `promote --live --as NAME` |

The live data client id is `okx`: the exchange's market data is public, so one feed serves
both accounts. Both execution clients are refused at deploy for the same reason as every
wall-clock client in this version — a stage node is a bounded replay of the catalog into
kanso's own simulated venue — and `kanso portfolio clients` and `kanso doctor` report the
refusal before an operator runs into it. They await the long-running stage node, entry 15
in `docs/backlog.md`.

### The region is declared, never defaulted

The exchange serves accounts from regional hosts, and **a key is accepted by one host
only**. Measured on 2026-09-30 with a signed `GET /api/v5/account/config`: an Australian
retail account's key was accepted by `us.okx.com` alone, and `www.okx.com`, `my.okx.com`
and `eea.okx.com` each answered "API key doesn't exist" — a refusal that reads like a
revoked key. The engine's regions and the hosts it maps them to:

| `region` | REST host |
|---|---|
| `global` | `https://www.okx.com` — also the engine's default when none is stated |
| `eea` | `https://eea.okx.com` |
| `us` | `https://us.okx.com` |

So **an Australian account states `region = "us"`**. Because the engine's default is the
global host, kanso gives `region` no default: the client will refuse to open until the
table states one. No engine region maps to `my.okx.com` or `app.okx.com`; an account served
only by one of those has no region to state (`docs/backlog.md` entry 101).

### Configuration

`[adapters.okx]` in `kanso.toml` is validated by the adapter's own model, which accepts
these keys and no others, and `kanso doctor` reads it through that model whether or not
the table is there — an unknown key or region fails the `execution` check. It holds no
credential.

| key | default | what it does |
|---|---|---|
| `region` | *none* | `global`, `eea` or `us`, in any case; the regional host that accepts the account's key |
| `rate_per_second` | `5` | the flat quota kanso's own public requests share — the reference's and the public-history loaders'; `1` to `1000` |

`rate_per_second` governs kanso's own requests only. The engine's own clients meter
themselves — its compiled client carries a global rate-limit bucket and one per endpoint —
and take no quota from their caller. Five a second is a conservative default, not a
measured ceiling. One endpoint is metered on a quota of its own whatever the table's rate: the
trade-archive listing, at one request a second after a two-second pause (below).

### The public reference

`[data] reference = "okx"` resolves the exchange's perpetual swaps, by the exchange's own
`instId` with the venue appended — `BTC-USDT-SWAP.OKX`, or the bare `BTC-USDT-SWAP` — into
the engine's `CryptoPerpetual`:

```toml
[adapters.okx]
region = "us"

[data]
reference = "okx"
```

```
$ kanso data instruments resolve BTC-USDT-SWAP.OKX ETH-USDT-SWAP.OKX --as-of 2026-10-01
```

It reads one public endpoint, `GET /api/v5/public/instruments?instType=SWAP&instId=…`, one
id per request, and **sends no credential**: no key, secret or passphrase, and no header but
a `User-Agent` — the exchange's edge refuses the Python standard library's default one with
HTTP 403 `error code: 1010`. So the adapter has no variable to be enabled by; it is enabled
by its table. A workspace with no `[adapters.okx]` makes no request of the exchange — `kanso
data adapters --check` and `kanso doctor --check-adapters` pass it by as unconfigured — and
one naming `okx` as its reference with no `region` is refused before anything is sent. The
host is the one the engine maps the region to; every regional host answered the listing on
2026-09-30, and the recordings the suite replays were made on `us.okx.com`. With the table
present and a `region` stated, `--check` makes one request, the unnarrowed swap listing, and
reports how many live linear swaps it lists. It never reports the exchange as "did not
authenticate", because nothing it sends could fail to: a host that does not answer the listing
— the edge's 403, a throttle, a gateway error, a refused connection — is the probe's failure.
`kanso data adapters --check` stops with exit 1 and that error, and `kanso doctor
--check-adapters` grades its `adapters` check `fail` with the same message; both carry a
network remedy — re-run, lower `rate_per_second`, or check the exchange's status page. A table that states no `region` — valid for the
broker, which refuses it only when a client opens — gives the reference no host, so it counts
as unconfigured: both probes pass it by without a request and go on to every other adapter.

What the listing's row becomes, measured on `BTC-USDT-SWAP` and `ETH-USDT-SWAP`:

| row field | definition field | `BTC-USDT-SWAP` on 2026-10-01 |
|---|---|---|
| `ctValCcy` | `base_currency` | `BTC` |
| second half of `uly` | `quote_currency` | `USDT` |
| `settleCcy` | `settlement_currency` | `USDT` |
| `ctVal` x `ctMult` | `multiplier` — the contract's size in the base currency | `0.01` |
| `tickSz` | `price_increment`, and `price_precision` from it | `0.1` |
| `lotSz` | `size_increment` and `lot_size` | `0.01` |
| `minSz` | `min_quantity` | `0.01` |
| `listTime` | the day it listed | 2019-11-12 |

A swap's row leaves `baseCcy` and `quoteCcy` empty, which is why the currencies are read from
`ctValCcy` and `uly`. **The definition's `maker_fee` and `taker_fee` are zero, stated as zero
by the adapter:** the listing carries no rate, and the runner charges commission once, from
the venue model below; a rate on the instrument would be charged again by the simulated venue
on every fill. Each id is refused by name (exit 3), and every refusal is reported together:

- an **inverse** contract (`ctType` `inverse`, such as `BTC-USD-SWAP`, margined and settled
  in the coin) — kanso trades linear perpetuals;
- a contract whose `state` is not `live` — suspended, or not yet open;
- an id asked for as of a day before its `listTime`, which is *listed after* that day;
- an id the exchange does not list (it answers code `51001` and no rows), or one it rejects
  as malformed (HTTP 400, code `51000` — its ids are in capitals);
- an id on another venue than `OKX`.

An answer that is not the API's own — a throttle, a gateway error, the edge's 403, a code
`51000` under HTTP 200, which the exchange was once seen to answer transiently for a valid id
— and a request that reached no answer at all stop the command (exit 1) rather than marking an
id, because nothing about the id was established.

The listing is today's. A contract the exchange has delisted is not in it and is unknown, and
a definition resolved as of an earlier day carries the terms the exchange lists today, dated
the day it was resolved as of. The entry kanso writes records `instrument_class: swap` and
`sources: {okx: BTC-USDT-SWAP}`; an `override` you add to it is applied over what the
exchange lists. A perpetual settles and is booked in USDT, so it validates on a USDT account:
`[research] currency = "USDT"`, or `[research] broker = "okx"`, whose venue `OKX` declares
one.

### The public history

Three loaders read the exchange's public history into the catalog. They send no credential —
the same client as the reference, a `User-Agent` and nothing else, on the table's host and
quota — so, like the reference, they are enabled by `[adapters.okx]` with a `region`, and a
workspace without the table makes no request. `kanso data adapters` lists their ids under
`okx`; listing them builds none.

| loader | type | reads | horizon, measured on 2026-09-30 |
|---|---|---|---|
| `okx_bars` | `bar` | `GET /api/v5/market/history-candles` | by bar size: `1m` reached back past 2021-01-01; `1s` reached 2026-03-14 and not 2026-03-01, a window that moves with the calendar |
| `okx_trades` | `trade` | the daily trade archives `GET /api/v5/public/market-data-history?module=1` lists, fetched from the exchange's file host | `BTC-USDT-SWAP`'s reach through 2022 and none is listed for 2021; the newest UTC day served is two behind today |
| `okx_funding` | `funding` | `GET /api/v5/public/funding-rate-history` | about three months: `BTC-USDT-SWAP`'s oldest settlement was 2026-06-29 08:00 UTC |

A spec names the exchange's swaps and a range of UTC days of `ts_event`; the venue is the
exchange's, so it states none, and an id on another venue is refused:

```yaml
loader: okx_bars
instruments: [BTC-USDT-SWAP]     # the exchange's instId, or BTC-USDT-SWAP.OKX
start: 2026-09-28
end: 2026-09-28
resolution: 1m                   # okx_bars only; okx_trades and okx_funding refuse one
```

```
$ kanso data instruments resolve BTC-USDT-SWAP.OKX --as-of 2026-09-30
$ kanso data load --loader okx_bars --spec bars.yaml
```

**Resolve first.** Prices and sizes are read at the precision of the `CryptoPerpetual` the
catalog holds for the id, so an id not yet resolved is refused (exit 2) before any request,
naming the `kanso data instruments resolve` that fixes it. The precisions are recorded in the
dataset's request parameters — `inst_id`, `price_precision`, `size_precision` and the `host`
it was read from — which is where `data sync` reads them. A served number the precision
cannot hold exactly is refused (exit 3), never rounded: a contract's tick is re-set from
time to time, and a price rounded onto today's tick is one the exchange never printed; state
the precision it had in the entry's `override` and resolve again. **Sizes are in
contracts** — the unit the definition's lot is stated in, and the one kanso's notional
`qty x px x multiplier` reads.

**A range is served in full or refused by name** (exit 3). A range reaching before an
endpoint's horizon is refused naming the horizon and the `start` to write — it is never
loaded as an empty market — and one reaching into a UTC day that has not ended is refused
naming the last day that has. `data sync`, which extends a series to today, stops where the
source stops, and the manifest records the span actually served. Every dataset is
`realtime`: a bar is public at its close, a print when it prints and a funding payment when it
settles, so `ts_init` equals `ts_event` and no publication rule is involved.

**Throttles are waited out; nothing else is.** An answer of HTTP 429, code `50011`, is asked
again after 2, 4, 6 and 8 seconds; a fifth stops the command (exit 1), as does any other
answer that is not the API's success, with a remedy to re-run or lower `rate_per_second`.

#### `okx_bars`

A candle is `[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]`, `ts` the millisecond
instant it **opened**. The bar is stamped at its close, `ts + size`, as both `ts_event` and
`ts_init`; its volume is `vol`, in contracts (on `BTC-USDT-SWAP`, `vol` 1439.97 beside
`volCcy` 14.3997 BTC at a contract of 0.01 BTC); and the candle still forming — the newest
row, `confirm` `"0"` — is dropped. Rows come newest first, 300 to a page whatever `limit`
asks, and `after` and `before` are both exclusive (`after=X` answers the candles that opened
before `X`); a day is walked back page by page to an empty page and yielded oldest first,
one day in memory at a time.

The sizes are the endpoint's, in the spelling whose candles open on UTC — its `6H`, `12H`,
`1D` and `1W` open on Hong Kong time, `1D` at 16:00 UTC — and any other size is refused
before a request (the endpoint itself answers code `51000`, "Parameter bar error", for `2s`,
`10s`, `4m`, `3H`, `8H`, `2W` and `1h` in lower case):

| `resolution` | `1s` `5s` `15s` `30s` | `1m` `2m` `3m` `5m` `15m` `30m` | `1h` `2h` `4h` | `6h` `12h` | `1d` | `1w` |
|---|---|---|---|---|---|---|
| asked as | the same | the same | `1H` `2H` `4H` | `6Hutc` `12Hutc` | `1Dutc` | `1Wutc` |

A day holds the bars that close in it, so a `1w` bar, which opens and closes at Monday 00:00
UTC, belongs to a Monday: a `1w` spec whose range holds no Monday yields no bar and is refused
as a series the source served no points for, although the source holds it.

The horizon is found with requests of one row: day `D` is served when a candle closing at or
before `D` 00:00 is, and when a range's first day is not, the first day that is is found by
bisection up to today — a handful of requests — and named in the refusal.

#### `okx_trades`

The REST endpoint for past prints answers 100 a request and about 80 days back, and a day of
`BTC-USDT-SWAP` was 3.56 million prints on 2026-09-28, so this loader reads the **daily archives** the
exchange publishes instead: one zip a day, listed with a URL on the exchange's file host.

- **An archive's day is the exchange's, UTC+8.** The archive named `2023-01-01` holds the
  prints from 2022-12-31 15:59:41 UTC to 2023-01-01 15:59:51 UTC, and consecutive archives
  continue each other's trade ids. So a UTC day `D` is served by two archives, `D`'s and
  `D+1`'s, and only when both are listed — a day with one of them would be a third short. A
  range with a day that is not is refused naming the days and the archives they need. The
  archive is not listed as its day ends, and when it is was not measured: the archive of
  2026-09-30, whose day ended at 16:00 UTC, was still unlisted at 16:01, 16:52 and 17:03 UTC
  that day, when the newest listed was 2026-09-29's, so the last UTC day served was
  2026-09-28.
- **The file.** One CSV, oldest print first, read by column name: the header was
  `instrument_name,trade_id,side,price,size,created_time` through 2023 and gained `source` by
  2026. `size` is in contracts — on `USDC-USDT-SWAP`, a contract of 10 USDC, trade 3031605
  reads `9.0` in the archive and `sz` 9 from the REST endpoint — `side` is the taker's, and
  `created_time` the print's millisecond instant. A print becomes a `TradeTick` with the
  exchange's trade id, the taker's side as its aggressor, and `ts_event` = `ts_init` = the
  instant it printed. Every print is kept, whatever its `source`.
- **The listing** answers at most ten days a request (HTTP 400, code `50076`, for eleven)
  and throttles hard: it answered 429 to every second request sent half a second apart, to
  three of eight sent a second apart, and to none of six sent two seconds apart. So every
  listing request is sent after a pause of two seconds, on top of its own quota of one
  request a second — the slowest the engine's quota states, and a quota that admits a burst
  as large as its rate — and a 429 it draws all the same is waited out.
- **The archives are kept** in the catalog's adapter cache, `catalog/.cache/okx/trades/`,
  because a day reads two of them and a backfill reads each twice; each is written only once
  it has arrived whole and passes the zip's own CRC. They are public and re-fetchable, so
  deleting the directory costs a download and nothing else. A `BTC-USDT-SWAP` archive was 6
  to 18 MB a day in late September 2026 and about 1 MB in January 2023.

The loader reads one archive at a time, but every path that writes a dataset — `kanso data
load`, `data backfill` and `data sync` — gathers all the points it will write before writing
any of them: `load` its whole span, `backfill` and `sync` each 30-day chunk whole. That one
day of `BTC-USDT-SWAP`, two archives of 17.6 and 16.3 MB, took `kanso data load` 82 and 87
seconds in two runs on 2026-09-30, the first at a peak of 1.8 GB resident, where the loader
alone, with no write path, streamed it in 62 seconds at 207 MB; so a backfill chunk of a
liquid swap's prints holds about thirty times that.
Load a liquid swap's trades one day to a spec (backlog entry 109).

#### `okx_funding`

A row is `{fundingRate, realizedRate, fundingTime, method, formulaType, instId, instType}`.
**`realizedRate` is the payment** — the rate the settlement at `fundingTime` actually paid —
and it is what becomes `Funding.rate`; `fundingRate`, the rate published for the period, is
never read, and a row with no finite `realizedRate` is refused rather than filled from it.
The two agreed on all 281 rows of `BTC-USDT-SWAP` served on 2026-09-30. The point is stamped
at the settlement, `ts_event` = `ts_init` = `fundingTime`; the endpoint lists settled
periods only. Pages of 400, newest first, `after` and `before` exclusive; the horizon is
measured by walking the history back to an empty page — two requests for a swap settling
every eight hours — and the first whole UTC day served is the oldest settlement's day when
it fell at midnight, the next day otherwise.

### The venue it declares

Instruments trade on the venue `OKX`, the exchange's own, and an instrument id is the
exchange's `instId` with it appended: `BTC-USDT-SWAP.OKX`. The declaration is a margin
account settled in `USDT`, `commission_bps: 5.0` on a fill that takes liquidity and
`maker_bps: 2.0` on one that rested — and nothing else. Slippage and the spread fall to
kanso's shipped defaults, so a hypothesis on bars alone still states `spread: fixed_bps`
and its width; with neither quotes nor a width the venue model is refused rather than
costed at a spread of zero. The rates are charged once, by the runner, like every venue's:
the public reference hands kanso instruments whose own maker and taker rates are zero, so the
simulated venue charges nothing on top.

The rates are the exchange's published Regular (Lv1) perpetual schedule, and were measured
on the operator's account on 2026-09-30 with `GET /api/v5/account/trade-fee?instType=SWAP`:
level `Lv1`, maker `-0.0002`, taker `-0.0005` (the exchange signs a fee the account pays
as negative). The same call for `SPOT` answered 0.70 %, the Australian retail spot
schedule, which is not declared because the package declares perpetuals. **The tier is
declared, not fetched:** a card is costed before any account is opened, and a tier is a
fact about one account on one day. An account on another tier states its own rates under
`venues.OKX.costs` in `portfolio.yaml`, and the origin is recorded as `venue_override`. The
same account read `posMode` as `net_mode`; the client will require net mode when it
connects.

## Writing your own

A **data adapter** is a package exposing a module-level `ADAPTER` with `id`, `kind`
(`data` or `reference`), `capabilities`, `credentials`, and the methods the registry calls:
`client(ws)`, `configured(ws)`, `credential_origins(ws)`, `quota(ws)`, `loaders(ws)`,
`provider(ws)` — a `kanso.data.instruments.InstrumentProvider`, or `None` — and `survey(ws)`.
There is one package per outside party: a pure data vendor's lives under `data/adapters/`,
and a broker whose public history or reference data kanso reads keeps those in its own
package under `nautilus/adapters/`, exposed as the same `ADAPTER` beside its `BROKER`. The
data registry finds both — the vendor packages first, then the broker packages, a vendor's
id winning a clash — so `kanso data adapters`, the loaders and the instrument providers
reach a broker-side adapter exactly as they reach a vendor's. A workspace extension declares
its ids in `PROVIDES["adapters"]` and exposes them in an `ADAPTERS` mapping, exactly as it
declares loaders; an id that ships from either directory wins over it.

A **broker adapter** is a package under `nautilus/adapters/` exposing a module-level `BROKER`
with `id`, `kind`, `exec_clients` (each an `ExecutionClientSpec` declaring `capital` and
`clock`), `data_clients`, and the methods the registry calls: `credentials(client_id)`,
`credential_origins(ws, client_id)`, `configured(ws, client_id)`,
`venue_declaration(venue)` and `config(ws)` — its `[adapters.<id>]` table read through its own
model, which `kanso doctor` calls — plus `engine_facts`, the claims about the engine's own
adapter its package rests on, each a `(claim, check)` pair that `kanso doctor` re-checks
among the engine facts. Those declarations are the whole of what the core is allowed
to know about a broker, and they are what the refusals are decided from — before anything
connects, which is the point of their being declarations. A workspace extension declares its
clients in an `EXEC_CLIENTS` table instead, exactly as it declares gates, and names the ids
in `PROVIDES["exec_clients"]` so that shadowing one that ships is reported — a packaged id
wins, so an extension that claimed one would be registered nowhere.

Two things are worth copying rather than reinventing.

`loaders(ws)` returns **factories**, not instances. Listing what an adapter can fetch must
not build a loader, because building one resolves a credential and `kanso data adapters`
has to answer in a workspace that has none.

`survey(ws)` returns measured reach, not declared reach. If your vendor states entitlement
and history in a document, the document is still not what your key holds today.

Everything that touches a credential takes the workspace as an argument and resolves at the
moment of use. Two workspaces on one host may hold two different accounts, and an adapter
that cached one would trade the other's money.
