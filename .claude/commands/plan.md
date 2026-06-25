---
description: Design + plan + fact-check a CLI feature, stopping before implementation so you can review the plan.
argument-hint: <feature / command description>
---

Produce a verified, ready-to-build plan for: **$ARGUMENTS**

1. `cli-designer` → command spec. ⏸ Present it.
2. `implementation-planner` → step-by-step plan + pytest test plan + pixi dep/task changes, with all `[VERIFY]` assumptions listed.
3. `fact-checker` → resolve every `[VERIFY]` item against the installed pixi env + docs. For anything refuted, loop back to `implementation-planner` to revise, then re-check, until the plan rests only on confirmed facts.
4. Present the final design, plan, test plan, and fact-check report. Do NOT implement — stop here for my review.
