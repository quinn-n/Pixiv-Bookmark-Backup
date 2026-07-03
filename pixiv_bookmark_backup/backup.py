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
import time
import unicodedata
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pixiv_bookmark_backup.config import ResolvedAuth
from pixiv_bookmark_backup.core import (
    BackupConfig,
    FollowingConfig,
    FollowingRunSummary,
    ProgressEvent,
    ProgressKind,
    RequestPacer,
    RunSummary,
)
from pixiv_bookmark_backup.downloader import download_image
from pixiv_bookmark_backup.manifest import (
    MANIFEST_NAME,
    ImageEntry,
    Manifest,
    build_work_entry,
    load_or_init,
    make_image_entry,
    merge,
    new_following_manifest,
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


def _follow_passes(follows: str) -> list[str]:
    """Return the Pixiv follow-list ``restrict`` passes for ``follows``.

    Mirrors :func:`_restrict_passes`: ``"all"`` is expanded into the two
    concrete passes (your public follows then your private follows); ``"public"``
    / ``"private"`` map to a single pass.
    """

    if follows == "all":
        return ["public", "private"]
    return [follows]


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


_SANITIZED_CATEGORIES = frozenset({"Cc", "Cf"})


def _sanitize_display_text(s: str) -> str:
    """Strip control and format characters from untrusted display text.

    A Pixiv display name is attacker-controlled and may embed:

    * Unicode category ``Cc`` (C0/C1 control chars, ``\\x00-\\x1f``/``\\x7f-\\x9f``):
      ANSI/OSC/CSI terminal escape sequences that would render unescaped when
      a verbose/dry-run ``line`` is written straight to a real TTY.
    * Unicode category ``Cf`` (format chars): bidirectional-override/isolate
      controls (e.g. U+202E RIGHT-TO-LEFT OVERRIDE -- Trojan-Source-style
      deception) and zero-width characters (e.g. U+200B ZERO WIDTH SPACE,
      U+FEFF), which visually misrepresent the name without needing any
      control byte.

    Stripping both categories neutralises escape-sequence injection *and*
    visual spoofing before the text reaches the terminal. (``work.title`` is
    already protected by ``!r``/repr, which escapes non-printable characters
    -- including every ``Cc``/``Cf`` code point -- to ``\\xXX``/``\\uXXXX``
    sequences; ``user.name`` is interpolated raw, so it is sanitised here.)

    Stripping ``Cf`` is a known, accepted display-fidelity trade-off shared
    with mainstream Trojan-Source mitigations: it also removes the
    zero-width joiner/non-joiner used in legitimate multi-codepoint emoji
    (e.g. family emoji) and some Indic-script conjuncts, which may then
    render as separate glyphs. That is preferable to leaving a spoofing
    vector in place.
    """

    return "".join(c for c in s if unicodedata.category(c) not in _SANITIZED_CATEGORIES)


def _work_event(
    counters: _Counters,
    work: BookmarkWork,
    *,
    line: str,
    index: int,
    user_id: str | None = None,
    users_total: int = 0,
) -> ProgressEvent:
    """Build a :attr:`ProgressKind.WORK` event with ``counters``' live totals.

    ``line`` is the ready-made per-work text (printed verbatim in verbose mode);
    the numeric fields snapshot ``counters`` *at the call site*, so the caller
    controls whether they reflect state before or after this work was tallied.

    ``users_total`` is threaded through explicitly (rather than read off
    ``counters``, whose base :class:`_Counters` type has no such field) so a
    following run's WORK events carry the running user count -- otherwise a
    ``unit="users"`` bar/milestone would render ``0`` for every WORK event. It
    defaults to ``0`` for the plain bookmark backup, where it is meaningless.
    """

    return ProgressEvent(
        kind=ProgressKind.WORK,
        works_total=counters.works_total,
        files_downloaded=counters.files_downloaded,
        files_skipped=counters.files_skipped,
        files_failed=counters.files_failed,
        bytes_downloaded=counters.bytes_downloaded,
        users_total=users_total,
        index=index,
        work_id=work.id,
        title=work.title,
        page_count=work.page_count,
        user_id=user_id,
        line=line,
    )


def _work_planned_event(
    work: BookmarkWork,
    planned: list[str],
    *,
    line: str,
    index: int,
    user_id: str | None = None,
    users_total: int = 0,
) -> ProgressEvent:
    """Build a :attr:`ProgressKind.WORK_PLANNED` event for a dry-run work.

    Carries the dry-run "would create" ``line`` and the ``planned`` filenames as
    a tuple so a renderer need not re-parse the line. ``users_total`` is threaded
    through for a following run's ``unit="users"`` bar (see :func:`_work_event`);
    it defaults to ``0`` for the plain bookmark backup.
    """

    return ProgressEvent(
        kind=ProgressKind.WORK_PLANNED,
        index=index,
        work_id=work.id,
        planned=tuple(planned),
        user_id=user_id,
        users_total=users_total,
        line=line,
    )


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


class _FollowingCounters(_Counters):
    """Mutable accumulator for a follow-backup run's tallies.

    Extends :class:`_Counters` with the per-user tallies a following run also
    tracks, so it can be passed wherever a :class:`_Counters` is expected (e.g.
    :func:`_image_entries_for_work`). The summary builder is named distinctly
    (:meth:`to_following_summary`) rather than overriding ``to_summary`` so the
    base ``-> RunSummary`` contract is not violated.
    """

    def __init__(self) -> None:
        super().__init__()
        self.users_total = 0
        self.users_with_works = 0

    def to_following_summary(self, following_of: str | None) -> FollowingRunSummary:
        """Freeze the running totals into a :class:`FollowingRunSummary`."""

        return FollowingRunSummary(
            works_total=self.works_total,
            works_succeeded=self.works_succeeded,
            works_failed=self.works_failed,
            files_downloaded=self.files_downloaded,
            files_skipped=self.files_skipped,
            files_failed=self.files_failed,
            bytes_downloaded=self.bytes_downloaded,
            users_total=self.users_total,
            users_with_works=self.users_with_works,
            following_of=following_of,
        )


#: The configs :func:`_image_entries_for_work` accepts. Both share the
#: ``output_dir`` / ``metadata_only`` / ``max_retries`` / ``skip_existing``
#: fields it reads; a union (rather than a structural ``Protocol``) is used
#: because ``output_dir``'s ``field(converter=Path)`` is not structurally
#: assignable to a plain ``Path`` protocol member under Pyright.
_ImageConfig = BackupConfig | FollowingConfig


def _image_entries_for_work(
    config: _ImageConfig,
    client: PixivClient,
    work: BookmarkWork,
    counters: _Counters,
    image_dir: Path | None = None,
) -> list[ImageEntry]:
    """Download (or account for) ``work``'s images and return their entries.

    For an ugoira a single GIF entry is produced; otherwise one entry per page.
    In ``metadata_only`` mode no download is attempted and every entry is
    recorded as :attr:`~pixiv_bookmark_backup.models.ImageStatus.METADATA_ONLY`.
    Each outcome is folded into ``counters`` (and the work is marked failed by
    the caller if any entry failed).

    Files are written under ``image_dir`` (defaulting to ``config.output_dir``
    for the flat bookmark layout); the per-author follow layout passes
    ``config.output_dir / <author_id>`` so works land in author subdirectories.
    Either way the recorded manifest path stays *relative to the output root*
    (``config.output_dir``), so a per-author entry reads ``<author_id>/<file>``.
    """

    root_dir = config.output_dir
    write_dir = image_dir if image_dir is not None else root_dir
    if work.type == "ugoira":
        dest = write_dir / ugoira_filename(work.id)
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
                relative_to_output(dest, root_dir),
                status,
                entry_bytes,
                error,
            )
        ]

    entries: list[ImageEntry] = []
    for img in work.images:
        ext = ext_from_url(img.source_url)
        dest = write_dir / image_filename(work.id, img.page, ext)
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
                relative_to_output(dest, root_dir),
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
    progress: Callable[[ProgressEvent], None] = lambda _e: None,
    clock: Callable[[], str] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
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
        progress: Sink for :class:`~pixiv_bookmark_backup.core.ProgressEvent`s;
            defaults to a no-op so the orchestrator stays silent unless the CLI
            wires up output.
        clock: Source of the manifest's ``generated_at`` timestamp; injectable
            for deterministic tests.
        monotonic: Monotonic clock backing the request-pacer; injectable for
            deterministic pacing tests.
        sleep: Sleep callable the request-pacer uses to space out processed
            works; injectable for deterministic pacing tests.

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
    pacer = RequestPacer(config.time_per_request, monotonic, sleep)

    try:
        _iterate_and_process(
            config, client, target, counters, works, errors, progress, pacer
        )
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
    progress: Callable[[ProgressEvent], None],
    pacer: RequestPacer,
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
            line = f"[{index}] {work.id} {work.title!r} " f"— {work.page_count} page(s)"

            if work.type != "ugoira" and not work.images:
                # No downloadable pages (deleted / restricted / limited work):
                # record a work-level error and skip it -- it is not counted as
                # a normal processed work. Keyed on "work_id" so manifest.merge
                # dedups it across resume runs (an "id" key would accumulate
                # duplicates). The listing-interrupt error below has no work id
                # and is intentionally always appended (it describes the run,
                # not a single work). The WORK event is still emitted (with
                # counts unchanged) so verbose mode shows the unavailable work.
                progress(_work_event(counters, work, line=line, index=index))
                errors.append(
                    {
                        "work_id": work.id,
                        "reason": "work unavailable (no downloadable pages)",
                    }
                )
                continue

            pacer.tick()

            if config.dry_run:
                planned = _planned_filenames(work)
                counters.works_total += 1
                counters.works_succeeded += 1
                counters.files_downloaded += len(planned)
                progress(_work_event(counters, work, line=line, index=index))
                progress(
                    _work_planned_event(
                        work,
                        planned,
                        line=f"    would create: {', '.join(planned)}",
                        index=index,
                    )
                )
                continue

            entries = _image_entries_for_work(config, client, work, counters)
            counters.works_total += 1
            if any(entry.status == ImageStatus.FAILED.value for entry in entries):
                counters.works_failed += 1
            else:
                counters.works_succeeded += 1
            works.append(build_work_entry(work, entries))
            progress(_work_event(counters, work, line=line, index=index))


def run_following(
    config: FollowingConfig,
    auth: ResolvedAuth,
    client: PixivClient,
    *,
    progress: Callable[[ProgressEvent], None] = lambda _e: None,
    clock: Callable[[], str] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FollowingRunSummary:
    """Back up the works of every followed user and return a summary.

    Authenticates, then iterates the authenticated user's follow list across the
    requested :attr:`~pixiv_bookmark_backup.core.FollowingConfig.follows` passes,
    backing up each followed user's works into a per-author subdirectory
    (``<output_dir>/<author_id>/``). A single aggregate ``details.json`` is
    written at the output root (unless ``dry_run``).

    The error contract mirrors :func:`run`:

    * :class:`~pixiv_bookmark_backup.pixiv_client.AuthRejected` from
      authentication propagates (CLI -> 3).
    * An :class:`~pixiv_bookmark_backup.pixiv_client.ApiError` raised *before any
      work has been processed* -- whether listing the follow list or the first
      user's works -- propagates (CLI -> 4). Once some works are recorded a
      listing failure is caught, noted as a partial-run error, and the manifest
      is still flushed (CLI -> 6).
    * ``KeyboardInterrupt`` flushes a partial manifest and re-raises (CLI -> 130).

    Args:
        config: The validated run parameters.
        auth: Resolved credentials (only the refresh token is read here).
        client: The Pixiv surface (authenticate, list following, list works,
            download, ugoira).
        progress: Sink for :class:`~pixiv_bookmark_backup.core.ProgressEvent`s;
            defaults to a no-op.
        clock: Source of the manifest's ``generated_at`` timestamp; injectable
            for deterministic tests.
        monotonic: Monotonic clock backing the request-pacer; injectable for
            deterministic pacing tests.
        sleep: Sleep callable the request-pacer uses to space out processed
            works; injectable for deterministic pacing tests.

    Returns:
        The accumulated :class:`~pixiv_bookmark_backup.core.FollowingRunSummary`.
    """

    self_id = client.authenticate(auth.refresh_token)

    counters = _FollowingCounters()
    works: list[Any] = []
    errors: list[dict[str, Any]] = []
    only_set = set(config.only)
    exclude_set = set(config.exclude)
    seen_users: set[str] = set()
    pacer = RequestPacer(config.time_per_request, monotonic, sleep)

    try:
        _iterate_following(
            config,
            client,
            self_id,
            only_set,
            exclude_set,
            seen_users,
            counters,
            works,
            errors,
            progress,
            pacer,
        )
    except ApiError as err:
        if counters.works_total == 0:
            # Nothing recorded yet -> a clean network failure; let the CLI map
            # it to exit 4.
            raise
        # Some works are already recorded -> degrade to a partial run.
        errors.append({"reason": f"listing interrupted: {err}"})
        counters.works_failed += 1
    except KeyboardInterrupt:
        if not config.dry_run:
            _flush_following_manifest(config, self_id, works, errors, clock)
        raise

    if not config.dry_run:
        _flush_following_manifest(config, self_id, works, errors, clock)

    return counters.to_following_summary(self_id)


def _iterate_following(
    config: FollowingConfig,
    client: PixivClient,
    self_id: str,
    only_set: set[str],
    exclude_set: set[str],
    seen_users: set[str],
    counters: _FollowingCounters,
    works: list[Any],
    errors: list[dict[str, Any]],
    progress: Callable[[ProgressEvent], None],
    pacer: RequestPacer,
) -> None:
    """Iterate followed users across the follow passes and process each.

    Applies ``--only``/``--exclude`` (compared by ``str(user.id)``), dedups users
    across passes (so a user followed both publicly and privately is processed
    once under ``follows="all"``), and stops the whole run once ``--max-users``
    users have been processed. Per-user work processing -- and the ``--limit``
    stop signal -- is delegated to :func:`_process_following_user`.
    ``ApiError`` from the follow-list listing and ``KeyboardInterrupt`` propagate
    to :func:`run_following`, which owns recovery.
    """

    for restrict in _follow_passes(config.follows):
        for user in client.iter_following(self_id, restrict):
            uid = str(user.id)
            if uid in seen_users:
                continue
            if exclude_set and uid in exclude_set:
                continue
            if only_set and uid not in only_set:
                continue
            if (
                config.max_users is not None
                and counters.users_total >= config.max_users
            ):
                return
            if config.limit is not None and counters.works_total >= config.limit:
                # The global work budget is already spent -> don't list (or even
                # count) any further users.
                return

            seen_users.add(uid)
            counters.users_total += 1
            safe_name = _sanitize_display_text(user.name)
            progress(
                ProgressEvent(
                    kind=ProgressKind.USER,
                    users_total=counters.users_total,
                    user_id=uid,
                    user_name=user.name,
                    line=f"user {uid} ({safe_name})",
                )
            )

            if _process_following_user(
                config, client, uid, counters, works, errors, progress, pacer
            ):
                return


def _process_following_user(
    config: FollowingConfig,
    client: PixivClient,
    uid: str,
    counters: _FollowingCounters,
    works: list[Any],
    errors: list[dict[str, Any]],
    progress: Callable[[ProgressEvent], None],
    pacer: RequestPacer,
) -> bool:
    """Back up one followed user's works; return whether to stop the whole run.

    Works land in the per-author subdirectory ``<output_dir>/<uid>/``. Returns
    ``True`` only when the global ``--limit`` has been reached (so the caller
    stops every remaining user); ``False`` otherwise. ``--per-user-limit`` caps
    this user's works (counted *after* the ``--since`` filter) and merely moves
    on to the next user. An :class:`~pixiv_bookmark_backup.pixiv_client.ApiError`
    from this user's listing is skip-and-continue once some works exist; while
    nothing has been recorded yet it propagates (so the run fails cleanly with
    exit 4 rather than silently backing up nothing).
    """

    image_dir = config.output_dir / uid
    per_user = 0
    index = 0
    contributed = False

    try:
        for work in client.iter_user_works(uid):
            if config.limit is not None and counters.works_total >= config.limit:
                return True
            if config.per_user_limit is not None and per_user >= config.per_user_limit:
                break
            if not _passes_since(work, config.since):
                continue

            index += 1
            line = f"  {work.id} {work.title!r} — {work.page_count} page(s)"

            if work.type != "ugoira" and not work.images:
                # No downloadable pages (deleted / restricted / limited work):
                # record a work-level error keyed on "work_id" (so manifest.merge
                # dedups across resume runs) and skip it -- it is not counted as a
                # processed work and does not consume the per-user budget. The
                # WORK event is still emitted so verbose mode shows the work.
                progress(
                    _work_event(
                        counters,
                        work,
                        line=line,
                        index=index,
                        user_id=uid,
                        users_total=counters.users_total,
                    )
                )
                errors.append(
                    {
                        "work_id": work.id,
                        "reason": "work unavailable (no downloadable pages)",
                    }
                )
                continue

            pacer.tick()

            if config.dry_run:
                planned = _planned_filenames(work)
                counters.works_total += 1
                counters.works_succeeded += 1
                counters.files_downloaded += len(planned)
                progress(
                    _work_event(
                        counters,
                        work,
                        line=line,
                        index=index,
                        user_id=uid,
                        users_total=counters.users_total,
                    )
                )
                progress(
                    _work_planned_event(
                        work,
                        planned,
                        line=f"    would create: {', '.join(planned)}",
                        index=index,
                        user_id=uid,
                        users_total=counters.users_total,
                    )
                )
            else:
                entries = _image_entries_for_work(
                    config, client, work, counters, image_dir=image_dir
                )
                counters.works_total += 1
                if any(entry.status == ImageStatus.FAILED.value for entry in entries):
                    counters.works_failed += 1
                else:
                    counters.works_succeeded += 1
                works.append(build_work_entry(work, entries))
                progress(
                    _work_event(
                        counters,
                        work,
                        line=line,
                        index=index,
                        user_id=uid,
                        users_total=counters.users_total,
                    )
                )

            per_user += 1
            if not contributed:
                counters.users_with_works += 1
                contributed = True
    except ApiError as err:
        if counters.works_total == 0:
            # Nothing recorded anywhere yet -> a clean network failure; propagate
            # so run_following maps it to exit 4 rather than silently skipping.
            raise
        # Some works already recorded -> note this user's failure and continue.
        errors.append({"reason": f"user {uid} listing interrupted: {err}"})
        counters.works_failed += 1

    return False


def _flush_following_manifest(
    config: FollowingConfig,
    following_of: str | None,
    works: list[Any],
    errors: list[dict[str, Any]],
    clock: Callable[[], str],
) -> None:
    """Merge a following run's ``works``/``errors`` into ``details.json``.

    Identical to :func:`_flush_manifest` except the manifest is built with
    :func:`~pixiv_bookmark_backup.manifest.new_following_manifest` so its
    ``source`` block is following-shaped. The manifest stays at the output root
    (not in any per-author subdirectory).
    """

    details_path = config.output_dir / MANIFEST_NAME
    config.output_dir.mkdir(parents=True, exist_ok=True)

    existing, schema_mismatch = _load_existing_manifest(details_path)
    if schema_mismatch and details_path.exists():
        os.replace(details_path, details_path.with_name(MANIFEST_BACKUP_NAME))

    new = new_following_manifest(following_of, config.follows, clock(), works, errors)
    final = merge(existing, new)
    write_atomic(details_path, final)
