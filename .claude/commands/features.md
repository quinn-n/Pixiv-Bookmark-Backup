---
description: From a Markdown file listing multiple CLI features, build each one in parallel in its own git worktree via the full `/feature` pipeline, then open a PR per feature. Forwards each pipeline's gates and any subagent questions back to you, labeled by feature.
argument-hint: <path/to/features.md>
---

Build **every feature listed in the Markdown file at `$ARGUMENTS`**, each isolated in its **own git worktree**, running the same pipeline `/feature` runs — design → plan → fact-check → parallel implement↔review loops → real-CLI test → feature review (security/CLI-UX) → fix — **concurrently across features**, then open **one pull request per feature**.

The worktrees give true isolation: parallel features can't collide on files, the `pixi` manifest, the `cli.py` click group, the lockfile, or the `pixi run check` gate — stronger than in-tree `Owns` sets. Correctness still beats concurrency: create worktrees serially, and never mutate the base checkout mid-run.

## 0. Preflight (once, before any worktree)
- If `$ARGUMENTS` is empty or not a readable `.md` file, ask me for the path. Read the file.
- Capture the **base branch** (`git rev-parse --abbrev-ref HEAD`) and confirm a sane starting state (`git status` — clean or intentionally so; not detached HEAD). If the tree is dirty, detached, or there are multiple remotes, **stop and ask me** before creating worktrees.
- **Verify PR tooling at use-time — do not assume it's present:**
  - `gh --version` and `gh auth status` (GitHub CLI installed + authenticated?),
  - `git remote get-url origin` and confirm it points at `github.com` (or the host `gh` is configured for).
  - If `gh` is missing/unauthenticated or there's no GitHub remote, we still do all the work but **fall back** to pushing each branch and printing its compare URL for manual PR creation. Tell me up front which path we're on.
- (Complementary, optional) Claude Code also has a **native worktree surface** — `claude --worktree <name>`/`--tmux`, the `isolation: worktree` subagent field, `EnterWorktree`, and a `.worktreeinclude` file for copying untracked files into new worktrees. It branches from the default branch and auto-names branches, so it isn't a drop-in for feature-derived branch names — we hand-roll `git worktree` below for deterministic names. Re-check `claude --help` / the worktrees docs at use-time, since that surface is new and evolving; use `.worktreeinclude` if features need `.env`/untracked files present in each worktree.

## 1. Parse the features file
- **One feature per top-level item — auto-detect the file's structure.** If the file is organized by **`##` headings**, each heading is a feature (heading text = *name*; the prose/bullets beneath it until the next `##` = *description*). If it's a **top-level list** — a **numbered list** (`1.`, `2.`, …), a **bulleted list** (`-`/`*`), or a **checklist** (`- [ ]`) — each top-level item is a feature (the item's text = *name*; any indented sub-bullets/lines beneath it = *description*). A leading `#` title or intro paragraph, and section headers that merely group the list, are context — not features; `---`-separated blocks also work. Whatever structure you detect, **state how you parsed the file** at the ⏸ gate below so I can correct it.
- For each feature, derive a **slug from its name** (lowercase, kebab-case, punctuation stripped, ≤ ~40 chars; e.g. "Add a `--json` output flag" → `json-output-flag`). Branch = `feature/<slug>`, worktree dir = `../<repo>-worktrees/<slug>`. De-duplicate collisions (`-2`, `-3`).
- ⏸ **Present the parsed list — each feature's name, one-line description, branch, and worktree path — and wait for my approval.** This is where I confirm you understood the features and like the names.

## 2. Create one worktree per feature (serial)
Create the worktrees **one at a time** (git multi-worktree ops aren't guaranteed concurrency-safe), even though the pipelines afterward run in parallel:
- Pre-check collisions before each: branch exists? (`git show-ref --verify --quiet refs/heads/<branch>`) path exists? — if so, pick a suffixed name (git otherwise hard-fails). Then:
  `git worktree add -b <branch> ../<repo>-worktrees/<slug> <base-branch>`
- Each worktree gets its **own pixi environment** — run `pixi install` in it before the pipeline builds/tests there, so `pixi run check` operates on that worktree's env.
- Don't run repo-wide git operations (`git gc`, `git fetch --all`, submodule ops) from more than one worktree at once.
- Show me the final (feature → branch → worktree path) mapping.

## 3. Run the full `/feature` pipeline in each worktree, in parallel
For each feature, run the **complete `/feature` pipeline scoped to its worktree** — every subagent (`cli-designer`, `implementation-planner`, `fact-checker`, `implementer`(s), `cli-tester`, `quality-reviewer`/`security-reviewer`/`cli-ux-reviewer`, `bug-fixer`) works against **that worktree's absolute path** and its pixi env, and `pixi run check` must go green **there**. Inside each feature, honor the studio's own rules: parallel file-disjoint implementer batches, the implement↔review loop to **APPROVED**, the feature-level review board, capped rounds. Launch the current phase for all unblocked features in the same turn so they progress concurrently.

**Forward gates and questions to me, labeled by feature.** Every pipeline has ⏸ gates (design approval, final-plan approval) and any subagent may hit a real question or blocker. **Collect these across all running features and surface them together**, each tagged with **feature name + worktree + phase + the exact question + the context I need** (the relevant spec/plan excerpt, the options, and the agent's recommendation). Never silently decide a genuine choice for me. Don't let one feature's pending gate stall the others — keep unblocked features moving and batch the questions. Route my answers back to the right feature's pipeline.

Give a **one-paragraph status per round** covering every feature: phase, current verdicts, round counts, and what's blocked on me.

## 4. Open a PR per feature
When a feature is fully green in its worktree (`pixi run check` passes, real-CLI tests pass, review board APPROVED):
- Commit its work on its branch with a clear message.
- **Push first — `gh pr create` will not push for you in this non-interactive context:** `git push -u origin <branch>` (from inside that worktree).
- Open the PR: capture the URL — `PR_URL=$(gh pr create --base <base-branch> --head <branch> --title "<feature name>" --body-file <summary-file>)`. Write a real body (what was built, new pixi deps/tasks, tests, review rounds) rather than `--fill`. Report `PR_URL`.
- **Fallback** (no `gh`/remote from preflight): after `git push -u origin <branch>`, print the compare URL for manual PR creation — `https://github.com/<owner>/<repo>/compare/<base>...<branch>?expand=1` (the `?expand=1` is an informal convention; treat it as best-effort UX, not something to depend on).
- **One PR per feature** — never fold two features into one branch/PR.

## 5. Summary & cleanup
- Report a table: feature → branch → worktree → PR URL (or compare URL) → gate/test result → review rounds → new pixi deps/tasks → outstanding Should-fix/Nit items → anything still waiting on me.
- **Ask before removing worktrees.** On my OK: `git worktree remove <path>` each (`-f` if dirty), then `git worktree prune`. Leave branches and PRs intact.

Stop and ask if a feature's preconditions fail, a pipeline loop won't converge, or a PR can't be created — surface it, don't paper over it.
