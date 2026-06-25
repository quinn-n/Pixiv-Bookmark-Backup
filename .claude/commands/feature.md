---
description: Run the full feature pipeline — design, plan, fact-check, parallel implement+review loops, test, review — for a CLI feature described in the argument.
argument-hint: <feature / command description>
---

Build the following CLI feature end to end: **$ARGUMENTS**

Orchestrate the specialized subagents in order, pausing for my approval at each ⏸ gate. Do not skip gates.

1. **Design** — delegate to `cli-designer`. Present the command spec (surface, options, exit codes, edge cases). ⏸ Wait for approval or edits.
2. **Plan** — delegate to `implementation-planner` using the approved design. It must list new pixi deps/tasks, all `[VERIFY]` assumptions, per-task `Owns` file sets, and a **Parallelization plan** of ordered execution batches.
3. **Fact-check** — delegate to `fact-checker` to resolve every `[VERIFY]` item against the installed pixi env + current docs.
   - If anything is **refuted**, send the plan back to `implementation-planner` to revise the affected steps, then re-run `fact-checker`. Repeat until the plan rests only on confirmed facts. ⏸ Present the final plan + test plan + parallelization plan for approval.
4. **Implement in parallel batches, each track gated by a review loop.** Work through the plan's execution batches **in order**. For each batch:
   - Launch **one `implementer` subagent per independent task in the batch, in parallel** (multiple Task calls in a single turn). Give each implementer only its task(s) and its `Owns` file set, and tell it to stay strictly within that scope. Do not put tasks whose `Owns` sets overlap (including the shared `pixi` manifest or the click group in `cli.py`), or whose dependencies haven't completed, in the same batch — serialize those into a later batch.
   - **Run an implement↔review loop per track** (the core requirement — code must *pass* review, not just be written once):
     a. The `implementer` writes code + the pytest tests the plan specifies and gets `pixi run check` green.
     b. Delegate that track's diff to `quality-reviewer`, which returns **VERDICT: APPROVED** or **VERDICT: CHANGES REQUESTED** with Must-fix items.
     c. If CHANGES REQUESTED, send the Must-fix list back to the **same** track's `implementer` to address (with a change log), then re-review with `quality-reviewer`.
     d. Repeat b–c until **APPROVED**. Cap at ~3 rounds; if not converged or implementer and reviewer disagree on a Must-fix, ⏸ surface it to me instead of looping further.
   - Review loops for different tracks run concurrently. When every track in the batch is APPROVED, **integrate the batch** (resolve shared touch-points like registering subcommands in the click group serially), run `pixi run check`, and only then start the next batch.
5. **CLI test** — once all batches are integrated, delegate to `cli-tester` to run the real command via pixi and verify the acceptance criteria, exit codes, and pipe/no-TTY behavior.
6. **Feature-level review board** — run `security-reviewer` and `cli-ux-reviewer` (and a final `quality-reviewer` pass) on the integrated feature, in parallel. Each returns a verdict. For any **CHANGES REQUESTED**, run the same implement↔review loop (route to `implementer`, or `cli-ux-reviewer` for help-text/format-only fixes) until that reviewer APPROVES; then re-run the relevant CLI tests.
7. **Fix** — route any remaining defects from testing to `bug-fixer`; loop its fixes back through `quality-reviewer` until APPROVED.
8. **Summary** — what was built, which tasks ran in parallel, review rounds per track, gate/test results, new pixi deps & tasks, and outstanding Should-fix/Nit items.

Give a one-paragraph status at each batch/loop (which tracks are running, current verdicts, round counts). Stop and ask if any agent reports a blocker or a loop fails to converge.
