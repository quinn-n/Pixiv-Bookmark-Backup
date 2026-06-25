"""Core logic for pixiv-bookmark-backup.

Pure, testable functions and value objects live here; the click layer in
``cli.py`` stays thin and delegates to this module.
"""

from __future__ import annotations

from pathlib import Path

from attrs import field, frozen
from attrs.validators import in_, instance_of

#: Bookmark visibilities the tool understands.
VISIBILITIES = ("public", "private", "all")


@frozen
class BackupConfig:
    """Parameters controlling a single bookmark backup run.

    A frozen ``attrs`` value object: validated once at construction, then
    safe to pass around. ``output_dir`` is coerced to a :class:`~pathlib.Path`
    so callers may pass a plain string.
    """

    user_id: str = field(validator=instance_of(str))
    output_dir: Path = field(converter=Path)
    visibility: str = field(default="public", validator=in_(VISIBILITIES))
    dry_run: bool = field(default=False, validator=instance_of(bool))


def describe_plan(config: BackupConfig) -> str:
    """Return a human-readable, one-line summary of what a run would do.

    This is the kind of pure, easily tested helper that the CLI command
    formats and prints; it performs no I/O.
    """

    verb = "Would back up" if config.dry_run else "Backing up"
    return (
        f"{verb} {config.visibility} bookmarks for user {config.user_id} "
        f"into {config.output_dir}"
    )
