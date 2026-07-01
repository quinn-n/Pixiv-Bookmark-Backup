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

## Manifest loading

### 4. Harden `load_or_init` against a corrupt or deeply-nested manifest
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

### 5. Add a `--layout` option for `search --symlink-dir`
*Source: design of the `--symlink-dir` feature (deferred to keep v1 scope tight). Additive.*

`search --symlink-dir` v1 names each symlink with the image's own flat, unique
basename (`<id>_p<n>.<ext>`) — no metadata sanitization needed, idempotent, safe.
A later `--layout {flat|by-author|titled}` option could make the browse directory
friendlier:

- `by-author` — group links into per-author subdirectories.
- `titled` — prefix link names with the work title.

Both non-flat layouts require sanitizing untrusted Pixiv metadata before it hits
the filesystem: strip ASCII control chars (reuse `naming._strip_control` /
`core`'s control-stripping) **and** path separators, cap component length for
filesystem limits, and resolve duplicate names deterministically. Keep the flat
layout as the default. Because it introduces metadata-on-disk naming, run it
through the normal design → plan → security-review pipeline rather than inline.

### 6. Handle cross-work link-name collisions in `search --symlink-dir`
*Source: quality/security review of the `--symlink-dir` feature (noted, out of scope). Robustness.*

`plan_and_link` in `linking.py` names each symlink with the image's basename
(`Path(image.path).name`) and treats a name already pointing at a *different*
resolved target as a conflict (fatal without `--force`, re-pointed with it). This
relies on link names being unique across works — which holds today because the
backup writer names every image `<id>_p<n>.<ext>` (ID-prefixed, globally unique),
documented as an assumption in the `plan_and_link` docstring but **not enforced by
the module**. If a future change (e.g. the `--layout` option in item 5, or a
different backup naming scheme) makes two matched works resolve to the same link
name, the second would be misclassified as a conflict/replace of the first rather
than a distinct image. Only worth addressing if/when naming stops guaranteeing
uniqueness: options are a deterministic de-dup suffix, or per-work subdirectories.
No action needed while the flat ID-prefixed scheme is the only one.

## Tooling

### 7. Fix the `bandit` pixi task and the `pip-audit` environment
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
