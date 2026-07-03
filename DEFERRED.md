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

### 4. `python -m pixiv_bookmark_backup` bypasses the click group — DONE
*Source: cli-tester (during the `--time-per-request` feature). Pre-existing bug, unrelated to that feature. Behavior fix.*

Fixed: `__main__.py` now does `from .cli import cli` / `cli()`, so the module
invocation dispatches through the group exactly like the console script.
Regression coverage lives in `tests/test_main_module.py`, which spawns a real
`python -m pixiv_bookmark_backup ...` subprocess (a `CliRunner` test invokes the
group object directly and so would not catch this wiring bug) and asserts that
`backup-following`/`login`/`search` dispatch to their own subcommands rather than
binding to `backup`'s `USER_ID` positional. Kept here for history.

Original report below.

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

## Manifest loading

### 5. Harden `load_or_init` against a corrupt or deeply-nested manifest
*Source: security review of the `search` feature (out of scope there). Behavior change.*

`load_or_init` in `manifest.py` (the resume-oriented loader used by `backup` /
`backup-following`) calls `json.load(handle)` with no exception handling. Two
unguarded failure modes on an existing `details.json`:

- A deeply-nested document raises `RecursionError` (CPython's JSON decoder is
  recursive), which propagates as a raw traceback.
- A corrupt / truncated file raises `json.JSONDecodeError`, also propagating raw.

The read-only `search` path already handles both cleanly in `load_manifest_strict`
(`except json.JSONDecodeError` / `except RecursionError` → `ManifestLoadError`),
so this is only about the resume path. Lower risk than the `search` case because
`load_or_init` reads a `details.json` the tool itself wrote at mode `0o600` in the
user's own output dir, so hostile input essentially requires the user to have
hand-edited or swapped their own manifest.

The fix isn't just catching the exceptions — it's a policy decision for a corrupt
existing manifest on resume: either **fail cleanly** with a teaching error, or
**move it aside to `.bak` and regenerate** (mirroring the existing
schema-mismatch branch, which already returns an empty manifest and lets the
caller write the `.bak`). Because it changes resume behavior, route it through
the planner rather than fixing inline.

## `search` command

### 6. Add a `--layout` option for `search --symlink-dir` — DONE

Implemented: `search --symlink-dir --layout {flat|by-author|titled}` (default
`flat`). `by-author` groups links into per-author subdirs `<account>_<id>/`;
`titled` prefixes link names with the sanitized work title. Untrusted metadata is
sanitized in `naming.sanitize_component`/`author_dir_name`/`titled_link_name`/
`safe_basename` (control chars stripped, separators/`.`/`..`/empty neutralized,
byte-capped on a codepoint boundary), with a parent-dir containment backstop in
`linking.plan_and_link`. Kept here for history.

### 7. Handle cross-work link-name collisions in `search --symlink-dir`
*Source: quality/security review of the `--symlink-dir` feature (noted, out of scope). Robustness.*

`plan_and_link` in `linking.py` names each symlink with the image's basename
(`Path(image.path).name`) and treats a name already pointing at a *different*
resolved target as a conflict (fatal without `--force`, re-pointed with it). This
relies on link names being unique across works — which holds today because the
backup writer names every image `<id>_p<n>.<ext>` (ID-prefixed, globally unique),
documented as an assumption in the `plan_and_link` docstring but **not enforced by
the module**. If a future change (e.g. the `--layout` option in item 6, or a
different backup naming scheme) makes two matched works resolve to the same link
name, the second would be misclassified as a conflict/replace of the first rather
than a distinct image. Only worth addressing if/when naming stops guaranteeing
uniqueness: options are a deterministic de-dup suffix, or per-work subdirectories.
No action needed while the flat ID-prefixed scheme is the only one.

### 8. Surface a clean error for a structurally malformed manifest in `--layout by-author`
*Source: security review of the `--layout` feature (out of scope, robustness/UX not security). Low priority.*

`naming.author_dir_name` reads `work.author["id"]` by direct subscript (matching
the `data["..."]` convention throughout `manifest.py`'s `from_dict` methods). A
`details.json` missing the `author.id` key therefore raises a raw `KeyError` →
unhandled traceback and a non-zero exit, rather than a clean `SymlinkError`/
`ManifestLoadError` with a teaching message. This is a robustness/UX gap, not a
security hole (a missing required field crashes loudly; it never yields an unsafe
path or data loss), and it only arises from a hand-edited/corrupted manifest.
Fix by validating required author fields at manifest-load time, or mapping the
`KeyError` to a teaching error at the `--symlink-dir` call site. Note the strict
loader `load_manifest_strict` does **not** catch this: `WorkEntry.from_dict`
copies the `author` dict wholesale without reading `author["id"]`, so a manifest
missing that key loads cleanly and only crashes later, at link time.

## Tooling

### 9. Fix the `bandit` pixi task and the `pip-audit` environment
*Source: security review of the `--symlink-dir` feature (environment defects, not code). Infra.*

Two security-tooling gaps surfaced when scanners were run (neither affects any
shipped code):

- `pixi run bandit` double-appends `-r pixiv_bookmark_backup`, so the task errors
  on argument parsing. Running `bandit` directly (e.g. `pixi exec bandit -r
  pixiv_bookmark_backup`) works and reports clean, so the task definition just
  needs its duplicated `-r <dir>` removed.
- `pip-audit` cannot run: the pixi-cached tool env is missing `filelock`
  (`ModuleNotFoundError` inside `cachecontrol`), and no `pip-audit` / `audit`
  pixi task wraps it. Add `filelock` to that env (or pin a working toolchain) and
  add a `pixi run audit` task so dependency-vuln scans can run on demand.

Fixing both restores on-demand `bandit` + `pip-audit` runs for future reviews.

## Progress indicator (`backup` / `backup-following`)

### 10. Tighten `ProgressReporter.unit` to `Literal["works", "users"]`
*Source: final quality review (Nit, cosmetic).*

`ProgressReporter.unit` and the `unit` values computed in `cli.py` are plain
`str`. Nothing currently constrains them, so Pyright can't catch a typo (e.g.
`"work"` instead of `"works"`) at the type level. Switching to
`Literal["works", "users"]` on the field and the call sites would close that
gap. Low risk, low value — left as plain `str` since the two literal values
are only ever produced by the two `cli.py` call sites, both already tested.

### 11. Deduplicate reporter construction between `backup` and `backup_following`
*Source: final quality review (Nit, refactor).*

The block that resolves `stream`/`is_tty`/`ascii_only`, calls
`_resolve_reporter_mode(...)`, and constructs `ProgressReporter(...)` is
near-identical (~10 lines) in both `backup()` and `backup_following()` in
`cli.py`, differing only in how `total`/`unit` are computed. A shared
`_build_reporter(...)` helper taking the resolved `total`/`unit` would remove
the duplication. Left inline since each command's `total`/`unit` logic differs
enough that extracting the whole block felt like premature abstraction for two
call sites; revisit if a third command needs the same wiring.

### 12. `_sanitize_display_text` doesn't cover visual-spoofing code points
*Source: security review of the Hypothesis property tests for this sanitizer. Robustness, not an injection hole.*

`_sanitize_display_text` (`backup.py`) strips exactly Unicode category `Cc`
(`\x00-\x1f`, `\x7f-\x9f`) from `user.name` before it reaches a terminal. This
fully neutralizes ANSI/OSC/CSI escape-sequence injection (no C0/C1 lead byte
can survive), which was the original threat this function was written for. It
does not strip other code points some terminals render specially for visual
spoofing rather than control-sequence injection -- e.g. U+202E (RTL override)
and other `Cf` format/bidi-control characters, or zero-width characters
(Trojan-Source-style deception, not escape injection). A malicious display
name using these could still visually misrepresent itself in verbose/dry-run
output, though it cannot execute terminal control sequences. Confirmed via a
Hypothesis property test (`test_sanitize_display_text_never_yields_control_chars`)
that the sanitizer does exactly what it promises for `Cc`; this item is about
whether the promise should be broader. Fix would be extending the strip set to
`Cf` (and/or zero-width) categories, but do it as a deliberate scope decision
(does `work.title`'s `!r` protection need the same treatment?) rather than
inline.
