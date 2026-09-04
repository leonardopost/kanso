"""The entry point every child of the daemon is started through.

A child is started as `python -m kanso.research <serve|lane|monitor> <workspace> [lane]`,
and this module is what that runs. It exists so that the module `runpy` executes as
`__main__` is one the package does not import: naming the daemon module itself would
execute it a second time under a second name, giving the child two copies of the stop flag
and the signal handlers that read it — which is what the interpreter's own warning about
running an already-imported module is about.
"""

from __future__ import annotations

import sys

from kanso.research.daemon import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
