"""Unit tests for the ``details.json`` manifest module."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    new_following_manifest,
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
# new_following_manifest
# --------------------------------------------------------------------------- #


def test_new_following_manifest_source_block() -> None:
    manifest = new_following_manifest(
        following_of="42",
        follow_visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[],
        errors=[],
    )
    assert manifest.source == {
        "mode": "following",
        "following_of": "42",
        "follow_visibility": "public",
    }
    assert manifest.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION == 1


def test_new_following_manifest_carries_through_fields() -> None:
    entry = build_work_entry(
        _make_work(work_id=7),
        [_image(bytes_=2048, status=ImageStatus.DOWNLOADED.value)],
    )
    manifest = new_following_manifest(
        following_of="99",
        follow_visibility="private",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[entry],
        errors=[{"work_id": 999, "message": "gone"}],
    )

    assert manifest.generated_at == "2026-06-24T00:00:00+00:00"
    assert manifest.works == [entry]
    assert manifest.errors == [{"work_id": 999, "message": "gone"}]
    assert manifest.source == {
        "mode": "following",
        "following_of": "99",
        "follow_visibility": "private",
    }


def test_new_following_manifest_accepts_none_following_of() -> None:
    manifest = new_following_manifest(
        following_of=None,
        follow_visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[],
        errors=[],
    )
    assert manifest.source == {
        "mode": "following",
        "following_of": None,
        "follow_visibility": "public",
    }


def test_merge_following_manifests_keeps_new_source_and_dedups() -> None:
    old_work = build_work_entry(
        _make_work(work_id=1),
        [_image(status=ImageStatus.DOWNLOADED.value, bytes_=1024)],
    )
    refreshed_work = build_work_entry(
        _make_work(work_id=1),
        [_image(status=ImageStatus.SKIPPED.value, bytes_=None)],
    )
    new_work = build_work_entry(_make_work(work_id=2), [_image()])

    existing = new_following_manifest(
        following_of="old",
        follow_visibility="private",
        generated_at="2020-01-01T00:00:00+00:00",
        works=[old_work],
        errors=[
            {"work_id": 1, "message": "old"},
            {"work_id": 9, "message": "kept"},
        ],
    )
    incoming = new_following_manifest(
        following_of="new",
        follow_visibility="public",
        generated_at="2026-06-24T00:00:00+00:00",
        works=[refreshed_work, new_work],
        errors=[
            {"work_id": 1, "message": "refreshed"},
            {"work_id": 3, "message": "brand new"},
        ],
    )

    result = merge(existing, incoming)

    # The new following-shaped source wins.
    assert result.source == {
        "mode": "following",
        "following_of": "new",
        "follow_visibility": "public",
    }
    assert result.generated_at == "2026-06-24T00:00:00+00:00"

    # Works are deduped by id; the refreshed record replaces the old one.
    ids = [work.id for work in result.works]
    assert ids == [1, 2]
    by_id = {work.id: work for work in result.works}
    assert by_id[1].images[0].status == "skipped"

    # Errors are unioned by work_id.
    assert {"work_id": 9, "message": "kept"} in result.errors
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


def test_load_or_init_corrupt_json_flags_mismatch(tmp_path: Path) -> None:
    # A truncated/malformed details.json must degrade like a schema mismatch
    # (empty manifest + mismatch True) rather than propagating a JSONDecodeError.
    path = tmp_path / "details.json"
    path.write_text("{ bad", encoding="utf-8")

    manifest, mismatch = load_or_init(path)

    assert mismatch is True
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.works == []
    assert manifest.errors == []


def test_load_or_init_deeply_nested_json_flags_mismatch(tmp_path: Path) -> None:
    # CPython's JSON decoder recurses, so a deeply-nested document raises
    # RecursionError. load_or_init must catch it and degrade like a mismatch
    # instead of surfacing a raw traceback. json bails around depth 1000, so
    # this construction is fast and deterministic.
    path = tmp_path / "details.json"
    path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")

    manifest, mismatch = load_or_init(path)

    assert mismatch is True
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.works == []
    assert manifest.errors == []


@pytest.mark.parametrize("payload", ["[]", '"foo"', "null", "42"])
def test_load_or_init_non_object_json_flags_mismatch(
    tmp_path: Path, payload: str
) -> None:
    # Valid JSON whose top-level value is not an object has no schema_version to
    # read; load_or_init must treat it as a mismatch rather than raising an
    # AttributeError from calling .get on a list/str/int/None.
    path = tmp_path / "details.json"
    path.write_text(payload, encoding="utf-8")

    manifest, mismatch = load_or_init(path)

    assert mismatch is True
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.works == []
    assert manifest.errors == []


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


# --------------------------------------------------------------------------- #
# property-based tests
# --------------------------------------------------------------------------- #
#
# The example-based round-trip/merge tests above pin known cases (Japanese
# text, a specific bytes_/None mix, a handful of ids). These generate whole
# manifests -- arbitrary text, missing fields, None-shaped ``bytes_``/
# ``bookmark_tags``/``error`` -- to check the to_dict/from_dict and merge
# contracts hold for any well-typed manifest, not just the ones written down.

_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.text(max_size=20),
)
_json_dict = st.dictionaries(st.text(max_size=10), _json_scalar, max_size=4)

_image_entries = st.builds(
    ImageEntry,
    page=st.integers(min_value=0, max_value=999),
    source_url=st.text(max_size=50),
    path=st.text(max_size=50),
    status=st.sampled_from([status.value for status in ImageStatus]),
    bytes_=st.one_of(st.none(), st.integers(min_value=0, max_value=10**9)),
    error=st.one_of(st.none(), st.text(max_size=50)),
)

_work_entries = st.builds(
    WorkEntry,
    id=st.integers(min_value=0, max_value=10**9),
    type=st.sampled_from(["illust", "manga", "ugoira"]),
    title=st.text(max_size=50),
    author=_json_dict,
    posted_at=st.text(max_size=30),
    nsfw=_json_dict,
    work_tags=st.lists(_json_dict, max_size=3),
    bookmark_tags=st.one_of(st.none(), st.lists(st.text(max_size=20), max_size=3)),
    page_count=st.integers(min_value=0, max_value=20),
    images=st.lists(_image_entries, max_size=3),
)

_manifests = st.builds(
    Manifest,
    schema_version=st.integers(min_value=0, max_value=10),
    generated_at=st.text(max_size=30),
    source=_json_dict,
    works=st.lists(_work_entries, max_size=3),
    errors=st.lists(_json_dict, max_size=3),
)

# Errors shaped like real ``{"work_id": ..., "message": ...}`` records, needed
# to exercise merge()'s work_id-keyed union rule precisely.
_merge_errors = st.lists(
    st.fixed_dictionaries(
        {
            "work_id": st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
            "message": st.text(max_size=20),
        }
    ),
    max_size=5,
)


@given(image=_image_entries)
def test_image_entry_round_trip_property(image: ImageEntry) -> None:
    assert ImageEntry.from_dict(image.to_dict()) == image


@given(work=_work_entries)
def test_work_entry_round_trip_property(work: WorkEntry) -> None:
    assert WorkEntry.from_dict(work.to_dict()) == work


@given(manifest=_manifests)
def test_manifest_round_trip_via_json_property(manifest: Manifest) -> None:
    # Through real JSON text (not just to_dict/from_dict) so unicode/None
    # handling is exercised the same way write_atomic/load_or_init use it.
    text = json.dumps(manifest.to_dict(), ensure_ascii=False)
    restored = Manifest.from_dict(json.loads(text))
    assert restored == manifest


@given(
    existing_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id),
    new_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id),
)
def test_merge_result_ids_are_union_with_no_duplicates(
    existing_works: list[WorkEntry], new_works: list[WorkEntry]
) -> None:
    existing = _manifest_with(existing_works)
    incoming = _manifest_with(new_works)

    result = merge(existing, incoming)

    result_ids = [work.id for work in result.works]
    expected_ids = {work.id for work in existing_works} | {
        work.id for work in new_works
    }
    assert set(result_ids) == expected_ids
    assert len(result_ids) == len(expected_ids)


@given(
    existing_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id),
    new_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id),
)
def test_merge_preserves_existing_order_then_appends_new_only(
    existing_works: list[WorkEntry], new_works: list[WorkEntry]
) -> None:
    existing = _manifest_with(existing_works)
    incoming = _manifest_with(new_works)

    result = merge(existing, incoming)

    existing_ids = [work.id for work in existing_works]
    existing_id_set = set(existing_ids)
    new_only_ids = [work.id for work in new_works if work.id not in existing_id_set]
    assert [work.id for work in result.works] == existing_ids + new_only_ids


@given(
    existing_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id),
    new_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id),
)
def test_merge_new_record_wins_for_shared_ids(
    existing_works: list[WorkEntry], new_works: list[WorkEntry]
) -> None:
    existing = _manifest_with(existing_works)
    incoming = _manifest_with(new_works)

    result = merge(existing, incoming)

    new_by_id = {work.id: work for work in new_works}
    existing_by_id = {work.id: work for work in existing_works}
    result_by_id = {work.id: work for work in result.works}

    # An id present in `new` always takes the new record...
    for work_id, new_work in new_by_id.items():
        assert result_by_id[work_id] == new_work
    # ...and an id present only in `existing` survives completely unchanged.
    for work_id, old_work in existing_by_id.items():
        if work_id not in new_by_id:
            assert result_by_id[work_id] == old_work


@given(existing_works=st.lists(_work_entries, max_size=5, unique_by=lambda w: w.id))
def test_merge_with_empty_new_preserves_works_but_takes_new_metadata(
    existing_works: list[WorkEntry],
) -> None:
    existing = _manifest_with(existing_works)
    empty_new = new_manifest(
        user_id="new-user",
        visibility="private",
        generated_at="2099-01-01T00:00:00+00:00",
        works=[],
        errors=[],
    )

    result = merge(existing, empty_new)

    assert result.works == existing_works
    assert result.generated_at == "2099-01-01T00:00:00+00:00"
    assert result.source == {"user_id": "new-user", "visibility": "private"}


@given(existing_errors=_merge_errors, new_errors=_merge_errors)
def test_merge_errors_union_by_work_id_property(
    existing_errors: list[dict[str, object]], new_errors: list[dict[str, object]]
) -> None:
    existing = _manifest_with([], errors=existing_errors)
    incoming = _manifest_with([], errors=new_errors)

    result = merge(existing, incoming)

    new_ids = {error["work_id"] for error in new_errors if error["work_id"] is not None}
    kept_existing = [
        error
        for error in existing_errors
        if error["work_id"] is None or error["work_id"] not in new_ids
    ]
    assert result.errors == kept_existing + new_errors
