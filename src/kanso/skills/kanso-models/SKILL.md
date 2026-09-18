---
name: kanso-models
description: Configure the kanso model register and routing table (models.yaml) — add or change LLM models, tiers, costs, effort levels, and provider protocols — and verify they respond. Use when the operator mentions models, providers, API keys, token spend, routing, cheap/expensive models, or local models.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-models

## Steps
1. Edit `models.yaml`. One entry per model: `id`, `provider`, `protocol` (`anthropic`, `openai_compat`, or the shipped `mock` for tests/demos), optional `base_url` (any hosted or local server speaking the OpenAI-compatible protocol), `tier` (`cheap | mid | frontier`), `local`, `ctx`, `cost_in`/`cost_out` per million tokens, `tools`. Keys never appear here: each provider's key is the variable `KANSO_<PROVIDER>_API_KEY` (optional `api_key_env` overrides that name).
2. Routing table `routing.<task_class>: {tier, effort, max_output}` for `classify`, `propose`, `align_check`, `certify_plan`, `explore`. Effort ∈ `none | low | medium | high`; an omitted class or field keeps its shipped default. The defaults spend where a wrong answer is dearest: full thinking on `classify` and `certify_plan`, which run once per hypothesis; a mid tier on `propose`, which runs on every card and so dominates spend; no thinking on `align_check`, which is a yes/no reached only after the deterministic checks passed; full thinking and a large cap on `explore`, which writes a whole hypothesis directory and runs only when asked or after a configured spell of stalls. Keep them unless the operator has a reason: a higher tier on `propose` raises cost per card without changing the keep rule, and raising `align_check` buys nothing.
3. `kanso models check` → one minimal call per model; report latency and failures. Missing key: kanso reads `KANSO_<PROVIDER>_API_KEY` from the workspace `.env` first, then from the environment, so the operator either adds `KANSO_<PROVIDER>_API_KEY=…` to `.env` (gitignored) or exports it in their shell profile. `kanso doctor` reports each required name and where it resolved, never the value. Never print or ask for a key in chat.
4. `kanso status` shows spend by lane/day from the ledger. There are no spend caps by design; if the operator wants less spend, lower the tier or effort of `propose` first, since it is the only class called on every card — never propose pausing research.

## Rules
- Never put API keys in `models.yaml` or in chat.
- A model you serve yourself — a `local` entry whose `base_url` is a process you wrote around a vendor's CLI — must give up before kanso does. One call connects within fifteen seconds and then waits seven minutes for the answer, and whichever side gives up first decides what the operator reads: the shim's own status arrives as `the provider answered 504`, kanso's as `the request did not complete (ReadTimeout)`, which names nothing the shim was doing. The two shims measured in a live workspace kill at 390s and 240s.
- Every tier must have at least one model, or `kanso research begin` exits 2; `tier` may list several tiers (the demo's mock lists all three).
- Model ids change; use the provider's current id and record `cost_in`/`cost_out` from its pricing page.
