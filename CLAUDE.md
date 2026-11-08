# Project: Python CLI tool(s)

This repo builds Python command-line tools. Environment & dependencies are managed with **pixi**, the terminal interface uses **click**, tests use **pytest**, formatting is **black**, linting is **flake8**, type-checking is **Pyright** (the engine behind VS Code's Pylance, run from the CLI as `basedpyright`), and classes are written with **attrs**. This file is your standing instructions — treat it as binding.

## Golden workflow
Never jump straight to code for a non-trivial change. Follow: **design → plan → fact-check → implement (parallel tracks) ↔ review loop → simplify pass → run-the-real-CLI test → feature review (security/UX) → fix → re-check.** The `/feature` command runs the whole pipeline; `/plan`, `/implement`, `/review`, `/fix`, `/harden` run pieces; `/setup` bootstraps a new project. `/features <file.md>` fans the whole pipeline out over **many features at once** — one **git worktree per feature** (named from the change) so they can't collide — and opens **a PR per feature** — or, for a big feature that splits cleanly, a **stack of small PRs** following the planner's approved PR slices (see the `stacked-prs` skill) — checking each feature off in the source Markdown file as its PR (or its stack's top PR) lands; it forwards each pipeline's approval gates and any subagent questions back to you, labeled by feature.

### Parallel implementation
When the plan's **Parallelization plan** marks tasks as independent (no shared dependency, disjoint `Owns` file sets), implement them **concurrently** — launch one `implementer` per task in the same batch (plus any supporting agents that track needs). Batches run in order; a batch integrates and its review loops pass before the next starts. Never run two tracks that touch the same file concurrently — and treat the shared `pixi` manifest and the click group in `cli.py` as serialization points. Correctness beats concurrency.

### Code must pass review (implement↔review loop)
Writing the code is not "done." Each implementer track runs a loop: `implementer` produces code + tests with `pixi run check` green → `quality-reviewer` returns **APPROVED** or **CHANGES REQUESTED** → on changes, the same implementer addresses every Must-fix (with a change log) → re-review → repeat until **APPROVED**. The feature-level `security-reviewer` and `cli-ux-reviewer` gate the same way. Loops are capped (~3 rounds); unresolved disagreements escalate to a human. Reviewers approve on "high quality, gate green, no real blockers," not unreachable perfection.

### Simplify before testing
Code that passes review per track can still be more complex than it needs to be once the tracks meet. So after the last batch integrates, one `code-refactorer` runs a behavior- and contract-preserving **simplify pass** over the feature's whole diff: cross-command duplication, single-use abstractions, dead code, and the refactors the implementers deferred. Its changes loop through `quality-reviewer` to APPROVED before the CLI test. It may find nothing worth changing, and it never blocks the feature.

## Everything runs through pixi
Run code and tools via `pixi run ...` so they use the project environment — never a stray global `python`/`pip`. Manage dependencies with `pixi add <pkg>` (conda-forge, preferred) or `pixi add --pypi <pkg>`; put dev/test tools on the `dev` feature. **Never hand-edit `pixi.lock`**; commit it. The shared task vocabulary: `pixi run fmt`, `fmt-check`, `lint`, `typecheck` (Pyright), `test`, and `check` (the aggregate gate). Locally, prefix these compute-heavy runs with the OS low-priority wrapper (`nice -n 10 pixi run check`) so they don't lag the machine — see the `low-priority-execution` skill. See the `pixi-workflow` skill.

## Specialized subagents (delegate proactively)
- **cli-designer** — turns an idea into a command spec (surface, options, exit codes, edge cases). Use before planning.
- **implementation-planner** — step-by-step plan + pytest test plan + pixi dep/task changes + per-task `Owns` sets + parallelization batches; lists every `[VERIFY]` assumption.
- **fact-checker** — confirms/refutes `[VERIFY]` items against the *installed* pixi env + current docs *before* coding. The orchestrator fans these out — one fact-checker per independent verification cluster — to resolve them concurrently. If one refutes something, the plan goes **back** to the planner to be revised, then re-checked.
- **implementer** — executes its assigned slice of the plan, adds deps via pixi, writes pytest tests, stays inside its `Owns` files, and iterates until its reviewer APPROVES. Often one of several running in parallel.
- **cli-tester** — runs the *real* installed command via pixi to verify behavior, exit codes, and pipe/no-TTY handling.
- **test-engineer** — backfills meaningful pytest coverage (unit / CLI / integration).
- **quality-reviewer / security-reviewer / cli-ux-reviewer** — the review board (correctness + the black/flake8/Pyright/pytest gate and attrs-convention check; security incl. bandit/pip-audit; intuitive CLI UX). Each emits an explicit **APPROVED / CHANGES REQUESTED** verdict and may be invoked repeatedly in the loop.
- **code-refactorer** — improves readability/structure with the gate green before and after (behavior unchanged). Runs as the post-integration simplify pass in `/feature` and `/implement`, and in `/harden`.
- **bug-fixer** — reproduces, root-causes, adds a pytest regression test, fixes minimally, proves it.

Delegate context-heavy or specialized work to keep the main session focused. Reviewers and the fact-checker are read-only by design.

## Hard rules
- **Verify, don't assume.** For any version- or API-specific fact (click APIs, package versions, conda-forge availability), check the installed env (`pixi list`, `pixi run python -c ...`) and current docs. The fact-checker does this; training data may be stale.
- **Green `pixi run check` is the definition of done.** That gate is black (`fmt-check`) + flake8 (`lint`) + **Pyright** (`typecheck`, via `basedpyright`) + pytest (`test`). New behavior ships with pytest tests. Never skip/weaken tests or `# noqa`/`# type: ignore`-spam to go green. (Pylance has no CLI — it's the VS Code extension built on Pyright — so the `typecheck` task runs Pyright and matches what Pylance shows in the editor.)
- **Write classes with attrs.** Data/config/value classes use the modern `attrs` API (`from attrs import define, frozen, field`): `@define` for mutable, `@frozen` for immutable/value objects, `field(factory=...)` for mutable defaults, validators/converters at the boundary. Don't hand-roll `__init__`/`__eq__`/`__repr__` or reach for `@dataclass`. Plain functions, behavior-only classes, and small `NamedTuple`s are fine where a data class isn't the right fit. See the `attrs-classes` skill.
- **The CLI contract is an API.** Flag names, output format, and exit codes are depended on by scripts — don't rename/remove/change them outside an approved plan. Keep **data on stdout, logs/errors on stderr**, and raise `click.ClickException`/`ctx.exit(code)` instead of leaking tracebacks for expected failures.
- **Security gates.** Run the security-reviewer before committing anything touching subprocess, paths, deserialization, network, or credentials. No `shell=True` with interpolated input; no `pickle`/`yaml.load`/`eval` on untrusted data; no secrets in argv or logs.
- **Intuitive CLI design is a requirement, not polish.** Clear `--help` with examples, teaching error messages, sensible defaults, scriptable `--json`/`--yes`, and `--dry-run` for destructive actions. See the `cli-design` skill.
- **Keep the click layer thin.** Parse/validate/format in the command; put logic in plain, testable functions in the core.
- **Big features ship as a stack when they split cleanly.** Over the stacked-PR threshold (Project specifics) and with ≥ 2 approved PR slices, `/features` opens a stack of small, path-disjoint PRs, each green at its tip; otherwise one PR. Stacks never cross features. **The studio never merges**, and a restack (a force-push to your branches) happens only after you confirm it. See the `stacked-prs` skill.
- **Run compute-heavy commands at low OS priority locally.** Prefix test/lint/typecheck/format (and bandit/pip-audit) invocations via pixi with the OS low-priority wrapper — `nice -n 10 …` on Linux, `taskpolicy -b …` on macOS, or a `BelowNormal` shell on Windows — so they don't lag the machine. **Never swallow the command's exit code, and never lower priority in CI.** See the `low-priority-execution` skill.

## Skills available
`cli-design` (intuitive, scriptable commands), `python-quality` (the gate + clean/testable code), `pixi-workflow` (envs, deps, tasks), `attrs-classes` (the attrs convention for writing classes), `low-priority-execution` (running heavy commands at low OS priority locally), `stacked-prs` (splitting a big feature into a stack of small PRs, and restacking after a merge). Consult them when relevant.

## Conventions
- Match existing patterns, option naming (kebab-case flags), output style, type hints, and docstrings. Read neighbors before writing.
- Small, reviewable diffs. Note out-of-scope improvements for the right agent rather than doing them inline.
- Let black own formatting; keep flake8 aligned to black (line length 88, ignore E203/W503).

## Project specifics (fill these in)
- Package / module name + entry point: <e.g. mytool, mytool.cli:cli>
- Source dir / tests dir: <e.g. src/mytool, tests/>
- Manifest: <pixi.toml or pyproject.toml [tool.pixi]>
- Confirmed task names if they differ from the defaults above: <...>
- Optional tools enabled: <mypy (second type checker)? bandit? pip-audit? flake8 plugins?>  (Pyright via basedpyright and attrs are on by default)
- Supported platforms / min Python: <...>
- Stacked-PR threshold: 400 changed lines (excl. lockfiles/generated/goldens) — set `never` to always open one PR
