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
import sys
from pathlib import Path

import click

from . import __version__
from .auth_login import GpptLogin, LoginDriverError, LoginFailed, LoginFlow
from .backup import MANIFEST_NAME, BackupUsageError
from .backup import run as run_backup
from .config import (
    ConfigError,
    ConfigWriteError,
    NoTokenError,
    _token_from_config,
    default_config_path,
    is_insecure_config,
    resolve_auth,
    write_refresh_token,
)
from .core import VISIBILITIES, BackupConfig, ExitCode, RunSummary, decide_exit_code
from .pixiv_client import ApiError, AuthRejected, Pixivpy3Client

#: Version stamped as the first key of the ``--json`` summary object, so a
#: scripting consumer can detect the payload shape and adapt across releases.
JSON_SUMMARY_SCHEMA = 1

#: Version stamped as the first key of the ``login --json`` result object, so a
#: scripting consumer can detect the payload shape and adapt across releases.
JSON_LOGIN_SCHEMA = 1


def _build_login() -> LoginFlow:
    """Construct the default interactive login flow (overridable in tests)."""

    return GpptLogin()  # pragma: no cover - real optional driver, not exercised offline


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


@cli.command()
@click.option(
    "--config",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="PIXIV_BOOKMARK_BACKUP_CONFIG",
    default=None,
    help="Path to the TOML config file to write [auth].refresh_token into.",
)
@click.option(
    "--headless/--no-headless",
    default=False,
    help="Drive the browser headlessly (no visible window).",
)
@click.option(
    "--username",
    type=str,
    envvar="PIXIV_USERNAME",
    default=None,
    help="Pixiv email/ID for non-interactive headless login (prefer the env var).",
)
@click.option(
    "--password",
    type=str,
    envvar="PIXIV_PASSWORD",
    default=None,
    help="Pixiv password for headless login (prefer the env var; argv is visible "
    "via ps).",
)
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    default=120,
    show_default=True,
    help="Seconds to wait for login to complete before giving up.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing [auth].refresh_token without prompting.",
)
@click.option(
    "--print-token/--no-print-token",
    default=False,
    help="Also print the refresh token to stdout (insecure: it lands in "
    "scrollback).",
)
@click.option(
    "--stdout-only",
    is_flag=True,
    help="Print ONLY the bare token to stdout and write no file (for "
    "export PIXIV_REFRESH_TOKEN=$(... --stdout-only)).",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print a machine-readable result object to stdout.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help='Assume "yes" for prompts (e.g. overwrite an existing token).',
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run the login flow but do not write or modify the config file.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress progress output on stderr.",
)
@click.option(
    "--verbose",
    "-v",
    count=True,
    help="Increase progress detail on stderr; repeat for more.",
)
@click.pass_context
def login(
    ctx: click.Context,
    config: Path | None,
    headless: bool,
    username: str | None,
    password: str | None,
    timeout: int,
    force: bool,
    print_token: bool,
    stdout_only: bool,
    json_output: bool,
    yes: bool,
    dry_run: bool,
    quiet: bool,
    verbose: int,
) -> None:
    """Log in to Pixiv and save a refresh token to the config file.

    Opens a Pixiv login in a browser (headed by default; pass --headless to run
    without a visible window), completes the OAuth flow, and writes the resulting
    token to [auth].refresh_token in the config file at mode 0o600 so a later
    'backup' run picks it up automatically.

    For non-interactive headless login, supply both --username and --password
    (preferably via the PIXIV_USERNAME / PIXIV_PASSWORD environment variables, so
    the password is not exposed to other processes via ps).

    \b
    Exit codes:
      0 success    2 usage/auth-input error   3 token rejected
      4 network    7 login driver/browser unavailable
      8 login failed/timeout                  9 config write failed
      130 interrupted

    \b
    Examples:
      # Open a browser and log in interactively, saving to the default config
      pixiv-bookmark-backup login

    \b
      # Non-interactive headless login using environment credentials
      PIXIV_USERNAME=me@example.com PIXIV_PASSWORD=... \\
          pixiv-bookmark-backup login --headless

    \b
      # Print only the token and export it without writing any file
      export PIXIV_REFRESH_TOKEN=$(pixiv-bookmark-backup login --stdout-only)
    """

    if quiet and verbose:
        raise click.UsageError("--quiet and --verbose are mutually exclusive")
    if stdout_only and force:
        raise click.UsageError("--stdout-only and --force are mutually exclusive")
    if (
        stdout_only
        and ctx.get_parameter_source("config") is click.core.ParameterSource.COMMANDLINE
    ):
        raise click.UsageError(
            "--stdout-only does not write a file, so --config has no effect; "
            "pass one or the other"
        )
    if (username is None) != (password is None):
        raise click.UsageError("provide both --username and --password, or neither")

    # A password passed as a CLI flag is visible to other processes (ps, /proc).
    # Warn (always, even when quiet -- it is a security warning) without ever
    # printing the password itself. Env-var-sourced passwords do not trip this.
    if ctx.get_parameter_source("password") is click.core.ParameterSource.COMMANDLINE:
        click.echo(
            "Warning: --password is visible to other processes (e.g. ps); "
            "prefer the PIXIV_PASSWORD environment variable.",
            err=True,
        )

    # click already applied the PIXIV_BOOKMARK_BACKUP_CONFIG envvar fallback.
    target = config if config is not None else default_config_path()

    # Decide whether we are overwriting a pre-existing token. This gate is
    # irrelevant when not writing a file (--stdout-only) or not modifying it
    # (--dry-run), so skip it entirely in those modes.
    overwrote_existing = False
    if not stdout_only and not dry_run:
        try:
            existing = _token_from_config(target)
        except ConfigError as exc:
            raise _exit_error(
                f"{exc}; fix the file or re-run with --stdout-only.",
                int(ExitCode.CONFIG_WRITE),
            ) from exc
        if existing:
            if force or yes:
                overwrote_existing = True
            elif sys.stdin.isatty():
                if not click.confirm(
                    f"A refresh token already exists in {target}. Overwrite it?"
                ):
                    click.echo("Aborted; existing token unchanged.", err=True)
                    ctx.exit(int(ExitCode.SUCCESS))
                overwrote_existing = True
            else:
                raise click.UsageError(
                    f"a refresh token already exists in {target}; pass --force "
                    "or --yes to overwrite"
                )

    flow = _build_login()

    if not quiet:
        if headless:
            click.echo("Logging in to Pixiv...", err=True)
        else:
            click.echo(
                "Opening Pixiv login in your browser; complete the login there...",
                err=True,
            )

    try:
        result = flow.login(
            headless=headless,
            username=username,
            password=password,
            timeout=timeout,
        )
    except LoginDriverError as exc:
        raise _exit_error(str(exc), int(ExitCode.LOGIN_DRIVER)) from exc
    except LoginFailed as exc:
        raise _exit_error(str(exc), int(ExitCode.LOGIN_FAILED)) from exc
    except AuthRejected as exc:
        raise _exit_error(str(exc), int(ExitCode.AUTH)) from exc
    except ApiError as exc:
        raise _exit_error(f"Pixiv API error: {exc}", int(ExitCode.NETWORK)) from exc
    except KeyboardInterrupt:
        click.echo("interrupted; no token saved", err=True)
        ctx.exit(int(ExitCode.INTERRUPTED))

    wrote_config = False
    if not dry_run and not stdout_only:
        try:
            write_refresh_token(target, result.refresh_token)
        except ConfigWriteError as exc:
            # Never print the token here -- only the failure and a recovery hint.
            raise _exit_error(
                f"{exc}; re-run with --stdout-only to print the token instead.",
                int(ExitCode.CONFIG_WRITE),
            ) from exc
        wrote_config = True

    # --stdout-only wins even when --json is also passed: the "pipeable token"
    # contract (export VAR=$(... --stdout-only)) takes precedence so only the
    # bare token reaches stdout.
    if stdout_only:
        click.echo(result.refresh_token)
        ctx.exit(int(ExitCode.SUCCESS))

    if json_output:
        _emit_login_result(
            target=target,
            wrote_config=wrote_config,
            overwrote_existing=overwrote_existing,
            dry_run=dry_run,
            token=result.refresh_token if print_token else None,
        )
        ctx.exit(int(ExitCode.SUCCESS))

    if dry_run:
        click.echo(f"Would save refresh token to {target}")
    else:
        click.echo(f"Saved refresh token to {target}")
    if print_token:
        click.echo(f"refresh_token: {result.refresh_token}")

    ctx.exit(int(ExitCode.SUCCESS))


def _emit_login_result(
    *,
    target: Path,
    wrote_config: bool,
    overwrote_existing: bool,
    dry_run: bool,
    token: str | None,
) -> None:
    """Write the ``login --json`` result object to **stdout**.

    ``token`` is included only when the user opted in (``--print-token``);
    otherwise it is ``None`` so the secret never lands in scrollback.
    """

    payload = {
        "schema": JSON_LOGIN_SCHEMA,
        "config_path": str(target),
        "wrote_config": wrote_config,
        "overwrote_existing": overwrote_existing,
        "dry_run": dry_run,
        "refresh_token": token,
        "exit_code": int(ExitCode.SUCCESS),
    }
    click.echo(json.dumps(payload))
