# kanso

[![ci](https://github.com/leonardopost/kanso/actions/workflows/ci.yml/badge.svg)](https://github.com/leonardopost/kanso/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/kanso)](https://pypi.org/project/kanso/)
[![python](https://img.shields.io/pypi/pyversions/kanso)](https://pypi.org/project/kanso/)
[![license](https://img.shields.io/github/license/leonardopost/kanso)](LICENSE)

A minimalist, agent-first quant research workbench. It turns a trading thesis into a Karpathy-style
autoresearch loop that runs indefinitely, with guards against overfitting built in. It supports
multiple asset classes, including stocks, futures and crypto.

The problem it solves: Quant research can be specified a hundred ways, and choosing between them by hand is
slow, inconsistent and easy to overfit. kanso makes the choice deterministic. An agent proposes
each specification, a reproducible backtest scores it net of costs, and only one that beats the
best so far is kept. The winner is then tested on data the search never saw.

It’s basically having a quant research team working 24/7 to test and validate your trading theses.

The philosophy: Kanso (簡素) is a principle of Zen aesthetics: the maximum effect from the minimum
means. kanso aims to reduce all the complexity of running trustworthy quant research to a single
prompt.

## Concepts

| term | what it means |
|---|---|
| workspace | a plain directory: `kanso.toml`, a few YAML files you own, and the SQLite store kanso owns |
| hypothesis | one falsifiable thesis plus what testing it needs — universe, holding period, data, risk limits — and three windows: research, certification and forward |
| construct | what a hypothesis is: a sleeve (a whole strategy), a filter, an overlay, an exit rule, an alpha, an execution tactic or an allocation |
| objective | the one number a run optimises; every constraint is a gate, never part of it |
| screen | a measurement, made before any research and with no model call, of whether the relationship an idea rests on exists and clears its costs |
| run, lane | a run is one research session on one hypothesis; a lane is a concurrent worker with its own directory |
| card | one experiment: the lane's `strategy.py`, backtested and recorded as kept, discarded or crashed |
| keep rule | when a card counts as an improvement: it must beat the best by more than its own noise |
| alignment | a periodic check that research still tests the thesis rather than something easier |
| snapshot | an immutable, content-addressed set of datasets a run is pinned to |
| embargo | the gap between the research and certification windows; research can never read the certification window |
| certification | the gates a model planned for the hypothesis, judged on the certification window; a pass is a certificate |
| strategy version | the closed record a certificate composes: the code by hash, a verbatim copy of it, the kanso and engine versions it was certified under, and what it is expected to earn |
| stage | `paper` or `live`: a capital allocation, limits, an execution client, a data client and a kill switch |
| promotion, demotion | moving a version between stages; paper to live is the one step that needs a named person |
| escalation | one of six things kanso puts in your inbox: `misaligned`, `cert_failed`, `promotable`, `demoted`, `deploy_blocked`, `explored` |
| model register | `models.yaml`: the LLMs kanso may call, their cost tiers, and which tier serves each task |
| adapter | one outside party — a data vendor or a broker — and everything kanso reaches it through; enabled by its credentials |

## Use cases

- **Research a thesis with no one in the loop.** `kanso classify` decides what the idea is — a
  sleeve, a filter, an overlay, an exit rule, an alpha, an execution tactic or an allocation —
  and how it will be judged. `kanso research` then has a model edit one file, `strategy.py`, one
  backtested experiment at a time, and keeps a change only when it beats the best so far.
- **Screen an idea before it gets a lane.** `kanso screen run` measures whether the relationship
  an idea rests on exists and clears the round-trip cost, without a model call.
- **Certify on data research never saw.** `kanso cert plan` has a model choose gates from a
  declared library — deflated Sharpe and edge, walk-forward, bootstrap, cost and latency stress,
  capacity, replay parity and more — and `kanso cert run` judges them on an embargoed window.
  Every search is charged for the trials it ran.
- **Run many lanes unattended.** `kanso research start` runs a daemon that serves a priority
  queue of hypotheses across the lanes the host can hold, and certifies a lane when it stalls.
- **Compose, deploy and watch.** A passing certificate becomes a versioned strategy on the paper
  stage, within `portfolio.yaml`'s limits; paper promotion and demotion are automatic. Real
  capital moves only on `kanso promote VERSION --live --as NAME`.
- **Keep a point-in-time catalog.** Load, backfill and sync from Massive (US stocks, options,
  futures, forex, indices), OKX's public swap history (bars, trades, funding) or file exports.
  Every point is stamped with when it became public, and a snapshot pins what each run measured.
- **Extend it from your workspace.** Loaders, data types, constructs, execution clients and
  adapters written in `kanso_ext/` use the same interfaces as the built-ins.

The reference is `docs/`: start with `docs/concepts.md`; `docs/cli.md` lists every command.

## Requirements

- macOS 26+ on Apple silicon, or Linux x86_64. The pinned NautilusTrader wheel decides the hosts.
- Python ≥3.12 (CI runs 3.12 and 3.13) and [uv](https://docs.astral.sh/uv/).
- A model for every decision an agent makes: at least one provider in `models.yaml`, its key in
  `.env` as `KANSO_<PROVIDER>_API_KEY`.
- Vendor and broker credentials only for the adapters you use.

The demo and the test suite need no model key and no credential.

## Install

```bash
uv tool install kanso          # the CLI
uv add kanso                   # or, inside a project
```

Check it with the demo workspace, which runs on a scripted model and synthetic data with no
key and no network:

```bash
kanso init demo --demo
cd demo
kanso doctor
kanso data load --loader synthetic --spec demo.yaml
kanso data instruments resolve DEMO.SIM --as-of 2024-01-02
kanso data snapshot
kanso hyp add hypotheses/demo_mr/hypothesis.yaml
kanso classify demo_mr
kanso research run demo_mr --cards 3
kanso status
```

For your own: `kanso init .`, add your models to `models.yaml` and their keys to `.env`, then
`kanso doctor`. `docs/workspace.md` says what every file in a workspace is for.

## Contributing

Read `AGENTS.md` first: the invariants the package is built on, and the definition of done.
`docs/maintainers.md` covers the development loop, upstreaming an extension and releases. In
short: one branch per change, conventional commits, `docs/` and the tests changed with any
behaviour they describe, and CI green on both hosts and both Python versions before a merge.

## Running tests

```bash
uv sync
uv run pytest --cov-fail-under=85    # the gate CI runs
uv run ruff format --check
uv run ruff check
uv run mypy src
```

License: Apache-2.0.
