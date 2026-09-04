"""Exit codes and the exception type that carries them.

Every command failure raises `KansoError`; the CLI turns it into the exit code and,
under `--json`, into a one-object error envelope. The codes are a contract with the
operator agent, which branches on them.
"""

from __future__ import annotations

from enum import IntEnum


class Exit(IntEnum):
    """Process exit codes."""

    OK = 0
    ERROR = 1
    PRECONDITION = 2
    VALIDATION = 3
    APPROVAL = 4


class KansoError(Exception):
    """A failure with an exit code, a message and an optional remedy.

    `PRECONDITION` means workspace or engine state forbids the action; `VALIDATION`
    means operator-authored input is malformed or semantically wrong; `APPROVAL` means
    a named operator approval is required and absent; `ERROR` is an unexpected fault.
    """

    def __init__(self, message: str, code: Exit = Exit.ERROR, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.remedy = remedy

    def payload(self) -> dict[str, object]:
        """The `--json` error envelope."""
        out: dict[str, object] = {"error": self.message, "code": int(self.code)}
        if self.remedy:
            out["remedy"] = self.remedy
        return out


class PreconditionError(KansoError):
    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, Exit.PRECONDITION, remedy)


class ValidationError(KansoError):
    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, Exit.VALIDATION, remedy)


class ApprovalError(KansoError):
    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, Exit.APPROVAL, remedy)
