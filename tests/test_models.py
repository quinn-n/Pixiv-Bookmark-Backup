"""Unit tests for the shared typed records in ``models``."""

from __future__ import annotations

import pytest
from attrs.exceptions import FrozenInstanceError

from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    ImageStatus,
    UgoiraFrame,
    UgoiraInfo,
    WorkImage,
    WorkTag,
)


def _make_work() -> BookmarkWork:
    return BookmarkWork(
        id=123,
        type="illust",
        title="A work",
        author=Author(id=7, name="Artist", account="artist"),
        posted_at="2023-01-02T03:04:05+09:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[WorkTag(name="original")],
        bookmark_tags=["fav"],
        page_count=1,
        images=[WorkImage(page=0, source_url="https://example.test/0.png")],
    )


def test_image_status_is_str_valued() -> None:
    assert ImageStatus.DOWNLOADED.value == "downloaded"
    assert ImageStatus.DOWNLOADED == "downloaded"
    assert isinstance(ImageStatus.DOWNLOADED, str)
    assert f"{ImageStatus.METADATA_ONLY.value}" == "metadata-only"


def test_work_tag_translation_defaults_to_none() -> None:
    tag = WorkTag(name="original")
    assert tag.translated_name is None
    translated = WorkTag(name="女の子", translated_name="girl")
    assert translated.translated_name == "girl"


def test_records_are_constructible_and_have_expected_fields() -> None:
    work = _make_work()
    assert work.id == 123
    assert work.author.account == "artist"
    assert work.images[0].page == 0
    assert work.bookmark_tags == ["fav"]


def test_bookmark_tags_may_be_none() -> None:
    work = BookmarkWork(
        id=1,
        type="manga",
        title="t",
        author=Author(id=1, name="n", account="a"),
        posted_at="2023-01-01T00:00:00+00:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=0,
        images=[],
    )
    assert work.bookmark_tags is None


def test_ugoira_records_compose() -> None:
    info = UgoiraInfo(
        zip_url="https://example.test/u.zip",
        frames=[UgoiraFrame(file="000000.jpg", delay=120)],
    )
    assert info.frames[0].file == "000000.jpg"
    assert info.frames[0].delay == 120


def test_records_are_equal_by_value() -> None:
    assert Author(id=7, name="Artist", account="artist") == Author(
        id=7, name="Artist", account="artist"
    )
    assert WorkImage(page=0, source_url="u") == WorkImage(page=0, source_url="u")
    assert _make_work() == _make_work()


def test_records_are_frozen() -> None:
    author = Author(id=7, name="Artist", account="artist")
    with pytest.raises(FrozenInstanceError):
        author.name = "Changed"  # type: ignore[misc]

    work = _make_work()
    with pytest.raises(FrozenInstanceError):
        work.title = "Changed"  # type: ignore[misc]
