"""Where a certificate lives, what makes writing one a repeat, and how one is read back.

A certificate is immutable, so this module is deliberately poor in verbs: it writes one
and it reads them; it never updates one. Everything that could make a second write
contradict a first is refused before anything runs — `refuse_repeat` is called with the
four facts that identify a certification and nothing else, so a certification that will
not be allowed to land costs no backtest.

**Three refusals, one meaning.** The state store's primary key is the subject, the plan
version and the engine version together, which is exactly what immutability forbids
repeating: the same code, judged by the same plan, on the same engine. Moving to
a new engine changes the third of those, so re-certifying an unchanged commit under a new
engine is a plain certification that writes a new file beside the old one rather than an
impossibility. The second refusal is the file at the exact target name, which is never
overwritten whatever the store believes, because the file is what an operator and their
agent read. The third is the certificate on disk under any trial count: a workspace whose
`state.db` never travelled — a fresh clone of a research repository — has no row to consult,
but it does have the certificate files, and a certificate of the same subject, plan and
engine already among them is the same immutability. The filename carries the trial count,
so keying only on the exact name would let a clone re-certifying from trial one write
`<sha7>-1-…` beside a committed `<sha7>-4-…`: the same bytes under the same plan and engine
with the count laundered. This refusal therefore matches the subject, the plan and the
engine, ignores the trial count, and names the file it found.

**The bytes travel with the record.** The certified `strategy.py` is written beside the
certificate as `<sha7>.py`, so a certified subject is legible from the files alone, in a
workspace whose state store was lost or was never copied. Writing those bytes over a
different file is refused rather than done quietly: two subjects sharing a seven-character
prefix are rare, and silently replacing one with the other would be worse than saying so.

The certificate document is written to a file *and* recorded as a row, and the row carries
every field the document does. Reading goes to the row, so `cert show`, the proposer's
view of failing gates and the scheduler's "what was certified last" all answer from one
place and keep answering when a file has been moved. The directory those files live in is
the one the plan is pinned in, and is named there.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from kanso.certify.plan import certificates_dir
from kanso.errors import PreconditionError
from kanso.schemas import Certificate, write_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "certificate_file",
    "filename",
    "latest",
    "of",
    "refuse_repeat",
    "source_file",
    "write",
]

_COLUMNS: Final = (
    "hyp_id",
    "strategy_sha",
    "plan_version",
    "nautilus_version",
    "venue_model",
    "snapshot_id",
    "criteria_version",
    "construct",
    "objective",
    "gates",
    "n_trials",
    "verdict",
    "path",
    "created_at",
)

_INSERT: Final = (
    f"INSERT INTO certificates ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"
)


def filename(strategy_sha: str, n_trials: int, plan_version: int, nautilus_version: str) -> str:
    """`<sha7>-<n_trials>-p<plan_version>-e<nautilus_version>.yaml`.

    Spelled from the four facts rather than from a certificate, so the target can be
    checked for existence before the certification that would fill it is run.
    """
    return f"{strategy_sha[:7]}-{n_trials}-p{plan_version}-e{nautilus_version}.yaml"


def certificate_file(ws: Workspace, certificate: Certificate) -> Path:
    """Where one certificate is written."""
    return certificates_dir(ws, certificate.hyp_id) / certificate.filename()


def source_file(ws: Workspace, hyp_id: str, strategy_sha: str) -> Path:
    """Where the certified `strategy.py` bytes are written, beside the certificate."""
    return certificates_dir(ws, hyp_id) / f"{strategy_sha[:7]}.py"


def refuse_repeat(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    strategy_sha: str,
    n_trials: int,
    plan_version: int,
    nautilus_version: str,
) -> None:
    """Refuse a certification that would contradict one already recorded or on disk."""
    held = store.connection.execute(
        "SELECT created_at FROM certificates WHERE hyp_id = ? AND strategy_sha = ?"
        " AND plan_version = ? AND nautilus_version = ?",
        (hyp_id, strategy_sha, plan_version, nautilus_version),
    ).fetchone()
    if held is not None:
        raise PreconditionError(
            f"{hyp_id} already certified {strategy_sha[:7]} under plan version {plan_version} "
            f"and nautilus_trader {nautilus_version} on {held['created_at']}; a certificate is "
            "immutable",
            remedy="research a better strategy, replan, or upgrade the engine",
        )
    directory = certificates_dir(ws, hyp_id)
    target = directory / filename(strategy_sha, n_trials, plan_version, nautilus_version)
    if target.exists():
        raise PreconditionError(
            f"{target} already exists, and a certificate is never overwritten",
            remedy="move the existing file aside if it does not belong to this workspace",
        )
    on_disk = _certified_on_disk(directory, strategy_sha, plan_version, nautilus_version)
    if on_disk is not None:
        raise PreconditionError(
            f"{on_disk.name} certifies {strategy_sha[:7]} under plan version {plan_version} "
            f"and nautilus_trader {nautilus_version}; a certificate is immutable, and this "
            "workspace's state store does not record it — this is a clone whose record did "
            "not travel",
            remedy="research a better strategy, replan, or upgrade the engine",
        )


def _certified_on_disk(
    directory: Path, strategy_sha: str, plan_version: int, nautilus_version: str
) -> Path | None:
    """A certificate file of this subject, plan and engine, whatever its trial count.

    The trial count is the one part of the filename that varies for the same certified
    bytes, so it is matched with a wildcard: any `<sha7>-*-p<plan>-e<engine>.yaml` present
    is the immutable certificate a repeat would contradict. A directory that does not exist
    yet globs to nothing, so no separate guard is needed for it.
    """
    pattern = f"{strategy_sha[:7]}-*-p{plan_version}-e{nautilus_version}.yaml"
    matches = sorted(path for path in directory.glob(pattern) if path.is_file())
    return matches[0] if matches else None


def write(ws: Workspace, store: StateStore, certificate: Certificate, source: bytes) -> Path:
    """Write the certificate, the certified bytes beside it, and the row that indexes both."""
    directory = certificates_dir(ws, certificate.hyp_id)
    directory.mkdir(parents=True, exist_ok=True)
    beside = source_file(ws, certificate.hyp_id, certificate.strategy_sha)
    if beside.exists() and beside.read_bytes() != source:
        raise PreconditionError(
            f"{beside} holds different bytes than the strategy being certified; two subjects "
            f"share the prefix {certificate.sha7}",
            remedy="certify under a workspace whose certificates directory is not shared",
        )
    beside.write_bytes(source)
    path = write_yaml(certificate, certificate_file(ws, certificate))
    store.connection.execute(
        _INSERT,
        (
            certificate.hyp_id,
            certificate.strategy_sha,
            certificate.plan_version,
            certificate.nautilus_version,
            _dump(certificate.venue_model.model_dump(mode="json")),
            certificate.snapshot_id,
            certificate.criteria_version,
            _dump(certificate.construct.model_dump(mode="json")),
            _dump(certificate.objective.model_dump(mode="json")),
            _dump([gate.model_dump(mode="json", by_alias=True) for gate in certificate.gates]),
            certificate.n_trials,
            certificate.verdict,
            str(path.relative_to(ws.root)),
            certificate.created_at.isoformat(),
        ),
    )
    return path


def latest(store: StateStore, hyp_id: str) -> Certificate | None:
    """This hypothesis's newest certificate, whatever its verdict."""
    found = of(store, hyp_id, limit=1)
    return found[0] if found else None


def of(store: StateStore, hyp_id: str, limit: int | None = None) -> list[Certificate]:
    """This hypothesis's certificates, newest first."""
    rows = store.connection.execute(
        "SELECT * FROM certificates WHERE hyp_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (hyp_id, -1 if limit is None else limit),
    ).fetchall()
    return [_certificate(row) for row in rows]


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _certificate(row: Any) -> Certificate:
    """One row as the document it was written from."""
    return Certificate.model_validate(
        {
            "hyp_id": row["hyp_id"],
            "strategy_sha": row["strategy_sha"],
            "nautilus_version": row["nautilus_version"],
            "venue_model": json.loads(str(row["venue_model"])),
            "snapshot_id": row["snapshot_id"],
            "criteria_version": row["criteria_version"],
            "plan_version": row["plan_version"],
            "construct": json.loads(str(row["construct"])),
            "objective": json.loads(str(row["objective"])),
            "gates": json.loads(str(row["gates"])),
            "n_trials": row["n_trials"],
            "verdict": row["verdict"],
            "created_at": row["created_at"],
        }
    )
