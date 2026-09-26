"""Command implementations. Each takes parsed args and prints a result."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import (
    adjudicate,
    agents,
    analysis,
    config,
    contracts,
    critics,
    decisions,
    gates,
    orchestrator,
    phases,
    planfiles,
    plancheck,
    planner,
    planning,
    plans,
    render,
    repair,
    runner,
    server,
    state,
    verdict,
)
from .model import (
    acceptance_summary,
    add_evidence,
    add_milestone,
    add_task,
    check_dag,
    blocked_on,
    blocking_dependencies,
    effective_status,
    find,
    get_task,
    milestone_tasks,
    ready_tasks,
    refresh_milestones,
    reviewable_tasks,
    set_acceptance,
    set_status,
    SETTABLE_STATUSES,
)
from .state import WritError


# --------------------------------------------------------------------------
# project setup


def cmd_init(args) -> None:
    location = state.initialize(args.root, force=args.force)
    print(f"initialized Writ project at {location}")
    # Written here rather than in `state.initialize` because it is not state: it
    # is a file the project owns, and the store can be reset without it.
    path, outcome = config.ensure(args.root)
    if outcome == "created":
        print(f"wrote {path.name} — writ's defaults, with a comment on each")
    elif outcome == "converted":
        print(
            f"wrote {path.name} from your {config.LEGACY_FILENAME}, which writ no"
            " longer reads; delete it once you have checked the new file"
        )
    else:
        print(f"kept your existing {path.name}")
    print("next: writ plan <design.md>")


def cmd_plan(args) -> int:
    """Turn a design document into a task DAG, and record the attempt as it runs.

    A wrapper around `_plan`, which is the command. Its whole job is the phase
    record: `writ/phases.py` is written to as planning happens rather than after,
    so `writ serve` has something to show during the one stretch of writ that used
    to write nothing to state at all.

    The `finally` is the point. `_plan` has half a dozen ways out — a deliberate
    stop at `--stage requirements`, a stage that failed, a synthesis with no plan,
    a `^C` in the middle of a critic — and every one of them has to close the
    record. A phase left `running` by a process that exited would have the
    dashboard reporting a plan in flight long after the terminal came back, which
    is worse than showing nothing: nothing does not claim to know.
    """
    held: dict[str, Any] = {}
    code = 1
    try:
        code = _plan(args, held)
    except BaseException as exc:
        # Includes `KeyboardInterrupt`: a `^C` is the most likely way a planning
        # run ends early, and it is exactly when a reader wants the record to say
        # where it stopped. The exception carries on to the CLI's own handler.
        phases.finish(
            Path(args.root),
            held.get("id"),
            status="failed",
            note=_first_line(str(exc)) or type(exc).__name__,
        )
        held["closed"] = True
        raise
    finally:
        if not held.get("closed"):
            phases.finish(
                Path(args.root),
                held.get("id"),
                status=held.get("status") or ("done" if code == 0 else "failed"),
                note=held.get("note", ""),
            )
    return code


def _plan(args, held: dict[str, Any]) -> int:
    """Turn a design document into a task DAG.

    By default this is a staged pipeline (`writ/analysis.py`): three analyses
    write artifacts — what the document requires, what the repository already is,
    how each requirement could be demonstrated — and a synthesis agent decomposes
    the work from all three. Writ then checks that the synthesizer honoured them
    (`analysis.reconcile`) as well as that the plan is structurally sound.

    `--no-stages` is the older single-shot planner, which makes every one of those
    judgements in one response. `--extract` uses the deterministic heading parser,
    and `--from-plan` re-imports a plan artifact without paying for an agent run.
    """
    root = Path(args.root)
    doc = _design_docs(args.design)
    names = planner.doc_names(doc)

    plan_path: Path | None = None
    #: the phase record's id, or None when there is nothing to record against.
    #: `--extract` and `--from-plan` run no agents before the commit, so there is
    #: no pre-execution phase to watch; every `phases.*` call is a no-op on None.
    phase: str | None = None
    #: set only on the staged path, and only once synthesis has produced a plan
    extra_findings: list[plancheck.Finding] = []
    pipeline_artifacts: analysis.Artifacts | None = None
    pipeline_results: list[analysis.Result] = []
    pipeline_id: str | None = None
    pipeline_directory: Path | None = None
    #: the plan directory an agent run wrote into, which the commit then keeps
    plan_id: str | None = None
    if args.from_plan:
        plan_path = Path(args.from_plan).expanduser()
        document = planning.read_document(plan_path)
        source = f"plan {plan_path.name}"
    elif args.extract:
        # Each document is parsed on its own, so a heading level means the same
        # thing in each, and their milestones follow in the order given.
        document = planning.PlanDocument(
            milestones=[
                milestone
                for path in doc
                for milestone in planner.parse(
                    path.read_text(encoding="utf-8"),
                    milestone_level=args.level,
                    split_subsections=not args.flat,
                )
            ]
        )
        source = f"{names} (extracted)"
    else:
        data = state.load(root)
        # check the overwrite gate before paying for an agent run, not after
        if data["tasks"] and not (args.append or args.force or args.dry_run):
            raise WritError(
                "this project already has tasks; use --append to add, "
                "or --force to replace the plan"
            )
        context = planning.plan_context(data)
        staged = bool(getattr(args, "stages", True))
        if args.dry_run:
            print(
                planning.build_prompt(
                    root=root.resolve(),
                    doc=doc,
                    plan_path=state.plan_dir(root, "<plan-id>") / planfiles.DRAFT_FILENAME,
                    instructions=args.instructions,
                    context=context,
                    artifacts=(
                        analysis.read_all(
                            state.plan_dir(root, getattr(args, "plan_id", None))
                        )
                        if staged and getattr(args, "plan_id", None)
                        else None
                    ),
                )
            )
            return 0

        plan_id = getattr(args, "plan_id", None) or planning.new_plan_id(doc)
        directory = state.plan_dir(root, plan_id)
        chosen_stages = (
            (analysis.upto(args.stage) if getattr(args, "stage", None) else list(analysis.STAGES))
            if staged
            else []
        )
        # Declared before anything runs, from the flags alone. This is what makes a
        # step visible as *pending*: the page shows the shape of the attempt — which
        # analyses, which critics, whether repair is armed — rather than boxes
        # appearing one at a time from nowhere. Only the steps that genuinely cannot
        # be foreseen are appended later: a repair round exists because the critics
        # objected, and a re-review is the critics reading a plan a patch changed.
        try:
            declared_critics = _chosen_critics(args) if _critics_requested(args) else []
        except WritError:
            # An unknown critic name is `_run_critics`'s error to raise, where it is
            # about to spend agent runs. Declaring must not be the thing that fails.
            declared_critics = []
        held["id"] = phases.begin(
            root,
            doc=", ".join(str(path) for path in doc),
            plan_id=plan_id,
            label=f"{names} ({'staged' if staged else 'single-shot'})",
            steps=phases.declare(
                stages=chosen_stages,
                critics=declared_critics,
                repair=bool(getattr(args, "repair", False)),
                auto_approve=bool(getattr(args, "auto_approve", False)),
            ),
        )
        phase = held["id"]
        artifacts = None
        if staged:
            artifacts, pipeline_results, stopped = _run_stages(
                args,
                root=root,
                doc=doc,
                plan_id=plan_id,
                context=context,
                stages=chosen_stages,
                phase=phase,
            )
            if stopped is not None:
                # Either a stage failed or `--stage` asked for the analyses alone.
                # `stopped == 0` is the second, which is writ doing as it was told
                # and is not a failed attempt.
                held["status"] = "stopped" if stopped == 0 else "failed"
                held["note"] = (
                    f"stopped after {args.stage}"
                    if stopped == 0
                    else "an analysis stage produced no usable artifact"
                )
                return stopped
            pipeline_id = plan_id
            pipeline_directory = directory

        print()
        print(f"planning {names} with {args.agent}...")

        def announce(resolved: agents.ResolvedAgent, directory: Path) -> None:
            phases.start_step(
                root,
                phase,
                "synthesis",
                resolved=resolved,
                directory=directory,
                artifact=directory.parent / planfiles.DRAFT_FILENAME,
            )
            print(f"  running: {resolved.display}")
            if resolved.warning:
                print(f"  warning: {resolved.warning}", file=sys.stderr)
            print(f"  transcript: {directory}")
            if not args.quiet:
                print("  " + "─" * 60)
            sys.stdout.flush()

        try:
            document, plan_path, code = planning.generate(
            root=root,
            doc=doc,
            agent=args.agent,
            agent_args=list(getattr(args, "agent_args", []) or []),
            model=args.model,
            timeout=args.timeout,
            cwd=args.cwd,
            instructions=args.instructions,
            context=context,
            stream=not args.quiet,
            on_start=announce,
                artifacts=artifacts,
                plan_id=plan_id,
            )
        except WritError as exc:
            # A synthesis that wrote no plan raises, and the record has to say so
            # before the exception leaves: the step is otherwise left `running` by
            # a process that is exiting, and the reason is the useful part.
            phases.finish_step(
                root, phase, "synthesis", status="failed", error=_first_line(str(exc))
            )
            raise
        phases.finish_step(
            root, phase, "synthesis", status="ok", exit_code=code, artifact=plan_path
        )
        if not args.quiet:
            print("  " + "─" * 60)
        label = "synthesis agent" if artifacts is not None else "planning agent"
        print(f"{label} exited {code}; plan: {plan_path}")
        source = f"{names} (staged)" if artifacts is not None else f"{names} (agent)"
        if artifacts is not None:
            pipeline_artifacts = artifacts
            extra_findings = analysis.reconcile(document, artifacts)
            # Whether each obligation traces to a heading that exists. Checked
            # against the inventory the requirements stage wrote rather than the
            # plan's copy of it, so a synthesizer that tidied a source cannot
            # launder a citation the stage got wrong.
            extra_findings += planning.untraceable_requirements(
                artifacts.requirements.requirements if artifacts.requirements else [],
                doc,
            )

    milestones = document.milestones
    summary = planner.summarize(milestones)
    missing = planning.unresolved_sections(milestones, doc)

    if args.dry_run:
        _print_plan(document, summary, missing)
        _print_findings(
            plancheck.check(
                plancheck.from_plan(
                    milestones, document.requirements, root=root.resolve()
                )
            ),
            preamble="what Writ would object to:",
        )
        return 0

    phases.start_step(root, phase, "commit", artifact=plan_path or "")
    try:
        created, findings = _commit_plan(
            args,
            root=root,
            doc=doc,
            document=document,
            plan_path=plan_path,
            source=source,
            extra_findings=extra_findings,
            pipeline_artifacts=pipeline_artifacts,
            pipeline_results=pipeline_results,
            pipeline_id=pipeline_id,
            pipeline_directory=pipeline_directory,
            plan_id=plan_id,
        )
    except WritError as exc:
        # The overwrite gate and every structural refusal come out here. Recorded
        # before the exception leaves, because a commit that refused is the most
        # informative thing on the record: the agents ran, their artifacts are on
        # disk, and what stopped is the one step that did not.
        phases.finish_step(
            root, phase, "commit", status="failed", error=_first_line(str(exc))
        )
        raise
    phases.finish_step(
        root,
        phase,
        "commit",
        status="ok",
        note=(
            f"{created} feature(s)"
            if document.features
            else f"{summary['milestones']} milestone(s), {created} task(s)"
        ),
    )
    if document.features:
        print(f"created {created} features from {source}")
    else:
        print(
            f"created {summary['milestones']} milestones and {created} tasks "
            f"from {source}"
        )
    if document.requirements:
        print(
            f"requirement inventory: {len(document.requirements)} entries "
            "(writ coverage)"
        )
    for section in missing:
        print(f"note: no section titled {section!r} in {names}", file=sys.stderr)
    _print_findings(findings)
    if _critics_requested(args):
        # `--critics` absent is None and runs nothing; `--critics` with no names is
        # `[]` and means all of them. The distinction matters because the flag is
        # opt-in — it spends an agent run per critic — so the empty list is a
        # request, not the absence of one.
        _run_critics(
            args,
            root=root,
            doc=doc,
            chosen=_chosen_critics(args),
            plan_path=plan_path,
            phase=phase,
        )
    # Repair before approval, because repair is what makes an unattended plan
    # approvable: the critics' blocking findings are exactly what `--auto-approve`
    # refuses to override, and answering them is the adjudicator's job. Opt-in for
    # the same reason as the critics — it spends agent runs — and bounded by
    # `plan.max_rounds`, so a finding that survives its own repair reaches a
    # human instead of looping.
    if getattr(args, "repair", False):
        _repair_plan(args, root=root, doc=doc, phase=phase)
    if getattr(args, "auto_approve", False):
        phases.start_step(root, phase, "approval")
        approved = _auto_approve(root)
        phases.finish_step(
            root,
            phase,
            "approval",
            status="ok" if approved else "skipped",
            note=(
                "approved: nothing blocking stood against the plan"
                if approved
                else "not approved: a blocking finding stands, which only "
                "`writ approve --force --reason` may overrule"
            ),
        )
    data = state.load(root)
    counts = plancheck.tally(plans.findings(data, open_only=True))
    if plans.runnable(data):
        record = plans.plan_status(data)
        print(f"plan approved by {record['approved_by']}: {record['approval_note']}")
        if counts["warning"]:
            print(f"  {counts['warning']} advisory finding(s) stand on the record")
        print("next: writ run")
    elif counts["error"]:
        print(
            f"plan held at {plans.plan_status(data)['status']}: "
            f"{counts['error']} blocking, {counts['warning']} advisory"
        )
        print("next: writ check   (then writ approve, or re-plan)")
    else:
        # Nothing objects, and that is deliberately not the same as approved.
        print(
            f"plan at {plans.plan_status(data)['status']}: nothing blocking, "
            f"{counts['warning']} advisory"
        )
        print("next: writ check, writ coverage   (then writ approve)")
    return 0


def _run_stages(
    args,
    *,
    root: Path,
    doc: planner.DesignDocs,
    plan_id: str,
    context: dict[str, Any],
    stages: list[analysis.Stage] | None = None,
    phase: str | None = None,
) -> tuple[analysis.Artifacts | None, list[analysis.Result], int | None]:
    """Run the analysis stages, and say whether planning should continue.

    Returns `(artifacts, results, stop)`. `stop` is an exit code when the caller
    must not go on to synthesis: either a stage failed, or `--stage` asked for
    the analyses alone. Otherwise it is None and `artifacts` is complete.

    A failed stage stops the pipeline rather than letting synthesis proceed on a
    partial set. Synthesising without the requirement inventory would produce
    exactly the plan the staging exists to prevent — one whose coverage claims
    nothing established — and it would cost an agent run to find that out.
    """
    if stages is None:
        stages = analysis.upto(args.stage) if getattr(args, "stage", None) else list(
            analysis.STAGES
        )
    agent = getattr(args, "stage_agent", None) or args.agent
    model = getattr(args, "stage_model", None) or args.model
    timeout = getattr(args, "stage_timeout", None) or args.timeout
    # Resolve before the pipeline creates anything. An unknown --model is a
    # WritError from `agents.resolve`, and raising it after the plan directory
    # exists leaves a project littered with empty pipelines that never ran.
    agents.resolve(agent, list(getattr(args, "agent_args", []) or []), model)
    directory = state.plan_dir(root, plan_id)
    grouped = analysis.waves(stages)
    print(
        f"analysing {planner.doc_names(doc)} in {len(stages)} stage(s) with {agent}..."
    )
    print(f"  artifacts: {directory}")
    if any(len(wave) > 1 for wave in grouped):
        print(
            "  at once: "
            + "; then ".join(", ".join(s.name for s in wave) for wave in grouped)
        )
        if any(
            len(wave) > 1 and any(s.name == "inventory" for s in wave)
            for wave in grouped
        ):
            # Said plainly, because it is a real loss and it is invisible
            # afterwards: the artifact is well-formed and simply has one field
            # empty, so nothing downstream reports the absence as a problem.
            print(
                "  the inventory runs without the requirement ids, so it will "
                "claim no existing coverage"
            )

    # A blank line before each stage and after each result. Four agents' worth of
    # streamed output runs into one wall otherwise, and the lines that say which
    # stage started and what it wrote are the ones a reader is scanning for.
    def record(
        stage: analysis.Stage, resolved: agents.ResolvedAgent, where: Path
    ) -> None:
        """Mark the stage running. Separate from `announce` because of the lock.

        `announce` prints, so `analysis.run_stage` holds it behind the mirror lock
        that keeps two concurrent stages from interleaving a line. This writes
        state, which takes a lock of its own, so it is called outside that one.
        """
        phases.start_step(
            root,
            phase,
            f"stage:{stage.name}",
            resolved=resolved,
            directory=where,
            artifact=directory / stage.artifact,
        )

    def announce(stage: analysis.Stage, resolved: agents.ResolvedAgent) -> None:
        print()
        print(f"  {stage.name}: {stage.summary}")
        # The command, not just the agent name: each stage may resolve its own
        # model and event flags, and the first thing anyone does with a stage
        # that failed or hung is run its invocation by hand.
        print(f"    running: {resolved.display}")
        if resolved.warning:
            print(f"    warning: {resolved.warning}", file=sys.stderr)
        if not args.quiet:
            print("  " + "─" * 60)
        sys.stdout.flush()

    def finished(result: analysis.Result) -> None:
        # A reused stage reaches here having never started: `run_stage` returns
        # before the launch hook when the artifact is already on disk, which is the
        # common case on `writ plan --plan-id <existing>`. `finish_step` tolerates
        # it rather than stamping a `started_at` for a run that did not happen.
        phases.finish_step(
            root,
            phase,
            f"stage:{result.stage}",
            status="reused" if result.reused else ("ok" if result.ok else "failed"),
            exit_code=result.exit_code,
            error=_first_line(result.error or ""),
            artifact=result.path,
            directory=directory / result.stage,
        )
        if not args.quiet and not result.reused:
            print("  " + "─" * 60)
        if result.reused:
            print(f"  {result.stage}: reusing {result.path.name}")
        elif result.ok:
            print(f"  {result.stage}: wrote {result.path.name}")
        else:
            print(f"  {result.stage}: failed — {result.error}", file=sys.stderr)
        sys.stdout.flush()

    artifacts, results = analysis.run_pipeline(
        root=root,
        doc=doc,
        directory=directory,
        chosen=stages,
        agent=agent,
        agent_args=list(getattr(args, "agent_args", []) or []),
        model=model,
        timeout=timeout,
        cwd=args.cwd,
        instructions=args.instructions,
        context=context,
        refresh=bool(getattr(args, "refresh", False)),
        stream=not args.quiet,
        on_start=announce,
        on_finish=finished,
        on_launch=record,
    )
    failed = [result for result in results if not result.ok]
    if failed:
        names = " and ".join(result.stage for result in failed)
        noun = "stages" if len(failed) > 1 else "stage"
        print(
            f"planning stopped: the {names} {noun} produced no usable artifact.\n"
            f"  fix or re-run with: writ plan {_doc_args(doc)} --plan-id {plan_id}\n"
            "  (completed stages are reused; add --refresh to redo them)",
            file=sys.stderr,
        )
        return artifacts, results, 1

    _print_stage_summary(artifacts)

    if getattr(args, "stage", None):
        print(
            f"\nstages complete: {args.stage}. Nothing has been committed.\n"
            f"  continue with: writ plan {_doc_args(doc)} --plan-id {plan_id}"
        )
        return artifacts, results, 0
    return artifacts, results, None


def _print_stage_summary(artifacts: analysis.Artifacts) -> None:
    """What the analyses found, before any of it becomes tasks.

    Printed because these artifacts are the plan's premises, and a premise nobody
    read is the failure mode staging is supposed to fix. The baseline in
    particular: a run that starts with a failing suite will attribute that failure
    to the first task that trips over it unless somebody saw this line.
    """
    print()
    requirements = artifacts.requirements
    if requirements is not None:
        musts = sum(1 for req in requirements.requirements if req.priority == "must")
        print(
            f"  requirements: {len(requirements.requirements)} obligations "
            f"({musts} must)"
        )
        unresolved = requirements.open_questions
        if unresolved:
            print(
                f"  warning: {len(unresolved)} ambiguity(ies) with no assumed "
                "reading — the plan will have to guess",
                file=sys.stderr,
            )
    inventory = artifacts.inventory
    if inventory is not None:
        status = inventory.baseline_status
        commands = ", ".join(inventory.baseline_commands) or "none named"
        print(f"  baseline: {commands} → {status}")
        if status == "fail":
            print(
                "  warning: this project's verification already fails. Failures "
                "during execution will be ambiguous unless these are fixed first: "
                + (", ".join(inventory.known_failures) or "unnamed"),
                file=sys.stderr,
            )
        elif status == "unknown":
            print(
                "  warning: the baseline was never established, so a later failure "
                "cannot be told from a pre-existing one",
                file=sys.stderr,
            )
        already = inventory.satisfied()
        if already:
            print(f"  already satisfied by this repository: {', '.join(already)}")
        if inventory.language or inventory.test_dirs:
            print(
                f"  repository: {inventory.language or 'language unstated'}; "
                f"tests in {', '.join(inventory.test_dirs) or 'no stated directory'}"
            )


def _print_plan(
    document: planning.PlanDocument,
    summary: dict[str, Any],
    missing: list[str],
) -> None:
    for requirement in document.requirements:
        flag = requirement.priority
        if requirement.status != "planned":
            flag = f"{flag}, {requirement.status}"
        print(f"{requirement.id} [{flag}] {requirement.text}")
    if document.requirements:
        print()
    for milestone in document.milestones:
        if not milestone.loose:
            print(milestone.title)
            if milestone.notes:
                print(f"    {milestone.notes}")
        for task in milestone.tasks:
            print(f"  - {task.title}")
            if task.goal:
                print(f"      {task.goal}")
            if task.notes:
                print(f"      {task.notes}")
            if task.requirement_ids:
                print(f"      covers: {', '.join(task.requirement_ids)}")
            if task.owns:
                print(f"      owns: {', '.join(task.owns)}")
            for line in task.provides:
                print(f"      provides: {line}")
            for line in task.consumes:
                print(f"      consumes: {line}")
            for item in task.acceptances:
                print(f"      · {item}")
            if task.depends_on:
                print(f"      after: {', '.join(task.depends_on)}")
            if task.allowed and not task.feature:
                print(f"      allowed: {', '.join(task.allowed)}")
            if task.forbidden:
                print(f"      forbidden: {', '.join(task.forbidden)}")
    if document.features:
        print(
            f"\nwould create {summary['tasks']} features, "
            f"{summary['acceptances']} acceptance criteria"
        )
    else:
        print(
            f"\nwould create {summary['milestones']} milestones, "
            f"{summary['tasks']} tasks, {summary['acceptances']} acceptance criteria"
        )
    for section in missing:
        print(f"note: design section {section!r} was not found", file=sys.stderr)


def _design_docs(design: str | list[str]) -> list[Path]:
    """The design documents named on the command line, each one checked."""
    docs: list[Path] = []
    for item in [design] if isinstance(design, str) else design:
        path = Path(item).expanduser()
        if not path.exists():
            raise WritError(f"design document not found: {path}")
        if path.resolve() not in {seen.resolve() for seen in docs}:
            docs.append(path)
    if not docs:
        raise WritError("name at least one design document")
    return docs


def _doc_args(doc: planner.DesignDocs) -> str:
    """The design documents as they would be typed again."""
    return " ".join(shlex.quote(str(path)) for path in planner.doc_list(doc))


def _section_doc(
    doc: planner.DesignDocs, section: str, doc_paths: list[str]
) -> str | None:
    """The registered path of the document that holds `section`.

    The first document when no heading matches: the section is then only a label,
    and the runner still has to hand the agent some document to read.
    """
    found, _ = planner.find_section(doc, section) if section else (None, "")
    if found is not None:
        return str(found.resolve())
    return doc_paths[0] if doc_paths else None


def _commit_plan(
    args,
    *,
    root: Path,
    doc: planner.DesignDocs,
    document: planning.PlanDocument,
    plan_path: Path | None,
    source: str,
    extra_findings: Iterable[plancheck.Finding] = (),
    pipeline_artifacts: analysis.Artifacts | None = None,
    pipeline_results: Iterable[analysis.Result] = (),
    pipeline_id: str | None = None,
    pipeline_directory: Path | None = None,
    plan_id: str | None = None,
) -> tuple[int, list[plancheck.Finding]]:
    """Write the plan to state, then check it and set the plan's status.

    Committed before it is approved, on purpose. The alternative — hold the plan
    outside the graph until a human signs it off — means the one view that would
    let them judge it (`writ graph`, `writ show`, `writ coverage`) cannot see it
    yet. So the tasks land, the objections land beside them, and what approval
    actually gates is `writ run`.
    """
    milestones = document.milestones
    with state.transaction(root) as data:
        if data["tasks"] and not (args.append or args.force):
            raise WritError(
                "this project already has tasks; use --append to add, "
                "or --force to replace the plan"
            )
        if args.force:
            data["tasks"] = {}
            data["milestones"] = {}
            data["findings"] = []
        offset = len(data["milestones"])
        doc_paths = [str(path.resolve()) for path in planner.doc_list(doc)]
        for doc_path in doc_paths:
            if doc_path not in data["design_docs"]:
                data["design_docs"].append(doc_path)

        plans.set_requirements(data, document.requirements, replace=bool(args.force))
        built = planner.build_ids(milestones, offset, _feature_offset(data))
        translate = planner.ref_map(built)
        test_dirs = (
            pipeline_artifacts.inventory.test_dirs
            if pipeline_artifacts is not None and pipeline_artifacts.inventory is not None
            else []
        )
        previous: str | None = None
        if args.chain:
            existing = sorted(data["tasks"])
            previous = existing[-1] if existing else None
        created = 0
        # Two passes: every task is inserted without its edges, then the edges are
        # attached once all of them exist. `add_task` requires a dependency to be
        # present already, which makes insertion order decide whether a plan
        # commits — a task in M08 that legitimately depends on one in M10 is a
        # backward edge, not a cycle, and rejecting it stranded a whole plan on an
        # ordering writ imposed rather than one the plan got wrong. Attaching
        # afterwards leaves the actual validation to `check_dag`, which sees the
        # finished graph and still refuses cycles and edges to nothing.
        deferred: list[tuple[str, list[str]]] = []
        for milestone_id, milestone, tasks in built:
            # A features plan has no milestones (docs/planning-redesign.md §4):
            # its features commit as loose tasks that only G-FINAL gathers.
            if not milestone.loose:
                add_milestone(
                    data,
                    milestone_id=milestone_id,
                    title=milestone.title,
                    design_section=milestone.section,
                )
            for task_id, task in tasks:
                depends = _resolve_depends(
                    task, translate, data, previous, chain=args.chain
                )
                add_task(
                    data,
                    task_id=task_id,
                    title=task.title,
                    milestone=milestone_id or None,
                    acceptances=task.acceptances,
                    allowed=(
                        contracts.fence(task.allowed, test_dirs)
                        if task.feature
                        else task.allowed
                    ),
                    forbidden=task.forbidden,
                    design_section=task.section,
                    design_doc=_section_doc(doc, task.section, doc_paths),
                    requirement_ids=task.requirement_ids,
                    notes=task.notes,
                    feature=(
                        {
                            "goal": task.goal,
                            "owns": task.owns,
                            "provides": task.provides,
                            "consumes": task.consumes,
                        }
                        if task.feature
                        else None
                    ),
                )
                if depends:
                    deferred.append((task_id, depends))
                if task.notes:
                    add_evidence(data["tasks"][task_id], f"plan: {task.notes}")
                created += 1
                previous = task_id
        for task_id, depends in deferred:
            data["tasks"][task_id]["depends_on"] = depends
        _derive_feature_edges(data)
        check_dag(data)
        refresh_milestones(data)
        if args.gates:
            gates.install(
                data, milestones=[m_id for m_id, _, _ in built if m_id]
            )
        data.setdefault("plans", []).append(
            {
                "source": source,
                "design_doc": doc_paths[0] if doc_paths else None,
                "design_docs": doc_paths,
                "artifact": str(plan_path) if plan_path else None,
                "milestones": [milestone_id for milestone_id, _, _ in built if milestone_id],
                "features": [
                    task_id
                    for milestone_id, _, tasks in built
                    if not milestone_id
                    for task_id, _ in tasks
                ],
                "requirements": [req.id for req in document.requirements],
                "created_at": state.utcnow(),
            }
        )
        if pipeline_artifacts is not None and pipeline_id and pipeline_directory:
            analysis.record(
                data,
                plan_id=pipeline_id,
                directory=pipeline_directory,
                results=pipeline_results,
                artifacts=pipeline_artifacts,
            )
        plans.bump(data)
        plans.set_status(data, "draft")
        # An appended plan joins the graph already on disk; anything else starts
        # a plan directory of its own (the pipeline's, when there was one).
        planfiles.assign_id(
            data,
            plan_id
            or pipeline_id
            or (
                None
                if args.append and planfiles.current_id(data)
                else planning.new_plan_id(doc)
            ),
        )
        # The reconcile findings land beside writ's structural ones rather than in a
        # channel of their own: a dropped requirement holds the plan the same way a
        # cycle does, and `writ approve` should not need to know which kind it is
        # overruling. `run_check` derives them itself from the artifacts recorded
        # just above, against the ids writ minted — passing the pre-commit copy as
        # well recorded each finding twice, once under the synthesizer's own task id
        # (`T-log`) and once under writ's (`M01-001`), and the first could never be
        # closed because no later check speaks that id.
        #
        # `untraceable_requirements` still comes through `extra`: it reads the design
        # document, which the committed graph does not carry.
        findings = plans.run_check(
            data,
            root=root.resolve(),
            extra=[
                finding
                for finding in extra_findings
                if finding.category == "untraceable-requirement"
            ],
        )
        planfiles.export(root, data)
    return created, findings


def _auto_approve(root: Path) -> bool:
    """Approve the plan without a human, when nothing blocking stands against it.

    Returns whether it did. The caller records the answer: a plan that reached
    approval and was declined is the most common end of an unattended run, and the
    reader looking for why should find the approval step saying so rather than
    find nothing where an approval would have been.

    Deliberately narrow. `--auto-approve` is for automation that has to get from a
    document to a running graph with nobody watching, and the one thing it must not
    become is a way to start executing a plan writ objected to. So a blocking
    finding is not overridden here — `writ approve --force --reason ...` is the only
    path that does that, because accepting an objection is a judgement and the
    record has to say whose.

    Runs last, after the critics and any repair, rather than inside the commit that
    writes the tasks. It used to run there, which meant it judged writ's structural
    findings alone and approved the plan minutes before the critics had read it: the
    approval was recorded, five blocking findings arrived, and `run_check` demoted
    the plan back to `needs-approval` — leaving a record that said a plan nothing
    blocking stood against had been approved, next to the five findings blocking it.
    Worse while it lasted: between the commit and the last critic the plan really
    was `approved`, so a `writ run` in another terminal would start executing a plan
    no critic had finished reading.
    """
    with state.transaction(root) as data:
        blocking = [
            finding
            for finding in plans.findings(data, open_only=True)
            if finding.severity == "error"
        ]
        if blocking:
            return False
        if plans.plan_status(data)["status"] not in ("draft", "needs-approval"):
            return False
        plans.approve(
            data,
            actor="writ --auto-approve",
            reason="no blocking findings stood against the plan",
        )
        return True


def _feature_offset(data: dict[str, Any]) -> int:
    """The highest `FT-nnn` number in use, so appended features never reuse one."""
    highest = 0
    for task_id in data["tasks"]:
        if task_id.startswith("FT-") and task_id[3:].isdigit():
            highest = max(highest, int(task_id[3:]))
    return highest


def _derive_feature_edges(data: dict[str, Any]) -> None:
    """Add the edges the features' contracts imply, across the whole graph.

    Derived over every feature in state, not just the ones this commit added: an
    appended feature that provides what an existing one consumes is an edge too.
    Stated edges are kept; the derived ones are added beside them.
    """
    features = {
        task_id: task
        for task_id, task in data["tasks"].items()
        if contracts.is_feature(task)
    }
    for task_id, deps in contracts.edges(features).items():
        stated = data["tasks"][task_id].setdefault("depends_on", [])
        for dep in deps:
            if dep not in stated:
                stated.append(dep)


def _resolve_depends(
    task: planner.PlannedTask,
    translate: dict[str, str],
    data: dict[str, Any],
    previous: str | None,
    *,
    chain: bool,
) -> list[str]:
    """Map a planned task's stated dependencies onto real task ids.

    A generated plan refers to tasks by the ids it invented, and may also refer
    to tasks that already exist. Anything we cannot resolve is an error, not a
    silently dropped edge.

    An omitted dependency stays omitted. Writ used to chain a task with no stated
    `depends_on` onto whatever task came before it in the plan, which looks
    conservative and is not: it manufactures an ordering the plan never claimed,
    so a plan that forgot "C needs A" still runs — C after some unrelated B — and
    the omission surfaces as a task failing for no visible reason rather than as a
    plan that is wrong. Independent by default means a missing edge shows up as
    what it is. `--chain` restores the old behaviour for a plan that really is
    meant to be a single line of work.
    """
    if not task.depends_on:
        return [previous] if chain and previous else []
    resolved: list[str] = []
    for ref in task.depends_on:
        target = translate.get(ref) or (ref if ref in data["tasks"] else None)
        if target is None:
            raise WritError(
                f"task {task.title!r} depends on {ref!r}, which is neither in "
                "this plan nor an existing task"
            )
        if target not in resolved:
            resolved.append(target)
    return resolved


def _print_findings(
    findings: list[plancheck.Finding],
    *,
    preamble: str = "",
    limit: int = 12,
) -> None:
    """Print findings worst-first, with what would close each one.

    Capped, because a plan with forty warnings is one nobody reads to the end, and
    the ones that matter are at the top. The count says what was withheld.

    The whole list goes to one stream, chosen by whether anything in it blocks.
    Routing each finding by its own severity splits one list across stdout and
    stderr: the reader sees a tally of four blocking findings with three notes
    above it and the blocking ones nowhere, and neither stream reads as a list.
    """
    if not findings:
        return
    counts = plancheck.tally(findings)
    stream = sys.stderr if counts["error"] else sys.stdout
    if preamble:
        print(preamble, file=stream)
    for finding in findings[:limit]:
        print(f"  {finding.line()}", file=stream)
        if finding.suggested_action:
            print(f"      → {finding.suggested_action}", file=stream)
    if len(findings) > limit:
        print(f"  … {len(findings) - limit} more (writ check)", file=stream)
    print(
        f"  {counts['error']} blocking, {counts['warning']} advisory, "
        f"{counts['note']} notes",
        file=stream,
    )


def cmd_check(args) -> int:
    """Re-check the committed plan and report what stands against it."""
    root = Path(args.root)
    with state.transaction(root) as data:
        found = plans.run_check(data, root=root.resolve())
        record = dict(plans.plan_status(data))
        listed = plans.findings(data, open_only=not args.all)
        coverage_rows = plans.coverage(data)
        # Which critics have not read the plan as it now stands. Structural checks
        # re-run here for free; a critic is an agent and does not, so the most this
        # can do is say that what a critic passed is not what is now committed.
        stale = critics.unreviewed(data)
    blocking = plancheck.blocking(listed)
    if args.json:
        render.emit_json(
            {
                "plan": record,
                "findings": [finding.to_dict() for finding in listed],
                "tally": plancheck.tally(listed),
                "coverage": coverage_rows,
                "unreviewed": stale,
            }
        )
        return 1 if blocking else 0
    if args.quiet:
        return 1 if blocking else 0
    print(f"plan {record['status']} at revision {record['revision']}")
    if coverage_rows:
        satisfied = sum(1 for row in coverage_rows if row["state"] == "satisfied")
        uncovered = [row["id"] for row in coverage_rows if row["state"] == "uncovered"]
        print(
            f"requirements: {len(coverage_rows)} total, {satisfied} satisfied"
            + (f", {len(uncovered)} uncovered" if uncovered else "")
        )
    if not listed:
        print("no findings stand against this plan")
        _print_stale(stale)
        if record["status"] not in plans.RUNNABLE_STATUSES:
            print("next: writ approve")
        return 0
    _print_findings(plancheck.sort_findings(listed), limit=40)
    _print_stale(stale)
    if blocking:
        print()
        print(
            "repair them with `writ adjudicate`, answer them one at a time with "
            "`writ set F-NNNN accepted|declined --reason ...`, or accept them all "
            "with `writ approve --force --reason ...`"
        )
        return 1
    if record["status"] not in plans.RUNNABLE_STATUSES:
        print("next: writ approve")
    return 0


def _print_stale(stale: list[str]) -> None:
    """Say which critics have not read the plan as it stands.

    Not a finding. Writ cannot tell whether an unreviewed plan is wrong — that is
    the entire reason it asks agents — so this states the absence of a review rather
    than objecting to the plan, and does not block anything.
    """
    if not stale:
        return
    if len(stale) == len(critics.CRITICS):
        print("no critic has read this plan   (writ critique)")
        return
    print(f"not reviewed at this revision: {', '.join(stale)}   (writ critique)")


def cmd_critique(args) -> int:
    """Have independent critics read the committed plan and report on it.

    Deliberately separate from `writ check`. Check is deterministic and free: it
    runs every time the plan changes. This spends agents, so it is asked for.
    """
    root = Path(args.root)
    data = state.load(root)
    if not data["tasks"]:
        raise WritError("there is no plan to review (run `writ plan` first)")
    chosen = _chosen_critics(args)
    reports = _run_critics(args, root=root, doc=None, chosen=chosen, plan_path=None)
    if args.json:
        render.emit_json(
            {
                "reports": [
                    {
                        "critic": report.critic,
                        "summary": report.summary,
                        "confidence": report.confidence,
                        "error": report.error,
                        "findings": [f.to_dict() for f in report.findings],
                    }
                    for report in reports
                ],
                "plan": dict(plans.plan_status(state.load(root))),
            }
        )
    blocking = sum(report.blocking for report in reports if report.ok)
    return 1 if blocking or any(not report.ok for report in reports) else 0


def _phase_for_adjudication(data: dict[str, Any]) -> str | None:
    """The planning phase whose plan this is, or None if there is no record.

    Matched on `plan_id` rather than just taking the newest: adjudication is about
    one committed plan, and attaching its rounds to some other plan's attempt would
    draw them into a graph they were no part of. None is a working answer — every
    `phases.*` call is a no-op for it — which is what a project planned by an older
    writ, or one whose plan came from `--from-plan`, gets.
    """
    record = phases.current(data)
    if not record:
        return None
    # A positive match, not the absence of a mismatch. `pipeline.plan_id` is what
    # names the planning run on disk and is the id `phases.begin` was given; a plan
    # committed with `--from-plan` has no pipeline and so nothing to match on. That
    # case has to be None: falling through on a missing id attached the rounds to
    # whatever attempt happened to be newest, which is how a repair ends up drawn
    # into a graph belonging to a different plan.
    pipeline = plans.plan_status(data).get("pipeline") or {}
    plan_id = str(pipeline.get("plan_id") or "")
    if not plan_id or str(record.get("plan_id") or "") != plan_id:
        return None
    return str(record.get("id") or "") or None


def _last_phase_step(root: Path, phase: str | None) -> str:
    """The id of the step in the last column, which a new round follows.

    A resumed loop appends after whatever the previous attempt ended with — the
    approval it skipped, or the last re-review — so the new round is drawn where it
    happened rather than back among the steps it came after.
    """
    if not phase:
        return ""
    data = state.load(root)
    record = phases.current(data) or {}
    steps = record.get("steps") or []
    if not steps:
        return ""
    return str(max(steps, key=lambda entry: int(entry.get("wave", 0))).get("id") or "")


def cmd_adjudicate(args) -> int:
    """Repair the plan against its own findings, bounded, before it executes.

    The loop writ was missing. `writ check` and `writ critique` produce findings;
    this is what answers them. An adjudicator edits a working copy of the plan
    files, writ validates it against the invariants no agent is trusted with — a
    bar may not be lowered, a requirement may not be dropped — promotes it if it
    holds, then re-checks and re-runs the critics so a finding closes on evidence
    rather than on the adjudicator's word.

    Bounded on purpose, and separate from `writ approve --force`. Force is a human
    accepting an objection on the record; this is an attempt to remove it. A plan
    that is still objected to after its budget stops for a person rather than
    looping.
    """
    root = Path(args.root)
    data = state.load(root)
    if not data["tasks"]:
        raise WritError("there is no plan to repair (run `writ plan` first)")
    if plans.plan_status(data)["status"] == "executing":
        raise WritError(
            "this plan is executing; a running plan is repaired by its gates, not "
            "by adjudication (writ status)"
        )
    open_blocking = [
        finding
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error"
    ]
    if not open_blocking:
        stale = critics.unreviewed(data)
        if not args.json:
            print("nothing blocking stands against this plan")
            _print_stale(stale)
        elif args.json:
            render.emit_json(
                {"rounds": [], "stopped": "clean", "plan": dict(plans.plan_status(data))}
            )
        return 0
    doc = Path(args.doc) if getattr(args, "doc", None) else None
    with state.transaction(root) as data:
        directory = planfiles.rounds_dir(root, data)
    # Attach to the planning phase this plan was built by, so a round run from
    # `writ adjudicate` reaches the dashboard the same way one run by
    # `writ plan --repair` does.
    #
    # `writ adjudicate` is how a repair is resumed: the loop stops for a human, the
    # human answers, and this is the command that carries on. Recording nothing
    # meant the page kept showing the planning run that stopped — the same critics,
    # the same one repair box — while rounds two and three were on disk and in
    # `state.json`. The loop had run, and the only view of it said it had not.
    phase = _phase_for_adjudication(data)
    step_of: dict[int, str] = {}

    # The revision as the loop starts, which is what `directory` is named for. Used
    # in the step ids too so a step and the round directory it points at carry the
    # same number; re-reading the revision per round would have named round 2 after
    # the revision round 1 created, while its transcript sat under the old one.
    opened_at_revision = plans.revision(data)

    def step_for(number: int) -> str:
        """The step id for this round, declared the first time the round opens."""
        if number not in step_of:
            step_of[number] = f"repair:r{opened_at_revision}-{number}"
            phases.add(
                root,
                phase,
                [
                    phases.make_step(
                        id=step_of[number],
                        kind="repair",
                        name=f"repair round {number}",
                        summary="answer the plan's blocking findings",
                    )
                ],
                after=_last_phase_step(root, phase),
            )
        return step_of[number]

    def announce(number: int, resolved) -> None:
        phases.start_step(
            root,
            phase,
            step_for(number),
            resolved=resolved,
            directory=directory / f"round-{number}",
        )
        if args.json:
            return
        print(f"adjudication round {number}: {len(open_blocking)} blocking")
        print(f"  running: {resolved.display}")
        sys.stdout.flush()

    def record(round_) -> None:
        """Close this round's step with how it ended. The record goes first."""
        step = step_of.get(round_.number)
        if step is None:
            return
        if round_.error:
            phases.finish_step(
                root, phase, step, status="failed", error=_first_line(round_.error)
            )
            return
        if round_.refused:
            note = f"writ refused the patch ({len(round_.refused)} reason(s))"
        elif round_.questions:
            note = f"raised {len(round_.questions)} question(s) for a human"
        else:
            note = (
                f"applied at plan revision {round_.applied.get('revision')}; "
                f"blocking now {round_.blocking_after}"
            )
        phases.finish_step(root, phase, step, status="ok", note=note)

    def report(round_) -> None:
        record(round_)
        if args.json:
            return
        if round_.error:
            print(f"  round {round_.number} failed: {round_.error}", file=sys.stderr)
            return
        if round_.refused:
            print(f"  writ refused the patch ({len(round_.refused)} reason(s)):")
            for finding in round_.refused[:6]:
                print(f"    {finding.line()}")
            return
        if round_.questions and not round_.unanswered:
            print(
                f"  answered {len(round_.questions)} question(s) with their "
                "recommendations (autonomous)"
            )
        elif round_.questions:
            print(f"  raised {len(round_.unanswered)} question(s) for a human")
            return
        print(f"  applied: {_applied_line(round_.applied)}")
        print(f"  blocking now: {round_.blocking_after}")

    def recheck() -> list[str]:
        """Re-run the critics against the patched plan."""
        if args.no_critics:
            return []
        chosen = _chosen_critics(args)
        if not args.json:
            print("  re-reviewing the patched plan")
        _run_critics(
            args,
            root=root,
            doc=doc,
            chosen=chosen,
            plan_path=None,
            phase=phase,
            verify=True,
        )
        return []

    # The phase closed when planning ended; these rounds reopen it. In a `finally`,
    # because every way out of here — a clean plan, a spent budget, an adjudicator
    # that would not start — has to leave the record settled rather than showing an
    # attempt still in flight after the process is gone.
    phases.resume(root, phase)
    try:
        result = adjudicate.loop(
            root=root,
            doc=doc,
            directory=directory,
            agent=getattr(args, "adjudicator_agent", None) or args.agent,
            model=getattr(args, "adjudicator_model", None) or args.model,
            timeout=args.timeout,
            cwd=args.cwd,
            max_rounds=args.max_rounds,
            recheck=recheck,
            stream=not args.quiet,
            on_round=report,
            on_start=announce,
            autonomous=_autonomous(args, root),
        )
    finally:
        phases.finish(root, phase, status="done")
    data = state.load(root)
    if args.json:
        render.emit_json(
            {
                "rounds": [
                    {
                        "number": round_.number,
                        "request": round_.request_id,
                        "revision": round_.revision,
                        "error": round_.error,
                        "refused": [f.to_dict() for f in round_.refused],
                        "applied": round_.applied,
                        "questions": round_.questions,
                        "blocking_after": round_.blocking_after,
                    }
                    for round_ in result.rounds
                ],
                "stopped": result.stopped,
                "resolved": result.resolved,
                "remaining": result.remaining,
                "plan": dict(plans.plan_status(data)),
            }
        )
        return 0 if result.clean else 1
    print()
    print(
        f"{len(result.rounds)} round(s): {result.resolved} finding(s) resolved, "
        f"{result.remaining} still blocking"
    )
    if result.stopped and result.stopped != "clean":
        print(f"stopped: {result.stopped}")
    if result.clean:
        print("next: writ approve")
    elif any(
        decisions.asked(data, finding.id)
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error"
    ):
        print(
            'next: writ set D-NNNN active --decision "..."   (then writ adjudicate '
            "again to repair the plan to follow it)"
        )
    else:
        print("next: writ check   (then writ approve --force --reason ..., or re-plan)")
    return 0 if result.clean else 1


def _repair_plan(
    args, *, root: Path, doc: planner.DesignDocs, phase: str | None = None
) -> None:
    """Run the bounded repair loop over the plan `writ plan` just committed.

    The same loop `writ adjudicate` drives, reached from planning so that one
    command can get from a document to a runnable graph unattended. Findings are
    the whole reason it exists: the deterministic checks and the critics both
    report, and until this runs there is nobody whose job is to answer them.

    Failures here do not fail planning. The plan is committed and its objections
    are on the record either way, and an adjudicator that could not be started is
    a reason to read `writ check` rather than to lose the plan that was just paid
    for. It is reported and planning carries on to say where the plan stands.
    """
    data = state.load(root)
    blocking = [
        finding
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error"
    ]
    if not blocking:
        # The declared repair step, which was armed and not needed. Said rather
        # than left blank: "nothing was blocking" is the good outcome, and a step
        # that simply vanished would read as one that never ran for unknown
        # reasons.
        phases.finish_step(
            root,
            phase,
            "repair",
            status="skipped",
            note="nothing blocking stood against the plan",
        )
        return
    print()
    print(f"repairing the plan: {len(blocking)} blocking finding(s)")
    with state.transaction(root) as data:
        directory = planfiles.rounds_dir(root, data)
    # The declared `repair` step becomes round 1, and every round after it is
    # appended as it opens. How many there are is not knowable here: it depends on
    # what each patch actually fixed, which is what `adjudicate.loop` is bounded
    # over. So the record grows as the loop does, which is the honest shape.
    step_of: dict[int, str] = {}

    def announce(number: int, resolved) -> None:
        if not step_of:
            step_of[number] = "repair"
        elif number not in step_of:
            previous = step_of[max(step_of)]
            step_of[number] = f"repair:round-{number}"
            phases.add(
                root,
                phase,
                [
                    phases.make_step(
                        id=step_of[number],
                        kind="repair",
                        name=f"repair round {number}",
                        summary="answer what the previous round left blocking",
                    )
                ],
                after=previous,
            )
        phases.start_step(
            root,
            phase,
            step_of[number],
            resolved=resolved,
            directory=directory / f"round-{number}",
        )
        print(f"  round {number}: {resolved.display}")
        sys.stdout.flush()

    def record(round_) -> None:
        """How the round ended, in the words the terminal uses for it."""
        step = step_of.get(round_.number, "repair")
        if round_.error:
            phases.finish_step(
                root, phase, step, status="failed", error=_first_line(round_.error)
            )
            return
        if round_.refused:
            note = f"writ refused the patch ({len(round_.refused)} reason(s))"
        elif round_.questions:
            note = f"raised {len(round_.questions)} question(s) for a human"
        else:
            note = (
                f"applied at plan revision {round_.applied.get('revision')}; "
                f"blocking now {round_.blocking_after}"
            )
        phases.finish_step(root, phase, step, status="ok", note=note)

    def report(round_) -> None:
        # One `on_round` hook, two audiences. The record goes first so the step is
        # closed before anything can go wrong in the printing.
        record(round_)
        if round_.error:
            print(f"  round {round_.number} failed: {round_.error}", file=sys.stderr)
            return
        if round_.refused:
            print(f"  writ refused the patch ({len(round_.refused)} reason(s)):")
            for finding in round_.refused[:6]:
                print(f"    {finding.line()}")
            return
        if round_.questions and not round_.unanswered:
            print(
                f"  answered {len(round_.questions)} question(s) with their "
                "recommendations (autonomous)"
            )
        elif round_.questions:
            print(f"  raised {len(round_.unanswered)} question(s) for a human")
            return
        print(
            f"  applied: {_applied_line(round_.applied)}; "
            f"blocking now {round_.blocking_after}"
        )

    def recheck() -> list[str]:
        """Re-run the critics against the patched plan, if they ran at all.

        Tied to whether the critics read the plan in the first place. Re-running
        them over a patch when nobody asked for them spends agent runs the operator
        declined; skipping them when they did run would close a critic's finding on
        the adjudicator's word, which is the one thing the loop must not do.
        """
        if not _critics_requested(args):
            return []
        print("  re-reviewing the patched plan")
        _run_critics(
            args,
            root=root,
            doc=doc,
            chosen=_chosen_critics(args),
            plan_path=None,
            phase=phase,
            verify=True,
        )
        return []

    try:
        result = adjudicate.loop(
            root=root,
            doc=doc,
            directory=directory,
            agent=getattr(args, "adjudicator_agent", None)
            or getattr(args, "critic_agent", None)
            or args.agent,
            model=getattr(args, "adjudicator_model", None)
            or getattr(args, "critic_model", None)
            or args.model,
            timeout=args.timeout,
            cwd=args.cwd,
            max_rounds=getattr(args, "max_rounds", None),
            recheck=recheck,
            stream=not args.quiet,
            on_round=report,
            on_start=announce,
            autonomous=_autonomous(args, root),
        )
    except WritError as exc:
        print(f"repair did not run: {exc}", file=sys.stderr)
        phases.finish_step(
            root,
            phase,
            step_of.get(max(step_of), "repair"),
            status="failed",
            error=_first_line(str(exc)),
        )
        return
    print(
        f"  {len(result.rounds)} round(s): {result.resolved} resolved, "
        f"{result.remaining} still blocking"
    )
    if result.stopped and result.stopped != "clean":
        print(f"  stopped: {result.stopped}")


def _applied_line(applied: dict[str, Any]) -> str:
    """What a promoted repair changed, in one line."""
    parts = [
        f"{label} {', '.join(applied.get(key) or []) or 'none'}"
        for label, key in (("added", "tasks"), ("revised", "revised"), ("removed", "removed"))
    ]
    return f"{'; '.join(parts)} (plan revision {applied.get('revision')})"


def _critics_requested(args) -> bool:
    """Whether the critics should read the plan `writ plan` just committed.

    Absent (None) or `plan.critics: false` runs nothing, since each critic costs
    an agent run and writ does not spend those unasked; `--critics` with no names,
    or `plan.critics: true`, runs all of them; and a list runs exactly those.
    """
    requested = getattr(args, "critics", None)
    if requested is None or requested is False:
        return False
    return True


def _chosen_critics(args) -> list[critics.Critic]:
    """Which critics to run: the ones named, or all of them.

    `--critics coverage` names them outright; a bare `--critics`, or
    `plan.critics: true`, runs every one.
    """
    named = getattr(args, "critics", None)
    if isinstance(named, list) and named:
        return critics.by_name(named)
    return list(critics.CRITICS)


def _run_critics(args, *, root, doc, chosen, plan_path, phase=None, verify=False):
    """Run the critics over the committed plan and merge what they found.

    The plan files they read are re-exported from committed state rather than
    taken from the planner's draft, so the critics review the ids and edges that actually
    exist — which are what will be executed, and not always what the plan proposed.

    `verify` is set on a re-review after repair (docs/planning-redesign.md §5):
    each critic that read an earlier revision re-checks its own blockers and may
    raise new ones only on features that changed since.
    """
    with state.transaction(root) as data:
        files = _plan_files(root, data)
        contexts = {}
        if verify:
            for critic in chosen:
                context = critics.verify_context(data, critic)
                if context is not None:
                    contexts[critic.name] = context
    found = plans.findings(data, open_only=True)
    revision = plans.revision(data)
    directory = planfiles.reviews_dir(root, data, revision)
    step_id = _critic_steps(root, phase, chosen, revision=revision, data=data)
    if len(chosen) > 1 and not args.json:
        grouped = critics.waves(chosen)
        print(
            "critics at once: "
            + "; then ".join(", ".join(c.name for c in wave) for wave in grouped)
        )

    def record(critic, resolved, where) -> None:
        """Mark the critic running, outside the lock `announce` is held behind."""
        phases.start_step(
            root,
            phase,
            step_id(critic.name),
            resolved=resolved,
            directory=where,
            artifact=where / critics.REPORT_FILENAME,
        )

    def close(report) -> None:
        counts = plancheck.tally(report.findings)
        phases.finish_step(
            root,
            phase,
            step_id(report.critic),
            status="ok" if report.ok else "failed",
            exit_code=report.exit_code,
            error=_first_line(report.error or ""),
            note=(
                f"{counts['error']} blocking, {counts['warning']} advisory"
                if report.ok
                else ""
            ),
        )

    def announce(critic, resolved) -> None:
        if not args.json:
            print(f"critic {critic.name}: {critic.brief}")
            print(f"  running: {resolved.display}")
            sys.stdout.flush()

    def report_back(report) -> None:
        if args.json:
            return
        if not report.ok:
            print(f"  {report.critic} failed: {report.error}", file=sys.stderr)
            return
        counts = plancheck.tally(report.findings)
        # Named because the critics overlap: every header is printed before any
        # of them reports, so a bare count belongs to nobody.
        print(
            f"  {report.critic}: {counts['error']} blocking, {counts['warning']} advisory"
            + (f", confidence {report.confidence}" if report.confidence else "")
        )
        if report.summary:
            print(f"  {_first_line(report.summary)}")

    reports = critics.review(
        root=root,
        doc=doc,
        plan=files,
        directory=directory,
        chosen=chosen,
        agent=getattr(args, "critic_agent", None) or args.agent,
        model=getattr(args, "critic_model", None) or args.model,
        timeout=args.timeout,
        cwd=args.cwd,
        found=found,
        stream=not args.quiet,
        on_start=announce,
        on_finish=report_back,
        on_launch=record,
        on_close=close,
        verify=contexts,
    )
    with state.transaction(root) as live:
        written = critics.record(live, reports, root=root.resolve())
    if not args.json:
        _print_findings(
            plancheck.sort_findings(written),
            preamble=f"what the critics found ({len(written)} recorded):",
        )
        if not written:
            print("the critics found nothing to report")
    return reports


def _critic_steps(root, phase, chosen, *, revision: int, data):
    """Decide what to call each critic's step, and register a re-review's.

    The critics can run more than once over one plan. `--repair` patches the plan
    between rounds, `repair.apply_patch` bumps its revision, and `recheck` re-runs
    every critic against the patched plan at that new revision — writing to a new
    `reviews/r{n}` directory. So a second pass is not the declared step happening
    again, it is a different run of the same critic against a different plan, and
    giving it the declared step's id would overwrite the first pass's result with
    the second's and point its transcript link at the wrong directory.

    The first pass uses the declared ids, which is what lets those steps be visible
    as pending before anything runs. A later pass mints `critic:<name>@r<revision>`
    and appends them, because nobody could have known there would be one: it exists
    only because the critics objected and the adjudicator patched what they found.
    """
    if not phase:
        # `writ critique` and `writ adjudicate` keep no phase record; every
        # `phases.*` call is a no-op, and the ids are never read.
        return lambda name: f"critic:{name}"
    record = phases.current(data) or {}
    if record.get("id") != phase:
        return lambda name: f"critic:{name}"
    declared_now = {
        str(entry.get("id")): entry for entry in record.get("steps", [])
    }
    first_pass = any(
        declared_now.get(f"critic:{critic.name}", {}).get("status") == "pending"
        for critic in chosen
    )
    if first_pass:
        return lambda name: f"critic:{name}"

    suffix = f"@r{revision}"
    grouped = critics.waves(chosen)
    declared = [
        phases.make_step(
            id=f"critic:{critic.name}{suffix}",
            kind="critic",
            # The revision is in the name, not only in the note: a re-review's note
            # is rewritten with its verdict when it finishes, and without the
            # revision the second pass then reads exactly like the first — five
            # critic boxes twice over, with nothing to say which plan each read.
            name=f"{critic.name} r{revision}",
            summary=critic.brief,
            wave=wave,
            note=f"re-reviewing the patched plan at revision {revision}",
        )
        for wave, group in enumerate(grouped)
        for critic in group
    ]
    phases.add(root, phase, declared, after="repair")
    return lambda name: f"critic:{name}{suffix}"


def _plan_files(root: Path, data: dict[str, Any]) -> critics.PlanFiles:
    """Export the plan and say where a critic finds each part of it.

    The repo summary is the analysis artifact when the staged pipeline wrote one;
    the feasibility critic reads it for the language, test command and baseline.
    """
    index = planfiles.ensure(root, data)
    inventory: Path | None = index.parent / "inventory.json"
    if not inventory.exists():
        inventory = None
    return critics.PlanFiles(
        index=index,
        features=planfiles.features_dir(root, data),
        inventory=inventory,
    )


def cmd_approve(args) -> int:
    """Record human approval, which is what `writ run` actually requires."""
    root = Path(args.root)
    with state.transaction(root) as data:
        if not data["tasks"]:
            raise WritError("there is no plan to approve (run `writ plan` first)")
        result = plans.approve(
            data,
            actor=args.by,
            reason=args.reason or "",
            force=bool(args.force),
        )
        record = dict(result["plan"])
        accepted = result["accepted"]
    print(f"plan {record['status']} at revision {record['revision']}")
    if accepted:
        print(
            f"accepted {len(accepted)} open finding"
            f"{'s' if len(accepted) != 1 else ''} on the record: "
            f"{', '.join(accepted)}"
        )
        print(f"reason: {record['approval_note']}")
    print("next: writ run")
    return 0


def cmd_coverage(args) -> None:
    """Print the requirement coverage matrix."""
    data = state.load(Path(args.root))
    rows = plans.coverage(data)
    if args.requirement:
        rows = [row for row in rows if row["id"] == args.requirement]
        if not rows:
            raise WritError(f"unknown requirement: {args.requirement}")
    if args.uncovered:
        rows = [row for row in rows if row["state"] in ("uncovered", "unevidenced")]
    if args.json:
        render.emit_json(rows)
        return
    if not rows:
        if not plans.requirements(data):
            print(
                "this plan states no requirement inventory, so there is nothing to "
                "trace. A plan from `writ plan` without --extract records one."
            )
            return
        print("nothing matched")
        return
    width = max(len(row["id"]) for row in rows)
    for row in rows:
        marker = _COVERAGE_MARKS.get(row["state"], "?")
        print(
            f"{marker} {row['id']:<{width}} [{row['priority']}/{row['state']}] "
            f"{_first_line(row['text'], 78)}"
        )
        if row["tasks"]:
            done = f"{row['complete']}/{len(row['tasks'])} complete"
            print(f"    tasks: {', '.join(row['tasks'])} ({done})")
        if row["gates"]:
            print(f"    gates: {', '.join(row['gates'])}")
        if row["evidence"]:
            print(f"    evidence: {row['evidence']}")
        if row["reason"]:
            print(f"    reason: {row['reason']}")
        if row["state"] == "uncovered":
            print("    nothing implements this")
    states: dict[str, int] = {}
    for row in rows:
        states[row["state"]] = states.get(row["state"], 0) + 1
    print()
    print(", ".join(f"{count} {state}" for state, count in sorted(states.items())))


#: one character per coverage state, so a long matrix can be skimmed
_COVERAGE_MARKS = {
    "satisfied": "✓",
    "in-progress": "~",
    "planned": "·",
    "at-risk": "!",
    "uncovered": "✗",
    "unevidenced": "✗",
    "out-of-scope": "–",
    "deferred": "–",
}


# --------------------------------------------------------------------------
# listing and inspection


def cmd_list(args) -> None:
    """One listing command for every collection.

    Which noun you want is an argument, not a separate command: the filters and
    the JSON shape are the same idea in each case, and keeping them together
    means `--json` and `--limit` behave identically everywhere.
    """
    data = state.load(args.root)
    handler = {
        "tasks": _list_tasks,
        "milestones": _list_milestones,
        "runs": _list_runs,
        "decisions": _list_decisions,
        "findings": _list_findings,
        "requirements": _list_requirements,
        "gates": _list_gates,
        "repairs": _list_repairs,
    }[args.what]
    headers, rows, payload = handler(data, args)
    if args.limit:
        rows, payload = rows[: args.limit], payload[: args.limit]
    if args.json:
        render.emit_json(payload)
        return
    print(render.table(headers, rows))


def _list_tasks(data, args):
    refresh_milestones(data)
    rows, payload = [], []
    for task_id in sorted(data["tasks"]):
        task = data["tasks"][task_id]
        status = effective_status(data, task)
        if args.status and status != args.status:
            continue
        if args.milestone and task.get("milestone") != args.milestone:
            continue
        if args.ready and status != "ready":
            continue
        if getattr(args, "awaiting_review", False) and status != "awaiting-review":
            continue
        counts = acceptance_summary(task)
        rows.append(
            [
                render.mark(status),
                task_id,
                status,
                f"{counts['passed']}/{counts['total']}",
                ",".join(task.get("depends_on", [])) or "-",
                task["title"],
            ]
        )
        payload.append(
            {
                "id": task_id,
                "status": status,
                # A reader filtering this listing almost always wants one or the
                # other — the work, or the checks over it — and without `kind` the
                # only way to tell them apart is the shape of the id.
                "kind": task.get("kind", "task"),
                "title": task["title"],
                "milestone": task.get("milestone"),
                "depends_on": task.get("depends_on", []),
                "requirement_ids": task.get("requirement_ids", []),
                "acceptances": counts,
            }
        )
    return ["", "ID", "STATUS", "ACC", "DEPS", "TITLE"], rows, payload


def _list_milestones(data, args):
    refresh_milestones(data)
    rows, payload = [], []
    for milestone_id in sorted(data["milestones"]):
        milestone = data["milestones"][milestone_id]
        if args.status and milestone["status"] != args.status:
            continue
        tasks = milestone_tasks(data, milestone_id)
        done = sum(1 for task in tasks if task["status"] == "completed")
        rows.append(
            [
                render.mark(milestone["status"]),
                milestone_id,
                milestone["status"],
                f"{done}/{len(tasks)}",
                milestone["title"],
            ]
        )
        payload.append(
            {
                "id": milestone_id,
                "status": milestone["status"],
                "title": milestone["title"],
                "tasks_total": len(tasks),
                "tasks_completed": done,
            }
        )
    return ["", "ID", "STATUS", "DONE", "TITLE"], rows, payload


def _list_runs(data, args):
    rows, payload = [], []
    for run_id in sorted(data["runs"]):
        run = data["runs"][run_id]
        if args.task and run["task"] != args.task:
            continue
        if args.status and run["status"] != args.status:
            continue
        if args.active and run["status"] not in runner.ACTIVE_RUN_STATUSES:
            continue
        alive = runner.run_alive(run)
        rows.append(
            [
                render.mark(run["status"]),
                run_id,
                run["task"],
                run["status"],
                run.get("exit_code") if run.get("exit_code") is not None else "-",
                "yes" if alive else "no",
                run.get("started_at") or run.get("created_at") or "-",
            ]
        )
        payload.append({**run, "alive": alive})
    return ["", "RUN", "TASK", "STATUS", "EXIT", "ALIVE", "STARTED"], rows, payload


def _list_decisions(data, args):
    items = data["decisions"]
    if args.task:
        items = [item for item in items if args.task in item.get("tasks", [])]
    if args.status:
        items = [item for item in items if item["status"] == args.status]
    if getattr(args, "proposed", False):
        items = [item for item in items if item["status"] == "proposed"]
    if getattr(args, "autonomous", False):
        items = [
            item for item in items if item.get("confirmed_by") == decisions.AUTONOMOUS
        ]
    rows = [
        [
            i["id"],
            i["status"],
            i.get("proposed_by") or "",
            i.get("confirmed_by") or "-",
            i["title"],
        ]
        for i in items
    ]
    return ["ID", "STATUS", "BY", "RULED BY", "TITLE"], rows, list(items)


def _list_findings(data, args):
    """Everything wrong with the plan that anyone has recorded.

    One ledger for writ's own checks and the gates' reports, because the reader's
    question is "what is wrong with this plan", not "which component noticed".
    """
    items = plans.finding_records(data)
    if args.status:
        items = [item for item in items if item.get("disposition") == args.status]
    if getattr(args, "open", False):
        items = [item for item in items if item.get("disposition") == "open"]
    if args.task:
        items = [item for item in items if args.task in (item.get("where") or "")]
    rows = [
        [
            render.mark("failed" if i.get("severity") == "error" else "blocked"),
            i.get("id") or "-",
            i.get("severity", ""),
            i.get("disposition", ""),
            i.get("where") or "-",
            i.get("message", ""),
        ]
        for i in items
    ]
    return ["", "ID", "SEVERITY", "STATE", "WHERE", "FINDING"], rows, list(items)


def _list_requirements(data, args):
    """The inventory, with what covers each entry.

    The coverage column is the point: a requirement with no task against it is the
    hole this whole inventory exists to make visible, and a list that showed only
    the text would hide it behind having been written down.
    """
    rows, payload = [], []
    for entry in plans.coverage(data):
        state_ = entry["state"]
        if getattr(args, "uncovered", False) and state_ != "uncovered":
            continue
        if args.status and state_ != args.status:
            continue
        rows.append(
            [
                _COVERAGE_MARKS.get(state_, "?"),
                entry["id"],
                entry.get("priority", ""),
                state_,
                ",".join(entry.get("tasks", [])) or "-",
                entry.get("text", ""),
            ]
        )
        payload.append(entry)
    return ["", "ID", "PRI", "COVERAGE", "TASKS", "REQUIREMENT"], rows, payload


def _list_gates(data, args):
    """The plan-level checks, and what each has decided so far."""
    items = gates.gates(data)
    rows, payload = [], []
    held = orchestrator.held_gates(data)
    for gate in items:
        status = effective_status(data, gate)
        if args.status and status != args.status:
            continue
        attempts = gates.attempts(gate)
        last = attempts[-1] if attempts else {}
        rows.append(
            [
                render.mark(status),
                gate["id"],
                gate.get("scope") or "-",
                status,
                last.get("decision") or "-",
                str(len(attempts)),
                held.get(gate["id"], "") or gate.get("title", ""),
            ]
        )
        payload.append({**gate, "effective_status": status})
    return ["", "ID", "SCOPE", "STATUS", "LAST", "RUNS", "NOTE"], rows, payload


def _list_repairs(data, args):
    """Every time the plan has been asked to change, and what came of it.

    Both occasions, in one list. A gate-scoped request names the gate that asked; a
    plan-scoped one shows `plan`, because what asked was `writ adjudicate` before
    any of it ran.
    """
    items = repair.requests(data)
    if args.status:
        items = [item for item in items if item.get("status") == args.status]
    if args.task:
        items = [
            item for item in items if repair.scope_of(item) == args.task
        ]
    rows = [
        [
            i.get("id", ""),
            repair.scope_of(i),
            i.get("status", ""),
            str(i.get("round", 1)),
            str(len(i.get("refusals") or [])),
            ",".join(i.get("findings") or []) or "-",
            _first_line(i.get("summary", ""), 48),
        ]
        for i in items
    ]
    return (
        ["ID", "SCOPE", "STATUS", "ROUND", "REFUSED", "FINDINGS", "SUMMARY"],
        rows,
        list(items),
    )


def cmd_show(args) -> None:
    """Show any one thing, whatever kind of id it is.

    Ids carry their own type (`M01`, `M01-001`, a run stamp, `D-0001`), so
    asking the user to also name the type would be redundant.
    """
    data = state.load(args.root)
    refresh_milestones(data)
    kind, item = find(data, args.id)
    if kind == "run" and getattr(args, "prompt", False):
        print(_run_prompt(item), end="")
        return
    if args.json:
        if kind == "milestone":
            item = dict(item)
            item["task_details"] = sorted(
                milestone_tasks(data, args.id), key=lambda t: t["id"]
            )
        elif kind == "run":
            item = {**item, "alive": runner.run_alive(item)}
        render.emit_json(item)
        return
    renderer = {
        "task": lambda: _render_task(data, item),
        "milestone": lambda: _render_milestone(
            data, item, verbose=getattr(args, "verbose", False)
        ),
        "run": lambda: _render_run(item),
        "decision": lambda: _render_decision(item),
        "finding": lambda: _render_finding(data, item),
        "repair": lambda: _render_repair(data, item),
    }[kind]
    print(renderer())


def _render_finding(data: dict[str, Any], record: dict[str, Any]) -> str:
    """One finding: what was said, who said it, and what became of it."""
    disposition = record.get("disposition", "open")
    lines = [f"{record['id']} — {record.get('category', 'unspecified')}"]
    lines.append(f"severity: {record.get('severity', 'warning')}")
    lines.append(f"disposition: {disposition}")
    lines.append(f"raised by: {record.get('source', 'writ')}")
    if record.get("where"):
        lines.append(f"about: {record['where']}")
    if record.get("requirement_ids"):
        lines.append(f"requirements: {', '.join(record['requirement_ids'])}")
    if record.get("first_seen_at"):
        lines.append(
            f"first seen: {record['first_seen_at']} "
            f"(revision {record.get('revision', '?')})"
        )
    if int(record.get("seen_count", 1)) > 1:
        # A finding re-raised by later checks is one the plan keeps reproducing,
        # which is worth more than the fact that it exists.
        lines.append(
            f"raised again since: {record['seen_count']} checks, last "
            f"{record.get('seen_at', '')}"
        )
    lines.append(f"\n{record.get('message', '')}")
    if record.get("suggested_action"):
        lines.append(f"\nsuggested:\n{record['suggested_action']}")
    if disposition != "open":
        # How it was answered is the point of reading a closed finding. A plan that
        # ran with a known objection is legible only if the reason it was overruled
        # is here, next to the objection, rather than in an approval note somewhere.
        # A person's judgement is `disposed_by`; writ closing its own finding is
        # `resolved_by`. Reading either one only ever printed the first, so every
        # resolved finding said "resolved by ?" about something it knew.
        who = record.get("disposed_by") or record.get("resolved_by") or "?"
        when = record.get("disposed_at") or record.get("resolved_at") or ""
        lines.append(f"\n{disposition} by {who}{f' ({when})' if when else ''}")
        if record.get("reason"):
            lines.append(f"reason: {record['reason']}")
        if record.get("change"):
            lines.append(f"change: {record['change']}")
    asked = [
        request
        for request in repair.requests(data)
        if record["id"] in (request.get("findings") or [])
    ]
    if asked:
        listed = ", ".join(request["id"] for request in asked)
        lines.append(f"\nrepair requested: {listed}   (writ show <id>)")
    if disposition == "open" and record.get("severity") == "error":
        # An open blocking finding is the reason a plan will not run, so the reader
        # is here to decide what to do about it, not only to read it.
        lines.append(
            f"\nblocking. answer it, with a reason either way:"
            f"\n  writ set {record['id']} accepted --reason ...   # stands, run anyway"
            f"\n  writ set {record['id']} declined --reason ...   # the reviewer is wrong"
        )
    return "\n".join(lines)


def _render_repair(data: dict[str, Any], record: dict[str, Any]) -> str:
    """One repair request: what a gate asked for, and every answer it got.

    The refusals are the substance when a gate is held as `repair-refused` — they
    are why writ would not apply what the planner proposed, and a reader sent here
    by that hold is here for exactly that.
    """
    lines = [f"{record['id']} — repair requested by {record.get('gate', '?')}"]
    lines.append(f"status: {record.get('status', 'open')}")
    lines.append(f"round: {record.get('round', 1)}")
    lines.append(
        f"opened: {record.get('opened_at', '')} by {record.get('opened_by', '?')}"
    )
    lines.append(f"plan revision when opened: {record.get('base_revision', '?')}")
    if record.get("closed_at"):
        lines.append(f"closed: {record['closed_at']}")
    if record.get("findings"):
        lines.append(f"findings to close: {', '.join(record['findings'])}")
    if record.get("summary"):
        lines.append(f"\nwhat the gate asked for:\n{record['summary']}")
    if record.get("note"):
        lines.append(f"\nnote:\n{record['note']}")
    for number, refusal in enumerate(record.get("refusals") or [], start=1):
        lines.append(
            f"\nrefused patch {number} ({refusal.get('at', '')}, "
            f"run {refusal.get('run', '?')}):"
        )
        for reason in refusal.get("reasons") or []:
            lines.append(f"  · {reason.get('message', '')}")
    for attempt in record.get("attempts") or []:
        lines.append(
            f"\napplied {attempt.get('at', '')}: "
            f"{_first_line(str(attempt.get('summary', '')), 64)}"
        )
    gate = data["tasks"].get(record.get("gate", ""))
    if gate is not None and gate.get("held"):
        held = gate["held"]
        lines.append(
            f"\n{gate['id']} is held ({held.get('reason', '')}) and will not "
            f"re-run until a human moves it."
        )
    return "\n".join(lines)


def _run_prompt(run: dict[str, Any]) -> str:
    path = Path(run["dir"]) / "prompt.txt"
    if not path.exists():
        raise WritError(f"no prompt recorded for run {run['id']}")
    return path.read_text(encoding="utf-8")


def _render_run(run: dict[str, Any]) -> str:
    lines = [f"{run['id']}  ({run['task']})"]
    lines.append(f"status: {run['status']}  exit: {run.get('exit_code')}")
    lines.append(f"command: {shlex.join(run['command'])}")
    if run.get("model"):
        lines.append(f"model: {run['model']}")
    lines.append(f"cwd: {run.get('cwd')}")
    lines.append(f"timeout: {run.get('timeout') or '-'}")
    lines.append(
        f"pid: {run.get('pid') or '-'}  "
        f"alive: {'yes' if runner.run_alive(run) else 'no'}"
    )
    if run.get("supervisor_pid"):
        lines.append(f"supervisor pid: {run['supervisor_pid']}")
    lines.append(f"created: {run.get('created_at')}")
    lines.append(f"started: {run.get('started_at') or '-'}")
    lines.append(f"finished: {run.get('finished_at') or '-'}")
    if run.get("note"):
        lines.append(f"note: {run['note']}")
    failure = run.get("failure")
    if failure:
        # The first question about a run that did not finish is whether to look at
        # the code or at the machine. An unclassified error string made a reader
        # guess, and a provider outage guesses as "the agent did badly".
        lines.append(
            f"failure: {failure.get('category')}"
            + ("  (retryable)" if failure.get("retryable") else "")
            + f"\n  {failure.get('reason', '')}"
        )
        for frame in failure.get("where") or []:
            lines.append(f"  at {frame}")
    lines.append(f"dir: {run.get('dir')}")
    lines.append(f"\noutput: writ logs {run['id']}")
    lines.append(f"prompt: writ show {run['id']} --prompt")
    return "\n".join(lines)


def _render_decision(record: dict[str, Any]) -> str:
    lines = [f"{record['id']} — {record['title']}"]
    lines.append(f"date: {record['date']}")
    lines.append(f"status: {record['status']}")
    if record.get("proposed_by"):
        lines.append(f"proposed by: {record['proposed_by']}")
    if record.get("confirmed_by") and record["status"] != "proposed":
        lines.append(f"ruled by: {record['confirmed_by']} ({record['confirmed_at']})")
    if record.get("supersedes"):
        lines.append(f"supersedes: {record['supersedes']}")
    if record.get("superseded_by"):
        lines.append(f"superseded by: {record['superseded_by']}")
    if record.get("tasks"):
        lines.append(f"tasks: {', '.join(record['tasks'])}")
    if record.get("context"):
        lines.append(f"\ncontext:\n{record['context']}")
    lines.append(f"\ndecision:\n{record['decision']}")
    if record.get("consequences"):
        lines.append(f"\nconsequences:\n{record['consequences']}")
    if record.get("rejected_reason"):
        lines.append(f"\nrejected:\n{record['rejected_reason']}")
    if record["status"] == "proposed":
        lines.append(
            f"\nproposed by an agent and not yet confirmed."
            f"\n  writ set {record['id']} active"
            f"\n  writ set {record['id']} rejected --reason ..."
        )
    return "\n".join(lines)


def _render_milestone(
    data: dict[str, Any], milestone: dict[str, Any], *, verbose: bool = False
) -> str:
    """A milestone, its rollup, and its member tasks.

    With `verbose`, every member task is expanded in full, so one command can
    answer "what does this milestone actually commit me to".
    """
    tasks = sorted(milestone_tasks(data, milestone["id"]), key=lambda t: t["id"])
    done = sum(1 for task in tasks if task["status"] == "completed")
    lines = [f"{milestone['id']}  {milestone['title']}"]
    lines.append(f"status: {milestone['status']}")
    lines.append(f"tasks: {done}/{len(tasks)}  {render.bar(done, len(tasks))}")
    lines.append(f"design section: {milestone.get('design_section') or '-'}")
    criteria = sum(len(task.get("acceptances", [])) for task in tasks)
    passed = sum(
        1
        for task in tasks
        for item in task.get("acceptances", [])
        if item["status"] == "passed"
    )
    lines.append(f"acceptance criteria: {passed}/{criteria} passed")

    if verbose:
        for task in tasks:
            lines.append("")
            lines.append("-" * 60)
            lines.append(_render_task(data, task))
        return "\n".join(lines)

    lines.append("\ntasks:")
    lines.append(
        render.table(
            ["", "ID", "STATUS", "ACC", "DEPS", "TITLE"],
            [
                [
                    render.mark(effective_status(data, task)),
                    task["id"],
                    effective_status(data, task),
                    f"{acceptance_summary(task)['passed']}/"
                    f"{acceptance_summary(task)['total']}",
                    ",".join(task.get("depends_on", [])) or "-",
                    task["title"],
                ]
                for task in tasks
            ],
        )
    )
    lines.append(f"\nfull detail: writ show {milestone['id']} --verbose")
    return "\n".join(lines)


def _render_task(data: dict[str, Any], task: dict[str, Any]) -> str:
    """Everything recorded about one task, including what depends on it."""
    status = effective_status(data, task)
    lines = [f"{task['id']}  {task['title']}"]
    lines.append(f"status: {status}")
    milestone_id = task.get("milestone")
    if milestone_id:
        milestone = data["milestones"].get(milestone_id, {})
        lines.append(f"milestone: {milestone_id} — {milestone.get('title', '')}")
    else:
        lines.append("milestone: -")
    lines.append(f"depends on: {', '.join(task.get('depends_on', [])) or '-'}")
    blockers = blocking_dependencies(data, task)
    if blockers:
        lines.append(f"blocked by: {', '.join(blockers)}")
    # Near the status rather than down in the evidence. A task blocked by its own
    # report has no unsatisfied dependency, so `blocked by:` above says nothing and
    # every dependency reads as met; without this the reason is one history line
    # under the criteria, and nothing clears a block on its own.
    reason = blocked_on(task)
    if reason:
        lines.append(f"blocked on: {reason}")
    dependents = [
        other["id"]
        for other in sorted(data["tasks"].values(), key=lambda t: t["id"])
        if task["id"] in other.get("depends_on", [])
    ]
    if dependents:
        lines.append(f"blocks: {', '.join(dependents)}")
    lines.append(f"design: {task.get('design_doc') or '-'}")
    lines.append(f"section: {task.get('design_section') or '-'}")
    counts = acceptance_summary(task)
    lines.append(
        f"\nacceptance criteria ({counts['passed']}/{counts['total']} passed):"
    )
    for index, acceptance in enumerate(task.get("acceptances", []), start=1):
        lines.extend(render.acceptance_detail(index, acceptance))
    if not task.get("acceptances"):
        lines.append("  (none recorded)")
    last = task.get("last_verdict")
    if last:
        who = last.get("actor") or last.get("role", "agent")
        claim = last.get("decision") or last.get("outcome")
        lines.append(f"\nlast verdict: {claim} by {who} at {last.get('at')}")
        if last.get("summary"):
            lines.append(f"  {last['summary']}")
    record = task.get("rework")
    if record:
        attempt = record.get("attempt", 0)
        head = (
            f"\nrework: attempt {attempt} of "
            f"{record.get('budget', record.get('max'))}, "
            + (
                f"returned unfinished ({record.get('reason')}) at {record.get('at')}"
                if record.get("kind") == "unfinished"
                else f"rejected by {record.get('reviewer') or '-'} "
                f"at {record.get('at')}"
            )
        )
        if record.get("resolved_at"):
            head += f" — answered, accepted at {record['resolved_at']}"
        elif record.get("exhausted"):
            head += " — budget spent, left failed"
        lines.append(head)
        for finding in record.get("findings") or []:
            lines.append(
                f"  {finding.get('number')}. {finding.get('status')}: "
                f"{finding.get('evidence', '')}".rstrip()
            )
        if record.get("notes"):
            lines.append(f"  notes: {record['notes']}")
    if status == "awaiting-review":
        lines.append(f"\nawaiting review: writ review {task['id']}")
    if task.get("allowed"):
        lines.append("\nallowed:")
        lines.extend(f"  - {entry}" for entry in task["allowed"])
    if task.get("forbidden"):
        lines.append("\nforbidden:")
        lines.extend(f"  - {entry}" for entry in task["forbidden"])
    if task.get("runs"):
        lines.append("\nruns:")
        for run_id in task["runs"]:
            run = data["runs"].get(run_id, {})
            role = run.get("role", "agent")
            lines.append(
                f"  - {run_id}  [{role}]  {run.get('status')}  "
                f"exit={run.get('exit_code')}"
            )
            if run.get("verdict_error"):
                lines.append(f"      unusable verdict: {run['verdict_error']}")
            elif run.get("no_verdict"):
                lines.append(f"      no verdict: {run['no_verdict']}")
            if run.get("verdict_downgraded"):
                lines.append(f"      downgraded: {run['verdict_downgraded']}")
            if run.get("verdict_misplaced"):
                lines.append(f"      verdict found at: {run['verdict_misplaced']}")
    if task.get("evidence"):
        lines.append("\nevidence:")
        for entry in task["evidence"]:
            actor = entry.get("actor", "operator")
            lines.append(f"  - {entry['at']}  [{actor}] {entry['text']}")
    return "\n".join(lines)


def _status_payload(data: dict[str, Any]) -> dict[str, Any]:
    refresh_milestones(data)
    tasks = data["tasks"]
    counts: dict[str, int] = {}
    for task in tasks.values():
        status = effective_status(data, task)
        counts[status] = counts.get(status, 0) + 1
    active_runs = [
        run
        for run in data["runs"].values()
        if run["status"] in runner.ACTIVE_RUN_STATUSES
    ]
    return {
        "design_docs": data.get("design_docs", []),
        "milestones": len(data["milestones"]),
        "milestones_completed": sum(
            1 for m in data["milestones"].values() if m["status"] == "completed"
        ),
        "tasks": len(tasks),
        "tasks_completed": counts.get("completed", 0),
        "counts": counts,
        "ready": [task["id"] for task in ready_tasks(data)],
        "awaiting_review": [task["id"] for task in reviewable_tasks(data)],
        "running": [
            task_id
            for task_id, task in sorted(tasks.items())
            if task["status"] in ("running", "reviewing")
        ],
        "failed": [
            task_id
            for task_id, task in sorted(tasks.items())
            if task["status"] == "failed"
        ],
        "active_runs": [
            {
                "id": run["id"],
                "task": run["task"],
                "status": run["status"],
                "pid": run.get("pid"),
                "started_at": run.get("started_at"),
                "alive": runner.run_alive(run),
            }
            for run in active_runs
        ],
        "decisions": len(data["decisions"]),
        "proposed_decisions": [
            item["id"] for item in decisions.proposed(data)
        ],
    }


def cmd_status(args) -> None:
    """Progress and live runs, once or repeatedly.

    Following is a mode of looking at status, not a different question, so it is
    a flag rather than a `watch` command.
    """
    if getattr(args, "watch", False):
        _watch_status(args)
        return
    data = state.load(args.root)
    payload = _status_payload(data)
    if args.json:
        render.emit_json(payload)
        return
    print(_render_status(payload))


def _watch_status(args) -> None:  # pragma: no cover - interactive loop
    try:
        while True:
            payload = _status_payload(state.load(args.root))
            if not args.no_clear:
                os.system("clear" if shutil.which("clear") else "")
            print(f"writ status --watch — {state.utcnow()}  (ctrl-c to exit)\n")
            print(_render_status(payload))
            if not payload["active_runs"] and args.until_idle:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def _render_status(payload: dict[str, Any]) -> str:
    lines = []
    lines.append(
        f"tasks {payload['tasks_completed']}/{payload['tasks']}  "
        + render.bar(payload["tasks_completed"], payload["tasks"])
    )
    lines.append(
        f"milestones {payload['milestones_completed']}/{payload['milestones']}  "
        + render.bar(payload["milestones_completed"], payload["milestones"])
    )
    breakdown = "  ".join(
        f"{status}:{count}" for status, count in sorted(payload["counts"].items())
    )
    lines.append(f"by status: {breakdown or '-'}")
    lines.append(f"decisions: {payload['decisions']}")
    if payload["running"]:
        lines.append(f"running: {', '.join(payload['running'])}")
    if payload["failed"]:
        lines.append(f"failed: {', '.join(payload['failed'])}")
    if payload["ready"]:
        lines.append(f"ready to dispatch: {', '.join(payload['ready'][:8])}")
    if payload.get("awaiting_review"):
        listed = ", ".join(payload["awaiting_review"][:8])
        lines.append(f"awaiting review: {listed}   (writ review)")
    if payload.get("proposed_decisions"):
        listed = ", ".join(payload["proposed_decisions"][:8])
        lines.append(f"decisions proposed: {listed}   (writ show <id>)")
    if payload["active_runs"]:
        lines.append("")
        lines.append(
            render.table(
                ["RUN", "TASK", "STATUS", "PID", "ALIVE", "STARTED"],
                [
                    [
                        run["id"],
                        run["task"],
                        run["status"],
                        run["pid"] or "-",
                        "yes" if run["alive"] else "no",
                        run["started_at"] or "-",
                    ]
                    for run in payload["active_runs"]
                ],
            )
        )
    return "\n".join(lines)


def cmd_graph(args) -> None:
    """Draw the dependency DAG.

    The default is a tree following dependencies forwards, because the useful
    questions about a DAG are shape questions: what unlocks next, where the work
    forks, what one task is holding up. A per-task list of dependencies answers
    none of those without the reader assembling the graph in their head.
    """
    data = state.load(args.root)
    check_dag(data)
    tasks = data["tasks"]
    if args.dot:
        _graph_dot(data, tasks)
        return
    if args.json:
        render.emit_json(
            {
                "levels": render.dag_levels(tasks),
                "tasks": {
                    task_id: {
                        "status": effective_status(data, task),
                        "depends_on": task.get("depends_on", []),
                        "blocks": sorted(
                            other
                            for other, item in tasks.items()
                            if task_id in item.get("depends_on", [])
                        ),
                    }
                    for task_id, task in tasks.items()
                },
            }
        )
        return
    if not tasks:
        print("(no tasks)")
        return
    if args.levels:
        print(_graph_levels(data, tasks))
        return
    print(_graph_tree(data, tasks, verbose=args.verbose))


def _graph_label(data, tasks, task_id: str, *, verbose: bool) -> str:
    task = tasks[task_id]
    status = effective_status(data, task)
    label = f"{render.mark(status)} {task_id}"
    if verbose:
        summary = acceptance_summary(task)
        label += f"  {task['title']}"
        label += f"  [{status}"
        if summary["total"]:
            label += f", {summary['passed']}/{summary['total']}"
        label += "]"
    else:
        label += f"  {task['title']}"
    return label


def _graph_tree(data, tasks, *, verbose: bool) -> str:
    lines = render.dag_tree(
        tasks, label=lambda t: _graph_label(data, tasks, t, verbose=verbose)
    )
    orphans = _graph_orphans(data, tasks, verbose=verbose)
    body = "\n".join(lines)
    if orphans:
        body += "\n\n" + "\n".join(orphans)
    return body + "\n\n" + _graph_legend(tasks)


def _graph_orphans(data, tasks, *, verbose: bool) -> list[str]:
    """Tasks the tree cannot reach, which only happens in a broken store.

    `check_dag` rules out cycles, so this should be empty. Printing it anyway
    beats silently dropping a task from a view someone is using to plan.
    """
    drawn = set()
    for line in render.dag_tree(tasks, label=lambda t: t):
        stripped = line.strip().lstrip("├└─│↩ ")
        if stripped:
            drawn.add(stripped.split()[0])
    missing = sorted(set(tasks) - drawn)
    if not missing:
        return []
    return ["unreachable (report this):"] + [
        f"  {_graph_label(data, tasks, t, verbose=verbose)}" for t in missing
    ]


def _graph_legend(tasks) -> str:
    levels = render.dag_levels(tasks)
    widest = max(len(level) for level in levels)
    count = len(tasks)
    line = f"{count} task{'' if count == 1 else 's'}, {len(levels)} deep"
    if widest > 1:
        line += f", up to {widest} in parallel"
    if any(
        len([d for d in task.get("depends_on", []) if d in tasks]) > 1
        for task in tasks.values()
    ):
        line += "   ↩ joins a task drawn under its last dependency"
    return line


def _graph_levels(data, tasks) -> str:
    """The DAG by dependency level: what could run at the same time."""
    lines = []
    for index, level in enumerate(render.dag_levels(tasks), start=1):
        count = len(level)
        lines.append(f"level {index}  ({count} task{'' if count == 1 else 's'})")
        for task_id in level:
            task = tasks[task_id]
            deps = ", ".join(task.get("depends_on", []))
            suffix = f"   after {deps}" if deps else ""
            status = effective_status(data, task)
            lines.append(
                f"  {render.mark(status)} {task_id}  {task['title']}{suffix}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


#: fill colours by status, for `--dot`. Muted on purpose: the graph is read for
#: its shape, and saturated fills fight the structure for attention.
DOT_FILLS = {
    "completed": "#d8ece0",
    "awaiting-review": "#fdf0cf",
    "reviewing": "#fdf0cf",
    "running": "#d9e7f7",
    "failed": "#f8d9d9",
    "blocked": "#f8d9d9",
    "cancelled": "#eeeeee",
    "ready": "#ffffff",
    "planned": "#f7f7f7",
}


def _graph_dot(data, tasks) -> None:
    """Emit graphviz, carrying the status the terminal view shows.

    A rendered graph is where progress is most legible, so dropping status here
    would make the prettier output the less useful one.
    """
    print("digraph writ {")
    print('  rankdir=LR;')
    print('  graph [fontname="Helvetica", fontsize=11];')
    print(
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", '
        'fontsize=10, color="#999999"];'
    )
    print('  edge [color="#777777", arrowsize=0.7];')
    for milestone_id in sorted(data["milestones"]):
        members = [
            task_id
            for task_id in sorted(tasks)
            if tasks[task_id].get("milestone") == milestone_id
        ]
        if not members:
            continue
        title = _dot_escape(data["milestones"][milestone_id]["title"])
        # An autocreated milestone is titled with its own id; "M02  M02" is noise.
        heading = milestone_id if title == milestone_id else f"{milestone_id}  {title}"
        print(f"  subgraph cluster_{milestone_id.replace('-', '_')} {{")
        print(
            f'    label="{heading}"; style=rounded; '
            'color="#bbbbbb"; fontsize=11;'
        )
        for task_id in members:
            print(f"    {_dot_node(data, tasks[task_id], task_id)}")
        print("  }")
    loose = [t for t in sorted(tasks) if tasks[t].get("milestone") not in data["milestones"]]
    for task_id in loose:
        print(f"  {_dot_node(data, tasks[task_id], task_id)}")
    for task_id in sorted(tasks):
        for dep in tasks[task_id].get("depends_on", []):
            print(f'  "{dep}" -> "{task_id}";')
    print("}")


def _dot_node(data, task, task_id: str) -> str:
    status = effective_status(data, task)
    summary = acceptance_summary(task)
    label = f"{task_id}\\n{_dot_escape(task['title'])}"
    if summary["total"]:
        label += f"\\n{status}  {summary['passed']}/{summary['total']}"
    else:
        label += f"\\n{status}"
    fill = DOT_FILLS.get(status, "#f7f7f7")
    extra = ' penwidth=2 color="#555555"' if status == "ready" else ""
    return f'"{task_id}" [label="{label}", fillcolor="{fill}"{extra}];'


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', "'")


# --------------------------------------------------------------------------
# mutation


def cmd_set(args) -> None:
    """Set a status on whatever the id points at.

    Tasks, decisions and findings all have a state a human may legitimately move,
    and which one you meant is already in the id, so one verb covers all three.
    What each of them refuses to accept differs, and says something about where
    authority sits: a task cannot be set `completed` (the reviewer judges that), and
    a finding cannot be set `resolved` (a check or a gate demonstrates that).
    """
    with state.transaction(args.root) as data:
        kind, _ = find(data, args.id)
        if kind == "decision":
            _set_decision(args, data)
            return
        if kind == "finding":
            _set_finding(args, data)
            return
        if kind != "task":
            raise WritError(
                f"{args.id} is a {kind}; only tasks, decisions and findings have "
                "a status you can set"
            )
        if args.status not in SETTABLE_STATUSES:
            raise WritError(
                f"{args.status!r} is a decision status, not a task status "
                f"(tasks accept {', '.join(SETTABLE_STATUSES)})"
            )
        set_status(
            data, args.id, args.status, evidence=args.evidence, force=args.force
        )
    print(f"{args.id} -> {args.status}")


def _set_finding(args, data) -> None:
    """Dispose of one finding, rather than accepting every one of them.

    `writ approve --force` is the wholesale lever: it accepts every open finding
    under a single reason. That is the wrong instrument for disagreeing with one
    finding, because it also silently accepts the ones the reader never looked at.
    Here each finding gets its own answer and its own reason, which is what makes
    the ledger an audit trail instead of a list that was once overruled in bulk.
    """
    if args.status not in plans.SETTABLE_DISPOSITIONS:
        if args.status == "resolved":
            raise WritError(
                f"{args.id} cannot be set resolved by hand: a finding resolves when "
                "a check or a gate demonstrates the outcome it asked for. To let the "
                "plan proceed with this finding standing, accept it."
            )
        raise WritError(
            f"{args.status!r} is not a finding disposition "
            f"(findings accept {', '.join(plans.SETTABLE_DISPOSITIONS)})"
        )
    if not args.reason:
        # Both answers need one. An accepted finding without a reason is the
        # silent ignoring the report is against, and a declined one without a
        # reason is an assertion that the reviewer was wrong, unargued.
        verb = {"accepted": "accept", "declined": "decline"}[args.status]
        raise WritError(
            f"--reason is required to {verb} a finding: say why {args.id} "
            "does not block the plan"
        )
    record = plans.dispose(
        data,
        args.id,
        args.status,
        actor=getattr(args, "by", None) or "operator",
        reason=args.reason,
        change=args.evidence or "",
    )
    print(f"{record['id']} {args.status}: {_first_line(record.get('message', ''), 64)}")
    print(f"reason: {record['reason']}")
    blocking = [
        finding
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error"
    ]
    if blocking:
        listed = ", ".join(finding.id for finding in blocking)
        print(f"still blocking: {listed}")
    elif not plans.runnable(data):
        # Disposing of the last blocker does not itself approve the plan. The
        # status comes from a check, so point at the one that will grant it rather
        # than leaving the reader to guess why `writ run` still refuses.
        print("no blocking findings left — next: writ check")


def _set_decision(args, data) -> None:
    """Rule on a decision an agent proposed."""
    if args.status not in decisions.SETTABLE_DECISION_STATUSES:
        raise WritError(
            f"{args.status!r} is a task status, not a decision status "
            f"(decisions accept {', '.join(decisions.SETTABLE_DECISION_STATUSES)})"
        )
    if args.status == "rejected":
        if not args.reason:
            raise WritError("rejecting a decision needs --reason")
        record = decisions.reject(data, args.id, reason=args.reason)
        decisions.sync_markdown(args.root, data)
        print(f"{record['id']} rejected: {record['title']}")
        print(f"reason: {record['rejected_reason']}")
        return
    ruling = (getattr(args, "decision", None) or "").strip()
    record = decisions.get(data, args.id)
    if decisions.undecided(record) and not ruling:
        # Confirming the placeholder would make "undecided" binding and leave the
        # finding it came from standing with nothing to repair it to.
        raise WritError(
            f"{args.id} is a question, not a proposal: give your answer with "
            f'`writ set {args.id} active --decision "..."`'
        )
    if ruling:
        if record["status"] != "proposed":
            raise WritError(f"{args.id} is {record['status']}; its text is settled")
        record["decision"] = ruling
    record = decisions.confirm(data, args.id, supersedes=args.supersedes)
    decisions.sync_markdown(args.root, data)
    print(f"{record['id']} active: {record['title']}")
    if ruling:
        print(f"decision: {ruling}")
    if record.get("supersedes"):
        print(f"supersedes: {record['supersedes']}")
    print(f"mirror: {state.decisions_file(args.root)}")
    if record.get("finding"):
        print(
            f"next: writ build   (repairs the plan to follow {record['id']}, "
            f"which answers {record['finding']})"
        )


def cmd_override(args) -> None:
    """Let a human take a decision the agents own, and say that they did.

    Writ routes judgements through agents, but a tool that cannot be overridden
    is a tool that traps you when a model is wrong or unavailable. The escape
    hatch exists; it just refuses to disguise itself as an agent's verdict.
    """
    with state.transaction(args.root) as data:
        task = get_task(data, args.id)
        for spec in args.accept or []:
            number, _, status = spec.partition("=")
            if not number.strip().isdigit():
                raise WritError(f"--accept expects N or N=STATUS, got {spec!r}")
            set_acceptance(
                data,
                args.id,
                int(number),
                (status or "passed").strip(),
                actor="operator",
                evidence=f"operator override: {args.reason}",
            )
        set_status(
            data,
            args.id,
            args.status,
            evidence=f"operator override to {args.status}: {args.reason}",
            force=True,
            actor="operator",
            allow_judged=True,
        )
        task["last_verdict"] = {
            "role": "operator",
            "actor": "operator",
            "outcome": args.status,
            "decision": None,
            "summary": args.reason,
            "at": state.utcnow(),
        }
    print(f"{args.id} -> {args.status} (operator override)")
    print(f"recorded reason: {args.reason}")


def cmd_task(args) -> None:
    """Create a task, or amend an existing one.

    Creating and amending take the same fields and differ only in whether the id
    already exists, so they are one command. Passing a known id amends it;
    omitting the id creates.
    """
    if args.id:
        _amend_task(args)
        return
    if not args.title:
        raise WritError("creating a task needs --title")
    with state.transaction(args.root) as data:
        milestone = args.milestone
        if milestone and milestone not in data["milestones"]:
            add_milestone(data, milestone_id=milestone, title=milestone)
        task_id = _next_task_id(data, milestone)
        add_task(
            data,
            task_id=task_id,
            title=args.title,
            milestone=milestone,
            depends_on=args.depends or [],
            acceptances=args.acceptance or [],
            allowed=args.allow or [],
            forbidden=args.forbid or [],
        )
        check_dag(data)
        refresh_milestones(data)
    print(f"created {task_id}")


def _amend_task(args) -> None:
    with state.transaction(args.root) as data:
        task = get_task(data, args.id)
        if args.title:
            task["title"] = args.title
        if args.acceptance:
            task["acceptances"].extend(
                {"text": text, "status": "pending"} for text in args.acceptance
            )
        if args.depends:
            for dep in args.depends:
                if dep not in data["tasks"]:
                    raise WritError(f"unknown dependency: {dep}")
            task["depends_on"] = args.depends
        if args.allow:
            task["allowed"] = args.allow
        if args.forbid:
            task["forbidden"] = args.forbid
        if args.milestone:
            if args.milestone not in data["milestones"]:
                raise WritError(f"unknown milestone: {args.milestone}")
            task["milestone"] = args.milestone
        task["updated_at"] = state.utcnow()
        check_dag(data)
        refresh_milestones(data)
    print(f"updated {args.id}")


def _next_task_id(data: dict[str, Any], milestone: str | None) -> str:
    prefix = milestone or "T"
    existing = [key for key in data["tasks"] if key.startswith(f"{prefix}-")]
    return f"{prefix}-{len(existing) + 1:03d}"


def cmd_dispatch(args) -> int:
    extra = list(getattr(args, "agent_args", []) or [])
    if args.dry_run:
        data = state.load(args.root)
        task = get_task(data, args.id)
        print(runner.build_prompt(data, task, Path(args.root)))
        return 0
    return _run_agent_on_task(args, role="agent", task_id=args.id, extra=extra)


def cmd_review(args) -> int:
    """Have an agent that did not write the code decide whether it is done.

    Self-assessment is not evidence, so the implementing agent's verdict only
    reaches `awaiting-review`. This is the step that can complete a task.
    """
    data = state.load(args.root)
    if args.dry_run:
        task = get_task(data, args.id) if args.id else None
        if task is None:
            raise WritError("--dry-run needs a task id")
        print(runner.build_review_prompt(data, task, Path(args.root)))
        return 0

    if args.id:
        targets = [args.id]
    else:
        targets = [task["id"] for task in reviewable_tasks(data)]
        if not targets:
            print("nothing is awaiting review")
            return 0
        print(f"reviewing {len(targets)} task(s): {', '.join(targets)}")

    worst = 0
    for index, task_id in enumerate(targets):
        if index:
            print()
        code = _run_agent_on_task(args, role="reviewer", task_id=task_id, extra=[])
        worst = worst or code
    return worst


def _run_agent_on_task(args, *, role: str, task_id: str, extra: list[str]) -> int:
    """Shared body of dispatch and review: run one agent, report its verdict."""
    root = Path(args.root)
    run_id, directory, _, resolved = runner.prepare(
        root,
        task_id,
        args.agent,
        extra,
        model=args.model,
        timeout=args.timeout,
        cwd=args.cwd,
        force=args.force,
        role=role,
        max_rework=getattr(args, "max_rework", None),
    )
    if resolved.warning:
        print(f"warning: {resolved.warning}", file=sys.stderr)
    verb = "reviewing" if role == "reviewer" else "dispatched"
    if getattr(args, "detach", False):
        pid = runner.detach(root, run_id)
        print(f"{verb} {task_id} as run {run_id} (detached, supervisor pid {pid})")
        print(f"logs: writ logs {run_id} --follow")
        return 0
    print(f"{verb} {task_id} as run {run_id}")
    print(f"running: {resolved.display}")
    print(f"logs: {directory}")
    if not args.quiet:
        print("" + "─" * 62)
        sys.stdout.flush()
    code = runner.execute_guarded(root, run_id, stream=not args.quiet, prefix="| ")
    if not args.quiet:
        print("" + "─" * 62)
    print(f"run {run_id} finished with exit code {code}")
    if not runner.produced_output(directory):
        if code == 124:
            print(agents.hang_hint(resolved), file=sys.stderr)
        else:
            # An agent that exits on its own without a word is the case that
            # otherwise reads as "it worked but did not report". Say so here,
            # where the invocation is still on screen.
            print(agents.silent_exit_hint(resolved, code), file=sys.stderr)
    _report_verdict(root, run_id, task_id, directory, role)
    return code


def _report_verdict(
    root: Path, run_id: str, task_id: str, directory: Path, role: str
) -> None:
    """Say what the agent claimed and what writ did about it.

    The status change is the interesting part of a run, so it is reported
    explicitly rather than left for the user to go and look up.
    """
    report = runner.verdict_summary(root, run_id)
    if report.error:
        print(f"warning: {report.error}", file=sys.stderr)
    if report.downgraded:
        print(f"warning: {report.downgraded}", file=sys.stderr)
    if report.misplaced:
        print(
            f"warning: the {role} wrote its verdict to {report.misplaced} rather "
            f"than the path it was given; writ used it from there",
            file=sys.stderr,
        )
    status = report.status
    if status is None:
        print(verdict.missing_message(task_id, directory, role), file=sys.stderr)
        return
    data = state.load(root)
    task = data["tasks"][task_id]
    counts = acceptance_summary(task)
    print(
        f"{task_id} -> {status} "
        f"({counts['passed']}/{counts['total']} criteria passed, "
        f"judged by the {role})"
    )
    record = task.get("rework") or {}
    if status == "awaiting-review":
        print(f"next: writ review {task_id}")
    elif status == "planned" and role == "reviewer" and record.get("attempt"):
        # The case this would otherwise report as a bare `-> planned`, which looks
        # like the run undid itself. It is the rejection being turned into another
        # attempt, and the next command is a dispatch, not an investigation.
        print(
            f"sent back for rework ({record['attempt']} of "
            f"{record.get('budget', record.get('max'))}): "
            "the next agent on this task is given this review"
        )
        print(f"next: writ dispatch {task_id}")
    elif status == "failed":
        if record.get("exhausted"):
            print(
                f"rework budget of {record.get('budget', record.get('max'))} "
                "attempts is spent, so this is left failed for you"
            )
        print(f"next: writ show {task_id}   # see what it could not meet")


def cmd_agents(args) -> None:
    """Show the headless invocation writ will use.

    Agent CLIs open an interactive session by default, which hangs when the
    prompt arrives on a pipe. This is how to check what writ will actually run
    before committing a long planning job to it.
    """
    if args.agent:
        resolved = agents.resolve(args.agent, [], args.model)
        if args.json:
            render.emit_json(
                {
                    "agent": resolved.name,
                    "command": resolved.command,
                    "known": resolved.profile is not None,
                    "warning": resolved.warning,
                }
            )
            return
        print(resolved.display)
        if resolved.profile and resolved.profile.note:
            print(f"note: {resolved.profile.note}")
        if resolved.warning:
            print(f"warning: {resolved.warning}", file=sys.stderr)
        return

    rows = []
    payload = []
    for name in agents.KNOWN_AGENTS:
        resolved = agents.resolve(name, [], None)
        profile = agents.PROFILES[name]
        available = "yes" if shutil.which(name) else "no"
        rows.append(
            [
                name,
                available,
                shlex.join(resolved.command),
                profile.model_flag or "-",
            ]
        )
        payload.append(
            {
                "agent": name,
                "installed": available == "yes",
                "command": resolved.command,
                "model_flag": profile.model_flag,
                "note": profile.note,
            }
        )
    configured = _configured_roles(args.root)
    if args.json:
        render.emit_json({"agents": payload, "roles": configured})
        return
    print(render.table(["AGENT", "FOUND", "HEADLESS INVOCATION", "MODEL FLAG"], rows))
    print("\nany other command is passed through unchanged; add its own")
    print("non-interactive flag to --agent so it does not wait on a terminal")
    _print_roles(configured, args.root)


def _configured_roles(root) -> list[dict[str, Any]]:
    """What this project has settled on for each role, and where that came from.

    Reported even when nothing is configured, because the default worth knowing
    about is the reviewer's: with no config and no `--reviewer`, review runs on the
    implementing agent, and the table saying so is how that stops being a surprise.
    """
    try:
        loaded = config.load(root)
    except WritError as exc:
        # A broken config is worth saying here rather than raising: this command
        # exists to explain what writ will run, and "your config is unreadable" is
        # the most useful thing it can say when that is true.
        return [{"role": "-", "error": str(exc)}]
    out = []
    for role, purpose in config.ROLES.items():
        entry = (loaded.get("agents") or {}).get(role) or {}
        out.append(
            {
                "role": role,
                "purpose": purpose,
                "command": entry.get("command"),
                "model": entry.get("model"),
                "timeout": entry.get("timeout"),
                "source": config.FROM_CONFIG if entry else config.FROM_BUILTIN,
            }
        )
    return out


#: what a role falls back to when the config does not name it
ROLE_FALLBACKS = {
    "planner": "pi",
    "critic": "the planning agent",
    "implementer": "pi",
    "reviewer": "the implementing agent",
}


def _print_roles(configured: list[dict[str, Any]], root) -> None:
    broken = next((row for row in configured if row.get("error")), None)
    if broken:
        print(f"\nwarning: {broken['error']}", file=sys.stderr)
        return
    rows = [
        [
            row["role"],
            row["command"] or f"({ROLE_FALLBACKS[row['role']]})",
            row["model"] or "-",
            str(row["timeout"]) if row["timeout"] else "-",
        ]
        for row in configured
    ]
    print()
    print(render.table(["ROLE", "COMMAND", "MODEL", "TIMEOUT"], rows))
    path = config.config_file(root)
    if any(row["source"] == config.FROM_CONFIG for row in configured):
        print(f"\nfrom {path}; a flag overrides any of it")
    else:
        print(f"\nno {path}; bracketed values are what writ falls back to")
    # Keyed on the reviewer rather than on whether anything is configured. Since
    # `writ init` writes every field, "this project has a config" stopped implying
    # "this project chose a reviewer" — and the unset reviewer is the whole reason
    # this warning exists.
    reviewer = next(row for row in configured if row["role"] == "reviewer")
    if not reviewer["command"]:
        print("\nreviewer is the implementing agent, the weakest of these:")
        print("a model checking its own work agrees with itself more than it should.")


def cmd_supervise(args) -> int:
    """Internal: owns a detached run until the agent exits."""
    return runner.execute_guarded(Path(args.root), args.run_id)


def cmd_serve(args) -> None:
    """Serve the read-only dashboard.

    A separate command rather than a flag on `graph`: it is not another rendering
    of the DAG, it is every view writ has — runs, prompts, logs, verdicts,
    decisions — and naming it after one of them would undersell it.
    """
    check_dag(state.load(args.root))  # fail at the prompt, not as a broken page
    server.serve(
        args.root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )


def cmd_logs(args) -> None:
    root = Path(args.root)
    data = state.load(root)
    run_id = args.id
    if run_id in data["tasks"]:
        latest = runner.latest_run_for(data, run_id)
        if latest is None:
            raise WritError(f"task {run_id} has no runs yet")
        run_id = latest
    stream = "stderr" if args.stderr else "stdout"
    path = runner.log_path(root, run_id, stream)
    if not path.exists():
        raise WritError(f"no {stream} log yet for {run_id}")
    if not args.follow:
        text = path.read_text(encoding="utf-8", errors="replace")
        if args.tail:
            text = "\n".join(text.splitlines()[-args.tail :])
        print(text, end="" if text.endswith("\n") else "\n")
        return
    _follow(path, root, run_id)


def _follow(path: Path, root: Path, run_id: str) -> None:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    continue
                data = state.load(root)
                run = data["runs"].get(run_id, {})
                if run.get("status") not in runner.ACTIVE_RUN_STATUSES:
                    remaining = handle.read()
                    if remaining:
                        sys.stdout.write(remaining)
                    print(
                        f"\n-- run {run_id} {run.get('status')} "
                        f"(exit {run.get('exit_code')}) --"
                    )
                    return
                time.sleep(0.3)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            print()


def cmd_cancel(args) -> None:
    """Stop one active run, or reconcile every run whose process is gone.

    Both are the same intent: make recorded state match reality. With an id we
    kill a live run; without one we reap the records whose owner already died.
    """
    if args.id:
        runner.cancel(Path(args.root), args.id)
        print(f"cancelled {args.id}")
        return
    reaped = runner.reap(Path(args.root))
    if not reaped:
        print("no stale runs")
        return
    for run_id in reaped:
        print(f"marked {run_id} interrupted")





# --------------------------------------------------------------------------
# autonomous run


#: `writ build` flag -> (the `writ plan` attribute, the `writ run` attribute) it sets.
#: None where the step has no such setting. Written out rather than shared by
#: name, because `--agent` under build is the implementer, as it is under run,
#: while the planner is `--planner`.
BUILD_FORWARDS: dict[str, tuple[str | None, str | None]] = {
    "planner": ("agent", None),
    "planner_model": ("model", None),
    "critic": ("critic_agent", None),
    "critic_model": ("critic_model", None),
    "instructions": ("instructions", None),
    "critics": ("critics", None),
    "repair": ("repair", None),
    "max_rounds": ("max_rounds", None),
    "agent": (None, "agent"),
    "model": (None, "model"),
    "timeout": (None, "timeout"),
    "reviewer": (None, "reviewer"),
    "reviewer_model": (None, "reviewer_model"),
    "reviewer_timeout": (None, "reviewer_timeout"),
    "parallel": (None, "parallel"),
    "max_tasks": (None, "max_tasks"),
    "max_rework": (None, "max_rework"),
    "order": (None, "order"),
    "no_stream": (None, "no_stream"),
    "cwd": ("cwd", "cwd"),
    "quiet": ("quiet", "quiet"),
    "json": (None, "json"),
    "dry_run": ("dry_run", "dry_run"),
    "autonomous": ("autonomous", "autonomous"),
}


def cmd_build(args) -> int:
    """Plan the design if it has not been planned, then run the plan.

    One command from a design to finished work: `writ plan` then `writ run`, with
    the plan approved on the way through unless `--no-auto-approve` says a human
    should read it first. Auto-approval still refuses a plan with a blocking
    finding standing against it, so a plan that is wrong stops here either way.

    It is also how a build resumes. With a plan already in place, `writ build`
    runs it (naming no design, or only designs the plan already covers); a design
    it does not cover is refused unless `--append` asks for it to be planned onto
    the graph that is there. Each step is the command itself, parsed and
    configured the way the command line would have, so the config applies to
    each exactly as it does to `writ plan` and `writ run`.
    """
    root = Path(args.root)
    data = state.load(root)
    docs = _design_docs(args.design) if args.design else []
    registered = {str(Path(path).resolve()) for path in data["design_docs"]}
    unplanned = [path for path in docs if str(path.resolve()) not in registered]
    if not data["tasks"] and not docs:
        raise WritError(
            "nothing to build yet: name the design, e.g. `writ build design.md`"
        )
    if data["tasks"] and unplanned and not args.append:
        raise WritError(
            f"this project already has a plan, and {planner.doc_names(unplanned)} "
            "is not part of it. Pass --append to plan it onto the graph there is, "
            "or leave it off to carry on with the current plan"
        )

    if not data["tasks"] or unplanned:
        plan_args = _step_args(
            args, "plan", [str(path) for path in (unplanned or docs)], index=0
        )
        plan_args.append = bool(data["tasks"])
        if plan_args.auto_approve is None:
            # build's own default, not `plan.auto_approve`: a build that stops
            # at every plan is `writ plan` followed by `writ run` again
            plan_args.auto_approve = not args.no_auto_approve
        config.apply(plan_args, config.load(root))
        code = cmd_plan(plan_args)
        if code != 0 or args.dry_run:
            return code
        data = state.load(root)
        print()
    else:
        if not args.json:
            print(f"the plan is in place ({len(data['tasks'])} tasks); running it")
        if (
            not args.dry_run
            and not plans.runnable(data)
            and (_ruled(data) or _build_autonomous(args, root))
        ):
            _repair_to_rulings(args, root)
            data = state.load(root)

    if not args.dry_run and not plans.runnable(data):
        print(plans.not_runnable_message(data))
        print("then run `writ build` again to carry on from there")
        return 1
    run_args = _step_args(args, "run", [], index=1)
    config.apply(run_args, config.load(root))
    return cmd_run(run_args)


def _build_autonomous(args, root: Path) -> bool:
    """Whether this build decides on its own: its flag, else the config."""
    if getattr(args, "autonomous", None) is not None:
        return bool(args.autonomous)
    return bool((config.load(root).get("decisions") or {}).get("autonomous"))


def _autonomous(args, root: Path) -> bool:
    """Whether this command decides on its own, recorded where verdicts read it.

    Applying a verdict happens several calls from the arguments, and sometimes in
    a later process (`writ run` resuming what a killed one left), so the mode
    goes in the state: the last command that resolved it says what holds.
    """
    on = bool(getattr(args, "autonomous", False))
    if decisions.autonomous(state.load(root)) != on:
        with state.transaction(root) as data:
            data["autonomous"] = on
    return on


def _ruled(data) -> list[str]:
    """Blocking findings a person has now ruled on, which a repair can close."""
    return [
        finding.id
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error"
        and finding.category == adjudicate.DECISION_CATEGORY
        and decisions.ruling(data, finding.id)
    ]


def _repair_to_rulings(args, root: Path) -> None:
    """Resume a build held for a ruling: repair the plan to it, then approve.

    `writ build` stopped because a critic's question needed a person. Once they
    have answered with `writ set D-NNNN active --decision`, running build again is
    the obvious next step, and it used to re-check the plan, find the same finding
    and stop at the same place — the answer was recorded and never applied.
    """
    # parsed only for what the config and build's flags say planning should do
    designs = list(state.load(root)["design_docs"]) or ["design.md"]
    plan_args = _step_args(args, "plan", designs, index=0)
    if plan_args.auto_approve is None:
        plan_args.auto_approve = not args.no_auto_approve  # as `cmd_build` does
    config.apply(plan_args, config.load(root))
    if not getattr(plan_args, "repair", False):
        return
    ruled = ", ".join(_ruled(state.load(root)))
    if ruled:
        print(f"repairing the plan to follow the ruling on {ruled}")
    else:
        print("repairing the plan, deciding its open questions (autonomous)")
    from .cli import build_parser

    # Not `_step_args`: under `writ adjudicate`, `--agent` is the critic's role,
    # so build's `--planner` must not land there.
    step = build_parser().parse_args(["--root", str(root), "adjudicate"])
    for source, target in (
        ("critic", "agent"),
        ("critic_model", "model"),
        ("critic", "critic_agent"),
        ("critic_model", "critic_model"),
        ("max_rounds", "max_rounds"),
        ("cwd", "cwd"),
        ("quiet", "quiet"),
        ("autonomous", "autonomous"),
    ):
        if getattr(args, source, None) is not None:
            setattr(step, target, getattr(args, source))
    step.agent_args = []
    config.apply(step, config.load(root))
    # re-reviewed exactly as the build's own planning would have been
    step.no_critics = not _critics_requested(plan_args)
    cmd_adjudicate(step)
    if plan_args.auto_approve and _auto_approve(root):
        print("plan approved: nothing blocking stands against it")
    print()


def _step_args(args, command: str, positional: list[str], *, index: int):
    """Parse one step of `writ build` as its own command, with build's flags on it.

    Only what build was actually given is carried over; everything else is left
    unset for the config to fill, the same as if the step had been typed alone.
    """
    from .cli import build_parser

    step = build_parser().parse_args(["--root", str(args.root), command, *positional])
    step.agent_args = list(getattr(args, "agent_args", []) or [])
    for source, targets in BUILD_FORWARDS.items():
        target = targets[index]
        value = getattr(args, source, None)
        if target is not None and value is not None:
            setattr(step, target, value)
    return step


def cmd_run(args) -> int:
    """Walk the DAG: dispatch what is ready, review what is reported, repeat.

    The individual commands each move one task one step. This is the one that
    finishes a project, so its output is a progress log first: one line per
    transition, which is what a reader comes back to after lunch and reads.

    The agents' own output is mirrored underneath it, each line tagged with the task
    and role it came from. That used to be left out on the grounds that several
    interleaved agents would be unreadable, which was true of an unlabelled
    character-at-a-time mirror and is the reason `_tee` now writes whole lines under
    one lock. The tradeoff it was trading against is worse: a silent terminal for
    the length of a model call is indistinguishable from a hung one, and the state
    it hides is exactly the state someone needs to see. `--no-stream` restores the
    progress log alone, and `--json` never mirrors, because a machine-readable
    stream with an agent's prose in it is not machine-readable.
    """
    root = Path(args.root)
    data = state.load(root)

    if args.dry_run:
        return _run_preview(data, args)

    # An unapproved plan does not execute. Checked before the session is claimed
    # and before anything is reaped, so a project held at `needs-approval` reads
    # as a plan waiting for review rather than as a run that failed.
    if not plans.runnable(data):
        message = plans.not_runnable_message(data)
        if not data.get("tasks"):
            # An empty project is not a refusal, it is an empty project. Exit 0
            # with the next command, the same as a project with nothing ready.
            if args.json:
                render.emit_json({"event": "idle", "reason": message})
            else:
                print(message)
            return 0
        raise WritError(message)

    existing = orchestrator.active_session(root)
    if existing and not args.force:
        raise WritError(
            f"another writ run is active (pid {existing}). Wait for it, stop it, "
            "or pass --force if you know it is gone."
        )

    # Before anything reconciles: a verdict a killed session left behind is
    # applied under the mode this run says holds.
    if _autonomous(args, root) and not args.json:
        print("autonomous: writ makes the decisions; each one is logged")
    # Reconcile before deciding there is nothing to do. A previous session that
    # was killed leaves tasks parked mid-flight, and they are exactly the work a
    # resume should pick up first.
    reaped = runner.reap(root)
    if reaped and not args.json:
        print(f"resuming: reconciled {len(reaped)} interrupted run(s)")
    data = state.load(root)

    jobs = orchestrator.preview(data, budget=args.max_tasks, order=args.order)
    if not jobs:
        if args.json:
            render.emit_json({"event": "idle", "reason": _nothing_to_run(data)})
        else:
            print(_nothing_to_run(data))
        return 0

    orchestrator.claim_session(root, force=args.force)
    parallel = max(1, args.parallel)
    # `--json` is a machine-readable stream and an agent's prose is not part of it.
    # `--quiet` asks for less, and an agent's whole transcript is not less.
    stream = not getattr(args, "no_stream", False) and not args.json and not args.quiet
    if not args.json:
        print(
            f"running up to {parallel} agent{'s' if parallel > 1 else ''} at a time"
            + (f", at most {args.max_tasks} tasks" if args.max_tasks else "")
            + (
                f", deepest work first"
                if args.order == "depth"
                else ", most-unblocking first"
                if args.order == "unlocks"
                else ""
            )
        )
        print(f"logs: {state.runs_dir(root)}")
        if not stream:
            print("streaming off: transitions only (writ logs <task> for output)")
        print("─" * 62)
    # Shared with the agents' mirrored output so the two cannot interleave mid-line.
    output_lock = threading.Lock()
    reporter = _RunReporter(
        quiet=args.quiet, json_events=args.json, lock=output_lock
    )
    try:
        session = orchestrator.run(
            root,
            agent=args.agent,
            model=args.model,
            reviewer=args.reviewer,
            reviewer_model=args.reviewer_model,
            reviewer_timeout=getattr(args, "reviewer_timeout", None),
            parallel=parallel,
            max_tasks=args.max_tasks,
            order=args.order,
            timeout=args.timeout,
            cwd=args.cwd,
            max_rework=getattr(args, "max_rework", None),
            max_infra_retries=getattr(args, "max_infra_retries", None),
            verify=getattr(args, "verify", None),
            on_event=reporter,
            stream=stream,
            lock=output_lock,
        )
    finally:
        orchestrator.release_session(root)
    data = state.load(root)
    unfinished = orchestrator.unfinished(
        data, session, budgeted=args.max_tasks is not None
    )
    if args.json:
        render.emit_json(
            {
                "event": "summary",
                "agents": session.agent_runs,
                "tasks": len(set(session.dispatched)),
                "completed": session.completed,
                "failed": session.failed,
                "reworked": session.reworked,
                # Separate keys, because these are not failures. A consumer that
                # summed them into `failed` would report an outage as rejected work.
                "infra_retries": session.infra_retries,
                "infra_blocked": session.infra_blocked,
                "errors": session.errors,
                "stopped": session.stopped or session.aborted,
                "remaining": [
                    task_id
                    for task_id, task in sorted(data["tasks"].items())
                    if task["status"] != "completed"
                ],
                "unfinished": unfinished,
            }
        )
        return 1 if (session.failed or session.errors or unfinished) else 0
    print("─" * 62)
    for line in orchestrator.summary(data, session):
        print(line)
    if session.stopped or session.aborted:
        print()
        print("stopped early; `writ run` again picks up where this left off")
    if session.errors:
        for message in session.errors:
            print(f"error: {message}", file=sys.stderr)
        return 1
    for reason in unfinished:
        print(f"unfinished: {reason}", file=sys.stderr)
    return 1 if (session.failed or unfinished) else 0


def _run_preview(data, args) -> int:
    """Show the intended walk without spending anything."""
    jobs = orchestrator.preview(data, budget=args.max_tasks, order=args.order)
    if args.json:
        render.emit_json(
            {
                "event": "preview",
                "parallel": max(1, args.parallel),
                "order": args.order,
                "invocations": [
                    {"role": job.role, "task": job.task_id} for job in jobs
                ],
            }
        )
        return 0
    if not jobs:
        print(_nothing_to_run(data))
        return 0
    parallel = max(1, args.parallel)
    print(
        f"would run {len(jobs)} agent invocations, up to {parallel} at a time:"
    )
    for index, job in enumerate(jobs, start=1):
        print(f"  {index:>2}. {job.verb:<8} {job.task_id}")
    print()
    print(
        "a projection, not a promise: a rejected verdict changes what comes next"
    )
    return 0


def _nothing_to_run(data) -> str:
    """Say which kind of nothing this is; they need different responses."""
    tasks = data["tasks"]
    if not tasks:
        return "no tasks (run `writ plan <doc>` first)"
    if all(task["status"] == "completed" for task in tasks.values()):
        return "every task is complete"
    held = orchestrator.held_gates(data)
    if held:
        # A held gate is the commonest reason a project with unfinished tasks has
        # nothing to run, and it is the one the old message described worst: the
        # work is not failed, it is waiting, and what it waits for is named here.
        listed = ", ".join(f"{gate} ({reason})" for gate, reason in sorted(held.items()))
        return (
            f"nothing can start: {listed}. See `writ list gates` and "
            "`writ list repairs`"
        )
    stalled = orchestrator._stalled(data)
    if stalled:
        return (
            "nothing can start: "
            + ", ".join(stalled)
            + " wait on failed work (see `writ list --status failed`)"
        )
    return "nothing is ready to dispatch or awaiting review"


def _first_line(text: str, limit: int = 96) -> str:
    """The agent's summary as one line, since the log is one line per event."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


class _RunReporter:
    """Turns scheduler events into a readable progress log.

    Transitions only: what started, what it produced, and what that unblocked. When
    the agents' own output is streamed it runs underneath these lines rather than
    replacing them — the transcript says what an agent did, and this says what writ
    did about it, which is the shorter and more re-readable of the two.
    """

    def __init__(
        self, *, quiet: bool, json_events: bool, lock: threading.Lock | None = None
    ) -> None:
        self.quiet = quiet
        self.json_events = json_events
        # The same lock the streamed agent output holds, when there is any, so a
        # transition line and an agent's line cannot land on top of each other.
        self.lock = lock or threading.Lock()

    def __call__(self, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.json_events:
                render.emit_json({"event": name, **payload})
                return
            line = self._format(name, payload)
            if line is not None:
                print(line, flush=True)

    def _format(self, name: str, payload: dict[str, Any]) -> str | None:
        if name == "reaped":
            runs = ", ".join(payload["runs"])
            return f"reaped {len(payload['runs'])} interrupted run(s): {runs}"
        if name == "started":
            if self.quiet:
                return None
            if payload["role"] == "reviewer":
                verb = "review  "
            elif payload.get("attempt"):
                verb = "rework  "
            else:
                verb = "dispatch"
            return f"{verb} {payload['task']}  ->  {payload['command']}"
        if name == "finished":
            return self._finished(payload)
        if name == "stopping":
            return "\nstopping: finishing the agents already running (^C again to kill)"
        if name == "abort":
            return "\naborting: killing the agents still running"
        if name == "cancelled":
            return f"cancelled {payload['task']} (run {payload['run']})"
        if name == "error":
            return f"error    {payload['task']}: {payload['message']}"
        if name == "retrying":
            # Said explicitly, and said as infrastructure. A retry that printed
            # like a rework would have the reader looking for a review that never
            # happened, which is the confusion this whole classification removes.
            return (
                f"retry    {payload['task']}  infrastructure "
                f"{payload['attempt']}/{payload['budget']} "
                f"in {payload['in']}s"
                + (f"  ({_first_line(payload['reason'])})" if payload.get("reason") else "")
            )
        return None

    def _finished(self, payload: dict[str, Any]) -> str:
        """One line per transition: the mark, the new status, and the reason.

        A bare `x M01-002  failed` sends the reader to `writ show` to find out
        why, which is the wrong default for the one line they will actually see.
        The agent already wrote a one-line account; use it.
        """
        status = payload["status"] or "unknown"
        rework = payload.get("rework")
        if rework:
            # `planned` is the truthful status and a useless thing to print: the
            # reader's question about a rejected task is whether anything happens
            # next, and a bare `planned` reads as though it never ran.
            attempt, budget = rework
            parts = [
                f"{render.mark('planned')} {payload['task']}  "
                f"rework {attempt}/{budget}"
            ]
        else:
            parts = [f"{render.mark(status)} {payload['task']}  {status}"]

        counts = payload.get("criteria")
        if counts and counts.get("total"):
            parts.append(f"{counts['passed']}/{counts['total']}")

        unmet = payload.get("unmet")
        if unmet:
            parts.append(
                "unmet " + ", ".join(str(number) for number in unmet)
            )

        line = "  ".join(parts)
        if payload.get("retry_in") is not None:
            # An infrastructure failure that bought another attempt. The status is
            # whatever the task was returned to, which on its own reads as work
            # that quietly went nowhere.
            line += f"  (retrying in {payload['retry_in']}s)"
        if payload["error"]:
            line += f"  ({payload['error']})"
        elif payload["exit_code"] not in (0, None):
            line += f"  (exit {payload['exit_code']})"
        if payload.get("failure"):
            # The classified failure, in addition to the exit code rather than
            # instead of it. The code says what happened to the process; this says
            # whether the reader should be looking at their code or at their
            # provider, and only one of those is answerable from a number.
            line += f"  ({payload['failure']})"

        detail = []
        reason = payload.get("summary")
        if reason and (rework or status in ("failed", "blocked")):
            detail.append(_first_line(reason))
        proposed = payload.get("decisions")
        if proposed:
            detail.append(
                f"proposed {len(proposed)} decision"
                + ("s" if len(proposed) > 1 else "")
                + ": "
                + "; ".join(proposed[:2])
                + (" …" if len(proposed) > 2 else "")
            )
        out = [f"         {line}"]
        out += [f"           {item}" for item in detail]
        return "\n".join(out)
