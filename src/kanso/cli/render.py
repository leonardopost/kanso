"""Turning a command's result into output and an exit code.

The command line is the only layer of kanso that prints. Every command computes a
`Report` — the object `--json` emits and the lines a human reads — or raises a
`KansoError`; this module renders exactly one of the two and exits with the code the
result or the failure carries. Under `--json` that is exactly one object on standard
output, the result on success and the error envelope on failure, so a caller parses one
document and branches on the exit code without scanning for a prefix.

An unexpected exception is rendered the same way, as the generic error with exit 1,
because a caller reading `--json` must never get a traceback where an object was
promised.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import click
import typer

from kanso.errors import Exit, KansoError

LABEL = 11
"""Width of the label column of the human two-column layout."""


@dataclass(frozen=True)
class Report:
    """What a command produced: the `--json` object, the human lines, the exit code."""

    data: dict[str, object]
    lines: tuple[str, ...] = ()
    code: Exit = Exit.OK


def emit(as_json: bool, work: Callable[[], Report]) -> None:
    """Run `work`, print its report or the failure envelope, and exit with the code."""
    try:
        report = work()
    except KansoError as error:
        _render_error(error, as_json)
        raise typer.Exit(int(error.code)) from None
    except Exception as error:
        _render_error(KansoError(f"{type(error).__name__}: {error}"), as_json)
        raise typer.Exit(int(Exit.ERROR)) from None
    _render(report, as_json)
    raise typer.Exit(int(report.code))


def field(label: str, value: object) -> str:
    """One line of the human layout: a short label, then the value."""
    return f"{label:<{LABEL}}{value}"


def indent(text: str, width: int = LABEL) -> str:
    """A continuation line, aligned under the value column."""
    return f"{'':<{width}}{text}"


def _render(report: Report, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(report.data, indent=2, ensure_ascii=False, default=str))
        return
    for line in report.lines:
        click.echo(line)


def _render_error(error: KansoError, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(error.payload(), indent=2, ensure_ascii=False, default=str))
        return
    click.echo(f"error: {error.message}", err=True)
    if error.remedy:
        click.echo(f"remedy: {error.remedy}", err=True)
