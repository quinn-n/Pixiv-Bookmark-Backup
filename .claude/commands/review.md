---
description: Run the full review board (quality, security, CLI UX) on the current diff and summarize.
---

Review the current changes (`git diff` / staged) thoroughly. Run these in parallel:
- `quality-reviewer` — runs the gate (black --check, flake8, mypy, pytest) + reviews correctness, tests, typing, conventions.
- `security-reviewer` — runs bandit/pip-audit + reviews subprocess/path/deserialization/secrets/network.
- `cli-ux-reviewer` — help text, error messages, defaults, output/stream design, scriptability.

Consolidate into one prioritized list: **Must fix**, **Should fix**, **Nits**, with file:line and concrete fixes, noting which agent flagged each. Don't change code unless I ask.
