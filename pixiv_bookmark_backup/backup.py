"""The backup orchestrator that ties the whole feature together.

This module owns the *control flow* of a single backup run: authenticate,
resolve the target user, iterate bookmarks across the requested visibility
passes, download (or, in dry-run / metadata-only mode, account for) each work's
images, accumulate tallies, and flush an aggregate ``details.json`` manifest.

It deliberately knows nothing about ``click``: progress is reported through an
injected ``progress`` callable and the timestamp through an injected ``clock``,
so the whole orchestrator is fully testable offline and the thin CLI layer keeps
all terminal concerns. Per-image/per-work failures are *recorded* (the
downloader and ugoira builder never raise for expected failures) rather than
aborting the run; only the unrecoverable conditions below escape:

* :class:`~pixiv_bookmark_backup.pixiv_client.AuthRejected` from authentication
  propagates (the CLI maps it to exit 3).
* :class:`~pixiv_bookmark_backup.pixiv_client.ApiError` raised while listing
  bookmarks *before any work has been processed* propagates (exit 4); once some
  works are recorded it is caught, noted as a partial-run error, and the manifest
  is still flushed.
* :class:`BackupUsageError` is raised for invalid request combinations only
  knowable after authentication (the CLI maps it to exit 2).
* ``KeyboardInterrupt`` during the loop flushes a partial manifest and re-raises
  (the CLI maps it to exit 130).

The exit-code decision for a normally completed run lives in
:func:`~pixiv_bookmark_backup.core.decide_exit_code`, applied by the CLI to the
returned :class:`~pixiv_bookmark_backup.core.RunSummary`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pixiv_bookmark_backup.config import ResolvedAuth
from pixiv_bookmark_backup.core import BackupConfig, RunSummary
from pixiv_bookmark_backup.downloader import download_image
from pixiv_bookmark_backup.manifest import (
    ImageEntry,
    Manifest,
    build_work_entry,
    load_or_init,
    make_image_entry,
    merge,
    new_manifest,
    write_atomic,
)
from pixiv_bookmark_backup.models import BookmarkWork, ImageStatus
from pixiv_bookmark_backup.naming import (
    ext_from_url,
    image_filename,
    relative_to_output,
    ugoira_filename,
)
from pixiv_bookmark_backup.pixiv_client import ApiError, PixivClient
from pixiv_bookmark_backup.ugoira import build_ugoira

#: Filename of the aggregate manifest written at the output root.
MANIFEST_NAME = "details.json"

#: Filename the manifest is moved aside to on corruption / schema mismatch.
MANIFEST_BACKUP_NAME = "details.json.bak"


class BackupUsageError(Exception):
    """An invalid backup request that is only knowable after authentication.

    The chief case is asking for private bookmarks of *another* user, which the
    Pixiv API does not allow. Raised from :func:`run`; the CLI maps it to exit
    code 2.
    """


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat()


def _restrict_passes(visibility: str) -> list[str]:
    """Return the Pixiv ``restrict`` passes for a requested visibility.

    The Pixiv API has no ``"all"`` restrict, so it is expanded into the two
    concrete passes; ``"public"`` / ``"private"`` map to a single pass.
    """

    if visibility == "all":
        return ["public", "private"]
    return [visibility]


def _passes_since(work: BookmarkWork, since: date | None) -> bool:
    """Return whether ``work`` should be kept under the ``--since`` filter.

    A work is kept when its posting date is on or after ``since``. ``since`` is a
    :class:`datetime.date` or ``None`` (no filter). Parsing ``work.posted_at`` is
    best-effort: if the timestamp cannot be parsed the work is *included* rather
    than crashing the run, so a malformed date never silently drops a bookmark.
    """

    if since is None:
        return True
    try:
        posted = datetime.fromisoformat(work.posted_at).date()
    except ValueError:
        # Best-effort filter: an unparseable timestamp keeps the work.
        return True
    return posted >= since


def _planned_filenames(work: BookmarkWork) -> list[str]:
    """Return the flat-layout filenames a non-dry run *would* create for ``work``.

    One ``{id}_p0.gif`` for an ugoira; otherwise one image filename per page.
    """

    if work.type == "ugoira":
        return [ugoira_filename(work.id)]
    return [
        image_filename(work.id, img.page, ext_from_url(img.source_url))
        for img in work.images
    ]


def _tally_status(
    status: ImageStatus | str,
    bytes_: int | None,
    counters: _Counters,
) -> bool:
    """Fold one image/gif outcome into ``counters``; return whether it failed.

    Increments exactly one of the per-file counters by status and adds the
    downloaded byte count. Returns ``True`` when the outcome is
    :attr:`~pixiv_bookmark_backup.models.ImageStatus.FAILED` (so the caller can
    mark the enclosing work failed), ``False`` otherwise.
    """

    value = status.value if isinstance(status, ImageStatus) else status
    if value == ImageStatus.DOWNLOADED.value:
        counters.files_downloaded += 1
        counters.bytes_downloaded += bytes_ or 0
        return False
    if value == ImageStatus.SKIPPED.value:
        counters.files_skipped += 1
        counters.bytes_downloaded += bytes_ or 0
        return False
    if value == ImageStatus.FAILED.value:
        counters.files_failed += 1
        return True
    # METADATA_ONLY (or any other recorded-but-not-downloaded status) counts as
    # neither a download nor a failure.
    return False


class _Counters:
    """Mutable accumulator for a run's tallies.

    A small behaviour-light scratch object: counters are folded in during the
    loop and the immutable :class:`~pixiv_bookmark_backup.core.RunSummary` is
    constructed from it once at the end. (A frozen ``attrs`` summary is the
    public value object; this is just the running total.)
    """

    def __init__(self) -> None:
        self.works_total = 0
        self.works_succeeded = 0
        self.works_failed = 0
        self.files_downloaded = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.bytes_downloaded = 0

    def to_summary(self) -> RunSummary:
        """Freeze the running totals into a :class:`RunSummary`."""

        return RunSummary(
            works_total=self.works_total,
            works_succeeded=self.works_succeeded,
            works_failed=self.works_failed,
            files_downloaded=self.files_downloaded,
            files_skipped=self.files_skipped,
            files_failed=self.files_failed,
            bytes_downloaded=self.bytes_downloaded,
        )


def _image_entries_for_work(
    config: BackupConfig,
    client: PixivClient,
    work: BookmarkWork,
    counters: _Counters,
) -> list[ImageEntry]:
    """Download (or account for) ``work``'s images and return their entries.

    For an ugoira a single GIF entry is produced; otherwise one entry per page.
    In ``metadata_only`` mode no download is attempted and every entry is
    recorded as :attr:`~pixiv_bookmark_backup.models.ImageStatus.METADATA_ONLY`.
    Each outcome is folded into ``counters`` (and the work is marked failed by
    the caller if any entry failed).
    """

    output_dir = config.output_dir
    if work.type == "ugoira":
        dest = output_dir / ugoira_filename(work.id)
        source_url = work.images[0].source_url if work.images else ""
        if config.metadata_only:
            status: ImageStatus | str = ImageStatus.METADATA_ONLY
            entry_bytes: int | None = None
            error: str | None = None
        else:
            res = build_ugoira(client, work, dest, skip_existing=config.skip_existing)
            status, entry_bytes, error = res.status, res.bytes_, res.error
        _tally_status(status, entry_bytes, counters)
        return [
            make_image_entry(
                0,
                source_url,
                relative_to_output(dest, output_dir),
                status,
                entry_bytes,
                error,
            )
        ]

    entries: list[ImageEntry] = []
    for img in work.images:
        ext = ext_from_url(img.source_url)
        dest = output_dir / image_filename(work.id, img.page, ext)
        if config.metadata_only:
            status = ImageStatus.METADATA_ONLY
            entry_bytes = None
            error = None
        else:
            res = download_image(
                client,
                img.source_url,
                dest,
                max_retries=config.max_retries,
                skip_existing=config.skip_existing,
            )
            status, entry_bytes, error = res.status, res.bytes_, res.error
        _tally_status(status, entry_bytes, counters)
        entries.append(
            make_image_entry(
                img.page,
                img.source_url,
                relative_to_output(dest, output_dir),
                status,
                entry_bytes,
                error,
            )
        )
    return entries


def _flush_manifest(
    config: BackupConfig,
    target: str,
    works: list[Any],
    errors: list[dict[str, Any]],
    clock: Callable[[], str],
) -> None:
    """Merge ``works``/``errors`` into ``details.json`` and write it atomically.

    Loads any existing manifest, guarding against a corrupt or schema-mismatched
    file: on either condition the existing file is moved aside to
    ``details.json.bak`` and the run starts from a fresh empty manifest. The
    output directory is created if needed. No-op callers (dry-run) never reach
    here.
    """

    details_path = config.output_dir / MANIFEST_NAME
    config.output_dir.mkdir(parents=True, exist_ok=True)

    existing, schema_mismatch = _load_existing_manifest(details_path)
    if schema_mismatch and details_path.exists():
        os.replace(details_path, details_path.with_name(MANIFEST_BACKUP_NAME))

    new = new_manifest(target, config.visibility, clock(), works, errors)
    final = merge(existing, new)
    write_atomic(details_path, final)


def _load_existing_manifest(details_path: Path) -> tuple[Manifest, bool]:
    """Load ``details.json`` defensively, treating corruption as a mismatch.

    Returns ``(existing, schema_mismatch)``. A corrupt file (unparseable JSON or
    an OS read error) is reported as a mismatch with a fresh empty manifest, so
    the caller backs the bad file up and regenerates rather than crashing.
    """

    try:
        return load_or_init(details_path)
    except (json.JSONDecodeError, OSError):
        # A corrupt or unreadable file is handled like a schema mismatch: start
        # fresh and let the caller move the bad file aside.
        return new_manifest(None, "", "", [], []), True


def run(
    config: BackupConfig,
    auth: ResolvedAuth,
    client: PixivClient,
    *,
    progress: Callable[[str], None] = lambda _msg: None,
    clock: Callable[[], str] = _utcnow,
) -> RunSummary:
    """Run a single bookmark backup and return its :class:`RunSummary`.

    Authenticates, resolves the target user, validates the visibility request,
    then iterates bookmarks across the requested visibility passes -- applying
    the ``--limit`` and ``--since`` filters -- downloading (or, for ``dry_run`` /
    ``metadata_only``, accounting for) each work's images and accumulating
    tallies. Unless ``dry_run`` is set, an aggregate ``details.json`` manifest is
    merged and written at the output root.

    Args:
        config: The validated run parameters.
        auth: Resolved credentials (only the refresh token is read here).
        client: The Pixiv surface (authenticate, list, download, ugoira).
        progress: Sink for human-readable progress lines; defaults to a no-op so
            the orchestrator stays silent unless the CLI wires up output.
        clock: Source of the manifest's ``generated_at`` timestamp; injectable
            for deterministic tests.

    Returns:
        The accumulated :class:`RunSummary` for a run that reached completion
        (including a partial run after a mid-listing network error).

    Raises:
        AuthRejected: If Pixiv rejects the refresh token (propagated; CLI -> 3).
        ApiError: If listing fails before any work is processed (CLI -> 4).
        BackupUsageError: For an invalid request (e.g. another user's private
            bookmarks) discovered after auth (CLI -> 2).
        KeyboardInterrupt: Re-raised after flushing a partial manifest
            (CLI -> 130).
    """

    self_id = client.authenticate(auth.refresh_token)
    target = config.user_id if config.user_id is not None else self_id

    if config.visibility in ("private", "all") and str(target) != str(self_id):
        raise BackupUsageError(
            "private bookmarks of another user are not accessible; use "
            "--visibility public or omit USER_ID to back up your own."
        )

    counters = _Counters()
    works: list[Any] = []
    errors: list[dict[str, Any]] = []

    try:
        _iterate_and_process(config, client, target, counters, works, errors, progress)
    except ApiError as err:
        if counters.works_total == 0:
            # Nothing recorded yet -> a clean network failure; let the CLI map
            # it to exit 4.
            raise
        # Some works are already recorded -> degrade to a partial run: note the
        # interruption, mark the outcome failed, and still flush the manifest.
        errors.append({"reason": f"listing interrupted: {err}"})
        counters.works_failed += 1
    except KeyboardInterrupt:
        # Flush whatever was gathered before re-raising (CLI -> exit 130).
        if not config.dry_run:
            _flush_manifest(config, str(target), works, errors, clock)
        raise

    if not config.dry_run:
        _flush_manifest(config, str(target), works, errors, clock)

    return counters.to_summary()


def _iterate_and_process(
    config: BackupConfig,
    client: PixivClient,
    target: str,
    counters: _Counters,
    works: list[Any],
    errors: list[dict[str, Any]],
    progress: Callable[[str], None],
) -> None:
    """Iterate bookmarks across the visibility passes and process each work.

    Maintains a running index across passes, applies ``--limit`` (stop once
    ``works_total`` reaches it) and ``--since``, and appends recorded works /
    errors plus per-file tallies into the shared accumulators. ``ApiError`` and
    ``KeyboardInterrupt`` propagate to :func:`run`, which owns recovery.
    """

    index = 0
    for restrict in _restrict_passes(config.visibility):
        for work in client.iter_bookmarks(target, restrict):
            if config.limit is not None and counters.works_total >= config.limit:
                return
            if not _passes_since(work, config.since):
                continue

            index += 1
            progress(
                f"[{index}] {work.id} {work.title!r} " f"— {work.page_count} page(s)"
            )

            if work.type != "ugoira" and not work.images:
                # No downloadable pages (deleted / restricted / limited work):
                # record a work-level error and skip it -- it is not counted as
                # a normal processed work. Keyed on "work_id" so manifest.merge
                # dedups it across resume runs (an "id" key would accumulate
                # duplicates). The listing-interrupt error below has no work id
                # and is intentionally always appended (it describes the run,
                # not a single work).
                errors.append(
                    {
                        "work_id": work.id,
                        "reason": "work unavailable (no downloadable pages)",
                    }
                )
                continue

            if config.dry_run:
                planned = _planned_filenames(work)
                counters.works_total += 1
                counters.works_succeeded += 1
                counters.files_downloaded += len(planned)
                progress(f"    would create: {', '.join(planned)}")
                continue

            entries = _image_entries_for_work(config, client, work, counters)
            counters.works_total += 1
            if any(entry.status == ImageStatus.FAILED.value for entry in entries):
                counters.works_failed += 1
            else:
                counters.works_succeeded += 1
            works.append(build_work_entry(work, entries))
