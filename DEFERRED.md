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

## Progress indicator (`backup` / `backup-following`)

### 4. Tighten `ProgressReporter.unit` to `Literal["works", "users"]`
*Source: final quality review (Nit, cosmetic).*

`ProgressReporter.unit` and the `unit` values computed in `cli.py` are plain
`str`. Nothing currently constrains them, so Pyright can't catch a typo (e.g.
`"work"` instead of `"works"`) at the type level. Switching to
`Literal["works", "users"]` on the field and the call sites would close that
gap. Low risk, low value — left as plain `str` since the two literal values
are only ever produced by the two `cli.py` call sites, both already tested.

### 5. Deduplicate reporter construction between `backup` and `backup_following`
*Source: final quality review (Nit, refactor).*

The block that resolves `stream`/`is_tty`/`ascii_only`, calls
`_resolve_reporter_mode(...)`, and constructs `ProgressReporter(...)` is
near-identical (~10 lines) in both `backup()` and `backup_following()` in
`cli.py`, differing only in how `total`/`unit` are computed. A shared
`_build_reporter(...)` helper taking the resolved `total`/`unit` would remove
the duplication. Left inline since each command's `total`/`unit` logic differs
enough that extracting the whole block felt like premature abstraction for two
call sites; revisit if a third command needs the same wiring.
