---
name: kanso-classify
description: Classify a registered kanso hypothesis as a quant construct (sleeve, alpha, filter, overlay, exit, execution, allocation), attach it to a host where the construct needs one, and name its objective, the keep rule's parameters and its card-stage constraints. Use after `kanso hyp add`, when the operator asks what kind of thing an idea is or how it fits the book, or to re-classify a hypothesis.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-classify

## Steps
1. `kanso classify <id> --json`. kanso computes deterministic features (universe overlap, horizon and resolution match, mechanism, which constructs can attach given the certified strategies), the objectives each objective mode admits for the hypothesis, and makes one `classify` call that returns the construct (with `host` and `params` where the construct needs them), the objective — one of the admitted ids, named by the model, never picked by kanso — with its keep rule (`min_delta`, `k_se`), and the card-stage constraints, each inside the catalogue's ranges. A contribution objective carries `edge_per_trade` with `min_bps` and `k_se` both set, from the constraints or the operator's `required_constraints`. kanso validates the result and writes `construct`, `objective` and `constraints` into `hypothesis.yaml`, pins it, and renders `strategy.py` from the construct's stub if the file is untouched. Status becomes `classified`.
2. Report in ≤3 lines: construct (and host), the rationale, `objective.id`, and the constraint ids. If the file already named an objective, say what became of it: `stated_objective.kept` true (the operator's stands), or false with its `reason` — tell the operator plainly that their objective was replaced and why.
3. A construct that classification recognises but 0.1.0 cannot run (`alpha`, `execution`, `allocation`, portfolio-level `overlay`) is still recorded; `kanso research begin` then exits 2 naming the seam. Tell the operator plainly; do not reshape the thesis into a runnable construct.
4. If the operator disagrees: edit `construct`, `objective`, or `constraints` in `hypothesis.yaml`, then `kanso hyp validate <path>` (exit 3 lists unknown ids, non-applicable objectives, out-of-range params, a contribution without `edge_per_trade`, a gate given half its parameters) and `kanso hyp add <path>` to re-pin. A re-pin or a re-classification that changes `construct.id` or `objective.id` clears the hypothesis's `best` (event `best_cleared`), because the cards were earned as the other construct or in other units; the cards and blobs stay in state. Re-running `kanso classify` after a manual override keeps the operator's objective when the construct admits it; to have the model choose the objective again, clear the `objective` key first.
5. The operator states a return target (bp a day, % a month): that is a contribution. Say so if the answer named the per-trade edge — it rewards trading less — and offer to write `objective: {id: wf_contribution_bps, params: …}` (the marginal form on an attached construct) with `edge_per_trade` under `required_constraints`, then classify again.

## Rules
- Classification precedes research: the harness and the objective's mode (absolute for a sleeve, relative to the pinned host for attached constructs) follow from the construct, and the objective is named within that mode; a `benchmark: {hold: first_leg}` the operator declared turns a daily sleeve's absolute Sharpe into `wf_sharpe_vs_hold`, a benchmark on a hypothesis held under a day exits 3 at `hyp validate` and at `kanso classify` before any model call, and one on a construct measured relative to its host (every attached construct but `alpha`) exits 3 at `kanso classify` once the construct is chosen — never drop the key to make classification pass, tell the operator.
- The catalogue is a library: `kanso ext show` lists constructs added by extensions. Certification, paper and live gates are not chosen here (skill `kanso-certify`).
- Exit 2 without a configured model: there is no non-LLM fallback by design (`kanso models check`).
- Exit 3 with "no applicable objective" is a framework bug (the objective set must be total) — report it, do not hand-pick.
