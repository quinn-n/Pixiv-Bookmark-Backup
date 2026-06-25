"""Unit tests for the ``details.json`` manifest module."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pixiv_bookmark_backup.manifest import (
    SCHEMA_VERSION,
    ImageEntry,
    Manifest,
    WorkEntry,
    build_work_entry,
    derive_nsfw,
    load_or_init,
    make_image_entry,
    merge,
    new_manifest,
    write_atomic,
)
from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    ImageStatus,
    WorkImage,
    WorkTag,
)


def _make_work(
    *,
    work_id: int = 123,
    x_restrict: int = 0,
    sanity_level: int = 2,
    work_tags: list[WorkTag] | None = None,
    bookmark_tags: list[str] | None = None,
) -> BookmarkWork:
    return BookmarkWork(
        id=work_id,
        type="illust",
        title="A work",
        author=Author(id=7, name="Artist", account="artist"),
        posted_at="2023-01-02T03:04:05+09:00",
        x_restrict=x_restrict,
        sanity_level=sanity_level,
        work_tags=(
            work_tags
            if work_tags is not None
            else [WorkTag(name="original", translated_name=None)]
        ),
        bookmark_tags=bookmark_tags,
        page_count=1,
        images=[WorkImage(page=0, source_url="https://example.test/0.png")],
    )


def _image(
    *,
    page: int = 0,
    path: str = "123_p0.png",
    status: str = ImageStatus.DOWNLOADED.value,
    bytes_: int | None = 1024,
    error: str | None = None,
) -> ImageEntry:
    return ImageEntry(
        page=page,
        source_url="https://example.test/0.png",
        path=path,
        status=status,
        bytes_=bytes_,
        error=error,
    )


# --------------------------------------------------------------------------- #
# derive_nsfw
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("x_restrict", "sanity_level", "is_nsfw"),
    [
        (0, 2, False),
        (1, 4, True),
        (2, 6, True),
        (0, 6, False),
    ],
)
def test_derive_nsfw(x_restrict: int, sanity_level: int, is_nsfw: bool) -> None:
    result = derive_nsfw(x_restrict, sanity_level)
    assert result == {
        "x_restrict": x_restrict,
        "sanity_level": sanity_level,
        "is_nsfw": is_nsfw,
    }


# --------------------------------------------------------------------------- #
# make_image_entry
# --------------------------------------------------------------------------- #


def test_make_image_entry_accepts_enum_and_stores_string() -> None:
    entry = make_image_entry(
        page=0,
        source_url="https://example.test/0.png",
        path="123_p0.png",
        status=ImageStatus.DOWNLOADED,
        bytes_=42,
        error=None,
    )
    assert entry.status == "downloaded"
    assert isinstance(entry.status, str)
    assert entry.bytes_ == 42


def test_make_image_entry_accepts_plain_string_status() -> None:
    entry = make_image_entry(
        page=1,
        source_url="https://example.test/1.png",
        path="123_p1.png",
        status="failed",
        bytes_=None,
        error="boom",
    )
    assert entry.status == "failed"
    assert entry.bytes_ is None
    assert entry.error == "boom"


# --------------------------------------------------------------------------- #
# build_work_entry
# --------------------------------------------------------------------------- #


def test_build_work_entry_shapes_fields() -> None:
    work = _make_work(
        x_restrict=1,
        sanity_level=4,
        work_tags=[
            WorkTag(name="original", translated_name=None),
            WorkTag(name="女の子", translated_name="girl"),
        ],
        bookmark_tags=["fav", "art"],
    )
    images = [_image(path="123_p0.png")]

    entry = build_work_entry(work, images)

    assert entry.id == 123
    assert entry.type == "illust"
    assert entry.title == "A work"
    assert entry.author == {"id": 7, "name": "Artist", "account": "artist"}
    assert entry.posted_at == "2023-01-02T03:04:05+09:00"
    assert entry.nsfw == {"x_restrict": 1, "sanity_level": 4, "is_nsfw": True}
    assert entry.work_tags == [
        {"name": "original", "translated_name": None},
        {"name": "女の子", "translated_name": "girl"},
    ]
    assert entry.bookmark_tags == ["fav", "art"]
    assert entry.page_count == 1
    assert entry.images[0].path == "123_p0.png"


def test_build_work_entry_preserves_none_bookmark_tags() -> None:
    work = _make_work(bookmark_tags=None)
    entry = build_work_entry(work, [_image()])
    assert entry.bookmark_tags is None


def test_build_work_entry_keeps_relative_paths() -> None:
    work = _make_work()
    images = [_image(path="nested/123_p0.png")]
    entry = build_work_entry(work, images)
    assert entry.images[0].path == "nested/123_p0.png"


# --------------------------------------------------------------------------- #
# to_dict / from_dict round-trip
# --------------------------------------------------------------------------- #


def test_image_entry_serializes_bytes_key() -> None:
    entry = _image(bytes_=2048)
    payload = entry.to_dict()
    assert payload["bytes"] == 2048
    assert "bytes_" not in payload
    assert ImageEntry.from_dict(payload) == entry


def test_manifest_round_trip_via_json() -> None:
    work = _make_work(
        work_tags=[WorkTag(name="女の子", translated_name="girl")],
        bookmark_tags=None,
    )
    entry = build_work_entry(
        work,
        [_image(bytes_=2048, status=ImageStatus.DOWNLOADED.value)],
    )
    manifest = new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[entry],
        errors=[{"work_id": 999, "message": "gone"}],
    )

    # Round-trip through real JSON text to exercise unicode handling.
    text = json.dumps(manifest.to_dict(), ensure_ascii=False)
    assert "女の子" in text
    restored = Manifest.from_dict(json.loads(text))

    assert restored == manifest
    assert restored.works[0].work_tags == [
        {"name": "女の子", "translated_name": "girl"}
    ]
    assert restored.works[0].images[0].bytes_ == 2048
    assert restored.errors == [{"work_id": 999, "message": "gone"}]


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #


def _manifest_with(
    works: list[WorkEntry], errors: list[dict] | None = None
) -> Manifest:
    return new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=works,
        errors=errors or [],
    )


def test_merge_updates_image_status_in_place() -> None:
    old_work = build_work_entry(
        _make_work(work_id=1),
        [_image(status=ImageStatus.DOWNLOADED.value, bytes_=1024)],
    )
    new_work = build_work_entry(
        _make_work(work_id=1),
        [_image(status=ImageStatus.SKIPPED.value, bytes_=None)],
    )
    existing = _manifest_with([old_work])
    incoming = _manifest_with([new_work])

    result = merge(existing, incoming)

    assert len(result.works) == 1
    assert result.works[0].id == 1
    assert result.works[0].images[0].status == "skipped"
    assert result.works[0].images[0].bytes_ is None


def test_merge_appends_new_work_and_preserves_old() -> None:
    work_one = build_work_entry(_make_work(work_id=1), [_image()])
    work_two = build_work_entry(_make_work(work_id=2), [_image()])
    existing = _manifest_with([work_one])
    incoming = _manifest_with([work_two])

    result = merge(existing, incoming)

    ids = [work.id for work in result.works]
    assert ids == [1, 2]


def test_merge_retains_failed_entry_absent_from_new_run() -> None:
    failed_work = build_work_entry(
        _make_work(work_id=5),
        [_image(status=ImageStatus.FAILED.value, bytes_=None, error="timeout")],
    )
    other_work = build_work_entry(_make_work(work_id=6), [_image()])
    existing = _manifest_with([failed_work])
    incoming = _manifest_with([other_work])

    result = merge(existing, incoming)

    by_id = {work.id: work for work in result.works}
    assert set(by_id) == {5, 6}
    assert by_id[5].images[0].status == "failed"
    assert by_id[5].images[0].error == "timeout"


def test_merge_keeps_new_generated_at_and_source() -> None:
    existing = new_manifest(
        user_id="old",
        visibility="private",
        generated_at="2020-01-01T00:00:00+00:00",
        works=[],
        errors=[],
    )
    incoming = new_manifest(
        user_id="new",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[],
        errors=[],
    )

    result = merge(existing, incoming)

    assert result.generated_at == "2026-06-24T00:00:00+00:00"
    assert result.source == {"user_id": "new", "visibility": "public"}


def test_merge_errors_union_by_work_id() -> None:
    existing = _manifest_with(
        [],
        errors=[
            {"work_id": 1, "message": "old"},
            {"work_id": 2, "message": "kept"},
        ],
    )
    incoming = _manifest_with(
        [],
        errors=[
            {"work_id": 1, "message": "refreshed"},
            {"work_id": 3, "message": "brand new"},
        ],
    )

    result = merge(existing, incoming)

    assert {"work_id": 2, "message": "kept"} in result.errors
    assert {"work_id": 1, "message": "refreshed"} in result.errors
    assert {"work_id": 1, "message": "old"} not in result.errors
    assert {"work_id": 3, "message": "brand new"} in result.errors


# --------------------------------------------------------------------------- #
# load_or_init
# --------------------------------------------------------------------------- #


def test_load_or_init_missing_file(tmp_path: Path) -> None:
    manifest, mismatch = load_or_init(tmp_path / "details.json")
    assert mismatch is False
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.works == []
    assert manifest.errors == []


def test_load_or_init_existing_v1_file(tmp_path: Path) -> None:
    path = tmp_path / "details.json"
    entry = build_work_entry(_make_work(work_id=7), [_image()])
    manifest = new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[entry],
        errors=[],
    )
    write_atomic(path, manifest)

    loaded, mismatch = load_or_init(path)

    assert mismatch is False
    assert loaded == manifest


def test_load_or_init_flags_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "details.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "generated_at": "2020-01-01T00:00:00+00:00",
                "source": {"user_id": "1", "visibility": "public"},
                "works": [],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    manifest, mismatch = load_or_init(path)

    assert mismatch is True
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.works == []


# --------------------------------------------------------------------------- #
# write_atomic
# --------------------------------------------------------------------------- #


def test_write_atomic_writes_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "out" / "details.json"
    entry = build_work_entry(
        _make_work(work_tags=[WorkTag(name="女の子", translated_name="girl")]),
        [_image()],
    )
    manifest = new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[entry],
        errors=[],
    )

    write_atomic(path, manifest)

    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert Manifest.from_dict(data) == manifest
    # Japanese stays unescaped on disk (ensure_ascii=False).
    assert "女の子" in path.read_text(encoding="utf-8")


def test_write_atomic_replaces_existing_and_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "details.json"
    path.write_text("stale", encoding="utf-8")

    manifest = new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[],
        errors=[],
    )
    write_atomic(path, manifest)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    # No temp artifact left behind.
    assert not (tmp_path / "details.json.tmp").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_cleans_up_temp_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pixiv_bookmark_backup.manifest as manifest_module

    path = tmp_path / "details.json"
    manifest = new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[],
        errors=[],
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(manifest_module.json, "dump", boom)

    with pytest.raises(RuntimeError, match="disk full"):
        write_atomic(path, manifest)

    # The failed write left no manifest and no orphaned temp artifact behind.
    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_write_atomic_writes_owner_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "details.json"
    manifest = new_manifest(
        user_id="42",
        visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[],
        errors=[],
    )

    write_atomic(path, manifest)

    # The token-bearing manifest must not be group/other-readable: mkstemp
    # creates the temp file 0o600 and os.replace preserves that mode.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
