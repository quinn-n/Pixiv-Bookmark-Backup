---
description: Improve existing code without changing behavior — backfill tests, refactor, fix quality/security/UX issues.
argument-hint: <file/area to harden, or blank for current diff>
---

Harden the target: **$ARGUMENTS** (default: the current diff/changed files).

Run in order, each on a green `pixi run check` baseline:
1. `test-engineer` — backfill meaningful pytest coverage so refactors are safe.
2. `security-reviewer` and `cli-ux-reviewer` — surface issues (parallel, read-only).
3. `code-refactorer` — improve readability/structure with the gate green before and after.
4. Route security/UX fixes touching logic to `implementer`/`bug-fixer`; UX text/format fixes to `cli-ux-reviewer`.
5. Re-run `pixi run check` and summarize improvements + what was left out of scope.

Run the heavy gate at low OS priority locally, e.g. `nice -n 10 pixi run check` — never in CI, never swallowing the exit code. See the `low-priority-execution` skill.
