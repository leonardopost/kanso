---
name: kanso-promote
description: Handle kanso escalations and strategy promotion — read the inbox, deploy paper, record the operator's approval for live capital, demote, and retire. Use when the operator asks what needs attention, wants to go live or paper, sees an inbox entry (`promotable`, `demoted`, `cert_failed`, `misaligned`, `deploy_blocked`), or asks about portfolio state.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-promote

## Session start
`kanso inbox` then `kanso status`. Summarise unread entries in one line each. Acknowledge only entries the operator has dealt with: `kanso inbox ack <id>`. Acknowledging is never an approval.

## Promotion path
- `certified` hypothesis → kanso auto-runs `kanso strat compose` and `kanso portfolio deploy --stage paper`. Nothing to approve. `deploy_blocked` means no capital was assignable within `portfolio.yaml` limits: raise stage `capital` or retire something, then `kanso portfolio deploy --stage paper`.
- `paper → promotable` happens when the plan's paper-stage gates pass (monitor). Inbox kind `promotable`.
- `promotable → live` **requires the operator**: only after they explicitly say to go live in this conversation, run `kanso promote <strategy> --live --as "<operator's name>"`. Exit 4 = `--as` missing. Never pass `--as` on your own initiative; there is no environment fallback by design.
- Demotion is automatic on live-gate failure (inbox kind `demoted`); manual: `kanso demote <strategy>`. Retire: `kanso strat retire <strategy>` or `kanso hyp retire <id>`.

## Portfolio changes
Stage capital, per-strategy capital, limits, the venue overrides and each stage's kill switch live in `portfolio.yaml`. Account type, currency and costs come from the broker behind the stage's execution client, so they are usually not set at all; `venues.<MIC>` overrides one venue and a hypothesis's `costs` overrides that hypothesis. Changing them changes what future backtests and certificates mean, never what an existing certificate meant, since each records the venue model it was produced under. Edit, then `kanso portfolio deploy --stage <paper|live>` (it refuses over-limit configs and refuses a stage whose kill switch is on). Setting `stages.<s>.kill_switch: true` makes the stage's node cancel, flatten and halt immediately; `daily_loss_kill` sets it automatically; only the operator resets it (`false`, then redeploy). Before a live decision, offer `kanso replay run --strategy <id>@<v>` over the paper period (skill `kanso-replay`) if the operator wants to see behaviour.

## Rules
- Do not promote to live to "test something"; paper and replay exist for that.
- Report money-related state as numbers from `kanso portfolio show`, never from memory.
