"""kanso — a minimal, agent-first quantitative research workbench on NautilusTrader.

kanso is a Python package. It scaffolds an operator workspace and writes files and
`state.db` inside it, and it never invokes git: committing a workspace is the operator's
business. Versioning of the files the research loop mutates is kanso's own, content
addressed in the state store.
"""

__version__ = "0.1.1"

__all__ = ["__version__"]
