---
name: kanso-data
description: Load market and reference data into the kanso catalog (Nautilus ParquetDataCatalog) from CSV/Parquet files or the synthetic generator, register instruments, list datasets, and freeze snapshots. Use when the operator mentions data, bars, quotes, trades, instruments, tickers, snapshots, or a research run fails for missing data.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-data

## Instruments first
A hypothesis's `universe` is a list of plain ids. kanso turns each into a Nautilus instrument, which needs far more than a ticker (currency, precisions, venue, and for derivatives expiry, strike and multiplier), so ids are **resolved**, not typed.

- With a reference adapter configured (`kanso data adapters` lists what is available and whether its credentials resolve): `kanso data instruments resolve <ID…> --as-of <research start>` writes the definitions into the catalog and caches them in `instruments.yaml`. `kanso hyp validate` resolves anything missing and exits 3 naming an id that is unknown, ambiguous across venues, delisted before or listed after that date.
- Without one (file loaders, the synthetic loader, the demo): add the entry by hand with `manual: true` and the full field set for the asset class.
- Edit `instruments.yaml` only for `override` (a field to correct after resolution), `attributes` (free-form facts strategies and gates may read), `corporate_actions` and `manual`. Everything else in the file is a cache and is rewritten on the next resolve.
- `kanso doctor` constructs every instrument, lists overrides and manual entries, and flags drift between what is resolved now and what the newest snapshot pinned.

## Load
1. Write a loader spec (example for files). One time zone for the whole spec, one entry per file, and a column map per entry — neither is ever inferred:
   ```yaml
   loader: csv_parquet
   timezone: America/New_York   # IANA name of the files' naive timestamps
   files:
     - path: data/<file>.parquet
       instrument: <SYMBOL>     # with venue below, this is the id in instruments.yaml
       venue: <MIC>
       type: bar                # bar | quote | trade | corporate_action | <registered custom type id>
       resolution: 1m           # bars only
       columns: {ts_event: timestamp, open: o, high: h, low: l, close: c, volume: v}
       adjusted: false
   ```
   Synthetic data for tests/demos: `loader: synthetic`, `model: ou|gbm`, `seed`, `start`, `end`, `resolution`, `instruments`, `venue`.
2. `kanso data load --loader csv_parquet --spec <file>` → writes the dataset to the catalog and its manifest under `catalog/manifests/`. A load overlapping data already held is refused (exit 2); `--replace` deletes and rewrites the overlapped span, and is refused outright where a snapshot pins it.
3. `kanso data backfill --loader <id> --spec <file>` → fills history back to the source's earliest servable date and closes any gaps. Run `--dry-run` first and report the chunk count and estimated bytes to the operator before a large pull. It is resumable and idempotent, so an interrupt is safe and a re-run costs nothing; never restart one by hand from the beginning. Reaching the source's history floor ends it normally, and the reported floor is the answer to "why does my data start there".
4. `kanso data sync` → extends each series from the end of its **newest** dataset to now, as a successor. This is the routine top-up before a research or deploy session. Only the newest dataset of a series is continued, because a request beginning after an interior one ends runs over the dataset behind it; `--dataset <id>` names one directly, which is how the dataset in front of a gap is extended on purpose.
5. `kanso data show` → datasets, served spans, gaps and row counts per instrument/type. Quote the **served** span, never the range that was requested: a source may return less than asked with no warning.
6. `kanso data instruments resolve [--as-of <research start>]` → resolves the universe into the catalog's instrument store, the registry of record a run reads its definitions from. Only this command writes it: `hyp validate`, `hyp add` and the cards build definitions in memory. An edited `override` is a correction; resolved as of a date the store already holds, the plain command refuses (exit 2) and `--refresh` replaces the held definition (refused while a run is active or a deployed version pins it).
7. `kanso data snapshot` → freezes all current datasets and the instrument checksum into `catalog/snapshots/<snapshot_id>.yaml`; refused (exit 2) while the store holds no definition and the datasets name instruments. `kanso research begin` pins the newest snapshot that covers the hypothesis's universe × data requirements over its research and certification windows **and** whose instrument checksum is the store's own, refusing by name when the definitions moved since; load, resolve and snapshot **before** beginning a run, and snapshot again after any resolve that changed the store.

## Rules
- Data sources are adapters. `kanso data adapters --json` lists the ids, their kinds and whether their credentials resolve; use an id from that list, never a name you assume exists. A source with no adapter is a file export loaded with `csv_parquet`, or a new adapter (docs/extensions.md).
- Credentials are environment variables, `KANSO_<ID>_API_KEY` and friends, read from the workspace `.env` and then the environment. Never ask for, print or write a key.
- An adapter reports capabilities and entitlement: a dataset the operator's plan does not cover is a normal, non-fatal result. Report it as "not entitled", do not retry it, and do not present it as a kanso failure.
- After loading, `kanso data sync` extends each held series to today without touching anything a pinned snapshot references.
- Datasets referenced by a pinned snapshot are immutable; the CLI refuses overlapping writes (exit 2). Load a new range or a new dataset, then snapshot again.
- Paper/live replay needs data from `forward.start` onward; load it (and snapshot) before `kanso portfolio deploy`.
- Instruments with corporate actions (splits, reverse splits, dividends, rolls): choose `corporate_actions: adjust_all` unless the thesis needs raw prices, load the corporate-action dataset too, and say which you chose. Prefer unadjusted prices plus corporate actions: a vendor-adjusted series is adjusted as of the day you asked for it, so it is not reproducible and certification refuses it.
- Data that is published later than the period it describes (fundamentals, short interest) carries its publication instant, and kanso refuses to load it without one. That is what stops a backtest from seeing a filing before it existed; never "fix" such a refusal by supplying a date yourself.
