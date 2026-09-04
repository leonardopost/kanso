# kanso

Minimal, agent-first quant workbench: hypotheses → infinite autoresearch → certification → strategies → portfolio, on [NautilusTrader](https://nautilustrader.io).

**Status: seed.** This repository contains the build specification (`SPEC.md`, deleted at the 0.1.0 release), build-agent instructions (`AGENTS.md`), the shipped operator skills (`src/kanso/skills/`) and workspace templates (`src/kanso/templates/`). The package itself is built milestone by milestone.

## For the build agent
Read `AGENTS.md`, then `SPEC.md`. Start at milestone M0.

## Setup (operator, after M5)
```bash
uv tool install kanso                 # the CLI; or `uv add kanso` inside a project
cd <any dir> && kanso init .              # scaffolds the workspace (no git operation; a .gitignore is written); skills linked; envelope detected
$EDITOR models.yaml .env              # your LLM providers; keys as KANSO_<PROVIDER>_API_KEY in .env, or exported in your shell profile
kanso doctor                          # green before anything else
```
Then resolve instruments → load data → snapshot → hypothesis (skills `kanso-data`, `kanso-hypothesis`). Instruments are resolved from a configured data adapter, not typed by hand; a workspace with no adapter loads file exports and declares its instruments manually. `kanso init demo --demo` builds a synthetic workspace that runs end to end without any vendor or provider. A workspace can be a plain directory or a subdirectory of an existing repository; kanso never runs git — it writes files and `state.db`, versions `strategy.py` content-addressed in `state.db`, and committing the workspace is yours to do.

## Operator touchpoints
Everything not listed here runs without you.

| moment | who | what |
|---|---|---|
| setup | operator | provider and vendor keys in `.env`, the data adapters to use (or file exports), portfolio capital and limits |
| new idea | operator → agent | you state the thesis in chat; the agent writes `hypothesis.yaml`; kanso validates it and classifies it as a construct — sleeve, filter, overlay, exit, … (you may override construct/objective/constraints) |
| research | kanso | 24/7 loop over lanes; stalls → certification (planned per hypothesis by an agent at runtime; you may ask to see or re-plan it) → paper, all automatic |
| `misaligned` | operator | research drifted from the thesis; kanso already reverted and continued — decide whether the drift deserves its own hypothesis |
| `cert_failed` ×3 | operator | the idea keeps failing certification; retire it or rewrite the thesis |
| `deploy_blocked` | operator | no capital assignable within limits; adjust `portfolio.yaml` |
| `promotable` | operator | paper period passed; the only step that moves real capital: `kanso promote <strategy> --live --as <you>` |
| `demoted` | operator | live surveillance pulled a strategy back to paper; read the reason |
| any time | operator | `kanso status`; `kanso replay run` to ask what a strategy would have done; `kanso research stop` / `kanso research start` |

## Maintaining the framework while using it
Two repositories. **Framework**: this one; releases are semver tags (and PyPI). **Projects**: any repo where you `kanso init`; they depend on the framework with `uv add kanso` (a release) or `uv add --editable ../kanso` (your local checkout, so framework fixes land in the project immediately). A capability you need before it exists in the framework is prototyped *inside the project* as an extension (`kanso_ext/`: gates, objectives, loaders, data types, same interfaces as the built-ins) and, once proven, moved upstream with the `kanso-upstream` skill (copy into the checkout, tests, branch, PR). Framework fixes found in use are made in the checkout and PR'd the same way; without a checkout, `kanso doctor --report` gives the block for an issue. The full procedure — repositories, development loop, extensions, releases, spec governance — is `docs/maintainers.md`; the release procedure is the `kanso-release` skill in `skills/`.

License: Apache-2.0.
