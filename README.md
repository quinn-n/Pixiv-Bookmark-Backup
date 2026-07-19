# pixiv-bookmark-backup

Back up your Pixiv bookmarks from the command line.

`pixiv-bookmark-backup` downloads your (or anyone's public) Pixiv bookmarks —
illustrations, manga, and ugoira (converted to GIF) — to a local directory,
alongside a `details.json` manifest recording every work and its tags. Re-runs
are safe: existing image files are skipped by default (`--skip-existing-files`),
and the manifest is merged. For a faster incremental re-run, `--skip-scanned`
(opt-in, default off) goes further and skips works already recorded in the
manifest entirely — avoiding the per-work Pixiv API fetch and its
`--time-per-request` pacing wait, not just the file download.
A companion `search` command lets you query that manifest offline, and
`backup-following` extends the same backup to everyone you follow.

## Features

- **`backup`** — back up bookmarks (yours or another user's public ones).
- **`backup-following`** — back up works from every creator you follow, one
  subdirectory per author.
- **`login`** — drive the Pixiv OAuth login flow in a browser and save a
  refresh token for later runs.
- **`search`** — search a local `details.json` manifest by tag/author/NSFW
  offline, with optional symlink export for browsing matches in a file
  manager.
- Resumable, rate-limitable downloads with `--dry-run`, `--json`, `--quiet`/
  `--verbose`, and machine-readable exit codes throughout.

## Installation

This project is managed with [pixi](https://pixi.sh):

```bash
git clone <this repo>
cd pixiv-bookmark-backup
pixi install
```

Run the CLI through pixi so it uses the project environment:

```bash
pixi run pixiv-bookmark-backup --help
```

Each subcommand also has its own `pixi run` shortcut — `pixi run backup`,
`pixi run backup-following`, `pixi run login`, and `pixi run search` — which
are used in the examples below.

## Authentication

Backing up bookmarks requires a Pixiv **refresh token**. The `login` command
drives the Pixiv OAuth flow for you, but its browser driver (`gppt` +
Playwright) is optional and lives on its own pixi environment. The
`pixi run login` task already targets that environment, so no `-e login` flag
is needed:

```bash
pixi run login
```

This opens a browser, completes the OAuth flow, and saves the token to
`${XDG_CONFIG_HOME:-~/.config}/pixiv-bookmark-backup/config.toml` (mode
`0600`) so `backup` and `backup-following` pick it up automatically. For
headless/CI use:

```bash
PIXIV_USERNAME=me@example.com PIXIV_PASSWORD=... \
    pixi run login --headless
```

Alternatively, supply a token directly to any command via `--refresh-token`,
the `PIXIV_REFRESH_TOKEN` environment variable (preferred — it keeps the
secret out of shell history and `ps`), or `[auth].refresh_token` in a config
file (`--config` / `PIXIV_BOOKMARK_BACKUP_CONFIG`).

## Usage

### Back up your own bookmarks

```bash
pixi run backup
```

```bash
# Preview without writing anything
pixi run backup --dry-run

# Everything (public + private), into ./out
pixi run backup -o ./out --visibility all

# Another user's public bookmarks, newest first, capped at 50
pixi run backup 12345 --limit 50 --since 2025-01-01

# Fast incremental re-run: skip works already recorded in details.json,
# not just their already-downloaded files
pixi run backup --skip-scanned
```

`--skip-scanned` is independent of `--skip-existing-files`: one skips
re-fetching a work from the API, the other skips re-downloading its image
files. Note that a work recorded by a prior `--metadata-only` run has no
downloaded images but also no failed ones, so it counts as already scanned; a
later full run with `--skip-scanned` will skip it and won't download its images.

### Back up everyone you follow

```bash
pixi run backup-following --dry-run
pixi run backup-following --follow-visibility all --per-user-limit 20
pixi run backup-following -o ./out --only 123 --only 456
```

### Search a local backup

```bash
pixi run search touhou original
pixi run search touhou --symlink-dir ./review --layout by-author
pixi run search --nsfw only --sort id --count
pixi run search --ugoira only --sort id
```

`search` never touches the network or requires a token — it only reads the
`details.json` manifest that `backup`/`backup-following` already wrote.

Run `pixiv-bookmark-backup <command> --help` for the full option list,
examples, and exit codes of each command.

## Output

Images are written as flat files named `<id>_p<n>.<ext>` (ugoira animations
as `<id>_p0.gif`) in the output directory, alongside `details.json`. Pass
`--json` to any command for a machine-readable summary on stdout instead of
the human-readable line; progress and warnings always go to stderr, so
`--json` output stays script-safe.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Success |
| 2 | Usage / auth-input error |
| 3 | Token rejected |
| 4 | Network error |
| 5 | No bookmarks/follows/works found |
| 6 | Partial success (some images failed) |
| 7 | Login driver/browser unavailable (`login`) |
| 8 | Login failed/timed out (`login`) |
| 9 | Config write failed (`login`) |
| 10 | Symlink write error (`search --symlink-dir`) |
| 130 | Interrupted (Ctrl-C) |

## Development

```bash
pixi run check   # fmt-check + lint + typecheck + test (the full gate)
pixi run fmt     # black
pixi run lint    # flake8
pixi run typecheck  # basedpyright
pixi run test    # pytest
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
