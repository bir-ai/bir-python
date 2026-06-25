"""Module entry point so ``python -m bir`` runs the same CLI as the script.

The ``bir`` console script (see ``[project.scripts]`` in ``pyproject.toml``) is
the usual way to invoke the CLI, but it is not always on ``PATH`` — fresh venvs,
``pipx run``, and some CI setups. ``python -m bir`` is the dependency-free
fallback, and dispatching to the same :func:`bir.cli.main` keeps both paths
byte-for-byte identical, exit code included.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
