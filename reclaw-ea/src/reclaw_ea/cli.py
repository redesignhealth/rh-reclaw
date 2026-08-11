"""Console-script entry point for `reclaw-ea` (`pyproject.toml`'s
`[project.scripts]`).

Stub only, for now (Argus round 1 finding: `pyproject.toml` declared
`reclaw-ea = "reclaw_ea.cli:main"` with no `cli.py` at all -- `pip install
-e .`/`uv sync` succeed silently and the entry point raises
`ModuleNotFoundError` at invocation, uncaught by anything in the test
suite). No CLI surface is designed yet -- `reclaw_ea` is a library
(`docs/DESIGN.md` §1a: it will be wrapped as `reclaw-ea-mcp`, an MCP
server, not driven from a command line) -- this exists purely so the
declared entry point resolves to something rather than nothing.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "reclaw-ea has no CLI surface yet -- it is a library wired into "
        "the reclaw-ea-mcp server (docs/DESIGN.md TECH-5065), not a "
        "standalone command-line tool.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
