# Deferred improvements

Non-blocking follow-ups surfaced during review that were intentionally left out
of their originating change. Each is safe to ship without, but worth doing later.

## `backup-following` command

### 1. Validate / warn on `--only` and `--exclude` user IDs
*Source: cli-ux review (Should-fix, Medium). Behavior change.*

Today `--only`/`--exclude` accept arbitrary strings. Two rough edges:

- `--only abc` (non-numeric) is accepted silently even though Pixiv user IDs are
  always integers.
- A syntactically valid but non-followed ID (e.g. `--only 999999`) silently
  yields a `0/0 works from 0/0 users` summary and exit 5, with no hint that the
  filter matched nobody.

Options (pick one or both):
- Validate each value as an integer at parse time via a click callback, raising a
  teaching `BadParameter` (exit 2) on a non-numeric ID.
- In `run_following`, when `--only`/`--exclude` is set but no followed user
  matched, emit a `"no followed users matched --only"` warning on stderr.

### 2. Consider renaming `--follows` to `--follow-visibility`
*Source: cli-ux review (design observation). CLI-contract change — route through the planner.*

`--follow-visibility` would be self-documenting and parallel `backup`'s
`--visibility`. The current help text was clarified to disambiguate
follow-visibility from work-visibility, so this is optional polish, not a fix.
Because renaming a flag changes the CLI contract, it should go through a design
pass rather than be done inline.

## Core

### 3. Use the `VISIBILITIES` constant in `FollowingConfig.follows`
*Source: multiple quality reviews (Nit, cosmetic).*

`FollowingConfig.follows` validates with an inline `("public", "private", "all")`
tuple, duplicating the module-level `VISIBILITIES` constant that `BackupConfig`
and the `--follows` / `--visibility` click options already use. Switching to
`validator=in_(VISIBILITIES)` removes the duplication and keeps the two configs
consistent. Left as-is originally on the argument that follow-visibility is a
conceptually distinct axis from bookmark-visibility; revisit if the values ever
need to diverge.

## Entry point

### 4. `python -m pixiv_bookmark_backup` bypasses the click group
*Source: cli-tester (during the `--time-per-request` feature). Pre-existing bug, unrelated to that feature. Behavior fix.*

`pixiv_bookmark_backup/__main__.py` does `from .cli import backup` / `backup()`,
so running `python -m pixiv_bookmark_backup ...` always invokes the standalone
`backup` command instead of the `cli` group. Any subcommand name typed after the
module (e.g. `backup-following`, `login`) is silently swallowed as `backup`'s
optional `USER_ID` positional — so `python -m pixiv_bookmark_backup backup-following`
tries to back up a user literally named `backup-following`, and `login` is
unreachable this way. Confirmed repro:

    pixi run python -m pixiv_bookmark_backup backup-following --dry-run --json \
        --refresh-token x
    # -> "Would back up public bookmarks for user backup-following into backup"

The installed console script (`pixiv-bookmark-backup`, entry point
`pixiv_bookmark_backup.cli:cli` in `pyproject.toml`) is unaffected — it correctly
dispatches to the group.

Fix: change `__main__.py` to `from .cli import cli` / `cli()` so the module
invocation matches the console script. Add a regression test invoking
`python -m pixiv_bookmark_backup backup-following --help` (or via `runpy`) and
asserting the subcommand dispatches rather than binding to `USER_ID`.
