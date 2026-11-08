---
name: stacked-prs
description: How this studio splits a large CLI feature into a stack of small PRs instead of one big one. Covers the slicing rules the implementation-planner follows for its PR slices section, the size threshold, building the stack from the approved tree by path, gating every lower slice tip with pixi run check, the bottom-up gh pr create chain and its stack-map PR bodies, the restack after a squash merge, and use-time detection of gh stack, Graphite and git-spice. Use when planning a feature's PR slices, opening PRs in /features, or restacking a stack after its bottom PR merged.
---

# Stacked PRs

A big feature reviewed as one PR gets a worse review. Google's small-CL guide says "100 lines is usually a reasonable size for a CL, and 1000 lines is usually too large". SmartBear reports that reviews of 200–400 lines find most defects (SmartBear/Cisco 2006, vendor-reported). So a feature over the threshold ships as a **stack**: an ordered chain of small PRs. Each PR is based on the one below it, is reviewable alone, and is safe to merge in order.

Stacking happens **when possible, never by force**. A small feature, or one that won't split cleanly, ships as one PR exactly as before.

## 1. The planner proposes slices
The `implementation-planner` ends its plan with a `### PR slices` section. Each slice in it lists:
- a number;
- a one-line purpose, such as "config model + loader";
- its tasks (T-ids);
- the exact paths it changes.

If the feature shouldn't stack, the section is the single line `Single PR — <reason>`. Valid reasons include "small", "one tightly-coupled change", and "no slice is safe alone". You approve the slicing at the final-plan ⏸ gate, alongside the batches.

Slice rules:
- **Path-disjoint.** Each file's whole change lives in exactly one slice, because slices are committed by path. If two slices need the same file, merge them. A **rename or deletion** lists both the old and the new path in the same slice, or `git add -A -- <paths>` stages only half of it. Shared touch-points (the click group in `cli.py` that registers each subcommand, a package `__init__.py` that re-exports, a shared config model) usually belong to the **highest** slice that touches them.
- **Ordered by dependency.** Slice k may depend only on slices below it. Land plumbing before the surface that uses it. A typical order for this stack:
  1. `attrs` data/config classes and pure core logic, with their unit tests;
  2. I/O and integration helpers the command calls (file formats, subprocess or network wrappers), with their tests;
  3. the click command, its registration in the click group, and its `CliRunner` tests;
  4. docs (README, `--help` examples, changelog).
- **Green and safe alone.** Every slice tip must pass `pixi run check` and leave the tool working:
  - no import of a module that doesn't exist yet;
  - no subcommand registered in the click group whose implementation isn't there;
  - no half-built flag or subcommand a user can reach. The CLI contract is an API, so a lower slice may add core code nobody calls yet, but must not expose a flag, output format or exit code that the top slice then changes.

  A slice that can't stand alone merges with its neighbour.
- **Dependencies travel together.** A manifest change (`pixi.toml`, or `pyproject.toml`'s `[tool.pixi]` tables) and its `pixi.lock` change share a slice. That slice is the lowest one that needs the dependency. A new pixi task definition goes with the manifest, in the same slice.
- **Review slices are not execution batches.** Batches group file-disjoint tasks for concurrency. Slices group reviewable units for merging. The two needn't match.

## 2. Decide at PR time
**Pin the start sha first.** Run `git merge-base <base-branch> HEAD` and record the printed sha as a literal `<start-sha>`; shell variables don't persist between Bash calls. Use that sha, never the live base ref, for the measurement and the rebuild below. If the base advances mid-run, a diff or reset against the ref would silently count or revert the base's new commits.

Measure the finished diff against `<start-sha>`, excluding paths that inflate size without adding review load. The work is still uncommitted here, so stage everything to count new files too, then unstage:
```bash
git add -A
git diff --cached --numstat <start-sha> -- . ':(exclude)pixi.lock' ':(exclude)**/__snapshots__/**'
git reset --quiet
```
`git reset` with no commit unstages and leaves the working tree untouched. Sum the added + deleted columns. Also exclude any generated, vendored, golden-file or snapshot paths this repo has. Check `CLAUDE.md`'s Project specifics and the tests dir (e.g. a `tests/golden/` tree of expected outputs) before you count.

**Stack only if** the total is over the **stacked-PR threshold** in Project specifics **and** the approved plan has ≥ 2 slices. The threshold defaults to 400 changed lines; `never` turns stacking off. Otherwise open one PR, as `/features` describes.

## 3. Build the stack from the approved tree
The `cli-tester` and the review board approved one finished tree. The stack must be that tree, cut into commits; nothing is re-implemented. Work in the feature's worktree, on `feature/<slug>`, with the work still uncommitted.

1. **Commit slice by slice, by path.** For k = 1 … n:
   ```bash
   git add -A -- <slice k paths>
   git commit -m "<feature> (k/n): <purpose>"
   ```
   After each lower slice (k < n), create its branch: `git branch feature/<slug>-pt<k>-<slice-slug>`. The top slice stays on `feature/<slug>`.
2. **Prove nothing was left behind.** `git status --porcelain` must print nothing. If it prints anything, a path is missing from the slices. Add it to the slice that needs it, then rebuild from step 5's reset.
3. **Gate every lower tip.** Use the feature's own worktree, which already ran `pixi install`. The manifest and `pixi.lock` sit in the lowest slice that needs them, so every tip at or above that slice has the top's dependencies. A tip below it has the base's `pixi.lock`; `pixi run` keeps the env in step with the checked-out lockfile, so expect it to re-sync there and again after you switch back.
   ```bash
   git switch --detach feature/<slug>-pt<k>-<slice-slug>
   nice -n 10 pixi run check
   git switch feature/<slug>
   ```
   Run the final `git switch feature/<slug>` **whether or not the gate passed**, so the worktree is never left on a detached HEAD. Use the full `pixi run check`, never a subset: its Pyright step is what catches a lower slice importing a module that only exists in a higher one.
   On macOS use `taskpolicy -b` instead of `nice -n 10`; never lower priority in CI (see the `low-priority-execution` skill). The top tip is the tree the pipeline already gated.
4. **A failing tip folds upward.** Merge that slice into the one above it, then rebuild.
5. **Rebuild** from scratch with `git reset --mixed <start-sha>`, which uncommits everything and leaves the working tree untouched. Then delete the lower branches with `git branch -D feature/<slug>-pt<k>-<slice-slug>`, naming each `-pt<k>-` branch **this run created**, one by one. Never delete any other branch: `Bash(git branch:*)` is allowlisted in `settings.json`, so this command won't prompt. Then redo step 1. If only one slice remains, open one PR.
6. **Evidence is per tip.** Lower tips carry **gate evidence only**: `pixi run check` (black, flake8, Pyright, pytest). The `cli-tester` and the review board (`quality-reviewer`, `security-reviewer`, `cli-ux-reviewer`) ran on the top tip. Never claim more for a lower PR.

## 4. Open the stack
- **Push every branch first.** `gh pr create` won't push in non-interactive mode:
  ```bash
  git push -u origin feature/<slug>-pt1-<slice-slug> … feature/<slug>
  ```
- **Open the PRs bottom-up.** PR 1 targets the base branch, and PR k targets slice k−1's branch:
  ```bash
  gh pr create --base <parent-branch> --head <slice-branch> --title "<feature> (k/n): <purpose>" --body-file <file>
  ```
- **Each body opens with a stack map.** List all n slices by **branch name**, mark this one, and add one line: "Merge bottom-up; review only this PR's diff." Use branch names, not URLs, because lower PRs are opened before upper ones exist. After the map come what this slice does, any new pixi deps/tasks it adds, and its tip's gate result.
- **Lower PRs claim gate evidence only.** The top tip is the only one where the whole feature runs, so only the top PR's body describes what the feature does end to end. Each lower PR's body says: "`pixi run check` green at this tip; the feature as a whole is reviewed on `<feature> (n/n)`."
- **No credentials, tokens, or real endpoints in any body.** Same rule as the single-PR path.
- **Without `gh`,** print one compare URL per slice:
  ```text
  https://github.com/<owner>/<repo>/compare/<parent-branch>...<slice-branch>?expand=1
  ```
- **Stacks never cross features.** Each feature in a `/features` run gets its own stack, and no PR names a sibling feature.

## 5. Stack tooling: detect it, never install it
Probe at preflight and report what you found at the ⏸ gate:
- **GitHub native stacks.** `gh stack --help` succeeds when the extension is installed.
  - They're in public preview (since 2026-07-30, so they may change) and need `gh` ≥ 2.90.
  - They make merging stack-aware: after a merge, the next PR retargets and rebases automatically, and merge queues take the stack in order.
  - If present, offer to submit the same slices with it, as the recommended option. Read `gh stack --help` at use-time for how it takes titles and bodies. Hardcode nothing beyond `init`, `add`, `push`, `submit`, `view` and `rebase`.
- **Graphite.** The repo is set up when `test -e "$(git rev-parse --git-path .graphite_repo_config)"` succeeds. If you use it, never run plain `git rebase` or `git branch -d` on its branches.
- **git-spice.** It's set up when `git show-ref --verify --quiet refs/spice/data` succeeds. A missing ref doesn't prove the repo doesn't use it.
- **None found.** Use plain git + `gh` as above. It always works. Never install a stack tool.

## 6. Merging and restacking (yours, with help on request)
The studio **never merges**. Merge bottom-up. With native `gh stack`, its merge handles the rest. With plain git, after the bottom PR merges:
- **Merge commit.** Retarget the next PR to the base: `gh pr edit <next-pr> --base <base-branch>`. Don't rely on GitHub's auto-retarget, which fires when the merged branch is deleted.
- **Squash or rebase merge.** The next branch still carries the original commits. Note the old bottom tip's sha **before** merging. Then:
  ```bash
  git fetch origin
  git rebase --onto origin/<base-branch> <old-bottom-tip-sha> <next-branch>
  git push --force-with-lease origin <next-branch>
  gh pr edit <next-pr> --base <base-branch>
  ```
  Repeat up the stack, each time using the just-rebased branch's old tip. With git ≥ 2.38, `git rebase --update-refs` moves the intermediate branches in one pass. **Restack from inside the feature's worktree, or after it's removed**: git won't rebase a branch checked out in another worktree, and `--update-refs` skips such branches.

A restack force-pushes your branches and retargets your PRs. Ask for one, and the studio does it only after you confirm each push.
