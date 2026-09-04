"""The workspace diagnosis: what `doctor` checks, and how each check is graded.

Every check answers one question about this workspace on this host and grades itself
`ok`, `warn` or `fail`. A workspace is green when no check fails: a warning is something
the operator should see and may act on, a failure is something that will stop a later
command. The command exits 0 when green — warnings printed — and 2 when anything fails,
so an operator agent branches on the code and reads the detail.

Nothing here writes to the workspace and nothing reaches the network. The checks that
touch the state database open it read-only in effect (they migrate nothing), the
envelope is re-detected in memory and never written, and the adapter check is a
statement about what this build ships rather than a request to a vendor.

Credentials are reported as names and origins. A value is never read into a message,
because `doctor --report` is the block an operator pastes into an issue: that mode also
replaces the workspace, repository and home paths with placeholders, so a report carries
the diagnosis and nothing about the machine it came from.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from kanso import __version__, creds, env, ext, skills_sync
from kanso.env import envelope as envelope_module
from kanso.errors import KansoError
from kanso.nautilus import facts
from kanso.schemas.models import ModelsFile
from kanso.schemas.yamlio import load_yaml
from kanso.state import StateStore, migrations
from kanso.workspace import Workspace, gitignore_entries, in_git_repo

Status = Literal["ok", "warn", "fail"]

STALE_DAYS = 7
"""An envelope older than this is worth re-detecting."""

HARDWARE = ("os", "os_version", "arch", "chip", "cores_total", "mem_gb")
"""The detected fields whose change invalidates an envelope; the rest fluctuate."""

BUILTIN_IDS: dict[str, tuple[str, ...]] = {}
"""The ids an extension would shadow, per kind.

Empty in this build: the gate, objective, construct, loader and adapter registries land
with the milestones that introduce them, and each fills its kind here. Discovery already
reports what an extension declares, so a shadowing extension is visible the moment there
is something to shadow.
"""


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
            ("skills", lambda: _skills(ws)),
            ("credentials", lambda: _credentials(ws)),
            ("adapters", lambda: _adapters(ws, check_adapters)),
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
    """The version stamped on `state.db`, and the migrations it has not been given."""
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
    now = env.detect().detected
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
    name = skill.name
    if not name.startswith(skills_sync.LINK_PREFIX):
        name = f"{skills_sync.LINK_PREFIX}{name}"
    link = directory / name
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
    """What `[adapters.<id>]` configures, and what this build can do about it."""
    configured = sorted(ws.config.adapters)
    detail = "no adapter ships in this build"
    if configured:
        detail += f"; kanso.toml configures {', '.join(configured)}"
    if check_adapters:
        detail += "; --check-adapters had nothing to check and made no network call"
    return Check("adapters", "ok", detail)


def _extensions(ws: Workspace) -> Check:
    """The workspace packages that loaded, those that did not, and what they shadow."""
    found = ext.discover(ws.root, ws.config.extensions_paths)
    if not found:
        return Check("extensions", "ok", "none")
    items = [
        f"{extension.name}: {extension.error}" if extension.error else f"{extension.name}: ok"
        for extension in found
    ]
    shadowing = ext.shadows(found, BUILTIN_IDS)
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
            remedy="fix or remove the extension; a shadowed id is resolved to neither by default",
        )
    return Check("extensions", "ok", detail, items=tuple(items))


def _engine_facts() -> Check:
    """The engine claims kanso binds to, re-established against the installed package.

    A claim that does not hold is a design constraint kanso is built around, not a
    defect, so the count is reported and the grade follows the engine version: facts
    verified against another version may no longer describe this one.
    """
    verified = facts.verify()
    gaps = [fact.claim for fact in verified if not fact.holds]
    installed = envelope_module.engine_version()
    detail = (
        f"{len(verified) - len(gaps)}/{len(verified)} claims hold on nautilus_trader {installed}"
    )
    items = tuple(f"does not hold: {claim}" for claim in gaps)
    if installed == facts.ENGINE_VERSION:
        return Check("engine facts", "ok", detail, items=items)
    return Check(
        "engine facts",
        "warn",
        f"{detail}; the facts were verified against {facts.ENGINE_VERSION}",
        items=items,
        remedy="re-verify the engine facts against this version before trusting a card",
    )
