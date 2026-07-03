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
from typing import TextIO

import click

from . import __version__
from .auth_login import GpptLogin, LoginDriverError, LoginFailed, LoginFlow
from .backup import BackupUsageError
from .backup import run as run_backup
from .backup import run_following
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
from .core import (
    NSFW_MODES,
    SEARCH_FIELDS,
    SORT_KEYS,
    VISIBILITIES,
    BackupConfig,
    ExitCode,
    FollowingConfig,
    FollowingRunSummary,
    ProgressEvent,
    ProgressKind,
    RunSummary,
    SearchConfig,
    SearchMatch,
    decide_exit_code,
    format_work_line,
    normalize_fields,
    run_search,
)
from .linking import (
    SymlinkConfig,
    SymlinkError,
    SymlinkResult,
    plan_and_link,
)
from .manifest import (
    MANIFEST_NAME,
    ManifestLoadError,
    load_manifest_strict,
    resolve_manifest_path,
)
from .pixiv_client import ApiError, AuthRejected, Pixivpy3Client
from .progress_reporter import ProgressReporter, ProgressUnit, ReporterMode

#: Version stamped as the first key of the ``--json`` summary object, so a
#: scripting consumer can detect the payload shape and adapt across releases.
JSON_SUMMARY_SCHEMA = 1

#: Version stamped as the first key of the ``backup-following --json`` summary
#: object, so a scripting consumer can detect the payload shape and adapt across
#: releases. Separate from :data:`JSON_SUMMARY_SCHEMA` because the field set
#: differs (per-user tallies, follow visibility).
JSON_FOLLOWING_SUMMARY_SCHEMA = 1

#: Version stamped as the first key of the ``login --json`` result object, so a
#: scripting consumer can detect the payload shape and adapt across releases.
JSON_LOGIN_SCHEMA = 1

#: Version stamped as the first key of the ``search --json`` result object, so a
#: scripting consumer can detect the payload shape and adapt across releases.
#: Separate from the backup schemas because ``search`` reports matches and the
#: echoed query rather than a download run.
JSON_SEARCH_SCHEMA = 1


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


def _resolve_reporter_mode(
    *,
    quiet: bool,
    verbose: int,
    dry_run: bool,
    is_tty: bool,
) -> tuple[ReporterMode, bool]:
    """Resolve the progress ``(mode, show_planned)`` for a backup run.

    Kept in the click layer (not in ``progress_reporter``) because it maps
    user-facing flags and terminal state onto the environment-agnostic
    reporter. The branch order encodes the precedence: an explicit ``--quiet``
    or ``--verbose`` always wins over auto-detection; ``--dry-run`` at ``-v0``
    stays verbose (preserving today's preview behaviour) and is exempt from the
    bar; only an interactive stderr gets the in-place bar; everything else (a
    redirected/piped run) falls back to periodic milestone lines.

    ``show_planned`` (whether ``WORK_PLANNED`` "would create" lines print in
    verbose mode) is only meaningful for the ``VERBOSE`` branches; the other
    modes ignore it, so ``False`` is returned as a harmless default.
    """

    if quiet:
        return ReporterMode.DISABLED, False
    if verbose >= 1:
        return ReporterMode.VERBOSE, dry_run or verbose >= 2
    if dry_run:
        return ReporterMode.VERBOSE, True
    if is_tty:
        return ReporterMode.BAR, False
    return ReporterMode.MILESTONE, False


def _stream_is_ascii_only(stream: TextIO) -> bool:
    """Report whether ``stream`` cannot render the Unicode spinner glyphs.

    The reporter never sniffs the terminal itself, so the caller decides which
    spinner frames are safe. A stream with no ``encoding`` (or one whose
    encoding cannot represent the Braille spinner char) forces the ASCII frames;
    encoding the probe glyph is the definitive test rather than guessing from
    the codec name.
    """

    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        "⠋".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def _build_reporter(
    *,
    quiet: bool,
    verbose: int,
    dry_run: bool,
    total: int | None,
    unit: ProgressUnit,
) -> ProgressReporter:
    """Build the :class:`ProgressReporter` shared by ``backup`` and
    ``backup_following``.

    Resolves terminal state at command-invocation time, never at import: under
    ``CliRunner`` ``sys.stderr`` is swapped per invocation, so a call-time
    lookup is what sees the active stream. ``is_tty`` selects ``BAR`` vs
    ``MILESTONE``; ``ascii_only`` selects the spinner alphabet. ``total`` and
    ``unit`` describe the bar's denominator and are left to the caller, since
    each command has its own rule for what counts as determinate.
    """

    stream = sys.stderr
    mode, show_planned = _resolve_reporter_mode(
        quiet=quiet, verbose=verbose, dry_run=dry_run, is_tty=stream.isatty()
    )
    return ProgressReporter(
        stream=stream,
        mode=mode,
        show_planned=show_planned,
        total=total,
        unit=unit,
        ascii_only=_stream_is_ascii_only(stream),
    )


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


def _validate_user_ids(
    ctx: click.Context,
    param: click.Parameter,
    value: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate every ``--only``/``--exclude`` value is a Pixiv user ID.

    Pixiv user IDs are always non-negative integers, and the follow-list filter
    compares them by ``str(user.id)`` (a plain decimal string), so a non-numeric
    value like ``abc`` can never match any followed user. Rejecting it at parse
    time (both options are ``multiple=True``, so ``value`` is the full tuple)
    turns a silently-ineffective filter into a teaching
    :class:`click.BadParameter` (exit code 2) instead of a confusing empty run.
    """

    for raw in value:
        if not (raw.isascii() and raw.isdigit()):
            raise click.BadParameter(
                f"{raw!r} is not a valid Pixiv user ID; user IDs are whole "
                "numbers (e.g. 12345).",
                ctx=ctx,
                param=param,
            )
    return value


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
    help="Suppress all stderr progress; the final summary line on stdout is "
    "still printed.",
)
@click.option(
    "--verbose",
    "-v",
    count=True,
    help="Show per-work lines on stderr instead of the default live summary; "
    "repeat (-vv) for more.",
)
@click.option(
    "--max-retries",
    type=click.IntRange(0, 10),
    default=3,
    show_default=True,
    help="Number of retries per image after the first attempt fails.",
)
@click.option(
    "--time-per-request",
    type=click.FloatRange(min=0),
    default=0.0,
    show_default=True,
    metavar="SECONDS",
    help=(
        "Client-side rate limit: minimum seconds from one work to the next, "
        "measured start-to-start (a work is one post and its images, not a "
        "single HTTP request). A work that already took longer is not "
        "delayed. Default 0 disables pacing."
    ),
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
    time_per_request: float,
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
        time_per_request=time_per_request,
    )

    if not quiet:
        target = "your own" if user_id is None else f"user {user_id}"
        verb = "Would back up" if dry_run else "Backing up"
        click.echo(
            f"{verb} {visibility} bookmarks for {target} into {output_dir}",
            err=True,
        )

    # --limit is the only determinate denominator for this command, so it is
    # the bar's total (works).
    reporter = _build_reporter(
        quiet=quiet, verbose=verbose, dry_run=dry_run, total=limit, unit="works"
    )

    client = Pixivpy3Client()

    # Anchor the bar's elapsed-time baseline at the actual run start (rather than
    # at reporter construction). No-op in every mode but BAR, where it only
    # records the start timestamp.
    reporter.handle(ProgressEvent(kind=ProgressKind.RUN_START))

    def _warn(msg: str) -> None:
        # A warning emitted from inside run_backup may land while the live bar is
        # still on screen; clear the dangling bar line first (finish() is
        # idempotent, so the inner finally calling it again is harmless) so the
        # warning prints on its own clean line instead of after the spinner.
        reporter.finish()
        click.echo(f"Warning: {msg}", err=True)

    try:
        try:
            summary = run_backup(
                run_config, auth, client, progress=reporter.handle, warn=_warn
            )
        finally:
            # finish() lives in this INNER finally so it runs before any outer
            # except body: on KeyboardInterrupt the dangling bar line is erased
            # before "interrupted..." prints on its own clean line, and on the
            # deferred click-exception paths before click prints the error.
            # finish() is idempotent, so the normal-return path calling it here
            # (and nowhere else) is safe.
            reporter.finish()
    except BackupUsageError as exc:
        raise click.UsageError(str(exc)) from exc
    except ManifestLoadError as exc:
        # A corrupt/hand-edited existing details.json on resume: surface the
        # teaching message and exit cleanly (2), mirroring the search command --
        # never a raw traceback, and no partial summary reaches stdout.
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


@cli.command(name="backup-following")
@click.option(
    "--output-dir",
    "-o",
    default="./backup-following",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the backup into (one subdirectory per author).",
)
@click.option(
    "--follow-visibility",
    type=click.Choice(VISIBILITIES),
    default="public",
    show_default=True,
    help="Which of your follows to back up, by the privacy of the follow itself: "
    "your public (visible) follows, your private (secret) follows, or all of them.",
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
    help="Stop after backing up this many works in total across all users.",
)
@click.option(
    "--per-user-limit",
    type=click.IntRange(min=1),
    default=None,
    help="Back up at most this many works per followed user (newest first).",
)
@click.option(
    "--max-users",
    type=click.IntRange(min=1),
    default=None,
    help="Stop after processing this many followed users (a quick sample).",
)
@click.option(
    "--only",
    multiple=True,
    metavar="USER_ID",
    callback=_validate_user_ids,
    help="Only back up these followed user IDs (repeatable).",
)
@click.option(
    "--exclude",
    multiple=True,
    metavar="USER_ID",
    callback=_validate_user_ids,
    help="Skip these followed user IDs (repeatable).",
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
    help="Suppress all stderr progress; the final summary line on stdout is "
    "still printed.",
)
@click.option(
    "--verbose",
    "-v",
    count=True,
    help="Show per-work lines on stderr instead of the default live summary; "
    "repeat (-vv) for more.",
)
@click.option(
    "--max-retries",
    type=click.IntRange(0, 10),
    default=3,
    show_default=True,
    help="Number of retries per image after the first attempt fails.",
)
@click.option(
    "--time-per-request",
    type=click.FloatRange(min=0),
    default=0.0,
    show_default=True,
    metavar="SECONDS",
    help=(
        "Client-side rate limit: minimum seconds from one work to the next, "
        "measured start-to-start (a work is one post and its images, not a "
        "single HTTP request). A work that already took longer is not "
        "delayed. Default 0 disables pacing."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be backed up without downloading or writing anything.",
)
@click.pass_context
def backup_following(
    ctx: click.Context,
    output_dir: Path,
    follow_visibility: str,
    refresh_token: str | None,
    config: Path | None,
    limit: int | None,
    per_user_limit: int | None,
    max_users: int | None,
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    since: datetime.date | None,
    skip_existing: bool,
    metadata_only: bool,
    json_output: bool,
    quiet: bool,
    verbose: int,
    max_retries: int,
    time_per_request: float,
    dry_run: bool,
) -> None:
    """Back up works from the Pixiv users you follow.

    Lists every creator your account follows and backs up each of their works
    (illustrations, manga, and ugoira) into a per-author subdirectory of the
    output directory (default ./backup-following), alongside a single
    details.json manifest at the output root recording every work and per-image
    status. Re-runs are safe: existing files are skipped and the manifest is
    merged.

    The refresh token is read from --refresh-token, the PIXIV_REFRESH_TOKEN
    environment variable, or the config file, in that order. See
    https://github.com/upbit/pixivpy for how to obtain a refresh token.

    \b
    Exit codes:
      0 success    2 usage/auth-input error   3 token rejected
      4 network    5 no follows/works found   6 partial (some images failed)
      130 interrupted

    \b
    Examples:
      # Preview what would be backed up from your public follows
      pixiv-bookmark-backup backup-following --dry-run

    \b
      # Back up everyone you follow (public + private), 20 newest works each
      pixiv-bookmark-backup backup-following --follow-visibility all --per-user-limit 20

    \b
      # Back up just two creators you follow, into ./out
      pixiv-bookmark-backup backup-following -o ./out --only 123 --only 456
    """

    if quiet and verbose:
        raise click.UsageError("--quiet and --verbose are mutually exclusive")
    if only and exclude:
        raise click.UsageError("--only and --exclude are mutually exclusive")

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

    run_config = FollowingConfig(
        output_dir=output_dir,
        follow_visibility=follow_visibility,
        dry_run=dry_run,
        limit=limit,
        per_user_limit=per_user_limit,
        max_users=max_users,
        only=only,
        exclude=exclude,
        since=since,
        skip_existing=skip_existing,
        metadata_only=metadata_only,
        json_output=json_output,
        verbosity=verbose,
        quiet=quiet,
        max_retries=max_retries,
        time_per_request=time_per_request,
    )

    if not quiet:
        verb = "Would back up" if dry_run else "Backing up"
        click.echo(
            f"{verb} works from your {follow_visibility} follows into {output_dir}",
            err=True,
        )

    # Pick the determinate bar denominator: an explicit total --limit counts
    # works; otherwise --max-users bounds the run in users; with neither the bar
    # is indeterminate (spinner + running counts).
    total: int | None
    unit: ProgressUnit
    if limit is not None:
        total = limit
        unit = "works"
    elif max_users is not None:
        total = max_users
        unit = "users"
    else:
        total = None
        unit = "works"

    reporter = _build_reporter(
        quiet=quiet, verbose=verbose, dry_run=dry_run, total=total, unit=unit
    )

    client = Pixivpy3Client()

    # Anchor the bar's elapsed-time baseline at the actual run start (rather than
    # at reporter construction). No-op in every mode but BAR, where it only
    # records the start timestamp.
    reporter.handle(ProgressEvent(kind=ProgressKind.RUN_START))

    def _warn(msg: str) -> None:
        # The warning is emitted from inside run_following, which is still inside
        # the live bar. Clear the dangling bar line first (finish() is
        # idempotent, so the inner finally calling it again is harmless) so the
        # warning prints on its own clean line instead of being appended onto
        # the spinner.
        reporter.finish()
        click.echo(f"Warning: {msg}", err=True)

    try:
        try:
            summary = run_following(
                run_config,
                auth,
                client,
                progress=reporter.handle,
                warn=_warn,
            )
        finally:
            # finish() lives in this INNER finally so it runs before any outer
            # except body: on KeyboardInterrupt the dangling bar line is erased
            # before "interrupted..." prints on its own clean line, and on the
            # deferred click-exception paths before click prints the error.
            # finish() is idempotent, so the normal-return path calling it here
            # (and nowhere else) is safe.
            reporter.finish()
    except ManifestLoadError as exc:
        # A corrupt/hand-edited existing details.json on resume: surface the
        # teaching message and exit cleanly (2), mirroring the search command --
        # never a raw traceback, and no partial summary reaches stdout.
        raise click.UsageError(str(exc)) from exc
    except AuthRejected as exc:
        raise _exit_error(str(exc), int(ExitCode.AUTH)) from exc
    except ApiError as exc:
        raise _exit_error(f"Pixiv API error: {exc}", int(ExitCode.NETWORK)) from exc
    except KeyboardInterrupt:
        click.echo("interrupted; partial backup saved", err=True)
        ctx.exit(int(ExitCode.INTERRUPTED))

    code = decide_exit_code(summary)
    _emit_following_summary(run_config, summary, code)
    ctx.exit(int(code))


def _emit_following_summary(
    config: FollowingConfig,
    summary: FollowingRunSummary,
    code: ExitCode,
) -> None:
    """Write the follow-backup summary to **stdout** (JSON or one human line)."""

    details_json = None if config.dry_run else str(config.output_dir / MANIFEST_NAME)

    if config.json_output:
        payload = {
            "schema": JSON_FOLLOWING_SUMMARY_SCHEMA,
            "mode": "following",
            "following_of": summary.following_of,
            "follow_visibility": config.follow_visibility,
            "output_dir": str(config.output_dir),
            "users_total": summary.users_total,
            "users_with_works": summary.users_with_works,
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
        f"({summary.works_failed} failed) from "
        f"{summary.users_with_works}/{summary.users_total} users, "
        f"{summary.files_downloaded} files to {config.output_dir}"
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


@cli.command()
@click.argument("keyword", nargs=-1)
@click.option(
    "--input",
    "-i",
    "input_path",
    type=click.Path(path_type=Path),
    default="./backup",
    show_default=True,
    help="details.json file or backup directory to search (default ./backup).",
)
@click.option(
    "--field",
    type=click.Choice(SEARCH_FIELDS),
    multiple=True,
    help="Tag surface(s) to match keywords against: work, bookmark, translated "
    "(default: all).",
)
@click.option(
    "--exact",
    is_flag=True,
    help="Match keywords as whole tags, not substrings.",
)
@click.option(
    "--match-all",
    is_flag=True,
    help="Require every keyword to match (AND) instead of any (OR).",
)
@click.option(
    "--author",
    multiple=True,
    metavar="TEXT",
    help="Keep only works by an author whose name/account contains TEXT "
    "(repeatable).",
)
@click.option(
    "--blacklist-tag",
    multiple=True,
    metavar="TAG",
    help="Exclude works carrying this tag on any surface (exact match, " "repeatable).",
)
@click.option(
    "--blacklist-author",
    multiple=True,
    metavar="TEXT",
    help="Exclude works by an author whose name/account contains TEXT " "(repeatable).",
)
@click.option(
    "--nsfw",
    type=click.Choice(NSFW_MODES),
    default="include",
    show_default=True,
    help="NSFW handling: include (default), exclude (SFW only), or only.",
)
@click.option(
    "--sort",
    type=click.Choice(SORT_KEYS),
    default="posted",
    show_default=True,
    help="Result order: posted (newest first), id, or title.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Print at most this many results (with --json, matches[] is truncated "
    "but match_count stays the full total).",
)
@click.option(
    "--symlink-dir",
    "symlink_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Populate DIR with symlinks to the matched works' image files so they "
    "can be browsed in a file browser (created if missing).",
)
@click.option(
    "--all-pages/--first-page-only",
    "all_pages",
    default=True,
    show_default=False,
    help="With --symlink-dir, link every existing page of each work "
    "(--all-pages, the default) or only the first existing page "
    "(--first-page-only).",
)
@click.option(
    "--layout",
    type=click.Choice(["flat", "by-author", "titled"]),
    default="flat",
    show_default=True,
    help="With --symlink-dir, how links are named/placed: flat (bare "
    "<id>_p<n>.<ext>), by-author (per-author subdir <account>_<id>/), or "
    "titled (<title>_<id>_p<n>.<ext>). Non-flat layouts sanitize Pixiv "
    "metadata into filesystem-safe names.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="With --symlink-dir, report what would be linked without creating "
    "anything.",
)
@click.option(
    "--force",
    is_flag=True,
    help="With --symlink-dir, re-point an existing symlink to the new target "
    "(never overwrites a regular file).",
)
@click.option(
    "--count",
    is_flag=True,
    help="Print only the number of matches.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print a machine-readable result object to stdout.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress the match-count summary on stderr.",
)
@click.option(
    "--verbose",
    "-v",
    count=True,
    help="Increase detail on stderr; repeat for more.",
)
@click.pass_context
def search(
    ctx: click.Context,
    keyword: tuple[str, ...],
    input_path: Path,
    field: tuple[str, ...],
    exact: bool,
    match_all: bool,
    author: tuple[str, ...],
    blacklist_tag: tuple[str, ...],
    blacklist_author: tuple[str, ...],
    nsfw: str,
    sort: str,
    limit: int | None,
    symlink_dir: Path | None,
    all_pages: bool,
    layout: str,
    dry_run: bool,
    force: bool,
    count: bool,
    json_output: bool,
    quiet: bool,
    verbose: int,
) -> None:
    """Search a local details.json manifest offline -- no token or network required.

    Reads the works recorded by 'backup' / 'backup-following' and matches each
    KEYWORD against the work's tag surfaces (work tags, bookmark tags, and the
    translated tag names). With multiple keywords, any match keeps a work (OR);
    pass --match-all to require every keyword (AND). Narrow results with --field
    (which surfaces to match on), filter by --author or --nsfw, and drop works
    with --blacklist-tag / --blacklist-author. With no KEYWORD it lists every
    work that passes the filters (browse mode).

    Point at a backup with -i: an existing directory is treated as a backup root
    (its details.json is loaded), otherwise the path is used as the manifest file
    directly. Results go to stdout, one per line; the match-count summary goes to
    stderr. This command never authenticates or hits the network.

    \b
    Exit codes:
      0 success (including zero matches)   2 usage / manifest-input error
      10 symlink write error
         (symlinks already created before the error are kept; re-run to resume)

    \b
    Examples:
      # Any of these tags (OR), newest first, from the default ./backup
      pixiv-bookmark-backup search touhou original

    \b
      # Symlink matches into ./review (all pages of each work, by default)
      pixiv-bookmark-backup search touhou --symlink-dir ./review

    \b
      # Link only the first existing page of each work
      pixiv-bookmark-backup search touhou --symlink-dir ./review --first-page-only

    \b
      # Group symlinks into per-author subdirectories
      pixiv-bookmark-backup search touhou --symlink-dir ./review --layout by-author

    \b
      # Prefix each link with the work title (metadata is sanitized)
      pixiv-bookmark-backup search touhou --symlink-dir ./review --layout titled

    \b
      # Require both tags (AND) and match them as whole tags
      pixiv-bookmark-backup search touhou original --match-all --exact

    \b
      # Only your bookmark tag "fav", exact match, from a specific manifest
      pixiv-bookmark-backup search fav --field bookmark --exact -i ./out

    \b
      # Browse only NSFW works ordered by id, and just count the JSON hits
      pixiv-bookmark-backup search --nsfw only --sort id --count
      pixiv-bookmark-backup search touhou --json
    """

    if quiet and verbose:
        raise click.UsageError("--quiet and --verbose are mutually exclusive")
    if count and json_output:
        raise click.UsageError("--count and --json are mutually exclusive")
    if (
        ctx.get_parameter_source("all_pages") is click.core.ParameterSource.COMMANDLINE
        and symlink_dir is None
    ):
        raise click.UsageError("--all-pages/--first-page-only requires --symlink-dir")
    if dry_run and symlink_dir is None:
        raise click.UsageError("--dry-run requires --symlink-dir")
    if force and symlink_dir is None:
        raise click.UsageError("--force requires --symlink-dir")
    if (
        ctx.get_parameter_source("layout") is click.core.ParameterSource.COMMANDLINE
        and symlink_dir is None
    ):
        raise click.UsageError("--layout requires --symlink-dir")

    effective_fields = normalize_fields(field)
    manifest_path = resolve_manifest_path(input_path)
    try:
        manifest = load_manifest_strict(manifest_path)
    except ManifestLoadError as exc:
        raise click.UsageError(str(exc)) from exc

    if verbose:
        click.echo(f"Reading {manifest_path} ({len(manifest.works)} works)", err=True)

    config = SearchConfig(
        input_path=manifest_path,
        keywords=keyword,
        fields=effective_fields,
        exact=exact,
        match_all=match_all,
        authors=author,
        blacklist_tags=blacklist_tag,
        blacklist_authors=blacklist_author,
        nsfw=nsfw,
        sort=sort,
        limit=limit,
        count=count,
        json_output=json_output,
        verbosity=verbose,
        quiet=quiet,
    )

    matches, scanned = run_search(manifest.works, config)
    match_count = len(matches)
    emitted = matches[:limit] if limit else matches

    if not quiet:
        click.echo(f"Matched {match_count} / {scanned} works", err=True)

    # Create the symlinks (or preview them under --dry-run) before emitting any
    # results, so a fatal SymlinkError (exit 10) aborts before printing a
    # partial result set, and so the --json symlink sub-object can be folded
    # into the single stdout object.
    link_result: SymlinkResult | None = None
    if symlink_dir is not None:
        symlink_config = SymlinkConfig(
            dir=symlink_dir,
            all_pages=all_pages,
            dry_run=dry_run,
            force=force,
            layout=layout,
        )
        try:
            link_result = plan_and_link(emitted, manifest_path, symlink_config)
        except SymlinkError as exc:
            raise _exit_error(str(exc), int(ExitCode.SYMLINK)) from exc

    if count:
        click.echo(str(match_count))
    elif json_output:
        _emit_search_result(
            config, manifest_path, emitted, scanned, match_count, symlink=link_result
        )
    else:
        for match in emitted:
            click.echo(format_work_line(match))

    if not quiet and link_result is not None:
        _emit_link_summary(link_result, verbose)

    ctx.exit(int(ExitCode.SUCCESS))


def _plural(count: int, noun: str) -> str:
    """Return ``"1 file"`` / ``"3 files"`` -- ``noun`` pluralized by adding ``s``.

    A tiny helper so the link summary reads naturally for singular counts; the
    project's nouns ("file", "work") pluralize with a plain trailing ``s``.
    """

    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _emit_link_summary(link_result: SymlinkResult, verbose: int) -> None:
    """Write the symlink run summary to **stderr** (after the match summary).

    A single human-readable line reporting how many files were linked -- broken
    down into new / reused (and replaced under ``--force``) so a re-run's reuses
    are visible -- for how many works and where. A non-``"flat"`` ``layout`` is
    reported as a ``(layout: ...)`` suffix right after the directory (flat output
    stays byte-identical to before the feature). Works skipped because they had
    no local file are appended in parentheses. Under ``-v`` each skipped work id
    is echoed; under ``-vv`` each ``link_name -> target`` pair is echoed as well,
    mirroring the command's existing verbose diagnostics.
    """

    verb = "Would link" if link_result.dry_run else "Linked"
    breakdown = f"{link_result.links_created} new, {link_result.links_reused} reused"
    if link_result.links_replaced:
        breakdown += f", {link_result.links_replaced} replaced"
    summary = (
        f"{verb} {_plural(link_result.links_total, 'file')} ({breakdown}) for "
        f"{_plural(link_result.works_linked, 'work')} into {link_result.dir}"
    )
    # Report a non-flat layout right after the directory; flat stays byte-
    # identical to the pre-layout summary (no suffix), so existing consumers
    # and tests see no change.
    if link_result.layout != "flat":
        summary += f" (layout: {link_result.layout})"
    if link_result.works_without_files:
        summary += (
            f" ({_plural(link_result.works_without_files, 'work')} "
            "skipped: no local file)"
        )
    click.echo(summary, err=True)

    if verbose >= 1:
        for work_id in link_result.skipped_work_ids:
            click.echo(f"  skipped {work_id}: no local file", err=True)
    if verbose >= 2:
        for link_name, target in link_result.linked:
            click.echo(f"  {link_name} -> {target}", err=True)


def _emit_search_result(
    config: SearchConfig,
    manifest_path: Path,
    emitted: list[SearchMatch],
    scanned: int,
    match_count: int,
    symlink: SymlinkResult | None = None,
) -> None:
    """Write the ``search --json`` result object to **stdout** (one JSON line).

    ``match_count`` is the true pre-limit total (also driving ``--count`` and the
    stderr summary), while ``matches`` reflects only the ``--limit``-truncated
    ``emitted`` slice actually shown to the user.

    When ``symlink`` is given (a ``--symlink-dir`` run), a top-level ``"symlink"``
    key carrying the run's directory, flags (including the ``"layout"``), and
    per-category link tallies is added to the payload; its ``"linked"`` entries
    are link-dir-relative POSIX names (nested ``subdir/basename`` under the
    ``"by-author"`` layout, bare basenames otherwise). It is absent entirely
    otherwise, so ``JSON_SEARCH_SCHEMA`` stays ``1`` (the default output is
    byte-identical to before this feature; the sub-object appears only with
    ``--symlink-dir``).
    """

    payload = {
        "schema": JSON_SEARCH_SCHEMA,
        "input": str(manifest_path),
        "query": {
            "keywords": list(config.keywords),
            "fields": list(config.fields),
            "exact": config.exact,
            "match_all": config.match_all,
            "authors": list(config.authors),
            "blacklist_tags": list(config.blacklist_tags),
            "blacklist_authors": list(config.blacklist_authors),
            "nsfw": config.nsfw,
            "sort": config.sort,
            "limit": config.limit,
        },
        "works_scanned": scanned,
        "match_count": match_count,
        "matches": [
            {
                "id": match.work.id,
                "type": match.work.type,
                "title": match.work.title,
                "author": dict(match.work.author),
                "posted_at": match.work.posted_at,
                "is_nsfw": bool(match.work.nsfw.get("is_nsfw")),
                "work_tags": [dict(tag) for tag in match.work.work_tags],
                "bookmark_tags": (
                    None
                    if match.work.bookmark_tags is None
                    else list(match.work.bookmark_tags)
                ),
                "matched_fields": match.matched_fields,
            }
            for match in emitted
        ],
        "exit_code": int(ExitCode.SUCCESS),
    }
    if symlink is not None:
        payload["symlink"] = {
            "dir": str(symlink.dir),
            "all_pages": symlink.all_pages,
            "dry_run": symlink.dry_run,
            "force": symlink.force,
            "layout": symlink.layout,
            "links_created": symlink.links_created,
            "links_reused": symlink.links_reused,
            "links_replaced": symlink.links_replaced,
            "links_total": symlink.links_total,
            "works_linked": symlink.works_linked,
            "works_without_files": symlink.works_without_files,
            "skipped_work_ids": list(symlink.skipped_work_ids),
            "linked": [list(pair) for pair in symlink.linked],
        }
    click.echo(json.dumps(payload))
