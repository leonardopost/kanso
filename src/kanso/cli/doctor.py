"""The workspace diagnosis: what `doctor` checks, and how each check is graded.

Every check answers one question about this workspace on this host and grades itself
`ok`, `warn` or `fail`. A workspace is green when no check fails: a warning is something
the operator should see and may act on, a failure is something that will stop a later
command. The command exits 0 when green — warnings printed — and 2 when anything fails,
so an operator agent branches on the code and reads the detail.

Nothing here writes to the workspace and nothing reaches the network. The checks that
touch the state database open it read-only in effect (they migrate nothing, and an
absent `state.db` is never created), the envelope is re-detected in memory and never
written, the instrument check builds manual definitions in memory and never stores one,
and the adapter check reports what is registered and what each would need rather than
asking a vendor anything — unless `--check-adapters` is passed, which is the one path
here that reaches a network.

Five checks compare the files against the records. `best` is the workspace `strategy.py`
against the blob kanso last wrote there; `certificates` is every certified subject
against the bytes that must still be held for it; `record` is the certified work on disk
against what the record remembers of it; `instruments` is the registry file, the
universes it must resolve and the definitions the newest snapshot pinned; `lanes` is
every open run against its lane directory and every lane directory against the open
runs. Each grades a fault by what it stops: a run that cannot take a card and a subject
whose bytes are gone fail, and everything an operator may have meant — an edited
best-so-far, a lane note, a snapshot older than a resolution, a clone — warns.

Credentials are reported as names and origins. A value is never read into a message,
because `doctor --report` is the block an operator pastes into an issue: that mode also
replaces the workspace, repository and home paths with placeholders, so a report carries
the diagnosis and nothing about the machine it came from.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from kanso import __version__, creds, env, ext, skills_sync
from kanso.certify.certificate import source_file
from kanso.cli.context import STATE_DB
from kanso.criteria.integrity import scope as lane_scope
from kanso.data import registry
from kanso.data.instruments import CACHE_NAME, ManualProvider, ResolveError, _lookup, read_store
from kanso.data.manifest import catalog_path
from kanso.data.snapshot import instrument_drift, newest
from kanso.env import envelope as envelope_module
from kanso.env import host
from kanso.errors import KansoError, ValidationError
from kanso.hyp import HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE, hypothesis_dir
from kanso.nautilus import adapters as brokers
from kanso.nautilus import facts
from kanso.portfolio import Declared, exec_client_declarations, stage_refusals
from kanso.research.daemon import active_runs
from kanso.schemas import Hypothesis, InstrumentEntry, InstrumentsFile, parse_yaml
from kanso.schemas.models import ModelsFile
from kanso.schemas.portfolio import STAGES
from kanso.schemas.run import RunRecord
from kanso.schemas.yamlio import load_yaml
from kanso.state import StateStore, migrations
from kanso.strategy import IMPL, STRATEGIES
from kanso.strategy.files import strategies as composed_strategies
from kanso.workspace import LANE_ROOT, Workspace, gitignore_entries, in_git_repo

Status = Literal["ok", "warn", "fail"]

STALE_DAYS = 7
"""An envelope older than this is worth re-detecting."""

HARDWARE = ("os", "os_version", "arch", "chip", "cores_total", "mem_gb")
"""The detected fields whose change invalidates an envelope; the rest fluctuate."""


@dataclass(frozen=True)
class Check:
    """One diagnosis: what was checked, how it graded, and what was observed."""

    name: str
    status: Status
    detail: str
    items: tuple[str, ...] = ()
    remedy: str | None = None

    def as_json(self) -> dict[str, object]:
        """The check as one `--json` object."""
        out: dict[str, object] = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.items:
            out["items"] = list(self.items)
        if self.remedy:
            out["remedy"] = self.remedy
        return out


def run(ws: Workspace, check_adapters: bool = False) -> list[Check]:
    """Every check, in report order. Never raises: a broken check is a graded check."""
    unread = _unread(ws)
    return [
        _guard(name, check)
        for name, check in (
            ("versions", _versions),
            ("install", _install),
            ("engine wheel", _wheel),
            ("schema", lambda: _schema(ws)),
            ("envelope", lambda: _envelope(ws)),
            ("repository", lambda: _repository(ws)),
            ("gitignore", lambda: _gitignore(ws)),
            ("best", lambda: _best(ws, unread)),
            ("certificates", lambda: _certificates(ws, unread)),
            ("record", lambda: _record(ws, unread)),
            ("skills", lambda: _skills(ws)),
            ("credentials", lambda: _credentials(ws)),
            ("adapters", lambda: _adapters(ws, check_adapters)),
            ("execution", lambda: _execution(ws)),
            ("instruments", lambda: _instruments(ws, unread)),
            ("lanes", lambda: _lanes(ws, unread)),
            ("extensions", lambda: _extensions(ws)),
            ("engine facts", _engine_facts),
        )
    ]


def _guard(name: str, check: Callable[[], Check]) -> Check:
    """Run one check; a check that raises is itself the finding, and the rest still run.

    A diagnosis that stops at the first broken thing is worth less than one that grades
    it and carries on, so an unexpected failure here is a failed check rather than a
    failed command.
    """
    try:
        return check()
    except KansoError as error:
        return Check(name, "fail", error.message, remedy=error.remedy)
    except Exception as error:
        return Check(name, "fail", f"{type(error).__name__}: {error}")


def redactor(ws: Workspace) -> Callable[[str], str]:
    """A function replacing the workspace, its repository and the home directory.

    Longest path first, so a workspace inside a repository inside the home directory
    still reads as `<workspace>` rather than as a path under `~`.
    """
    repository = in_git_repo(ws.root)
    pairs = [(str(ws.root), "<workspace>"), (str(Path.home()), "~")]
    if repository is not None:
        pairs.append((str(repository), "<repository>"))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def redact(text: str) -> str:
        for path, placeholder in pairs:
            text = text.replace(path, placeholder)
        return text

    return redact


def redacted(checks: Iterable[Check], redact: Callable[[str], str]) -> list[Check]:
    """The checks with every path a report must not carry replaced."""
    return [
        replace(
            check,
            detail=redact(check.detail),
            items=tuple(redact(item) for item in check.items),
            remedy=redact(check.remedy) if check.remedy else None,
        )
        for check in checks
    ]


def _versions() -> Check:
    """kanso, Python and the engine actually installed here."""
    installed = envelope_module.engine_version()
    detail = (
        f"kanso {__version__} · python {platform.python_version()} · nautilus_trader {installed}"
    )
    if installed == facts.ENGINE_VERSION:
        return Check("versions", "ok", detail)
    return Check(
        "versions",
        "warn",
        f"{detail}; the engine facts were verified against {facts.ENGINE_VERSION}",
        remedy="install the pinned engine, or re-verify the facts against this one",
    )


def _install() -> Check:
    """Editable or package, and the directory the running code came from."""
    package = Path(__file__).resolve().parent.parent
    try:
        distribution = importlib.metadata.distribution("kanso")
    except importlib.metadata.PackageNotFoundError:
        return Check(
            "install",
            "warn",
            f"no kanso distribution is installed; running from {package}",
            remedy="install kanso, so its version and its files agree",
        )
    return Check("install", "ok", f"{_install_mode(distribution)} · {package}")


def _install_mode(distribution: importlib.metadata.Distribution) -> str:
    """`editable` when the installer recorded an editable directory, else `package`."""
    raw = distribution.read_text("direct_url.json")
    if not raw:
        return "package"
    try:
        recorded = json.loads(raw)
    except ValueError:
        return "package"
    directory = recorded.get("dir_info") if isinstance(recorded, dict) else None
    editable = bool(directory.get("editable")) if isinstance(directory, dict) else False
    return "editable" if editable else "package"


def _wheel() -> Check:
    """The installed engine wheel against this host's OS and architecture."""
    compatible, detail = env.wheel_ok()
    if compatible:
        return Check("engine wheel", "ok", detail)
    return Check(
        "engine wheel",
        "fail",
        detail,
        remedy="install a nautilus_trader wheel built for this operating system and architecture",
    )


def _schema(ws: Workspace) -> Check:
    """The version stamped on `state.db`, and whether this package can write it.

    Both directions, because `pending` only looks upward: a database a later kanso wrote
    has migrations this one has never heard of, and reporting it up to date would make
    `doctor` the one command that says a workspace is well when every other refuses it.
    """
    path = ws.path("state.db")
    if not path.exists():
        pending = [migration.name for migration in migrations()]
        return Check(
            "schema",
            "warn",
            f"no state.db yet · {len(pending)} migration(s) pending",
            items=tuple(pending),
            remedy="run `kanso migrate`",
        )
    with StateStore(path) as store:
        version = store.schema_version()
        pending = store.pending()
        ahead = store.ahead_by()
    if ahead:
        return Check(
            "schema",
            "fail",
            f"schema {version} · {ahead} version(s) past the newest this package ships",
            remedy="install the kanso that wrote it, or start a workspace this one owns",
        )
    if not pending:
        return Check("schema", "ok", f"schema {version} · up to date")
    return Check(
        "schema",
        "warn",
        f"schema {version} · {len(pending)} migration(s) pending",
        items=tuple(pending),
        remedy="run `kanso migrate`",
    )


def _envelope(ws: Workspace) -> Check:
    """The written envelope against the machine running now."""
    try:
        written = env.read(ws)
    except KansoError as error:
        return Check(
            "envelope", "warn", error.message, remedy=error.remedy or "run `kanso env detect`"
        )
    if written is None:
        return Check("envelope", "warn", "no envelope.yaml", remedy="run `kanso env detect`")
    detail = (
        f"{written.plan.lanes} lane(s) · {written.detected.cores_total} cores · "
        f"{written.detected.mem_gb} GB · detected {written.detected_at}"
    )
    changed = _hardware_changed(written.detected)
    if changed:
        return Check(
            "envelope",
            "warn",
            f"{detail}; the host changed since detection",
            items=tuple(changed),
            remedy="run `kanso env detect`",
        )
    age = _age_days(written.detected_at)
    if age is None:
        return Check(
            "envelope",
            "warn",
            f"{detail}; detected_at is not a timestamp",
            remedy="run `kanso env detect`",
        )
    if age > STALE_DAYS:
        return Check(
            "envelope", "warn", f"{detail}; {age} days old", remedy="run `kanso env detect`"
        )
    return Check("envelope", "ok", detail)


def _hardware_changed(written: env.Detected) -> list[str]:
    """The hardware fields the host no longer reports as the envelope recorded them."""
    now = host.facts()
    return [
        f"{name}: {getattr(written, name)} → {getattr(now, name)}"
        for name in HARDWARE
        if getattr(written, name) != getattr(now, name)
    ]


def _age_days(detected_at: str) -> int | None:
    """Whole days since the envelope was detected, or `None` when it cannot be read."""
    try:
        when = datetime.fromisoformat(detected_at)
    except ValueError:
        return None
    now = datetime.now(tz=when.tzinfo) if when.tzinfo else datetime.now()  # noqa: DTZ005
    return max(0, (now - when).days)


def _repository(ws: Workspace) -> Check:
    """Whether a repository encloses the workspace. A filesystem check that only reports.

    kanso never invokes git, so both answers are correct and neither is graded down:
    committing the workspace, or not, is the operator's business.
    """
    repository = in_git_repo(ws.root)
    if repository is None:
        return Check("repository", "ok", "no enclosing git repository")
    where = "the workspace root" if repository == ws.root else str(repository)
    return Check("repository", "ok", f"enclosed by a repository at {where}")


def _gitignore(ws: Workspace) -> Check:
    """The entries the workspace template guarantees, present or not."""
    path = ws.path(".gitignore")
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = {line.strip() for line in text.splitlines()}
    wanted = gitignore_entries()
    missing = [entry for entry in wanted if entry not in present]
    if not path.is_file():
        return Check(
            "gitignore",
            "warn",
            "no .gitignore",
            items=tuple(wanted),
            remedy="add these entries; state.db and .env do not belong in a repository",
        )
    if missing:
        return Check(
            "gitignore",
            "warn",
            f"{len(wanted) - len(missing)}/{len(wanted)} entries present",
            items=tuple(missing),
            remedy="add the missing entries to .gitignore",
        )
    return Check("gitignore", "ok", f"{len(wanted)} entries present")


def _skills(ws: Workspace) -> Check:
    """One link per packaged skill in every configured target."""
    targets = ws.config.skills_targets
    if not targets:
        return Check("skills", "ok", "no [skills] targets configured")
    packaged = skills_sync.packaged_skills()
    items: list[str] = []
    linked = 0
    for target in targets:
        directory = Path(target).expanduser()
        directory = directory if directory.is_absolute() else ws.path(target)
        good = sum(1 for skill in packaged if _linked(directory, skill))
        linked += good
        items.append(f"{target}: {good}/{len(packaged)}")
    expected = len(packaged) * len(targets)
    if linked == expected:
        return Check("skills", "ok", f"{linked} links across {len(targets)} target(s)")
    return Check(
        "skills",
        "warn",
        f"{linked}/{expected} links across {len(targets)} target(s)",
        items=tuple(items),
        remedy="run `kanso skills sync`",
    )


def _linked(directory: Path, skill: Path) -> bool:
    """Is there a link in `directory` pointing at this packaged skill?"""
    link = directory / skills_sync._link_name(skill.name)
    return link.is_symlink() and Path(link.readlink()) == skill


def _credentials(ws: Workspace) -> Check:
    """Every variable a configured consumer needs: its name and where it resolves from.

    A value is never read here — only whether the name resolves, and from which of the
    two places. An unresolved credential is a warning: the step that needs it refuses
    when it is called, and a workspace is allowed to be half-configured.
    """
    try:
        needed = _needed_credentials(ws)
    except KansoError as error:
        return Check("credentials", "warn", error.message, remedy=error.remedy)
    if not needed:
        return Check("credentials", "ok", "no credential is needed by this configuration")
    items: list[str] = []
    unresolved = 0
    required = 0
    for name, consumers, is_required in needed:
        origin = creds.origin(name, ws.root)
        required += int(is_required)
        if is_required and origin is None:
            unresolved += 1
        note = "" if is_required else " (optional)"
        items.append(f"{name}: {origin or 'unset'} · {', '.join(consumers)}{note}")
    detail = f"{required - unresolved}/{required} required credentials resolve"
    if unresolved:
        return Check(
            "credentials",
            "warn",
            detail,
            items=tuple(items),
            remedy=f"set the unset names in {ws.path('.env')}, or export them",
        )
    return Check("credentials", "ok", detail, items=tuple(items))


def _needed_credentials(ws: Workspace) -> list[tuple[str, list[str], bool]]:
    """`(variable, consumers, required)` for the register and the webhook, deduplicated.

    A model reached over the mock protocol needs no key, and a local server usually
    needs none either, so both are listed without being required.
    """
    found: dict[str, tuple[list[str], bool]] = {}

    def note(name: str, consumer: str, required: bool) -> None:
        consumers, was_required = found.setdefault(name, ([], False))
        consumers.append(consumer)
        found[name] = (consumers, was_required or required)

    register = ws.path("models.yaml")
    if register.is_file():
        for model in load_yaml(ModelsFile, register).models:
            if model.protocol == "mock":
                continue
            name = model.api_key_env or creds.standard_name(model.provider)
            note(name, model.id, not model.local)
    if ws.config.webhook.url is None:
        note(creds.standard_name("webhook", "URL"), "escalation webhook", False)
    return [(name, consumers, required) for name, (consumers, required) in found.items()]


def _adapters(ws: Workspace, check_adapters: bool) -> Check:
    """What is registered, what each needs, and — only when asked — what its key reaches.

    Registration and configuration are separate facts and both are reported. An adapter is
    enabled by the presence of its credentials, never by installation, so a registered
    adapter with nothing set is the ordinary state of a fresh workspace and is graded `ok`.

    `--check-adapters` is the only path here that reaches a network, and it asks only the
    adapters whose credentials resolve: opening one that has none would fail on the missing
    variable, and reporting that as "your plan excludes these datasets" is the wrong answer
    in the most expensive direction. What comes back is the measured reach — entitlement
    and history floor per dataset, at the grain the source gates on — which is why a
    dataset the plan excludes is reported and not graded down: it is a fact about a
    subscription, not a fault in this workspace. A credential that does not authenticate
    is the one failure, because every later command through that adapter will stop on it.
    """
    known = registry.adapters(ext.discover(ws.root, ws.config.extensions_paths))
    configured = {name: adapter for name, adapter in known.items() if adapter.configured(ws)}
    items = [_adapter_item(ws, adapter) for _, adapter in sorted(known.items())]
    items += list(_unprovided(ws, known))
    detail = f"{len(known)} registered · {len(configured)} configured"
    if not check_adapters:
        return Check(
            "adapters",
            "ok",
            f"{detail}; no request was made",
            items=tuple(items),
            remedy=None if not configured else "run `kanso doctor --check-adapters` to probe them",
        )
    if not configured:
        return Check(
            "adapters",
            "ok",
            f"{detail}; --check-adapters had no configured adapter to reach and made no "
            "network call",
            items=tuple(items),
        )
    surveys = [adapter.survey(ws) for _, adapter in sorted(configured.items())]
    items += [line for survey in surveys for line in _survey_items(survey)]
    spent = sum(survey.requests for survey in surveys)
    reach = [item for survey in surveys for item in survey.reach]
    included = f"{sum(1 for item in reach if item.ok)}/{len(reach)} datasets included"
    refused = [survey.adapter for survey in surveys if not survey.reachable]
    if refused:
        return Check(
            "adapters",
            "fail",
            f"{detail}; {', '.join(refused)} did not authenticate · {spent} request(s)",
            items=tuple(items),
            remedy="check the credential named above; every command through that adapter stops",
        )
    return Check("adapters", "ok", f"{detail}; {included} · {spent} request(s)", items=tuple(items))


def _adapter_item(ws: Workspace, adapter: registry.Adapter) -> str:
    """One adapter as a line: its kind, its quota, its offer and where each name resolves."""
    origins = adapter.credential_origins(ws)
    names = ", ".join(f"{name}={origins.get(name) or 'unset'}" for name in adapter.credentials)
    return (
        f"{adapter.id}: {adapter.kind} · {adapter.quota(ws)} · "
        f"{', '.join(adapter.capabilities.names())} · {names or 'no credential'}"
    )


def _unprovided(ws: Workspace, known: Mapping[str, registry.Adapter]) -> tuple[str, ...]:
    """The `[adapters.<id>]` tables naming something nothing here registers.

    A broker adapter is configured through the same table as a data adapter and lives in
    its own registry, so both are consulted: a table for a broker that is installed is
    configuration, not a mistake, and reporting it as one would send an operator to delete
    the settings their stage depends on.
    """
    provided = set(known) | set(brokers.packaged())
    return tuple(
        f"{name}: configured in kanso.toml, and nothing registered here provides it"
        for name in sorted(ws.config.adapters)
        if name not in provided
    )


def _execution(ws: Workspace) -> Check:
    """Which execution clients exist, which are configured, and what each stage would do.

    Registration and configuration are separate facts here too: a broker's clients are
    discovered from the adapter directory whether or not a credential is set, and a
    workspace with none set is the ordinary fresh one. What is graded is the stage
    configuration, and it is graded by calling the refusals `deploy` itself makes rather
    than by restating them — a stage naming a client nothing provides, pairing a
    wall-clock client with replayed data or any speed but one, putting real capital
    anywhere but the live stage, or naming a client this version's node cannot run.

    No value is read: a client is reported by the variables its account needs and where
    each of them resolves from.
    """
    found = exec_client_declarations(ws)
    items = [_client_item(one) for one in found]
    configured = [one for one in found if one.credentials and one.configured]
    problems = stage_refusals(ws)
    items += [f"{stage}: {problem or 'ok'}" for stage, problem in sorted(problems.items())]
    refused = sorted(stage for stage, problem in problems.items() if problem is not None)
    detail = (
        f"{len(found)} client(s) · {len(configured)} broker account(s) configured · "
        f"{len(STAGES) - len(refused)}/{len(STAGES)} stages deployable"
    )
    if refused:
        return Check(
            "execution",
            "fail",
            f"{detail}; {', '.join(refused)} would be refused",
            items=tuple(items),
            remedy="fix the stage in portfolio.yaml; `kanso portfolio clients` lists what "
            "may be named",
        )
    return Check("execution", "ok", detail, items=tuple(items))


def _client_item(one: Declared) -> str:
    """One execution client as a line: what it trades, where, and which names resolve."""
    origins = ", ".join(f"{name}={one.origins.get(name) or 'unset'}" for name in one.credentials)
    return (
        f"{one.id}: {one.capital} · clock {one.clock} · {one.source} · "
        f"stages {', '.join(one.stages)} · {origins or 'no credential'}"
    )


def _survey_items(survey: registry.Survey) -> list[str]:
    """One adapter's measured reach, one line per dataset, then what qualifies them."""
    return [
        *(f"{survey.adapter} {item.line()}" for item in survey.reach),
        *(f"{survey.adapter}: {note}" for note in survey.notes),
    ]


def _extensions(ws: Workspace) -> Check:
    """The workspace packages that loaded, those that did not, and what they shadow."""
    found = ext.discover(ws.root, ws.config.extensions_paths)
    if not found:
        return Check("extensions", "ok", "none")
    items = [
        f"{extension.name}: {extension.error}" if extension.error else f"{extension.name}: ok"
        for extension in found
    ]
    shadowing = ext.shadows(found, ext.shipped(ws))
    items += [f"{name} shadows the built-in {kind} '{item}'" for name, kind, item in shadowing]
    broken = [extension for extension in found if not extension.ok]
    detail = f"{len(found)} loaded"
    if broken or shadowing:
        return Check(
            "extensions",
            "warn",
            f"{len(found) - len(broken)}/{len(found)} loaded"
            + (f" · {len(shadowing)} shadowed id(s)" if shadowing else ""),
            items=tuple(items),
            remedy="fix or remove the extension; a shadowed id resolves to the packaged one",
        )
    return Check("extensions", "ok", detail, items=tuple(items))


def _engine_facts() -> Check:
    """The engine claims kanso binds to, re-established against the installed package.

    A claim `facts.DESIGN_CONSTRAINTS` names is a constraint kanso is built around, not
    a defect, and is listed by design. Any other claim that does not hold is a binding a
    card, a certificate or a deployment depends on, so it fails with its evidence — a
    check that raised reports the exception — rather than reading as one more design
    constraint under `ok`. Short of that, the grade follows the engine version: facts
    verified against another version may no longer describe this one.
    """
    verified = facts.verify()
    gaps = [fact for fact in verified if not fact.holds]
    broken = [fact for fact in gaps if fact.claim not in facts.DESIGN_CONSTRAINTS]
    installed = envelope_module.engine_version()
    detail = (
        f"{len(verified) - len(gaps)}/{len(verified)} claims hold on nautilus_trader {installed}"
    )
    items = tuple(
        f"by design: {fact.claim}"
        if fact.claim in facts.DESIGN_CONSTRAINTS
        else f"does not hold: {fact.claim} — {fact.evidence}"
        for fact in gaps
    )
    if broken:
        return Check(
            "engine facts",
            "fail",
            f"{detail}; {len(broken)} binding(s) broken",
            items=items,
            remedy=f"reinstall nautilus_trader {facts.ENGINE_VERSION}; "
            "a card built on a broken binding measures nothing",
        )
    if installed == facts.ENGINE_VERSION:
        return Check("engine facts", "ok", detail, items=items)
    return Check(
        "engine facts",
        "warn",
        f"{detail}; the facts were verified against {facts.ENGINE_VERSION}",
        items=items,
        remedy="re-verify the engine facts against this version before trusting a card",
    )


# --- the files against the records ---------------------------------------------


@dataclass(frozen=True)
class Unread:
    """Why the state store is not read by a check, and what the check reports instead.

    An absent `state.db` is graded `ok`: nothing is recorded, so nothing the files could
    disagree with exists yet, and the schema check already carries the migration to run.
    A database this package cannot read — behind it, ahead of it, or not a database —
    is a warning, because the files may well disagree with records nobody can see.
    """

    reason: str
    remedy: str | None
    status: Status

    def check(self, name: str) -> Check:
        """The check as it is reported when the store is not read."""
        if self.status == "ok":
            return Check(name, "ok", f"nothing recorded yet · {self.reason}")
        return Check(name, "warn", f"not checked: {self.reason}", remedy=self.remedy)


def _unread(ws: Workspace) -> Unread | None:
    """Why the state store is not one this package reads now, or `None` when it is.

    Asked once per diagnosis and handed to every check that reads records, rather than
    each of them opening the database itself. An absent, unmigrated, newer or unreadable
    `state.db` is the schema check's finding, and a check that opened it regardless would
    grade the same fault a second time — or, for an absent file, create the database it
    was only meant to read. Never raises: it runs outside the guard each check has, so a
    store that cannot be opened is what those checks report, not what stops the diagnosis.
    """
    path = ws.path(STATE_DB)
    try:
        if not path.exists():
            return Unread("no state.db", None, "ok")
        with StateStore(path) as store:
            pending = store.pending()
            ahead = store.ahead_by()
    except Exception as error:
        return Unread(f"state.db could not be opened: {error}", None, "warn")
    if pending:
        return Unread(
            f"state.db is {len(pending)} migration(s) behind", "run `kanso migrate`", "warn"
        )
    if ahead:
        return Unread(
            "state.db was written by a later kanso",
            "install the kanso that wrote it, or start a workspace this one owns",
            "warn",
        )
    return None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _best(ws: Workspace, unread: Unread | None) -> Check:
    """`hypotheses/<id>/strategy.py` against the `best` blob kanso last wrote there.

    Once a hypothesis has a `best`, kanso owns that file: every keep and every re-point
    of `best` rewrites it, so a workspace copy holding other bytes was edited by hand or
    is missing. That is a warning and never a failure, because editing the file is
    exactly how an operator prepares `research begin --from-workspace` — the run that
    starts from the edit and clears `best`. A retired hypothesis is left alone.
    """
    if unread is not None:
        return unread.check("best")
    with StateStore(ws.path(STATE_DB)) as store:
        rows = store.connection.execute(
            "SELECT hyp_id, best_sha FROM hypotheses WHERE best_sha IS NOT NULL"
            " AND status != 'retired' ORDER BY hyp_id"
        ).fetchall()
    items: list[str] = []
    edited: list[str] = []
    for row in rows:
        hyp_id, best = str(row["hyp_id"]), str(row["best_sha"])
        path = hypothesis_dir(ws, hyp_id) / STRATEGY_FILE
        if not path.is_file():
            items.append(f"{hyp_id}: {STRATEGY_FILE} is missing · best {best[:7]}")
            edited.append(hyp_id)
        elif (held := _sha(path)) != best:
            items.append(f"{hyp_id}: {STRATEGY_FILE} is {held[:7]} · best {best[:7]}")
            edited.append(hyp_id)
        else:
            items.append(f"{hyp_id}: {STRATEGY_FILE} is best {best[:7]}")
    detail = f"{len(rows)} hypothesis(es) with a best · {len(rows) - len(edited)} hold it"
    if not edited:
        return Check("best", "ok", detail, items=tuple(items))
    return Check(
        "best",
        "warn",
        f"{detail}; edited: {', '.join(edited)}",
        items=tuple(items),
        remedy="; ".join(
            f"run `kanso research begin {hyp_id} --from-workspace` to research from the "
            f"edited file, or `kanso research show {hyp_id} > hypotheses/{hyp_id}/"
            f"{STRATEGY_FILE}` to restore the best"
            for hyp_id in edited
        ),
    )


def _certificates(ws: Workspace, unread: Unread | None) -> Check:
    """Every certified subject against the bytes that must still be held for it.

    A certificate and a strategy version each name their subject by `strategy_sha`, and
    those bytes are what `research show`, composition and a replay read. They are held
    twice: as a blob in state and as `<sha7>.py` beside the certificate, so a subject is
    lost only when both are gone — or the file beside the certificate holds other bytes.
    That is a failure, because every command that reads the subject stops on it; the
    remedy names a copy kanso can still find in the workspace, when there is one.
    """
    if unread is not None:
        return unread.check("certificates")
    with StateStore(ws.path(STATE_DB)) as store:
        subjects, counted = _certified_subjects(store)
        held = {subject for subject in subjects if store.has_blob(subject[1])}
    items: list[str] = []
    remedies: list[str] = []
    for (hyp_id, sha), cited in subjects.items():
        beside = source_file(ws, hyp_id, sha)
        if (hyp_id, sha) in held or (beside.is_file() and _sha(beside) == sha):
            continue
        relative = beside.relative_to(ws.root)
        where = f"{relative} holds other bytes" if beside.is_file() else f"no {relative}"
        copy = _copy_of(ws, hyp_id, sha)
        found = f" · bytes held by {copy}" if copy else ""
        items.append(f"{hyp_id} {sha[:7]}: {', '.join(cited)} · no blob in state · {where}{found}")
        restore = (
            f"copy {copy} to {relative}"
            if copy
            else f"restore {relative} from a copy of the workspace"
        )
        remedies.append(f"{restore}, or certify anew with `kanso cert run {hyp_id}`")
    detail = f"{counted[0]} certificate(s) · {counted[1]} version(s) · {len(subjects)} subject(s)"
    if not items:
        return Check("certificates", "ok", f"{detail} · every one held")
    return Check(
        "certificates",
        "fail",
        f"{detail}; {len(items)} whose bytes are held nowhere",
        items=tuple(items),
        remedy="; ".join(remedies),
    )


def _record(ws: Workspace, unread: Unread | None) -> Check:
    """Certified work on disk the record has no memory of.

    A repository carries the files — hypotheses, certificates, composed versions — and not
    `state.db`, which is gitignored by design. So a clone holds certified work the record
    has never seen: no history, no `best`, no trial count, no approvals, no version or
    session index. That is a fresh workspace that inherits certified files, and this check
    says so once, rather than leaving a reader to discover it one refusal at a time. A
    warning and never a failure: everything on disk is usable, and what did not travel is
    re-established by the commands named — approvals deliberately not among them, since real
    capital always needs a person to say so again, on the machine that will trade.
    """
    if unread is not None:
        return unread.check("record")
    on_disk: dict[str, int] = {}
    root = ws.path("certificates")
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            files = list(directory.glob("*-p*-e*.yaml")) if directory.is_dir() else []
            if files:
                on_disk[directory.name] = len(files)
    versions = {file.id: len(file.versions) for file in composed_strategies(ws)}
    with StateStore(ws.path(STATE_DB)) as store:
        registered = {
            str(row["hyp_id"]) for row in store.connection.execute("SELECT hyp_id FROM hypotheses")
        }
        certified = {
            str(row["hyp_id"])
            for row in store.connection.execute("SELECT DISTINCT hyp_id FROM certificates")
        }
        composed = {
            str(row["strategy_id"])
            for row in store.connection.execute(
                "SELECT DISTINCT strategy_id FROM strategy_versions"
            )
        }
    items: list[str] = []
    for hyp_id, count in on_disk.items():
        if hyp_id not in certified:
            known = "" if hyp_id in registered else " · not registered"
            items.append(f"{hyp_id}: {count} certificate(s) on disk, none in the record{known}")
    for strategy_id, count in versions.items():
        if strategy_id not in composed:
            items.append(f"{strategy_id}: {count} version(s) on disk, none in the record")
    detail = f"{len(on_disk)} certified subject(s) · {len(versions)} strategy(ies)"
    if not items:
        return Check("record", "ok", f"{detail} · the record knows every one")
    return Check(
        "record",
        "warn",
        f"{detail}; {len(items)} the record has no memory of — a clone, or a removed state.db",
        items=tuple(items),
        remedy=(
            "this workspace inherits the certified files and nothing else: history, best, "
            "trial counts, approvals and the version index did not travel. "
            "`kanso hyp add hypotheses/<id>/hypothesis.yaml` re-registers a hypothesis from "
            "its committed best-so-far; a certificate on disk stays the certificate of record "
            "and a repeat is refused; a promotion to real capital is asked again with "
            "`kanso promote <strategy> --live --as NAME`, by a person, here"
        ),
    )


def _certified_subjects(
    store: StateStore,
) -> tuple[dict[tuple[str, str], list[str]], tuple[int, int]]:
    """Every `(hyp_id, strategy_sha)` a certificate or a version cites, and who cites it."""
    subjects: dict[tuple[str, str], list[str]] = {}
    certificates = store.connection.execute(
        "SELECT hyp_id, strategy_sha, path FROM certificates ORDER BY hyp_id, created_at, rowid"
    ).fetchall()
    for row in certificates:
        key = (str(row["hyp_id"]), str(row["strategy_sha"]))
        subjects.setdefault(key, []).append(f"certificate {row['path']}")
    versions = store.connection.execute(
        "SELECT strategy_id, version, sleeve, attached FROM strategy_versions"
        " ORDER BY strategy_id, version"
    ).fetchall()
    for row in versions:
        name = f"{row['strategy_id']}@{row['version']}"
        sleeve = json.loads(str(row["sleeve"]))
        subjects.setdefault((str(sleeve["hyp_id"]), str(sleeve["strategy_sha"])), []).append(name)
        for attached in json.loads(str(row["attached"])):
            key = (str(attached["hyp_id"]), str(attached["strategy_sha"]))
            subjects.setdefault(key, []).append(f"{name} {attached['construct']}")
    return subjects, (len(certificates), len(versions))


def _copy_of(ws: Workspace, hyp_id: str, sha: str) -> str | None:
    """A file in the workspace still holding these bytes, relative to the root, or `None`.

    The best-so-far file and the verbatim copies under `impl/` are where the same bytes
    are written on purpose, so they are the places to look before telling an operator
    the subject is gone.
    """
    candidates = [
        hypothesis_dir(ws, hyp_id) / STRATEGY_FILE,
        *sorted(ws.path(STRATEGIES).glob(f"*/{IMPL}/*/*.py")),
    ]
    for candidate in candidates:
        if candidate.is_file() and _sha(candidate) == sha:
            return str(candidate.relative_to(ws.root))
    return None


def _instruments(ws: Workspace, unread: Unread | None) -> Check:
    """The instrument registry: its entries, the universes it must resolve, and drift.

    Three things. Every manual entry and every override in `instruments.yaml` is listed,
    because those are the operator's own assertions about a definition. Every id in the
    universe of every registered hypothesis is resolved as of that hypothesis's research
    start, the read-only way: a manual entry is built in memory, a resolved entry is
    looked up in the store, and an id that would need the reference adapter is reported
    rather than fetched, since `doctor` makes no network call. And the definitions the
    store holds are compared with what the newest snapshot pinned, because a run pinned
    to that snapshot resolves against the store, and a resolution since the snapshot was
    taken means the run and its pin disagree about the instruments. That comparison is
    `kanso.data.snapshot.instrument_drift`, the one `research begin` pins a run by, asked
    here rather than remade, so the two cannot disagree about whether the store moved.

    An id that cannot resolve at all fails, since registering, classifying and planning
    the hypothesis all stop on it; an id doctor could not verify, and a store the newest
    snapshot no longer describes, warn.
    """
    path = ws.path(CACHE_NAME)
    file = load_yaml(InstrumentsFile, path) if path.is_file() else InstrumentsFile({})
    held = read_store(ws) if catalog_path(ws).is_dir() else {}
    items = [_entry_item(key, entry) for key, entry in file.root.items() if entry.override]
    manual = sum(1 for entry in file.root.values() if entry.manual)
    overridden = len(items)
    failed: list[str] = []
    unverified: list[str] = []
    remedies: list[str] = []
    universes = 0
    hypotheses = 0

    if unread is not None:
        items.append(f"universes not checked: {unread.reason}")
    else:
        with StateStore(ws.path(STATE_DB)) as store:
            pinned = _pinned_hypotheses(store)
        for hyp in pinned:
            hypotheses += 1
            as_of = hyp.windows.research.start
            for wanted, (status, outcome) in _resolvable(
                ws, file, held, hyp.universe, as_of
            ).items():
                universes += 1
                items.append(f"{hyp.id} {wanted}: {outcome} · as of {as_of}")
                if status == "fail":
                    failed.append(wanted)
                elif status == "warn":
                    unverified.append(wanted)
    if failed:
        remedies.append(
            f"correct {', '.join(sorted(set(failed)))} in {CACHE_NAME}, then run "
            "`kanso data instruments resolve`"
        )
    if unverified:
        remedies.append(
            f"run `kanso data instruments resolve {' '.join(sorted(set(unverified)))}` to "
            "resolve them"
        )

    # A snapshot on disk means `catalog/` exists, so asking the store creates nothing.
    latest = newest(ws)
    drift = instrument_drift(ws, latest) if latest is not None else None
    detail = (
        f"{len(file)} entr{'y' if len(file) == 1 else 'ies'} · {manual} manual · "
        f"{overridden} overridden · {universes} universe id(s) across {hypotheses} "
        "hypothesis(es)"
    )
    drifted = drift is not None
    if latest is None:
        detail += " · no snapshot yet"
    elif drift is not None:
        items.append(
            f"store {drift.held[:12]} · newest snapshot {drift.snapshot_id[:7]} pinned "
            f"{drift.pinned[:12]}"
        )
        remedies.append("run `kanso data snapshot` to pin the definitions the store holds now")
    else:
        detail += " · the store matches the newest snapshot"

    if failed:
        return Check(
            "instruments",
            "fail",
            f"{detail}; {len(failed)} id(s) do not resolve",
            items=tuple(items),
            remedy="; ".join(remedies),
        )
    if unverified or drifted or (unread is not None and unread.status == "warn"):
        reasons = [
            *([f"{len(unverified)} id(s) not verified"] if unverified else []),
            *(["the store differs from what the newest snapshot pinned"] if drifted else []),
            *(["universes not checked"] if unread is not None and unread.status == "warn" else []),
        ]
        remedy = "; ".join([*remedies, *([unread.remedy] if unread and unread.remedy else [])])
        return Check(
            "instruments",
            "warn",
            f"{detail}; {' · '.join(reasons)}",
            items=tuple(items),
            remedy=remedy or None,
        )
    return Check("instruments", "ok", detail, items=tuple(items))


def _entry_item(key: str, entry: InstrumentEntry) -> str:
    """One entry of the registry file: what the operator asserted, and its provenance."""
    fields = ", ".join(sorted(entry.override))
    if entry.manual:
        return f"{key}: manual · override {fields}"
    if entry.resolved is None:
        return f"{key}: override {fields} · unresolved"
    return (
        f"{key}: override {fields} · resolved by {entry.resolved.adapter} as of "
        f"{entry.resolved.as_of}"
    )


def _pinned_hypotheses(store: StateStore) -> list[Hypothesis]:
    """Every registered, unretired hypothesis as the document it is pinned to."""
    rows = store.connection.execute(
        "SELECT hyp_id, hypothesis_sha FROM hypotheses WHERE hypothesis_sha IS NOT NULL"
        " AND status != 'retired' ORDER BY hyp_id"
    ).fetchall()
    return [
        parse_yaml(
            Hypothesis,
            store.get_blob(str(row["hypothesis_sha"])).decode("utf-8"),
            f"{row['hyp_id']}/{HYPOTHESIS_FILE}",
        )
        for row in rows
    ]


def _resolvable(
    ws: Workspace,
    file: InstrumentsFile,
    held: Mapping[str, object],
    ids: Iterable[str],
    as_of: date,
) -> dict[str, tuple[Status, str]]:
    """How each id would resolve as of `as_of` without a vendor, graded.

    `ok` when a manual entry builds or a resolved entry's definition is in the store for
    exactly this date; `warn` when the reference adapter would be asked, which doctor
    does not do; `fail` when resolution would refuse — an unknown, ambiguous or dated-out
    id, a manual entry the engine rejects, or an adapter call with no adapter configured.
    """
    provider = ManualProvider(file)
    reference = ws.config.data.reference
    out: dict[str, tuple[Status, str]] = {}
    for wanted in dict.fromkeys(ids):
        try:
            outcome = provider.resolve([wanted], as_of)[wanted]
        except ValidationError as error:
            out[wanted] = ("fail", error.message)
            continue
        if not isinstance(outcome, ResolveError):
            out[wanted] = ("ok", "manual, built")
            continue
        entry = _lookup(file, wanted)
        if isinstance(entry, ResolveError) or entry.manual:
            out[wanted] = ("fail", outcome.reason)
        elif (
            entry.resolved is not None
            and entry.resolved.as_of == as_of
            and entry.resolved.checksum in held
        ):
            out[wanted] = ("ok", f"resolved by {entry.resolved.adapter}, in the store")
        elif reference == "none":
            out[wanted] = (
                "fail",
                "not in the store for this date, and no reference adapter is configured "
                "([data] reference)",
            )
        else:
            out[wanted] = ("warn", f"would resolve through {reference}, which doctor does not call")
    return out


def _lanes(ws: Workspace, unread: Unread | None) -> Check:
    """Every open run against its lane directory, and every lane directory against the runs.

    A lane directory is `runs/<lane>/<hyp>/`: exactly the three scoped files as the run
    pinned them, existing for as long as the run is open and holding nothing that is not
    a blob in state. So an open run whose directory is gone cannot take a card and fails;
    a directory with no open run behind it is left over from a run this store does not
    know — the store was replaced, or a copy of the workspace brought `runs/` along —
    and warns; and a directory departing from its three files is what the next card's
    `strategy_integrity` refuses, and warns before the card does.
    """
    if unread is not None:
        return unread.check("lanes")
    with StateStore(ws.path(STATE_DB)) as store:
        open_runs = active_runs(store)
    directories = _lane_directories(ws)
    items: list[str] = []
    remedies: list[str] = []
    gone = 0
    for run in open_runs:
        directory = ws.root / run.dir
        if not directory.is_dir():
            gone += 1
            items.append(f"{run.lane} {run.hyp_id}: {run.dir} is gone")
            remedies.append(
                f"run `kanso research end {run.hyp_id}`; the run's cards and blobs are in state"
            )
            continue
        problems = _lane_problems(directory, run)
        if problems:
            items.append(f"{run.lane} {run.hyp_id}: {'; '.join(problems)}")
            remedies.append(
                f"run `kanso research end {run.hyp_id}` and begin again; a fresh lane holds "
                "the three files as pinned, and the run's cards and best stay in state"
            )
        else:
            items.append(f"{run.lane} {run.hyp_id}: {run.dir} holds the three scoped files")
    open_dirs = {run.dir for run in open_runs}
    orphans = [directory for directory in directories if directory not in open_dirs]
    for orphan in orphans:
        items.append(f"{orphan}: no open run behind it")
        remedies.append(f"`rm -r {orphan}`; a lane directory holds only copies of blobs in state")
    plural = "y" if len(directories) == 1 else "ies"
    detail = f"{len(open_runs)} open run(s) · {len(directories)} lane director{plural}"
    if gone:
        return Check(
            "lanes",
            "fail",
            f"{detail}; {gone} open run(s) whose directory is gone",
            items=tuple(items),
            remedy="; ".join(remedies),
        )
    if remedies:
        return Check(
            "lanes",
            "warn",
            f"{detail}; {len(remedies)} to look at",
            items=tuple(items),
            remedy="; ".join(remedies),
        )
    return Check("lanes", "ok", detail, items=tuple(items))


def _lane_directories(ws: Workspace) -> list[str]:
    """Every `runs/<lane>/<hyp>/` directory, relative to the root, in path order.

    Files beside the lanes — the daemon's log and pid, a run's logs — are not lane
    directories, and neither is a lane root with nothing in it.
    """
    root = ws.path(LANE_ROOT)
    if not root.is_dir():
        return []
    return [
        str(hyp.relative_to(ws.root))
        for lane in sorted(root.iterdir())
        if lane.is_dir() and not lane.name.startswith(".")
        for hyp in sorted(lane.iterdir())
        if hyp.is_dir() and not hyp.name.startswith(".")
    ]


def _lane_problems(directory: Path, run: RunRecord) -> list[str]:
    """How this lane directory departs from the three files the run pinned."""
    pinned = {HYPOTHESIS_FILE: run.hypothesis_sha, PROGRAM_FILE: run.program_sha}
    return lane_scope(directory, pinned)
