---
description: Run the full feature pipeline — design, plan, fact-check, parallel implement+review loops, simplify, test, review — for a CLI feature described in the argument.
argument-hint: <feature / command description>
---

Build the following CLI feature end to end: **$ARGUMENTS**

Orchestrate the specialized subagents in order, pausing for my approval at each ⏸ gate. Do not skip gates.

1. **Design** — delegate to `cli-designer`. Present the command spec (surface, options, exit codes, edge cases). ⏸ Wait for approval or edits.
2. **Plan** — delegate to `implementation-planner` using the approved design. It must list new pixi deps/tasks, all `[VERIFY]` assumptions, per-task `Owns` file sets, a **Parallelization plan** of ordered execution batches, and its **PR slices** (or `Single PR — <reason>`).
3. **Fact-check (fan out)** — the `[VERIFY]` items are independent lookups, so resolve them in parallel: launch **one `fact-checker` per verification cluster** from the plan in a single turn (multiple Task calls), giving each only its cluster's items, each checking against the installed pixi env + current docs. If the plan lists no clusters, split the items into disjoint groups yourself (one fact-checker each) — or use a single fact-checker when there are only a couple. Merge their verdicts into one report.
   - If anything is **refuted**, send the plan back to `implementation-planner` to revise the affected steps, then re-run only the relevant `fact-checker`(s). Repeat until the plan rests only on confirmed facts. ⏸ Present the final plan + test plan + parallelization plan + **PR slices** for approval. The slices decide how the feature is reviewed if it's later opened as PRs: `/features` stacks them when the diff is over the threshold (see the `stacked-prs` skill). Approve or edit them here alongside the batches.
4. **Implement in parallel batches, each track gated by a review loop.** Work through the plan's execution batches **in order**. For each batch:
   - Launch **one `implementer` subagent per independent task in the batch, in parallel** (multiple Task calls in a single turn). Give each implementer only its task(s) and its `Owns` file set, and tell it to stay strictly within that scope. Do not put tasks whose `Owns` sets overlap (including the shared `pixi` manifest or the click group in `cli.py`), or whose dependencies haven't completed, in the same batch — serialize those into a later batch.
   - **Run an implement↔review loop per track** (the core requirement — code must *pass* review, not just be written once):
     a. The `implementer` writes code + the pytest tests the plan specifies and gets `pixi run check` green.
     b. Delegate that track's diff to `quality-reviewer`, which returns **VERDICT: APPROVED** or **VERDICT: CHANGES REQUESTED** with Must-fix items.
     c. If CHANGES REQUESTED, send the Must-fix list back to the **same** track's `implementer` to address (with a change log), then re-review with `quality-reviewer`.
     d. Repeat b–c until **APPROVED**. Cap at ~3 rounds; if not converged or implementer and reviewer disagree on a Must-fix, ⏸ surface it to me instead of looping further.
   - Review loops for different tracks run concurrently. When every track in the batch is APPROVED, **integrate the batch** (resolve shared touch-points like registering subcommands in the click group serially), run `pixi run check`, and only then start the next batch.
4b. **Simplify pass** — once every batch is integrated and green, delegate to **one** `code-refactorer` in pipeline mode. Use one refactorer, not one per track, because duplication *across* tracks is what this pass is for. Give it:
   - **Scope:** the feature's whole diff, as the list of files the tracks changed. It edits only those files.
   - **Inputs:** the refactor notes the implementers deferred, plus any simplicity, duplication, or dead-code **Should-fix** items from the track reviews.
   - **Bar:** behavior and the CLI contract (flag names, output, exit codes) stay unchanged, and `pixi run check` stays green before and after. **"Nothing worth simplifying"** is a valid result and ends the step.
   - **Review loop:** if it changed anything, run that diff through `quality-reviewer` until **APPROVED**, with the refactorer in the implementer's seat (cap ~3 rounds). If a change can't win approval, revert that change instead of escalating — the pass is optional polish, never a blocker.
   - **Hand-offs:** carry anything it deferred (bugs, contract-changing simplifications) into step 8 as follow-ups.
5. **CLI test** — once all batches are integrated, delegate to `cli-tester` to run the real command via pixi and verify the acceptance criteria, exit codes, and pipe/no-TTY behavior.
6. **Feature-level review board** — run `security-reviewer` and `cli-ux-reviewer` (and a final `quality-reviewer` pass) on the integrated feature, in parallel. Each returns a verdict. For any **CHANGES REQUESTED**, run the same implement↔review loop (route to `implementer`, or `cli-ux-reviewer` for help-text/format-only fixes) until that reviewer APPROVES; then re-run the relevant CLI tests.
7. **Fix** — route any remaining defects from testing to `bug-fixer`; loop its fixes back through `quality-reviewer` until APPROVED.
8. **Summary** — what was built, which tasks ran in parallel, review rounds per track, what the simplify pass changed (or that it found nothing) and what it deferred, gate/test results, new pixi deps & tasks, and outstanding Should-fix/Nit items.

Give a one-paragraph status at each batch/loop (which tracks are running, current verdicts, round counts). Stop and ask if any agent reports a blocker or a loop fails to converge.

Run the heavy gate at low OS priority locally, e.g. `nice -n 10 pixi run check` — never in CI, never swallowing the exit code. See the `low-priority-execution` skill.
