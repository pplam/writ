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


#: the flags that reproduce writ's older planning contract.
#:
#: `--chain` puts the implicit milestone-to-milestone edges back, and `--no-gates`
#: leaves the plan-level checks out. Tests about graph shape, dispatch order and
#: the read model want a graph with depth and nothing else in it; naming the flags
#: here means the reason appears once instead of in forty call sites.
LEGACY_PLAN = ("--extract", "--chain", "--no-gates", "--auto-approve")


@pytest.fixture
def planned(writ, design: Path):
    """A plan under writ's older contract: chained milestones, no gates.

    Pinned deliberately. Most tests that use this fixture are about something
    else — graph rendering, dispatch, the API — and they need *a* graph with
    depth in it, not today's defaults. Both flags are still supported paths, so
    keeping the fixture here keeps them covered while the tests that are actually
    about the current defaults say so themselves (`approved`, below, and
    tests/test_gates.py).
    """
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    return writ


@pytest.fixture
def approved(writ, design: Path):
    """A plan as `writ plan` builds one now: stated edges only, gates installed.

    Approved explicitly, because a clean check no longer approves itself. The
    fixture is named for the state it produces, and `--auto-approve` is what
    produces it now — the flag automation uses for exactly this reason.
    """
    writ("init")
    writ("plan", str(design), "--extract", "--auto-approve")
    return writ


def python_agent(script: str) -> list[str]:
    """An agent command that is deterministic and offline."""
    return ["--agent", f"{sys.executable} -c {script!r}"]
