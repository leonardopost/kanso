---
name: kanso-classify
description: Classify a registered kanso hypothesis as a quant construct (sleeve, alpha, filter, overlay, exit, execution, allocation), attach it to a host where the construct needs one, and set its objective parameters and card-stage constraints. Use after `kanso hyp add`, when the operator asks what kind of thing an idea is or how it fits the book, or to re-classify a hypothesis.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-classify

## Steps
1. `kanso classify <id> --json`. kanso computes deterministic features (universe overlap, horizon and resolution match, mechanism, which constructs can attach given the certified strategies), evaluates which objectives apply, and makes one `classify` call that returns the construct (with `host` and `params` where the construct needs them), the objective parameters (`min_delta`, `k_se`) and the card-stage constraints, each inside the catalogue's ranges. kanso validates the result and writes `construct`, `objective` and `constraints` into `hypothesis.yaml`, pins it, and renders `strategy.py` from the construct's stub if the file is untouched. Status becomes `classified`.
2. Report in ≤3 lines: construct (and host), the rationale, `objective.id`, and the constraint ids.
3. A construct that classification recognises but 0.1.0 cannot run (`alpha`, `execution`, `allocation`, portfolio-level `overlay`) is still recorded; `kanso research begin` then exits 2 naming the seam. Tell the operator plainly; do not reshape the thesis into a runnable construct.
4. If the operator disagrees: edit `construct`, `objective`, or `constraints` in `hypothesis.yaml`, then `kanso hyp validate <path>` (exit 3 lists unknown ids, non-applicable objectives, out-of-range params) and `kanso hyp add <path>` to re-pin. Do not re-run `kanso classify` after a manual override unless asked.

## Rules
- Classification precedes research: the harness and the objective (absolute for a sleeve, relative to the pinned host for attached constructs) follow from the construct.
- The catalogue is a library: `kanso ext show` lists constructs added by extensions. Certification, paper and live gates are not chosen here (skill `kanso-certify`).
- Exit 2 without a configured model: there is no non-LLM fallback by design (`kanso models check`).
- Exit 3 with "no applicable objective" is a framework bug (the objective set must be total) — report it, do not hand-pick.
