# kanso

[![ci](https://github.com/leonardopost/kanso/actions/workflows/ci.yml/badge.svg)](https://github.com/leonardopost/kanso/actions/workflows/ci.yml)

A minimal, agent-first quantitative research workbench on
[NautilusTrader](https://nautilustrader.io): hypotheses → autoresearch → certification →
strategies → portfolio, in one directory, behind one command.

You state a thesis in a sentence. kanso decides what kind of thing it is, researches it in a
loop that never stops to ask you anything, proves or disproves it on data the research was
never allowed to see, composes what survives into a versioned strategy, deploys that to
paper and watches it. The one act it will not perform without you is moving real money.

## What it is

A **workspace** is a plain directory — `kanso.toml`, a few YAML files you own, and one
SQLite database kanso owns. It may sit inside a git repository or nowhere at all. Every
command acts on the nearest one, takes `--json`, and exits `0`, `1` (an unexpected fault),
`2` (a precondition forbids it), `3` (your input is wrong) or `4` (a named approval is
missing).

A **hypothesis** is a thesis plus the three windows it will be judged over. kanso classifies
it into a **construct** — a sleeve, a filter, an overlay, an exit rule, an alpha, an
execution tactic, an allocation — which decides what shape the code takes and whether it
attaches to something already deployed. Research then edits exactly one file,
`strategy.py`, one experiment at a time; each experiment is a **card**, and a card is kept
only if it beats the current best by the hypothesis's own keep rule.

When a run stalls, **certification** runs the gates an agent planned for that hypothesis,
over a window nothing was allowed to backtest. A passing certificate composes a **strategy
version** and offers it to the paper stage on its own. Paper to live is the one step that
needs you.

| moment | who | what |
|---|---|---|
| setup | operator | provider keys in `.env`, the data adapters to use (or file exports), portfolio capital and limits |
| a new idea | operator → agent | you state the thesis; the agent writes `hypothesis.yaml`; `kanso classify` decides the construct, the objective's parameters and the card-stage constraints |
| research | kanso | one run per lane, indefinitely; stall → certification → paper, all automatic |
| `misaligned` | operator | research drifted from the thesis. kanso already rewound the lane and carried on — decide whether the drift deserves its own hypothesis |
| `cert_failed` ×3 | operator | the idea keeps failing certification: retire it or rewrite the thesis |
| `deploy_blocked` | operator | no capital assignable within your limits; adjust `portfolio.yaml` |
| `promotable` | operator | the paper period passed. `kanso promote <strategy> --live --as <you>` |
| `demoted` | operator | live surveillance pulled a version back to paper; read the reason |
| any time | operator | `kanso status`, `kanso inbox`, `kanso replay run`, `kanso research stop` / `start` |

Everything not in that table runs without you.

## What kanso refuses to do

The refusals are the design. Each one is a thing that would have been easy to allow, and
each is enforced in code rather than asked for in a prompt.

**It never invokes git.** Not `init`, not commit, not branch, not tag — no `git` process at
all, in your workspace or anywhere else. A framework that commits on your behalf is a
framework that can rewrite your history while you sleep; kanso writes plain files and one
database, and versions the file research mutates content-addressed in that database. Two
tests spy on every process launch to keep it true.

**It never moves real money without a named person.** `kanso promote <version> --live --as
NAME` is the only path to real capital, `--as` has no default and no environment fallback,
and the approval is recorded against that exact version before anything moves. Editing
`portfolio.yaml` by hand therefore cannot promote anything: the file says what is deployed,
the record says what was allowed.

```
$ kanso promote demo_mr --live
error: promote: moving demo_mr onto the live stage is a named operator act
remedy: kanso promote demo_mr --live --as NAME
$ echo $?
4
```

**It never lets research see the certification window.** The embargo is a property of the
backtest runner, not an instruction to a model. A card runs in a child process with no path
to any catalog at all: the parent reads the window from the catalog and hands the points
across, and the child re-checks what it was given against the window it was asked for. A
prompt that says "do not look" is a prompt that will eventually be ignored, summarised away
or edited by the loop it constrains.

**It never invents a judgement.** There is no default certification plan, no default
threshold and no non-LLM fallback for a decision an agent is supposed to make. With no
model configured, `kanso cert plan` exits 2 rather than certifying against numbers nobody
chose. The framework's own opinions are structural invariants and nothing else: agents
decide, the framework evaluates.

```
$ kanso cert plan demo_mr
error: models: no model serves the cheap, mid, frontier tier; every tier needs one
remedy: add a model for that tier to models.yaml, or list an existing one on it
$ echo $?
2
```

**It never dates a point by when it arrived.** Every catalog point carries `ts_event`, its
economic reference time, and `ts_init`, the instant its information became public;
`ts_init >= ts_event` always, and the engine orders by `ts_init`. A delayed dataset with no
declared publication rule is refused at write. Backtesting on ingest time is the most
comfortable way to be wrong, because it is invisible in every metric.

**It never rewrites data a snapshot pins.** A run is pinned to a snapshot, so the bytes it
was measured on stay exactly as they were; a later load writes a successor dataset that
records what it supersedes.

```
$ kanso data load --loader synthetic --spec demo.yaml
error: DEMO.SIM-bar-1m-raw-20250901 is named by a snapshot and cannot be rewritten
remedy: write a successor dataset recording supersedes=<dataset_id>
$ echo $?
2
```

**It never runs a version under an engine it was not certified under.** A strategy version
pins the NautilusTrader version it was measured on, and `deploy` refuses a mismatch: running
it under another engine is running something nobody measured. Re-certifying the same bytes
after an upgrade is a plain `kanso cert run`.

**Its core knows no vendor and no broker.** Every outside party lives in exactly one adapter
package, discovered from a directory rather than listed anywhere, enabled by its credentials
rather than by installation. The whole test suite, `kanso doctor` and the demo are green
with every vendor and broker credential unset — which is what makes the package usable
before you have bought anything, and what keeps a second copy of a vendor's wire from
appearing somewhere nobody is looking.

**It writes you no notes.** No learnings file, no progress report, no design log, in this
repository or in your workspace. The cards, the certificates, the sessions and the state
store are the record; anything else is a second version of the truth that drifts from the
first.

**It has no UI.** `kanso status` is one screen, `--json` is on every command, and every
number it prints is read back out of the state store — `results.tsv` beside a hypothesis is
rendered from the cards, not appended to, so deleting it costs nothing and editing it
changes nothing. A dashboard would be a fourth place the numbers live.

## Install

```bash
uv tool install kanso          # the CLI
uv add kanso                   # or, inside a project
```

Python ≥3.12 on macOS 26+ arm64 or Linux x86_64 — the pinned NautilusTrader wheel decides
the hosts, and `kanso doctor` checks the wheel's platform tag against yours.

## A first run, with no credential of any kind

`--demo` renders a workspace whose model register is the shipped `mock` protocol and whose
data comes from the shipped synthetic loader. It classifies, proposes, aligns, backtests,
certifies, composes and deploys to paper with no provider key, no vendor key and no network.

```bash
kanso init demo --demo
cd demo
kanso doctor
kanso data load --loader synthetic --spec demo.yaml
kanso data snapshot
kanso hyp add hypotheses/demo_mr/hypothesis.yaml
kanso classify demo_mr
kanso research run demo_mr --cards 3
kanso status
```

The last two:

```
$ kanso research run demo_mr --cards 3
run        d99f1b6cd8734b278a7c8f1160b32f16 · lane op · cards
cards      3 proposed · 1 keep · 1 discard · 1 crash · trial 4
aligned    0 check(s) · 0 drift(s)
best       f729a53 at 9.986730
next       kanso research run demo_mr --cards 3

$ kanso status                                 # its leading workspace-path line is elided
daemon     stopped · 0 queued
lanes      1/2 working
           l1   idle
           op   demo_mr · 4 card(s) · best 9.986730
cards/h    4 in the last hour
best       1 hypothesis(es)
           demo_mr             researching  9.986730      4 card(s)
spend      $0.0000 today · 4 call(s) · 2026-09-06
inbox      0 unread
```

One keep, one discard and one crash is the loop working rather than a fault. A card is
`keep`, `discard` or `crash`; the last two restore the lane's `strategy.py` from the best,
and every one of the three stays in state, so the experiment log is complete whatever the
lane directory looks like afterwards.

A run that stalls certifies itself before it returns, so a daemon left running reaches a
certificate on every lane with no operator at all. Driven by hand, the same path is:

```bash
kanso research end demo_mr
kanso cert plan demo_mr     # what would count as proof for this hypothesis
kanso cert run demo_mr      # whether it holds, on the window research never saw
kanso portfolio show
```

```
$ kanso cert run demo_mr                       # its trailing written/source/next lines are elided
verdict    demo_mr · f729a53 · pass
gates      5 judged · 5 pass · 0 fail · 0 skipped
           pass  embargoed_window      certification=10.149870877220001, min_fraction=0.5, objective=net_edge_bps, research=9.98672990339711
           pass  publication_lag       n_datasets=1, published_too_early=[], tolerance_s=0.0, unknown=[]
           pass  parity_replay         compared=850, divergence=None, engine=20260906T010229Z-engine-6ca3f78, engine_intents=850, identical=True, max_ts_delta_ns=0, node=20260906T010225Z-node-6ca3f78, node_intents=850, ts_ns=0
           pass  cost_stress           metric_a=5.146082462396991, metric_b=0.14229404757397998, mult_a=2.0, mult_b=3.0, objective=net_edge_bps
           pass  bootstrap             limit_pct=15.0, mdd_p95=0.38450757237500577, n=1000, objective=net_edge_bps, objective_ci90=[7.823304135204389, 12.394500908889786]
objective  net_edge_bps 10.149871 ± 0.603055
pins       engine 1.231.0 · plan 1 · snapshot 52aca7b6664cdd1f235cc2ae3bd1843b2ac6240ab365a340deefdd80b92a1b94 · trial 4

$ kanso portfolio show
paper      up · exec sandbox (simulated) · data replay · speed 1 · capital 100,000
           clock 2025-09-01 · catalog to 2025-09-01 · allocated 40,000 · pnl +2,192.07
           demo_mr@1               40,000  pnl +2,192.07 over 1 window(s)
live       down · exec sandbox (simulated) · data replay · speed 1 · capital 0
           clock never run · catalog to nothing · allocated 0 · pnl +0.00
limits     gross 100% · net 100% · per strategy 40% · daily loss 3%
```

Nothing composed or deployed that strategy by hand: the passing certificate did both,
because neither act has a decision left in it. `parity_replay` compared 850 order intents
from the live code path against 850 from the research one at a tolerance of zero
nanoseconds. Live is `down` and stays down until a person types `--as`.

## Your own workspace

```bash
kanso init .                   # anywhere; skills linked, envelope detected, no git touched
$EDITOR models.yaml .env       # your providers; keys as KANSO_<PROVIDER>_API_KEY
kanso doctor                   # green before anything else
```

Then resolve instruments, load data, take a snapshot and write your first hypothesis — the
shipped skills `kanso-data` and `kanso-hypothesis` are the procedures, and
`docs/workspace.md` says what every file in the directory is for. Instruments come from a
configured data adapter; a workspace with no adapter loads file exports and declares its
instruments by hand. Keys live only in environment variables, read from the gitignored
`.env` and then from your shell, and never appear in anything kanso writes.

## Where to go next

| page | what it answers |
|---|---|
| `docs/concepts.md` | the vocabulary: hypothesis, construct, card, keep rule, certification, embargo, version, stage, promotion |
| `docs/cli.md` | every command, every exit code, every refusal |
| `docs/workspace.md` | every file a workspace holds, who writes it, and what is refused if you edit one that is not yours |
| `docs/constructs.md` | the construct catalogue: what each is, what it attaches to, and which are runnable |
| `docs/adapters.md` | how an outside party reaches kanso, and what `--check` measures rather than declares |
| `docs/extensions.md` | writing a loader, a data type, a construct, an execution client or an adapter in `kanso_ext/`, what a gate and an objective need instead, and upstreaming one |
| `docs/maintainers.md` | maintaining and releasing the framework, and running the research daemon as a service |
| `docs/backlog.md` | the known limitations, honestly, with what each would take |
| `AGENTS.md` | the standing rules for changing this package |

## What 0.1.0 does not do

- **A wall-clock broker client cannot be deployed.** The declarations, the refusals, the
  credential handling and `promote --live --as NAME` are all live and tested, but a stage
  node in this version is a bounded run over the catalog, so `kanso portfolio deploy`
  refuses a `clock: wall` execution client outright rather than filling its orders in
  simulation while calling the money the broker's. The long-running node is the last piece
  between this build and a broker actually trading.
- **Two first-party adapters** ship: one data vendor and one broker. Everything else is
  file exports, the synthetic loader, or an adapter you write.
- Fill-quality drift has no measurement channel yet, so that gate skips honestly on every
  stage; the execution client polls for fills rather than reading an order stream; and
  several constructs in the catalogue are declared but not runnable.

`docs/backlog.md` is the full list — thirty-four entries, twenty-five of them open, what the
build did about each and what each would need. It is worth reading before you trust a number.

## Maintaining the framework while using it

Two repositories. **Framework**: this one. **Projects**: any directory you `kanso init`,
depending on the framework by release (`uv add kanso`) or by checkout
(`uv add --editable ../kanso`). A capability you need before it exists is prototyped inside
the project as an extension in `kanso_ext/` — constructs, loaders, data types, execution
clients and adapters use the same interfaces as the built-ins, and a gate or an objective is
written against the same ones but has to land in the package to judge anything — and moved
upstream with the `kanso-upstream` skill once it has earned it. `docs/maintainers.md` is the
whole procedure; `skills/` holds the maintainer skills.

License: Apache-2.0.
