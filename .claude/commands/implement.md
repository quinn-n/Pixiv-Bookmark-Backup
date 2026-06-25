---
description: Implement an already-approved plan with parallel tracks and per-track review loops, then run the real CLI and the feature-level review board.
argument-hint: <plan file path or description of the approved plan>
---

Execute the approved plan: **$ARGUMENTS**

1. **Read the plan's Parallelization plan** (execution batches + per-task `Owns` sets). If absent, derive batches from dependencies and file ownership; two tasks may share a batch only if independent and file-disjoint (and neither touches the shared pixi manifest or click group). If everything is interdependent, run a single sequential batch.
2. **Per batch, in order:** launch **one `implementer` per independent task in parallel** (multiple Task calls in one turn), each scoped to its `Owns` files. For each track run the **implement↔review loop**:
   - `implementer` writes code + pytest tests, gets `pixi run check` green.
   - `quality-reviewer` returns **APPROVED** or **CHANGES REQUESTED** (Must-fix list).
   - On CHANGES REQUESTED, the same `implementer` addresses them with a change log; re-review. Loop until APPROVED (cap ~3 rounds, then surface disagreement to me).
   - When all tracks in the batch are APPROVED, integrate (serialize shared touch-points like the click group / manifest), run `pixi run check`, then start the next batch.
3. `cli-tester` — run the real command via pixi on the integrated feature; verify behaviors, exit codes, and pipe/no-TTY handling.
4. In parallel: `security-reviewer`, `cli-ux-reviewer`, and a final `quality-reviewer` pass — each returns a verdict. Loop any CHANGES REQUESTED back through `implementer` (or `cli-ux-reviewer` for help-text/format-only) until APPROVED; re-test after.
5. Route remaining defects to `bug-fixer` (loop through `quality-reviewer` until APPROVED). Summarize parallel tracks, review rounds per track, results, new pixi deps/tasks, and follow-ups.
