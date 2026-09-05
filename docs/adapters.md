# Adapters

An **adapter** is one vendor's whole presence in kanso: the datasets it offers, the
credential names it needs, the loaders that fetch through it and the provider that resolves
its instruments. It is the only way the rest of kanso reaches a vendor, and it is the same
interface for an adapter this package ships and one you write in `kanso_ext/`.

Two rules hold for every adapter and are worth stating before any particular one.

**The core knows no vendor.** No module outside an adapter's own package names it, no
framework behaviour requires one, and the whole test suite, `kanso doctor` and the demo are
green with every vendor credential unset. Adapters are discovered from the adapter
directory rather than listed anywhere, so nothing in kanso has to be edited when one lands.

**An adapter is enabled by its credentials, never by installation.** There are no extras to
install and no switch to flip. A registered adapter with nothing set is the ordinary state
of a fresh workspace: `kanso data adapters` lists it, says which variables it would need
and where each resolves from, and reaches nothing to say so.

## What a command tells you

| command | what it answers |
|---|---|
| `kanso data adapters` | what is registered: id, kind, capabilities, quota, loader ids, and per credential the name and where it resolves from — never a value. No network I/O |
| `kanso data adapters --check` | what your key *actually reaches*: one authenticated lookup first, then one entitlement probe per dataset and one history-floor measurement per entitled price series. It reports the number of requests it made, and exits 2 if a configured key does not authenticate |
| `kanso doctor` | the same registration facts, graded. Green whether or not an adapter is configured |
| `kanso doctor --check-adapters` | the same probe, graded. A dataset your plan excludes is reported and never graded down — it is a subscription, not a fault in the workspace; a credential that does not authenticate is the one failure |

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
the measured floor and says that it did.

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
one, and `data sync` extends a dataset over whichever transport first wrote it.

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
  `kinds: [dividend]`.
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

## Writing your own

An adapter is a package exposing a module-level `ADAPTER` with `id`, `kind`, `capabilities`,
`credentials`, and the methods the registry calls: `client(ws)`, `configured(ws)`,
`credential_origins(ws)`, `quota(ws)`, `loaders(ws)`, `provider(ws)` and `survey(ws)`. A
workspace extension declares its ids in `PROVIDES["adapters"]` and exposes them in an
`ADAPTERS` mapping, exactly as it declares loaders.

Two things are worth copying rather than reinventing.

`loaders(ws)` returns **factories**, not instances. Listing what an adapter can fetch must
not build a loader, because building one resolves a credential and `kanso data adapters`
has to answer in a workspace that has none.

`survey(ws)` returns measured reach, not declared reach. If your vendor states entitlement
and history in a document, the document is still not what your key holds today.
