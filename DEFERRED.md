# Deferred improvements

Non-blocking follow-ups surfaced during review that were intentionally left out
of their originating change. Each is safe to ship without, but worth doing later.

## `backup-following` command

### 1. Validate / warn on `--only` and `--exclude` user IDs — DONE
*Source: cli-ux review (Should-fix, Medium). Behavior change.*

Implemented both options. `--only`/`--exclude` values are validated as Pixiv
user IDs (non-negative integers) at parse time via a shared click callback
(`_validate_user_ids` in `cli.py`), so `--only abc` now fails fast with a
teaching `click.BadParameter` (exit 2) instead of being silently accepted. And
when an `--only`/`--exclude` ID matches no followed user, a warning naming the
unmatched IDs is emitted on stderr: the click-agnostic `run_following` gained an
injectable `warn` callback (mirroring `progress`/`clock`/`sleep`), while
`_iterate_following` records which requested IDs were observed and reports
whether enumeration was truncated by `--limit`/`--max-users`/a partial `ApiError`
so the warning is suppressed on a truncated run (avoiding false positives). This
also surfaces the previously-silent partial-miss case (e.g. `--only 111 999`
where only 111 is followed, which used to exit 0 with no signal). Exit codes,
stdout, and the JSON summary are unchanged; the warning is stderr-only. Kept here
for history.

Original report below.

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

### 2. Consider renaming `--follows` to `--follow-visibility` — DONE
*Source: cli-ux review (design observation). CLI-contract change — route through the planner.*

Done: `backup-following`'s option was hard-renamed `--follows` → `--follow-visibility`
(no backward-compat alias). A design pass justified the hard rename: the tool is
pre-1.0 and unpublished with no CHANGELOG or evidence of external scripting, and the
JSON manifest/summary key was *already* `follow_visibility`, so the rename reduces net
inconsistency rather than introducing a migration. The internal
`FollowingConfig.follows` field was renamed to match (see item #3) and the local
`_follow_passes` parameter in `backup.py` followed. Help text/examples and `README.md`
were updated; regression tests assert the new flag plumbs through, the old `--follows`
spelling now exits 2 ("No such option"), and an out-of-range choice still exits 2.

Original report below.

`--follow-visibility` would be self-documenting and parallel `backup`'s
`--visibility`. The current help text was clarified to disambiguate
follow-visibility from work-visibility, so this is optional polish, not a fix.
Because renaming a flag changes the CLI contract, it should go through a design
pass rather than be done inline.

## Core

### 3. Use the `VISIBILITIES` constant in `FollowingConfig.follows` — DONE
*Source: multiple quality reviews (Nit, cosmetic).*

Done: bundled with the item #2 rename, since both touch the same field definition
line. `FollowingConfig.follow_visibility` (renamed from `follows`) now validates with
`validator=in_(VISIBILITIES)` instead of the inline `("public", "private", "all")`
tuple, removing the duplication and keeping it consistent with `BackupConfig` and the
click options. `VISIBILITIES` was already imported in `core.py`.

Original report below.

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

### 5. Harden `load_or_init` against a corrupt or deeply-nested manifest — DONE
*Source: security review of the `search` feature (out of scope there). Behavior change. Supersedes closed PR #13, whose silent-`.bak`+regenerate-on-corrupt approach the maintainer rejected.*

Done with a **fail-loud** policy (the maintainer's decision), *not* the rejected
silent-regenerate approach. `load_or_init` in `manifest.py` now wraps the
`json.load` in `except json.JSONDecodeError` / `except RecursionError` and guards
`isinstance(data, dict)`, raising a teaching `ManifestLoadError` (naming the path,
explaining the file looks corrupt/hand-edited, and how to recover) on a corrupt,
deeply-nested, or non-object manifest — the corrupt file is **left untouched on
disk**, never silently discarded. `OSError` from opening the file is deliberately
*not* caught here so `backup.py`'s `_load_existing_manifest` (narrowed from
`except (json.JSONDecodeError, OSError)` to `except OSError`) keeps its genuine
I/O-recovery path. The `backup` and `backup-following` click commands map
`ManifestLoadError` → `click.UsageError` (exit 2), mirroring the `search`
command exactly — clean stderr message, no traceback, no partial summary on
stdout.

A genuine `schema_version` mismatch keeps the safe move-aside-to-`details.json.bak`
+ regenerate behavior (a routine tool-version bump must not block resume) but is
**no longer silent**: it is announced on stderr, naming the `.bak` file. The
announcement is emitted via an injected click-agnostic `warn` callback threaded
through `run()`/`run_following()` → `_flush_manifest`/`_flush_following_manifest`,
so `manifest.py`/`backup.py` stay click-free; `cli.py` wires `warn` to
`click.echo("Warning: ...", err=True)` (reusing the reporter-aware closure the
`backup-following` `--only` warning already used, and adding an equivalent to the
`backup` command). A structurally-valid dict with a matching schema but missing
required keys (raw `KeyError` from `Manifest.from_dict`) is deliberately left out
of scope. Tests were inverted to assert the fail-loud behavior (unit at the
loader level and CLI level via `CliRunner` for both commands) plus a
schema-mismatch test asserting the new stderr `.bak` warning. Kept here for
history.

Original report below.

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

### 11. Deduplicate reporter construction between `backup` and `backup_following` — DONE
*Source: final quality review (Nit, refactor).*

Implemented: a shared `_build_reporter(*, quiet, verbose, dry_run, total, unit)`
helper now lives in `cli.py` (right after `_stream_is_ascii_only`) and resolves
`stream`/`is_tty`/`ascii_only`, calls `_resolve_reporter_mode(...)`, and
constructs `ProgressReporter(...)`. Both `backup()` and `backup_following()`
call it, passing only their own already-resolved `total`/`unit` (`backup`
passes `total=limit, unit="works"`; `backup_following` keeps its own
`total`/`unit` branching on `limit`/`max_users` untouched and passes the
result through). No behavior change; `pixi run check` stays green at the same
708 passed / 2 skipped / 99% coverage baseline.

### 12. `_sanitize_display_text` doesn't cover visual-spoofing code points — DONE
*Source: security review of the Hypothesis property tests for this sanitizer. Robustness, not an injection hole.*

Fixed: `_sanitize_display_text` now strips Unicode categories `Cc` **and** `Cf`
(`"".join(c for c in s if unicodedata.category(c) not in {"Cc", "Cf"})`,
replacing the old `re.sub` over a fixed C0/C1 byte range). Verified empirically
against the installed Python's `unicodedata` (version 16.0.0) that every
commonly-cited bidi-override and zero-width character -- U+202E/U+202D
(RTL/LTR override), U+2066-U+2069 (directional isolates), U+200B/U+200C/U+200D
(zero-width space/ZWNJ/ZWJ), U+FEFF, U+2060 (word joiner), U+00AD (soft
hyphen), U+061C (Arabic letter mark), U+200E/U+200F (LRM/RLM) -- is category
`Cf`, so stripping `Cf` alone covers both the bidi-override and zero-width
spoofing vectors named below; no separate zero-width-specific logic was
needed. `work.title` was deliberately left unchanged: `repr()` (used via
`work.title!r`) was verified to already backslash-escape every `Cc`/`Cf`
character it encounters (e.g. `repr("a‮b") == "'a\\u202eb'"`), so its
existing protection already defeats the same spoofing vectors for titles --
adding sanitization there would be redundant. Regression coverage: the
existing `test_sanitize_display_text_never_yields_control_chars` (`Cc`)
property test is preserved unchanged; a new
`test_sanitize_display_text_never_yields_format_chars` property test and a new
concrete `test_user_event_line_strips_rtl_override` example test (U+202E,
mirroring the existing ESC-byte example) cover `Cf`; the identity test's
blacklist was extended from `("Cc", "Cs")` to `("Cc", "Cf", "Cs")`. Kept here
for history.

Original report below.

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

### 13. A lone Unicode surrogate in `user.name` can crash a verbose run
*Source: security review of item #12's `Cc`/`Cf` fix. Pre-existing gap, not introduced by that change.*

`_sanitize_display_text` (and the `Cc`-only sanitizer it replaced) does not
strip Unicode category `Cs` (lone/unpaired surrogates, e.g. `U+D800`).
`json.loads('"\\ud800abc"')` happily produces a Python `str` containing one
(a malformed or adversarial Pixiv API response could do the same for a
display name), and `click.echo()` on a string containing a lone surrogate
raises an unhandled `UnicodeEncodeError` -- crashing a `--verbose`
`backup-following` run instead of degrading gracefully. `Cs` was deliberately
left out of item #12's fix (that item was scoped to visual-spoofing code
points, not encoding crashes), but it is a real, concrete robustness gap
directly adjacent to the same sanitizer. Fix: either strip `Cs` in
`_sanitize_display_text` (broadening its docstring accordingly) or replace
lone surrogates with `U+FFFD REPLACEMENT CHARACTER` so a malformed name still
displays as *something* rather than crashing the run.

### 14. `Zl`/`Zp` line/paragraph separators pass through `_sanitize_display_text` unstripped
*Source: security review of item #12's `Cc`/`Cf` fix. Low priority, robustness.*

`U+2028 LINE SEPARATOR` and `U+2029 PARAGRAPH SEPARATOR` (categories `Zl`/`Zp`)
are not stripped by `_sanitize_display_text`, so a Pixiv display name
containing one could still visually reflow a verbose/dry-run `line` across
what looks like multiple lines in some terminals/loggers. They sit right next
to the bidi-override/zero-width threat model item #12 addressed, and
`work.title`'s `!r`/repr protection already escapes both (confirmed:
`repr(chr(0x2028)) == "'\\u2028'"`), so the practical risk is low and mostly a
consistency question. Left out of item #12's fix since it was scoped to
`Cf`; worth folding into `_SANITIZED_CATEGORIES` (as `{"Cc", "Cf", "Zl",
"Zp"}`) if a future review wants a broader "never lets untrusted text
reshape terminal output" guarantee.
