---
name: kanso-replay
description: Replay catalog data through the Nautilus engine for any kanso strategy version or hypothesis `strategy_sha` over any date range — live code path (node) or research code path (engine) — and run parity checks between them. Use when the operator asks what a strategy would have done over a period, to replay, simulate, dry-run, or check test/live parity, or to inspect a replay session.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-replay

## Steps
1. Pick the target: `--strategy <id>[@<version>]` (deployed/composed impl) or `--hyp <id> [--sha <s>]` (defaults to `best`; `<s>` is any unique prefix of a card's `strategy_sha`). Pick the range: `--from <date> --to <date>`; omit for `forward.start` → last data in the catalog. `kanso data show` shows what ranges exist.
2. Replay: `kanso replay run --strategy <id>@<version> --from <date> --to <date> --speed 0 --json`. Default mode `node` = the live code path (TradingNode + replay data client + sandbox fills); `--mode engine` = the research code path (BacktestNode) on the same data. The feed is the target's universe.
3. Read the session: `kanso replay show <session>` → fills, PnL, order intents, and the session parameters; `kanso replay show` without an argument lists sessions.
4. Parity: `kanso replay parity --hyp <id> --from <d> --to <d>` runs node then engine on identical data and prints the first divergence in order intents, or `identical`. This is the same check the `parity_replay` certification gate runs over the certification window.

## Rules
- Replay is evaluation only: it never creates cards, never changes `best`, never certifies. Use it to answer questions, not to search parameters — that is the research loop's job.
- Any window may be replayed, including certification and forward; say which window you used when reporting numbers.
- `--speed 1` replays at wall-clock pace (useful to watch a node behave); `--speed 0` is the default for questions.
- Sessions persist under `sessions/`; large ranges produce large sessions — prefer the narrowest range that answers the question.
