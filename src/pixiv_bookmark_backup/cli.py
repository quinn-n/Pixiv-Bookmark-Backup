"""Command-line interface for pixiv-bookmark-backup.

The click layer is intentionally thin: it parses and validates input, builds a
:class:`~pixiv_bookmark_backup.core.BackupConfig`, delegates to the core, and
formats output. Data goes to stdout; errors are raised as click exceptions so
they land on stderr with a non-zero exit code.
"""

from __future__ import annotations

from pathlib import Path

import click

from . import __version__
from .core import VISIBILITIES, BackupConfig, describe_plan


@click.group()
@click.version_option(__version__, prog_name="pixiv-bookmark-backup")
def cli() -> None:
    """Back up your Pixiv bookmarks."""


@cli.command()
@click.argument("user_id")
@click.option(
    "--output-dir",
    "-o",
    default="./backup",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the backup into.",
)
@click.option(
    "--visibility",
    type=click.Choice(VISIBILITIES),
    default="public",
    show_default=True,
    help="Which bookmarks to back up.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would happen without writing anything.",
)
def backup(
    user_id: str,
    output_dir: Path,
    visibility: str,
    dry_run: bool,
) -> None:
    """Back up the bookmarks for USER_ID.

    \b
    Examples:
      pixiv-bookmark-backup backup 12345 --dry-run
      pixiv-bookmark-backup backup 12345 -o ./out --visibility all
    """

    config = BackupConfig(
        user_id=user_id,
        output_dir=output_dir,
        visibility=visibility,
        dry_run=dry_run,
    )

    click.echo(describe_plan(config))

    if config.dry_run:
        return

    # Scaffold: the real Pixiv fetch/download is not wired up yet.
    raise click.ClickException(
        "backup is not implemented yet — this is a freshly scaffolded project. "
        "Use --dry-run to preview the planned action."
    )
