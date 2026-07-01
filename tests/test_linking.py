"""Unit tests for the ``search --symlink-dir`` symlink-materialization module."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pixiv_bookmark_backup.core import SearchMatch
from pixiv_bookmark_backup.linking import (
    SymlinkConfig,
    SymlinkError,
    SymlinkResult,
    plan_and_link,
)
from pixiv_bookmark_backup.manifest import ImageEntry, WorkEntry


def _image(
    *,
    page: int = 0,
    path: str = "1_p0.jpg",
    status: str = "downloaded",
) -> ImageEntry:
    """Build an :class:`ImageEntry` with only the fields linking reads."""

    return ImageEntry(
        page=page,
        source_url="https://example.invalid/img",
        path=path,
        status=status,
        bytes_=None,
        error=None,
    )


def _work(*, work_id: int = 1, images: list[ImageEntry] | None = None) -> WorkEntry:
    """Build a :class:`WorkEntry` carrying ``images`` (defaults to one p0)."""

    return WorkEntry(
        id=work_id,
        type="illust",
        title="T",
        author={"id": 1, "name": "A", "account": "a"},
        posted_at="2024-01-01T00:00:00+09:00",
        nsfw={"x_restrict": 0, "sanity_level": 2, "is_nsfw": False},
        work_tags=[],
        bookmark_tags=None,
        page_count=1,
        images=[_image()] if images is None else images,
    )


def _manifest_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(manifest_dir, manifest_path)`` with a dummy details.json file.

    The backup directory holds an (empty) ``details.json`` file, matching what
    :func:`plan_and_link` expects: it only reads ``manifest_path.parent``.
    """

    manifest_dir = tmp_path / "backup"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "details.json"
    manifest_path.write_text("{}", encoding="utf-8")
    return manifest_dir, manifest_path


def _touch(directory: Path, name: str) -> Path:
    """Create an empty file ``name`` under ``directory`` and return its path."""

    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# --------------------------------------------------------------------------- #
# Config validation / result property
# --------------------------------------------------------------------------- #


def test_config_defaults_and_path_coercion(tmp_path: Path) -> None:
    config = SymlinkConfig(dir=str(tmp_path))
    assert config.dir == tmp_path
    assert config.all_pages is True
    assert config.dry_run is False
    assert config.force is False


def test_links_total_property() -> None:
    result = SymlinkResult(
        dir=Path("/x"),
        all_pages=False,
        dry_run=False,
        force=False,
        links_created=2,
        links_reused=3,
        links_replaced=1,
        works_linked=6,
        works_without_files=0,
    )
    assert result.links_total == 6


# --------------------------------------------------------------------------- #
# Target resolution / page selection
# --------------------------------------------------------------------------- #


def test_created_link_resolves_to_target(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"

    result = plan_and_link(
        [SearchMatch(work=_work(), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir),
    )

    link_path = link_dir / "1_p0.jpg"
    assert link_path.is_symlink()
    assert link_path.resolve() == (manifest_dir / "1_p0.jpg").resolve()
    assert result.links_created == 1
    assert result.works_linked == 1
    assert result.linked == (("1_p0.jpg", str((manifest_dir / "1_p0.jpg").resolve())),)


def test_first_page_only_links_only_lowest_page(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    _touch(manifest_dir, "1_p1.jpg")
    link_dir = tmp_path / "links"
    images = [
        _image(page=0, path="1_p0.jpg"),
        _image(page=1, path="1_p1.jpg"),
    ]

    result = plan_and_link(
        [SearchMatch(work=_work(images=images), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, all_pages=False),
    )

    assert (link_dir / "1_p0.jpg").is_symlink()
    assert not (link_dir / "1_p1.jpg").exists()
    assert result.links_created == 1


def test_first_page_only_links_lowest_existing_page(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    # p0's file is absent; only p1 exists on disk.
    _touch(manifest_dir, "1_p1.jpg")
    link_dir = tmp_path / "links"
    images = [
        _image(page=0, path="1_p0.jpg"),
        _image(page=1, path="1_p1.jpg"),
    ]

    result = plan_and_link(
        [SearchMatch(work=_work(images=images), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, all_pages=False),
    )

    assert not (link_dir / "1_p0.jpg").exists()
    assert (link_dir / "1_p1.jpg").is_symlink()
    assert result.links_created == 1


def test_all_pages_links_every_existing_page(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    _touch(manifest_dir, "1_p2.jpg")
    # p1 has an entry but no file on disk -> skipped.
    link_dir = tmp_path / "links"
    images = [
        _image(page=0, path="1_p0.jpg"),
        _image(page=1, path="1_p1.jpg"),
        _image(page=2, path="1_p2.jpg"),
    ]

    result = plan_and_link(
        [SearchMatch(work=_work(images=images), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, all_pages=True),
    )

    assert (link_dir / "1_p0.jpg").is_symlink()
    assert not (link_dir / "1_p1.jpg").exists()
    assert (link_dir / "1_p2.jpg").is_symlink()
    assert result.links_created == 2
    assert result.works_linked == 1


# --------------------------------------------------------------------------- #
# Existence check (status is not trusted)
# --------------------------------------------------------------------------- #


def test_downloaded_status_without_file_is_skipped(tmp_path: Path) -> None:
    _, manifest_path = _manifest_dir(tmp_path)
    link_dir = tmp_path / "links"
    work = _work(images=[_image(status="downloaded", path="1_p0.jpg")])

    result = plan_and_link(
        [SearchMatch(work=work, matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir),
    )

    assert result.links_total == 0
    assert result.works_linked == 0
    assert result.works_without_files == 1
    assert result.skipped_work_ids == (1,)


def test_failed_status_with_existing_file_is_linkable(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    work = _work(images=[_image(status="failed", path="1_p0.jpg")])

    result = plan_and_link(
        [SearchMatch(work=work, matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir),
    )

    assert (link_dir / "1_p0.jpg").is_symlink()
    assert result.links_created == 1
    assert result.works_linked == 1


# --------------------------------------------------------------------------- #
# Traversal guard
# --------------------------------------------------------------------------- #


def test_traversal_guard_relative_escape_skipped(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    # A real file exists outside the backup dir; a crafted ``../`` path aims at it.
    _touch(tmp_path, "evil.png")
    link_dir = tmp_path / "links"
    work = _work(images=[_image(path="../evil.png")])

    result = plan_and_link(
        [SearchMatch(work=work, matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir),
    )

    assert not (link_dir / "evil.png").exists()
    assert result.links_total == 0
    assert result.works_without_files == 1
    assert result.skipped_work_ids == (1,)


def test_traversal_guard_absolute_escape_skipped(tmp_path: Path) -> None:
    _, manifest_path = _manifest_dir(tmp_path)
    outside = _touch(tmp_path, "outside.png")
    link_dir = tmp_path / "links"
    work = _work(images=[_image(path=str(outside))])

    result = plan_and_link(
        [SearchMatch(work=work, matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir),
    )

    assert not (link_dir / "outside.png").exists()
    assert result.links_total == 0
    assert result.works_without_files == 1


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_rerun_reuses_existing_correct_link(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    matches = [SearchMatch(work=_work(), matched_fields=[])]
    config = SymlinkConfig(dir=link_dir)

    first = plan_and_link(matches, manifest_path, config)
    assert first.links_created == 1

    second = plan_and_link(matches, manifest_path, config)
    assert second.links_created == 0
    assert second.links_reused == 1
    assert second.links_total == 1
    assert (link_dir / "1_p0.jpg").resolve() == (manifest_dir / "1_p0.jpg").resolve()


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #


def test_foreign_regular_file_is_fatal_even_with_force(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    foreign = link_dir / "1_p0.jpg"
    foreign.write_text("mine", encoding="utf-8")

    with pytest.raises(SymlinkError):
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir, force=True),
        )

    assert not foreign.is_symlink()
    assert foreign.read_text(encoding="utf-8") == "mine"


def test_link_dir_is_a_regular_file_is_fatal(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    # The chosen link directory path is occupied by a regular file, so mkdir
    # raises OSError (FileExistsError) -> SymlinkError.
    link_dir = tmp_path / "links"
    link_dir.write_text("not a dir", encoding="utf-8")

    with pytest.raises(SymlinkError):
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir),
        )


def test_symlink_to_different_target_without_force_raises(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    other = _touch(manifest_dir, "other.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(other)

    with pytest.raises(SymlinkError):
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir),
        )

    assert link_path.resolve() == other.resolve()


def test_symlink_to_different_target_with_force_replaces(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    target = _touch(manifest_dir, "1_p0.jpg")
    other = _touch(manifest_dir, "other.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(other)

    result = plan_and_link(
        [SearchMatch(work=_work(), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, force=True),
    )

    assert link_path.resolve() == target.resolve()
    assert result.links_replaced == 1
    assert result.links_total == 1
    # The off-target file is untouched.
    assert other.exists()


# --------------------------------------------------------------------------- #
# Empty matches / dry-run
# --------------------------------------------------------------------------- #


def test_empty_matches_creates_dir_with_zero_links(tmp_path: Path) -> None:
    _, manifest_path = _manifest_dir(tmp_path)
    link_dir = tmp_path / "links"

    result = plan_and_link([], manifest_path, SymlinkConfig(dir=link_dir))

    assert link_dir.is_dir()
    assert result.links_total == 0
    assert result.works_linked == 0
    assert result.works_without_files == 0


def test_dry_run_creates_nothing_but_returns_real_counts(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"

    result = plan_and_link(
        [SearchMatch(work=_work(), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, dry_run=True),
    )

    assert not link_dir.exists()
    assert result.links_created == 1
    assert result.links_total == 1
    assert result.works_linked == 1
    assert result.linked == (("1_p0.jpg", str((manifest_dir / "1_p0.jpg").resolve())),)


def test_dry_run_still_raises_on_foreign_conflict(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "1_p0.jpg").write_text("mine", encoding="utf-8")

    with pytest.raises(SymlinkError):
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir, dry_run=True),
        )


def test_dry_run_and_real_both_raise_when_link_dir_is_file(tmp_path: Path) -> None:
    # config.dir is an existing regular file: the real run raises (mkdir) and
    # the dry run must raise the SAME error, preserving the preview invariant.
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    link_dir.write_text("not a dir", encoding="utf-8")
    matches = [SearchMatch(work=_work(), matched_fields=[])]

    with pytest.raises(SymlinkError):
        plan_and_link(matches, manifest_path, SymlinkConfig(dir=link_dir))
    with pytest.raises(SymlinkError):
        plan_and_link(matches, manifest_path, SymlinkConfig(dir=link_dir, dry_run=True))
    # The occupying file is left untouched by the dry run.
    assert link_dir.read_text(encoding="utf-8") == "not a dir"


def test_dry_run_and_real_both_raise_when_ancestor_is_file(tmp_path: Path) -> None:
    # An existing ANCESTOR of config.dir is a regular file: the real run's
    # mkdir(parents=True) raises NotADirectoryError, and the dry run must raise
    # the SAME error via the walk-up precheck, preserving the preview invariant.
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    link_dir = blocker / "sub" / "links"
    matches = [SearchMatch(work=_work(), matched_fields=[])]

    with pytest.raises(SymlinkError):
        plan_and_link(matches, manifest_path, SymlinkConfig(dir=link_dir))
    with pytest.raises(SymlinkError):
        plan_and_link(matches, manifest_path, SymlinkConfig(dir=link_dir, dry_run=True))
    # The blocking ancestor file is left untouched by the dry run.
    assert blocker.read_text(encoding="utf-8") == "not a dir"


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses directory write permissions"
)
def test_real_run_maps_unwritable_parent_to_symlink_error(tmp_path: Path) -> None:
    # An unwritable existing parent is not statically detectable by the precheck
    # (the path is a real directory); mkdir attempts the write and raises
    # PermissionError, which the real-run OSError catch-all maps to SymlinkError.
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    parent = tmp_path / "ro"
    parent.mkdir()
    link_dir = parent / "links"
    parent.chmod(0o500)
    try:
        with pytest.raises(SymlinkError):
            plan_and_link(
                [SearchMatch(work=_work(), matched_fields=[])],
                manifest_path,
                SymlinkConfig(dir=link_dir),
            )
    finally:
        parent.chmod(0o700)


def test_dry_run_reuse_reports_count_without_mutation(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    target = _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(target)

    result = plan_and_link(
        [SearchMatch(work=_work(), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, dry_run=True),
    )

    assert result.links_reused == 1
    assert result.links_created == 0
    assert result.links_total == 1
    # The link is unchanged: still the same symlink pointing at the same target.
    assert link_path.is_symlink()
    assert link_path.resolve() == target.resolve()


def test_dry_run_force_replace_reports_count_without_mutation(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    other = _touch(manifest_dir, "other.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(other)

    result = plan_and_link(
        [SearchMatch(work=_work(), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, dry_run=True, force=True),
    )

    assert result.links_replaced == 1
    assert result.links_total == 1
    # No mutation: the link still points at the OLD off-target file.
    assert link_path.resolve() == other.resolve()


# --------------------------------------------------------------------------- #
# Dangling pre-existing symlink
# --------------------------------------------------------------------------- #


def test_dangling_symlink_without_force_raises(tmp_path: Path) -> None:
    # A broken pre-existing link at the name is a symlink-to-a-different-target,
    # NOT "absent": without force it conflicts and raises.
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(tmp_path / "nonexistent-target.jpg")
    assert link_path.is_symlink() and not link_path.exists()

    with pytest.raises(SymlinkError):
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir),
        )

    # The dangling link is unchanged.
    assert link_path.is_symlink() and not link_path.exists()


def test_dangling_symlink_with_force_is_repointed(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    target = _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(tmp_path / "nonexistent-target.jpg")

    result = plan_and_link(
        [SearchMatch(work=_work(), matched_fields=[])],
        manifest_path,
        SymlinkConfig(dir=link_dir, force=True),
    )

    assert result.links_replaced == 1
    assert result.links_created == 0
    assert link_path.resolve() == target.resolve()


# --------------------------------------------------------------------------- #
# Write-side OSError -> SymlinkError (exit-10 contract)
# --------------------------------------------------------------------------- #


def test_create_write_oserror_maps_to_symlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An OSError from the create-branch symlink write (a platform/filesystem
    # that refuses symlink creation -- e.g. Windows without privilege) must
    # surface as SymlinkError (exit 10), not a bare OSError (exit 1).
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    link_dir = tmp_path / "links"

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("symlink privilege not held")

    monkeypatch.setattr(Path, "symlink_to", boom)

    with pytest.raises(SymlinkError) as excinfo:
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir),
        )

    message = str(excinfo.value)
    assert "Cannot create symlink" in message  # teaching, names the link path
    assert str(link_dir / "1_p0.jpg") in message


def test_replace_write_oserror_maps_to_symlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The force-replace branch (unlink + re-symlink) must likewise map a write
    # OSError to SymlinkError. Set up a divergent pre-existing link BEFORE the
    # monkeypatch so only the replacement write is made to fail.
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")
    other = _touch(manifest_dir, "other.jpg")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link_path = link_dir / "1_p0.jpg"
    link_path.symlink_to(other)

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("symlink privilege not held")

    monkeypatch.setattr(Path, "symlink_to", boom)

    with pytest.raises(SymlinkError) as excinfo:
        plan_and_link(
            [SearchMatch(work=_work(), matched_fields=[])],
            manifest_path,
            SymlinkConfig(dir=link_dir, force=True),
        )

    assert "Cannot re-point symlink" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Tally coherence
# --------------------------------------------------------------------------- #


def test_tally_coherence_across_mixed_works(tmp_path: Path) -> None:
    manifest_dir, manifest_path = _manifest_dir(tmp_path)
    _touch(manifest_dir, "1_p0.jpg")  # work 1: linkable
    _touch(manifest_dir, "3_p0.jpg")  # work 3: linkable
    link_dir = tmp_path / "links"

    matches = [
        SearchMatch(
            work=_work(work_id=1, images=[_image(path="1_p0.jpg")]), matched_fields=[]
        ),
        # work 2: file absent -> without files
        SearchMatch(
            work=_work(work_id=2, images=[_image(path="2_p0.jpg")]), matched_fields=[]
        ),
        SearchMatch(
            work=_work(work_id=3, images=[_image(path="3_p0.jpg")]), matched_fields=[]
        ),
    ]

    result = plan_and_link(matches, manifest_path, SymlinkConfig(dir=link_dir))

    assert result.links_total == (
        result.links_created + result.links_reused + result.links_replaced
    )
    assert result.works_linked == 2
    assert result.works_without_files == 1
    assert result.works_linked + result.works_without_files == len(matches)
    assert result.skipped_work_ids == (2,)
