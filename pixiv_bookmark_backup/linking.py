"""Symlink creation for the ``search --symlink-dir`` feature.

This module owns *all* of the filesystem I/O involved in materializing a set of
:class:`~pixiv_bookmark_backup.core.SearchMatch` results as symlinks into a
user-chosen directory. The click layer parses options into a
:class:`SymlinkConfig`, hands the matches and the manifest path here, and renders
the returned :class:`SymlinkResult`; every ``mkdir``/``symlink``/``unlink`` and
every read-side safety check lives in :func:`plan_and_link`.

Design constraints that keep this module standalone and testable:

* It imports only *data* types -- :class:`~pixiv_bookmark_backup.core.SearchMatch`
  and :class:`~pixiv_bookmark_backup.manifest.WorkEntry` /
  :class:`~pixiv_bookmark_backup.manifest.ImageEntry` -- and never reaches into
  ``cli.py`` or references any exit-code enum. The CLI is responsible for
  mapping :class:`SymlinkError` to its exit code.
* Only genuinely *fatal* filesystem/platform faults raise
  :class:`SymlinkError`; per-image conditions that a run can safely step over
  (a missing file on disk, a path that escapes the backup directory) are
  silently skipped and reflected in the tallies instead.
* ``dry_run`` performs every *read* (existence, traversal, conflict) check a
  real run would and raises the *same* :class:`SymlinkError` -- only the writes
  are skipped -- so a preview can never disagree with the real run's outcome.
"""

from __future__ import annotations

from pathlib import Path

from attrs import field, frozen
from attrs.validators import instance_of

from pixiv_bookmark_backup.core import SearchMatch
from pixiv_bookmark_backup.manifest import ImageEntry, WorkEntry


class SymlinkError(Exception):
    """A fatal filesystem or platform fault while creating symlinks.

    Raised only for conditions a run cannot safely continue past: a link name
    occupied by non-symlink content (never deleted, fatal even with
    ``--force``), a link name pointing at a *different* target without
    ``--force``, an unwritable or unusable target directory, or an OS/platform
    that refuses the symlink itself. Non-fatal per-image skips (a missing file,
    a traversal escape) do *not* raise; they are reflected in the tallies.

    A plain message-carrying exception so the CLI can surface a clear error and
    map it to a non-zero exit code.
    """


@frozen
class SymlinkConfig:
    """Parameters controlling a single ``search --symlink-dir`` run.

    A frozen ``attrs`` value object mirroring :class:`SearchConfig`'s style:
    validated once at construction, then safe to pass around. ``dir`` is coerced
    to a :class:`~pathlib.Path` so callers may pass a plain string.

    ``all_pages`` links every page of a multi-page work rather than only the
    lowest-numbered one; it defaults to ``True`` so ``--symlink-dir`` links
    every downloaded page unless ``--first-page-only`` is given. ``dry_run``
    previews without touching the filesystem; ``force`` re-points an existing
    symlink that aims at a different target.
    """

    dir: Path = field(converter=Path)
    all_pages: bool = field(default=True, validator=instance_of(bool))
    dry_run: bool = field(default=False, validator=instance_of(bool))
    force: bool = field(default=False, validator=instance_of(bool))


@frozen
class SymlinkResult:
    """Tallies and reporting data from a completed symlink run.

    ``dir`` is the resolved link directory that was acted on (and that the CLI
    reports). The three ``links_*`` counters partition the links this run
    touched: freshly created, reused (already pointing at the right target), and
    replaced (re-pointed under ``--force``). ``works_linked`` and
    ``works_without_files`` partition the input works; ``skipped_work_ids`` names
    the latter. ``linked`` records the ``(link_name, target_str)`` pairs this run
    created/reused/replaced so the CLI can report them verbosely without redoing
    any I/O.
    """

    dir: Path
    all_pages: bool
    dry_run: bool
    force: bool
    links_created: int
    links_reused: int
    links_replaced: int
    works_linked: int
    works_without_files: int
    skipped_work_ids: tuple[int, ...] = field(factory=tuple)
    linked: tuple[tuple[str, str], ...] = field(factory=tuple)

    @property
    def links_total(self) -> int:
        """Return the total links touched: created + reused + replaced.

        This drives the "Linked N" summary and stays stable across re-runs (a
        re-run reuses rather than recreates, so the total is unchanged).
        """

        return self.links_created + self.links_reused + self.links_replaced


def _surviving_images(
    work: WorkEntry, manifest_dir: Path, all_pages: bool
) -> list[tuple[ImageEntry, Path]]:
    """Return the ``(image, resolved_target)`` pairs of ``work`` worth linking.

    Images are considered in ascending ``page`` order. An image survives only
    when its resolved target stays inside ``manifest_dir`` (the traversal guard
    drops crafted relative/absolute paths that escape the backup directory) and
    the target file actually exists on disk -- the recorded ``status`` is *not*
    trusted, since a "downloaded" file may have been deleted and a "failed"
    image's file may coincidentally exist.

    With ``all_pages`` every surviving image is returned; otherwise only the
    first survivor (the lowest existing page) is kept.
    """

    survivors: list[tuple[ImageEntry, Path]] = []
    for image in sorted(work.images, key=lambda entry: entry.page):
        target = (manifest_dir / image.path).resolve()
        if not target.is_relative_to(manifest_dir):
            continue
        if not target.exists():
            continue
        survivors.append((image, target))
        if not all_pages:
            break
    return survivors


def plan_and_link(
    matches: list[SearchMatch], manifest_path: Path, config: SymlinkConfig
) -> SymlinkResult:
    """Create symlinks to ``matches``' images under ``config.dir``.

    ``manifest_path`` locates the backup: image paths are resolved relative to
    its parent directory. For each matched work the linkable images are selected
    by :func:`_surviving_images` (traversal-guarded, existence-checked, and
    reduced to the lowest existing page unless ``config.all_pages``); a work with
    no survivors is counted under ``works_without_files`` and its id recorded in
    ``skipped_work_ids``.

    For each surviving image the link is named after the image's filename and
    dispatched by what already occupies that name: nothing -> create; a symlink
    already aimed at the same target -> reuse (idempotent); a symlink aimed
    elsewhere -> replace under ``config.force`` else :class:`SymlinkError`; any
    non-symlink (regular file or directory) -> :class:`SymlinkError`, never
    overwritten even with ``--force``.

    When ``config.dry_run`` every read-side check still runs and raises the same
    :class:`SymlinkError` a real run would; only the writes (``mkdir``,
    ``symlink``, ``unlink``) are skipped and the target directory is left
    untouched. Returns a :class:`SymlinkResult` carrying every tally, the
    resolved link directory, and the ``(link_name, target)`` pairs touched.

    Link-name uniqueness across works is *assumed*, inherited from the caller's
    flat, ID-prefixed image naming (e.g. ``144036249_p0.jpg``); this module does
    not enforce it, so two works claiming the same filename would collide.

    :raises SymlinkError: on any fatal filesystem or platform fault (see
        :class:`SymlinkError`).
    """

    manifest_dir = manifest_path.resolve().parent
    link_dir = config.dir.resolve()

    # Read-only precheck, run unconditionally (dry-run and real) so a preview
    # raises the *same* SymlinkError a real run's mkdir would when the chosen
    # link directory -- or an existing ancestor of it -- is a non-directory.
    # Walk up to the nearest existing path component: if it is not a directory,
    # mkdir(parents=True) would raise FileExistsError/NotADirectoryError. This
    # only covers structurally read-detectable faults; permission errors and
    # other exotic OSErrors remain the job of the real mkdir's catch-all below.
    probe = link_dir
    while not probe.exists():
        probe = probe.parent
    if not probe.is_dir():
        raise SymlinkError(
            f"Cannot use symlink directory {link_dir}: {probe} is a file, not "
            "a directory. Choose a directory path or remove that file."
        )

    if not config.dry_run:
        try:
            link_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SymlinkError(
                f"Cannot create symlink directory {link_dir}: {exc}. "
                "Choose a writable path whose parents are directories."
            ) from exc

    links_created = 0
    links_reused = 0
    links_replaced = 0
    works_linked = 0
    works_without_files = 0
    skipped_work_ids: list[int] = []
    linked: list[tuple[str, str]] = []

    for match in matches:
        work = match.work
        survivors = _surviving_images(work, manifest_dir, config.all_pages)
        if not survivors:
            works_without_files += 1
            skipped_work_ids.append(work.id)
            continue
        works_linked += 1

        for image, target in survivors:
            name = Path(image.path).name
            link_path = link_dir / name
            target_str = str(target)

            present = link_path.is_symlink() or link_path.exists()
            if not present:
                if not config.dry_run:
                    try:
                        link_path.symlink_to(target)
                    except OSError as exc:
                        raise SymlinkError(
                            f"Cannot create symlink {link_path} -> {target}: "
                            f"{exc}. This platform or filesystem may refuse "
                            "symlink creation (e.g. Windows without the "
                            "privilege) or the link name may be unusable."
                        ) from exc
                links_created += 1
                linked.append((name, target_str))
            elif link_path.is_symlink():
                if link_path.resolve() == target:
                    links_reused += 1
                    linked.append((name, target_str))
                elif not config.force:
                    raise SymlinkError(
                        f"Refusing to overwrite symlink {link_path}: it points "
                        f"at {link_path.resolve()}, not {target}. Re-run with "
                        "--force to re-point existing symlinks."
                    )
                else:
                    if not config.dry_run:
                        try:
                            link_path.unlink()
                            link_path.symlink_to(target)
                        except OSError as exc:
                            raise SymlinkError(
                                f"Cannot re-point symlink {link_path} -> "
                                f"{target}: {exc}. This platform or filesystem "
                                "may refuse symlink creation (e.g. Windows "
                                "without the privilege) or the link name may be "
                                "unusable."
                            ) from exc
                    links_replaced += 1
                    linked.append((name, target_str))
            else:
                raise SymlinkError(
                    f"Refusing to overwrite {link_path}: it is existing "
                    "non-symlink content (a regular file or directory). Even "
                    "--force will not delete it; move or rename it first."
                )

    return SymlinkResult(
        dir=link_dir,
        all_pages=config.all_pages,
        dry_run=config.dry_run,
        force=config.force,
        links_created=links_created,
        links_reused=links_reused,
        links_replaced=links_replaced,
        works_linked=works_linked,
        works_without_files=works_without_files,
        skipped_work_ids=tuple(skipped_work_ids),
        linked=tuple(linked),
    )
