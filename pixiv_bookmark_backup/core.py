"""Core logic for pixiv-bookmark-backup.

Pure, testable functions and value objects live here; the click layer in
``cli.py`` stays thin and delegates to this module.
"""

from __future__ import annotations

from datetime import date
from enum import IntEnum
from pathlib import Path

from attrs import field, frozen
from attrs.validators import ge, in_, instance_of, le, optional

#: Bookmark visibilities the tool understands.
VISIBILITIES = ("public", "private", "all")


@frozen
class BackupConfig:
    """Parameters controlling a single bookmark backup run.

    A frozen ``attrs`` value object: validated once at construction, then
    safe to pass around. ``output_dir`` is coerced to a :class:`~pathlib.Path`
    so callers may pass a plain string. The refresh token is intentionally
    *not* stored here -- it is handled separately from run parameters.
    """

    output_dir: Path = field(converter=Path)
    user_id: str | None = field(default=None, validator=optional(instance_of(str)))
    visibility: str = field(default="public", validator=in_(VISIBILITIES))
    dry_run: bool = field(default=False, validator=instance_of(bool))
    limit: int | None = field(
        default=None, validator=optional([instance_of(int), ge(1)])
    )
    since: date | None = field(default=None, validator=optional(instance_of(date)))
    skip_existing: bool = field(default=True, validator=instance_of(bool))
    metadata_only: bool = field(default=False, validator=instance_of(bool))
    json_output: bool = field(default=False, validator=instance_of(bool))
    verbosity: int = field(default=0, validator=instance_of(int))
    quiet: bool = field(default=False, validator=instance_of(bool))
    max_retries: int = field(default=3, validator=[instance_of(int), ge(0), le(10)])


@frozen
class RunSummary:
    """Tallies collected over a backup run.

    A plain frozen value object: every counter defaults to ``0`` so a fresh
    summary represents "nothing happened yet". The orchestrator accumulates
    these counts and hands the final summary to :func:`decide_exit_code`.
    """

    works_total: int = 0
    works_succeeded: int = 0
    works_failed: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_downloaded: int = 0


class ExitCode(IntEnum):
    """Process exit codes for the CLI.

    The numeric values are part of the CLI contract -- scripts depend on them,
    so they must not change without an approved plan.

    Codes 7, 8, and 9 are specific to the interactive ``login`` command:

    * :attr:`LOGIN_DRIVER` (7) -- the interactive-login browser/driver is
      unavailable (e.g. gppt not installed, or the Playwright browser binary
      is missing).
    * :attr:`LOGIN_FAILED` (8) -- the login flow ran but did not yield a token
      (login failed, timed out, or credentials were rejected).
    * :attr:`CONFIG_WRITE` (9) -- a token was obtained but writing it to the
      config file failed.
    """

    SUCCESS = 0
    USAGE = 2
    AUTH = 3
    NETWORK = 4
    NO_BOOKMARKS = 5
    PARTIAL = 6
    LOGIN_DRIVER = 7
    LOGIN_FAILED = 8
    CONFIG_WRITE = 9
    INTERRUPTED = 130


def decide_exit_code(summary: RunSummary) -> ExitCode:
    """Map a completed run's :class:`RunSummary` to an :class:`ExitCode`.

    This is a pure, count-based decision for runs that reached completion:

    * no works at all -> :attr:`ExitCode.NO_BOOKMARKS`
    * works processed with no failures -> :attr:`ExitCode.SUCCESS`
    * works processed with any failure -> :attr:`ExitCode.PARTIAL`

    The :attr:`ExitCode.AUTH`, :attr:`ExitCode.NETWORK`, and
    :attr:`ExitCode.INTERRUPTED` outcomes are *not* decided here. They are
    determined by the orchestrator's control flow (caught exceptions and
    signal handling), because no completed summary exists in those cases.
    """

    if summary.works_total == 0:
        return ExitCode.NO_BOOKMARKS
    if summary.works_failed == 0 and summary.files_failed == 0:
        return ExitCode.SUCCESS
    return ExitCode.PARTIAL


def describe_plan(config: BackupConfig) -> str:
    """Return a human-readable, one-line summary of what a run would do.

    This is the kind of pure, easily tested helper that the CLI command
    formats and prints; it performs no I/O.
    """

    verb = "Would back up" if config.dry_run else "Backing up"
    target = "your own (self)" if config.user_id is None else f"user {config.user_id}"
    return (
        f"{verb} {config.visibility} bookmarks for {target} "
        f"into {config.output_dir}"
    )
