# kanso — maintainer guide

Audience: whoever owns the framework repository. Operator-side procedures are the shipped
skills; this is the other side. `AGENTS.md` holds the standing rules for changing the
package; this page holds the process around them.

## 1. Repositories and roles
| repository | contains | agent instructions | skills |
|---|---|---|---|
| **framework** (`kanso`) | package, tests, docs, shipped skills and templates | `AGENTS.md` (the standing rules for changing the package) | `skills/` (maintainer: `kanso-release`) |
| **project** (any directory, optionally inside a git repository, holding a workspace) | `kanso.toml`, hypotheses, data, state; optional `kanso_ext/` | rendered `AGENTS.md` | linked from the installed package (`kanso skills sync`), incl. `kanso-upstream` |

A project depends on the framework with `uv add kanso` (a release) or `uv add --editable
<path-to-checkout>` (local development). `kanso doctor`'s `install` check says which — `package`
or `editable` — and from where.

## 2. Development loop
1. Change the framework in its checkout; `uv run pytest` (coverage ≥85%), `uv run ruff
   format --check`, `uv run ruff check`, `uv run mypy src`.
2. Try it from a project with an editable install; the demo workspace — `kanso init demo
   --demo` and the first-run sequence in `README.md` — is the smoke test, and it must stay
   green with every `KANSO_*` and vendor variable unset.
3. Conventional commit on a `feat/`, `fix/`, `docs/` or `chore/` branch; PR; CI (macOS
   arm64 + Linux x86_64, Python 3.12/3.13) must be green.

## 3. Prototype in a project, promote to the framework
Capabilities are prototyped as **workspace extensions** (`kanso_ext/`, same interfaces as
built-ins: `Construct`, `Loader`, `register_custom_type`, `ExecutionClientSpec`, and the data
and broker adapter protocols) and moved upstream with the `kanso-upstream` skill (copy, real
tests, branch, PR with the workspace evidence). `Gate` and `Objective` are written against the
same interfaces and are the two exceptions to prototyping: no workspace registry reads either
in 0.1.0 — the toolbox is the package's own library — so one judges nothing until it is
upstream (`docs/backlog.md` entry 26). Built-in additions land in
`src/kanso/criteria/library/` (gate/objective YAML + `impl`),
`src/kanso/classify/constructs/`, `src/kanso/data/loaders/`, `src/kanso/data/types/`,
`src/kanso/data/adapters/<vendor>/` or `src/kanso/nautilus/adapters/<broker>/`, each with
tests and a line in `docs/`. `docs/extensions.md` is the interface reference for both sides.
Anything found in use without a checkout goes to the issue tracker with `kanso doctor
--report`.

## 4. Releases (skill `kanso-release`)
- Semver. Patch: fixes. Minor: features, new gates/loaders, `nautilus_trader` range bumps,
  schema migrations (additive). Major: incompatible schema/CLI changes.
- A release = version bump (`pyproject.toml`, `kanso.__version__`), `CHANGELOG.md` entry
  (one line per change, no prose), tag `vX.Y.Z`; CI publishes to PyPI on the tag.
- Publishing is by **trusted publishing**: `publish.yml` requests an OIDC token and stores no
  credential. The index-side registration is done once and must match the workflow exactly —
  repository `leonardopost/kanso`, workflow filename `publish.yml`, environment `pypi`. A
  mismatch in any of the three fails the upload with an authentication error that looks like
  a missing secret; check the registration before touching the workflow. A failed publish
  uploaded nothing: fix and re-run the job, never retag.
- `nautilus_trader` range: bump in a minor release only after the demo e2e and the parity
  tests pass on the new version; note wheel/OS constraints in the changelog. Strategy
  versions pin the engine they were certified under; operators re-certify to move.
- Schema changes ship with a migration `src/kanso/state/migrations/NNNN_name.sql` and a
  `schema_version` bump. `kanso migrate` applies them, and every other command refuses a
  database behind the package with exit 2 rather than migrating it behind the operator's
  back. A database *ahead* of the package — an operator who downgraded — is not detected:
  nothing is pending, `doctor` reports it up to date, and commands run against a schema this
  kanso does not know. Downgrading across a migration is unsupported until that check
  exists, and a release whose migrations are not additive should say so in the changelog.
- Skills and templates ship inside the wheel; a release changes them for every workspace at
  the next `kanso skills sync` / `kanso init`.

## 5. Documentation governance
`docs/` and the tests are the reference; there is no separate specification and no build
document. Changing behaviour = changing the relevant `docs/` page and the tests in the same
PR; the PR body states why. No decision logs, deviation logs or design notes are kept in the
repository. `docs/backlog.md` is the one place a known limitation is recorded, and an entry
says what the build did about it and what closing it would take — never what someone
intends to do.

Every command a page shows must have been run, and the output pasted rather than composed.
If a page and the code disagree, the code is what happened: fix the page, or fix the code
and say so, but do not document the version you would have preferred.

## 6. Running the research daemon as a service

`kanso research start` detaches a supervisor and returns. The supervisor takes an exclusive
lock on `runs/daemon.pid`, writes its pid there, and starts one worker process per lane the
envelope allows plus the monitor; `kanso research stop` signals it and everything is left
exactly where it was, so the next start resumes the open runs before it takes new work.

A service manager already provides detachment, restart and log capture, and it can only
supervise a process it owns. So a unit does **not** run `kanso research start`: it runs the
supervisor in the foreground, which is the same code `start` detaches.

```bash
"$(uv tool dir)"/kanso/bin/python -m kanso.research serve /path/to/workspace
```

The interpreter is the one that owns the install — for `uv tool install kanso`, the path
above; for a project venv, that venv's `python`. The workspace is the argument, not the
working directory, so the unit's `WorkingDirectory` is a convenience and not a requirement.
The supervisor exits `0` when it is asked to stop and non-zero when it could not run at all,
which is what makes both restart policies below behave.

### systemd (Linux), one unit per workspace

`~/.config/systemd/user/kanso-alpha.service`:

```ini
[Unit]
Description=kanso research daemon (alpha)
After=network-online.target

[Service]
Type=exec
WorkingDirectory=/home/you/kanso/alpha
ExecStart=/home/you/.local/share/uv/tools/kanso/bin/python -m kanso.research serve /home/you/kanso/alpha
Restart=on-failure
RestartSec=30
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now kanso-alpha
journalctl --user -u kanso-alpha -f
loginctl enable-linger "$USER"        # so it keeps running when you log out
```

`KillMode=mixed` sends `SIGTERM` to the supervisor alone, which is what lets it terminate
its own children with the grace it gives them — a worker looks up between cards, leaves its
run open and exits — and kills whatever is left. The default `control-group` also works,
because a worker treats a `SIGTERM` of its own the same way, but it takes the ordering away
for nothing. `TimeoutStopSec` must stay above that grace so a lane still inside a card is
killed by the supervisor rather than by systemd.

`Restart=on-failure` does not restart after `kanso research stop`, because a clean stop
exits `0`. It does restart a crash, and that is safe: the lock is released when the kernel
closes the dead process's file, so the leftover pid blocks nothing and the new supervisor
resumes the open runs.

### launchd (macOS), a LaunchAgent

`~/Library/LaunchAgents/com.kanso.alpha.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.kanso.alpha</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>/Users/you/.local/share/uv/tools/kanso/bin/python</string>
    <string>-m</string>
    <string>kanso.research</string>
    <string>serve</string>
    <string>/Users/you/kanso/alpha</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/you/kanso/alpha</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>/Users/you/kanso/alpha/runs/service.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/kanso/alpha/runs/service.log</string>
</dict>
</plist>
```

```bash
plutil -lint ~/Library/LaunchAgents/com.kanso.alpha.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kanso.alpha.plist
launchctl print gui/$(id -u)/com.kanso.alpha
launchctl bootout gui/$(id -u)/com.kanso.alpha
```

`caffeinate -i` is what `kanso research start` already does for you on macOS, and it belongs
in the unit because launchd holds no power assertion of its own: a host that idle-sleeps
does no research. It is safe as the first argument because `caffeinate` execs the utility in
the process it was given and forks a child to hold the assertion — so the pid launchd tracks
is the supervisor itself, and launchd's signal reaches it directly rather than a wrapper
that might not pass it on.

`SuccessfulExit=false` restarts on a non-zero exit only, which gives the same split as
systemd: `kanso research stop` really stops the service, and a crash brings it back.

### What both get wrong if you are not careful

- **A workspace that cannot start will loop.** A workspace with no `envelope.yaml`, or one
  whose lock another daemon already holds, exits the supervisor `1` with a traceback; the
  restart policy starts it again, and the throttle is the only thing keeping it to twice a
  minute. `kanso doctor` names the cause; fix the workspace rather than the unit.
- **Run `kanso doctor` before enabling the unit, and after every upgrade.** The CLI refuses
  a state database behind the package's schema (exit 2, remedy `kanso migrate`); the
  supervisor entry point does not check, and will happily start lanes against an
  un-migrated database. Nothing else in the daemon substitutes for that check.
- **One daemon per workspace, and the lock is what says so.** A hand-run `kanso research
  start` and a running unit cannot coexist: whichever is second refuses, the CLI with exit 2
  and the bare supervisor with a traceback and exit 1.
- **`kanso research stop` stops the service** — and a restart-always policy would start it
  straight back. Use the service manager to stop the service and the CLI to stop a daemon
  you started by hand.
- **The logs are in two places.** The supervisor's own stream goes wherever the unit sends
  it — the journal, or the file the plist names — while its children's streams go to
  `runs/daemon.log` inside the workspace, which is gitignored and never rotated. Watch its
  size on a host that runs for months.
- **No credentials in the unit.** kanso resolves every key at the moment of use from the
  workspace `.env` and then the ambient environment, so a unit needs no `Environment=` line
  and should not have one — `systemctl show` prints them. If the ambient environment is
  genuinely where a key must live, use `EnvironmentFile=` and a mode-0600 file.
- **The lane count comes from the envelope, which was measured once.** A host whose cores or
  memory changed keeps the old plan until `kanso env detect` runs again; `kanso doctor`
  warns that the host changed since detection.
