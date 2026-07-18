---
description: Diagnose and fix a bug, with a pytest regression test proving the fix.
argument-hint: <bug description / repro / error / failing command>
---

Fix this bug: **$ARGUMENTS**

Delegate to `bug-fixer`: reproduce (running the real CLI via pixi if relevant), find the root cause, add a failing pytest regression test, fix minimally, and prove it with the now-passing test plus `pixi run check`. Report symptom → root cause → fix → test evidence. Afterward, ask whether I want `quality-reviewer` and `security-reviewer` to check the change.

Run the heavy gate at low OS priority locally, e.g. `nice -n 10 pixi run check` — never in CI, never swallowing the exit code. See the `low-priority-execution` skill.
