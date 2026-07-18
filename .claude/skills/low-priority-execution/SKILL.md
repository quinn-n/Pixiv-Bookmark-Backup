---
name: low-priority-execution
description: How to run this project's compute-heavy commands (pixi run check/test/fmt/lint/typecheck, black, flake8, basedpyright, mypy, pytest, bandit, pip-audit) at lowered OS priority so they don't lag the machine — nice -n 10 on Linux, taskpolicy -b on macOS, a BelowNormal shell on Windows — while never swallowing the exit code and never slowing CI. Use whenever invoking or wiring any long-running or CPU/IO-heavy command locally, or editing a hook that runs one.
---

# Low-priority execution

Local/interactive runs of compute-heavy commands (the pytest suite, black/flake8/Pyright/mypy
gates, bandit, pip-audit — anything driven by `pixi run ...`) should run at **lowered OS
priority** so they don't lag the rest of the machine while you keep working. **CI runs at full
priority** — there's nothing to yield to, and lowering it just slows the run. The wrapper must
**never swallow the command's exit code** — the `pixi run check` gate depends on seeing it.

Default niceness: **`nice -n 10`** — balanced; it yields under contention without starving.
Escalate to **`nice -n 19`** / Windows **`Idle`** only for pure-background runs you're not waiting on.

## The convention

| OS | Default low-priority form | Notes |
|----|---------------------------|-------|
| **Linux** | `nice -n 10 <cmd>` | Optionally `ionice -c3 nice -n 10 <cmd>` — `ionice` is **opportunistic**: it only bites under the **BFQ** I/O scheduler and is a no-op on `mq-deadline`/`none`. Niceness inherits across fork/exec, so wrapping the runner (`nice -n 10 pixi run test`) covers the child processes it spawns. Only bites under CPU contention — no effect on an idle machine. |
| **macOS** | `taskpolicy -b <cmd>` | `-b` (PRIO_DARWIN_BG) throttles **both CPU and disk I/O**; ships in `/usr/bin`. `nice -n 10 <cmd>` also works but is **CPU-only**. No `ionice` on macOS. |
| **Windows (PowerShell, reliable)** | `(Get-Process -Id $PID).PriorityClass='BelowNormal'; <cmd>` | Lower the shell, then run the command **normally** — the child (python/pytest/etc.) inherits `BelowNormal` (Windows children inherit priority class **only** when the parent is `Idle`/`BelowNormal`), and the exit code flows via `$LASTEXITCODE` with no bug. Run in a subshell to avoid lowering your whole session: `powershell -NoProfile -Command "(Get-Process -Id $PID).PriorityClass='BelowNormal'; <cmd>"`. |
| **Windows (alternatives — verify-at-use-time)** | cmd: `start "" /belownormal /b /wait <cmd>` · PS: `Start-Process <cmd> -PassThru -NoNewWindow` then set `.PriorityClass` | The empty `""` in `start` is the window-title placeholder. **Exit-code propagation is NOT guaranteed** for these: `start /b /wait` for non-batch programs isn't documented to set `%ERRORLEVEL%`, and `Start-Process -PassThru -NoNewWindow` + `.ExitCode` is an open PowerShell bug (#5421/#20400). If you use either, smoke-test that the non-zero exit code actually propagates before relying on it in a gate. |

## Rules

1. **Never swallow the exit code.** `nice`/`ionice`/`taskpolicy` exec through transparently and
   preserve it; on Windows prefer the inherit-from-parent form above. A gate that can't see a
   non-zero exit is broken.
2. **No-op in CI.** When `$CI` / `$GITHUB_ACTIONS` / `$GITLAB_CI` is set, run at full priority.
   The convention is for local/interactive runs.
3. **Contention only.** Lowering priority is a "good neighbor" tool, not a speed knob — on an
   idle machine it changes nothing.
4. **`ionice` is opportunistic** (BFQ-only); don't promise I/O relief on Linux. macOS
   `taskpolicy -b` does throttle I/O reliably.
5. **Timing-sensitive tests:** under contention a low-priority run can trip wall-clock/timeout
   assertions or expose scheduling races. Logic is unchanged, but keep priority-lowering off for
   known timing-fragile pytest suites or give their timeouts generous margin.

## This project's commands, wrapped

Prefix the runner, not each inner tool — niceness inherits to the children `pixi` spawns:

```sh
nice -n 10 pixi run check        # the aggregate gate: fmt-check + lint + typecheck + test
nice -n 10 pixi run test         # pytest
nice -n 10 pixi run fmt          # black
nice -n 10 pixi run lint         # flake8
nice -n 10 pixi run typecheck    # Pyright (basedpyright) / mypy
nice -n 10 pixi run -q black --check src   # a single tool directly, when not via a task
```

On macOS swap `nice -n 10` for `taskpolicy -b`; on Windows use the `BelowNormal` shell form above.
Security scans are the same: `nice -n 10 pixi run bandit ...`, `nice -n 10 pixi run pip-audit`.

## Canonical bash helper (for hooks)

Add near the top of any hook that shells out to a heavy tool, then call `low_prio <tool> <args...>`.
It preserves the tool's exit code and degrades to a plain run when no wrapper is available:

```sh
# Run a compute-heavy command at low OS priority locally; full speed in CI.
# Preserves the command's exit code. Degrades to a plain run if no tool is available.
low_prio() {
  if [ -n "${CI:-}${GITHUB_ACTIONS:-}${GITLAB_CI:-}" ]; then "$@"; return; fi
  if [ "$(uname -s)" = "Darwin" ] && command -v taskpolicy >/dev/null 2>&1; then
    taskpolicy -b "$@"; return
  fi
  if command -v nice >/dev/null 2>&1; then
    if command -v ionice >/dev/null 2>&1; then ionice -c3 nice -n 10 "$@"; else nice -n 10 "$@"; fi
    return
  fi
  "$@"
}
```

Keep the helper consistent with the hook's existing shebang and `set` flags — the `${CI:-}`
default-expansions are `set -u`-safe. Do not change the hook's violation/exit-code logic;
`low_prio` returns the tool's own exit status.

## Sibling skills

- `python-quality` — the black/flake8/Pyright/pytest gate these commands run; this skill is just
  how to run that gate as a good neighbor locally.
