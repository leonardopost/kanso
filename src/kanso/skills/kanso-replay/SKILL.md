---
name: kanso-replay
description: Replay catalog data through the Nautilus engine for any kanso strategy version or hypothesis `strategy_sha` over any date range — live code path (node) or research code path (engine) — and run parity checks between them. Use when the operator asks what a strategy would have done over a period, to replay, simulate, dry-run, or check test/live parity, or to inspect a replay session.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-replay

## Steps
1. Pick the target: `--strategy <id>[@<version>]` (deployed/composed impl) or `--hyp <id> [--sha <s>]` (defaults to `best`; `<s>` is any unique prefix of a card's `strategy_sha`). Pick the range: `--from <date> --to <date>`; omit for `forward.start` → last data in the catalog. `kanso data show` shows what ranges exist. The default range starts at `forward.start`, so a bare replay is a counted look at the forward window (Rules).
2. Replay: `kanso replay run --strategy <id>@<version> --from <date> --to <date> --speed 0 --json`. Default mode `node` = the live code path (TradingNode + replay data client + sandbox fills); `--mode engine` = the research code path (BacktestNode) on the same data. The feed is the target's universe.
3. Read the session: `kanso replay show <session>` → what ran, over which range, at what speed and against which execution client, plus how many points were released, the sha256 of their stream and how many order intents came back. The intents themselves are `sessions/<session>/intents.jsonl`; the stream is not written — replay the range again to see its points, and the digest says whether it matched. `kanso replay show` without an argument lists sessions.
4. Parity: `kanso replay parity --hyp <id> --from <d> --to <d>` runs node then engine on identical data and prints the first divergence — in what the two paths were released (count, then digest), then in order intents — or `identical`. This is the same check the `parity_replay` certification gate runs over the certification window.

## Rules
- Replay is evaluation only: it never creates cards, never changes `best`, never certifies. Use it to answer questions, not to search parameters — that is the research loop's job.
- A replay that reads any day on or after its target's `forward.start` is a counted look at its lineage's forward window — `forward_look` under `--json`, the `forward` line otherwise, which says which look it was — and the look can never be taken back. A replay of a certification window reads the hold-out, and nothing counts it. So never replay either window to choose or tune anything research does; say which window you used when reporting numbers, and report the look beside any number taken from forward days.
- The one exemption is `kanso replay parity --strategy <id>@<version>` over days the paper or live stage already showed that version: the days of its paper and live sessions in `kanso replay show`, less the first day of a window that resumed past the stage's clock when the version joined there, and less any window that halted. The `forward` line says `no look` when it was. A `replay run` over the same days, a `--hyp` card, or a range one day past them is a look.
- A target whose hypothesis declares a `warmup` is fed the sessions before the range on both paths, with every order dropped until the range opens; `released` and the stream's digest are the range's own points. Report the range, not the prefix.
- `--speed 1` replays at wall-clock pace (useful to watch a node behave); `--speed 0` is the default for questions.
- Sessions persist under `sessions/` and nothing prunes them; a session's size is its intents, so a strategy that trades often over a long range writes a large one — prefer the narrowest range that answers the question.
