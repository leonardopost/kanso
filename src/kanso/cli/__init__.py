"""The kanso command line.

`app` is the typer application the `kanso` console script runs. It is the only layer of
the package that prints: every other module raises a `KansoError` carrying the exit code,
and this package renders it — as one line for a human, as one JSON object under `--json`.
"""

from __future__ import annotations

from kanso.cli.main import app

__all__ = ["app"]
