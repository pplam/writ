import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from writ.cli import main

DESIGN = """\
# Sample Project

Intro prose that is not a milestone.

## Milestone 0 — Foundations

Set up the harness.

**Pass:** the project builds; tests run; malformed input fails deterministically.

## Milestone 1 — Storage

### Event log

Append-only writes.

**Gate:** appends are atomic. Replay is byte-identical.

### Projection

Inline projection.

**Acceptance:** projection matches replay.

## Milestone 2 — Interface

No explicit gate here, so generic criteria apply.
"""


@pytest.fixture
def design(tmp_path: Path) -> Path:
    path = tmp_path / "design.md"
    path.write_text(DESIGN, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def writ(project: Path):
    def invoke(*args: str) -> tuple[int, str, str]:
        return run("--root", str(project), *args)

    return invoke


@pytest.fixture
def planned(writ, design: Path):
    writ("init")
    writ("plan", str(design))
    return writ


def python_agent(script: str) -> list[str]:
    """An agent command that is deterministic and offline."""
    return ["--agent", f"{sys.executable} -c {script!r}"]
