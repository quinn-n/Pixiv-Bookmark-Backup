"""Command-line interface for pixiv-bookmark-backup.

The click layer is intentionally thin: it parses and validates input, resolves
credentials, builds a :class:`~pixiv_bookmark_backup.core.BackupConfig`,
delegates to the backup orchestrator, and formats the result. Data (the run
summary) goes to **stdout**; progress, logs, and errors go to **stderr**.
Expected failures are surfaced as click exceptions with the right exit code
rather than leaking tracebacks.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import click

from . import __version__
from .backup import MANIFEST_NAME, BackupUsageError
from .backup import run as run_backup
from .config import (
    ConfigError,
    NoTokenError,
    default_config_path,
    is_insecure_config,
    resolve_auth,
)
from .core import VISIBILITIES, BackupConfig, ExitCode, RunSummary, decide_exit_code
from .pixiv_client import ApiError, AuthRejected, Pixivpy3Client

#: Version stamped as the first key of the ``--json`` summary object, so a
#: scripting consumer can detect the payload shape and adapt across releases.
JSON_SUMMARY_SCHEMA = 1


def _exit_error(message: str, exit_code: int) -> click.ClickException:
    """Build a :class:`click.ClickException` that exits with ``exit_code``.

    :class:`click.ClickException` prints its message to stderr and exits with
    its ``exit_code`` attribute (which defaults to 1). We set it per-instance so
    each mapped failure (auth, network, ...) lands on stderr with the contract's
    code, without subclassing (which would trip flake8-bugbear's B042 over the
    custom ``__init__`` signature).
    """

    error = click.ClickException(message)
    error.exit_code = exit_code
    return error


def _parse_since(
    ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> datetime.date | None:
    """Parse a ``--since`` value (``YYYY-MM-DD``) into a :class:`datetime.date`.

    Returns ``None`` when the flag is omitted. Raises
    :class:`click.BadParameter` (exit code 2) on a malformed date so the user
    gets a teaching message instead of a traceback.
    """

    if value is None:
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise click.BadParameter(
            f"{value!r} is not a valid date; expected the form YYYY-MM-DD "
            "(e.g. 2025-01-31)."
        ) from None


@click.group()
@click.version_option(__version__, prog_name="pixiv-bookmark-backup")
def cli() -> None:
    """Back up your Pixiv bookmarks."""


@cli.command()
@click.argument("user_id", required=False)
@click.option(
    "--output-dir",
    "-o",
    default="./backup",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the backup (images and details.json) into.",
)
@click.option(
    "--visibility",
    type=click.Choice(VISIBILITIES),
    default="public",
    show_default=True,
    help="Which bookmarks to back up. Private/all only work for your own account.",
)
@click.option(
    "--refresh-token",
    type=str,
    envvar="PIXIV_REFRESH_TOKEN",
    default=None,
    help=(
        "Pixiv refresh token. Prefer the PIXIV_REFRESH_TOKEN environment "
        "variable so the secret stays out of your shell history and process "
        "list."
    ),
)
@click.option(
    "--config",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="PIXIV_BOOKMARK_BACKUP_CONFIG",
    default=None,
    help="Path to a TOML config file providing [auth].refresh_token.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Stop after backing up this many works (useful for a quick sample).",
)
@click.option(
    "--since",
    type=str,
    callback=_parse_since,
    default=None,
    metavar="YYYY-MM-DD",
    help="Only back up works posted on or after this date (the work's post date).",
)
@click.option(
    "--skip-existing/--no-skip-existing",
    default=True,
    show_default=True,
    help="Skip files already present on disk (re-run safe), or re-download them.",
)
@click.option(
    "--metadata-only",
    is_flag=True,
    help=(
        "Record metadata in details.json without downloading any image bytes "
        "(writes details.json but no image files; --dry-run writes nothing)."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print a machine-readable run summary to stdout.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress progress output on stderr (the summary is still printed).",
)
@click.option(
    "--verbose",
    "-v",
    count=True,
    help="Increase progress detail on stderr; repeat for more (-vv).",
)
@click.option(
    "--max-retries",
    type=click.IntRange(0, 10),
    default=3,
    show_default=True,
    help="Number of retries per image after the first attempt fails.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be backed up without downloading or writing anything.",
)
@click.pass_context
def backup(
    ctx: click.Context,
    user_id: str | None,
    output_dir: Path,
    visibility: str,
    refresh_token: str | None,
    config: Path | None,
    limit: int | None,
    since: datetime.date | None,
    skip_existing: bool,
    metadata_only: bool,
    json_output: bool,
    quiet: bool,
    verbose: int,
    max_retries: int,
    dry_run: bool,
) -> None:
    """Back up Pixiv bookmarks for USER_ID (defaults to your own account).

    The refresh token is read from --refresh-token, the PIXIV_REFRESH_TOKEN
    environment variable, or the config file, in that order. See
    https://github.com/upbit/pixivpy for how to obtain a refresh token.

    Images are written to the output directory (default ./backup) as flat files
    named <id>_p<n>.<ext> (ugoira animations as <id>_p0.gif), alongside a
    details.json manifest recording every work and per-image status.

    \b
    Exit codes:
      0 success    2 usage/auth-input error   3 token rejected
      4 network    5 no bookmarks found       6 partial (some images failed)
      130 interrupted

    \b
    Examples:
      # Preview your own public bookmarks without writing anything
      pixiv-bookmark-backup backup --dry-run

    \b
      # Back up everything (public + private) into ./out
      pixiv-bookmark-backup backup -o ./out --visibility all

    \b
      # Back up another user's public bookmarks, newest first, capped at 50
      pixiv-bookmark-backup backup 12345 --limit 50 --since 2025-01-01
    """

    if quiet and verbose:
        raise click.UsageError("--quiet and --verbose are mutually exclusive")

    # A token passed as a CLI flag is visible to other processes (ps, /proc).
    # Warn (always, even when quiet -- it is a security warning) without ever
    # printing the token itself. Env-var-sourced tokens do not trip this.
    if (
        ctx.get_parameter_source("refresh_token")
        is click.core.ParameterSource.COMMANDLINE
    ):
        click.echo(
            "Warning: --refresh-token is visible to other processes (e.g. ps); "
            "prefer the PIXIV_REFRESH_TOKEN environment variable.",
            err=True,
        )

    try:
        auth = resolve_auth(refresh_token, config_path=config)
    except (NoTokenError, ConfigError) as exc:
        raise click.UsageError(str(exc)) from exc

    # Warn if the effective config file is readable by other users -- the token
    # it carries should be owner-only. The path is the explicit --config flag if
    # given, otherwise the default location.
    config_file = config if config is not None else default_config_path()
    if is_insecure_config(config_file):
        click.echo(
            f"Warning: {config_file} is readable by other users; run "
            f"'chmod 600 {config_file}' to protect your token.",
            err=True,
        )

    run_config = BackupConfig(
        output_dir=output_dir,
        user_id=user_id,
        visibility=visibility,
        dry_run=dry_run,
        limit=limit,
        since=since,
        skip_existing=skip_existing,
        metadata_only=metadata_only,
        json_output=json_output,
        verbosity=verbose,
        quiet=quiet,
        max_retries=max_retries,
    )

    def progress(message: str) -> None:
        if not quiet:
            click.echo(message, err=True)

    if not quiet:
        target = "your own" if user_id is None else f"user {user_id}"
        verb = "Would back up" if dry_run else "Backing up"
        click.echo(
            f"{verb} {visibility} bookmarks for {target} into {output_dir}",
            err=True,
        )

    client = Pixivpy3Client()

    try:
        summary = run_backup(run_config, auth, client, progress=progress)
    except BackupUsageError as exc:
        raise click.UsageError(str(exc)) from exc
    except AuthRejected as exc:
        raise _exit_error(str(exc), int(ExitCode.AUTH)) from exc
    except ApiError as exc:
        raise _exit_error(f"Pixiv API error: {exc}", int(ExitCode.NETWORK)) from exc
    except KeyboardInterrupt:
        click.echo("interrupted; partial backup saved", err=True)
        ctx.exit(int(ExitCode.INTERRUPTED))

    code = decide_exit_code(summary)
    _emit_summary(run_config, summary, code)
    ctx.exit(int(code))


def _emit_summary(
    config: BackupConfig,
    summary: RunSummary,
    code: ExitCode,
) -> None:
    """Write the run summary to **stdout** (JSON or one human-readable line)."""

    details_json = None if config.dry_run else str(config.output_dir / MANIFEST_NAME)

    if config.json_output:
        payload = {
            "schema": JSON_SUMMARY_SCHEMA,
            "user_id": config.user_id,
            "visibility": config.visibility,
            "output_dir": str(config.output_dir),
            "works_total": summary.works_total,
            "works_succeeded": summary.works_succeeded,
            "works_failed": summary.works_failed,
            "files_downloaded": summary.files_downloaded,
            "files_skipped": summary.files_skipped,
            "files_failed": summary.files_failed,
            "bytes_downloaded": summary.bytes_downloaded,
            "details_json": details_json,
            "dry_run": config.dry_run,
            "exit_code": int(code),
        }
        click.echo(json.dumps(payload))
        return

    verb = "Would back up" if config.dry_run else "Backed up"
    click.echo(
        f"{verb} {summary.works_succeeded}/{summary.works_total} works "
        f"({summary.works_failed} failed), {summary.files_downloaded} files "
        f"to {config.output_dir}"
    )
