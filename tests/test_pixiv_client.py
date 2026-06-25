"""Offline unit tests for the typed Pixiv client wrapper.

Every test injects a hand-rolled ``FakeApi`` (a plain object that mimics the
``pixivpy3.AppPixivAPI`` surface using :class:`types.SimpleNamespace` attribute
bags) so the suite never touches the network. ``PixivError`` is imported from
``pixivpy3`` purely to construct/raise it -- importing the exception class does
no network I/O.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pixivpy3 import PixivError

from pixiv_bookmark_backup.models import BookmarkWork, UgoiraInfo
from pixiv_bookmark_backup.pixiv_client import (
    DEFAULT_TIMEOUT,
    ApiError,
    AuthRejected,
    Pixivpy3Client,
)

SELF_ID = "12345"
OTHER_ID = "99999"


def _no_sleep(_seconds: float) -> None:
    """Sleep stub used to keep tests instant and deterministic."""


def _make_illust(
    illust_id: int,
    *,
    title: str = "Untitled",
    work_type: str = "illust",
    single_url: str | None = None,
    page_urls: list[str] | None = None,
    tags: list[tuple[str, str | None]] | None = None,
) -> SimpleNamespace:
    """Build a JsonDict-like illust object for the fake API.

    ``single_url`` populates ``meta_single_page`` (single-page works);
    ``page_urls`` populates ``meta_pages`` (multi-page works). Pixiv leaves the
    unused container empty, which is what we mimic here.
    """

    meta_pages = [
        SimpleNamespace(image_urls=SimpleNamespace(original=url))
        for url in (page_urls or [])
    ]
    tag_objs = [
        SimpleNamespace(name=name, translated_name=translated)
        for name, translated in (tags or [("scenery", "風景")])
    ]
    return SimpleNamespace(
        id=illust_id,
        title=title,
        type=work_type,
        user=SimpleNamespace(id=7, name="Artist", account="artist_acct"),
        create_date="2024-01-02T03:04:05+09:00",
        x_restrict=0,
        sanity_level=2,
        tags=tag_objs,
        page_count=len(page_urls) if page_urls else 1,
        meta_single_page=SimpleNamespace(original_image_url=single_url),
        meta_pages=meta_pages,
    )


class FakeApi:
    """Minimal stand-in for ``pixivpy3.AppPixivAPI`` used in tests.

    Records calls and returns canned attribute-bag responses. Individual tests
    tweak the public attributes (``pages``, ``bookmark_detail_response``, etc.)
    to drive specific behaviours.
    """

    def __init__(self, *, user_id: str = SELF_ID) -> None:
        self.user_id: str = user_id
        self.auth_calls: list[str] = []
        self.auth_should_raise = False
        # Bookmark pagination: list of (illusts, next_url) tuples consumed
        # in order as ``user_bookmarks_illust`` is re-invoked.
        self.pages: list[tuple[list[SimpleNamespace], str | None]] = []
        self._page_index = 0
        self.bookmark_calls: list[dict[str, Any]] = []
        # illust_bookmark_detail behaviour.
        self.bookmark_detail_tags: list[tuple[str, bool]] = []
        self.bookmark_detail_should_raise = False
        self.bookmark_detail_calls: list[int] = []
        # ugoira_metadata behaviour.
        self.ugoira_should_raise = False
        self.ugoira_frames: list[tuple[str, int]] = []
        self.ugoira_zip_url = "https://i.pximg.net/u/frames.zip"
        # download behaviour.
        self.download_should_raise = False
        self.download_bytes = b""
        self.download_calls: list[dict[str, Any]] = []

    # -- auth -----------------------------------------------------------

    def auth(self, *, refresh_token: str) -> SimpleNamespace:
        self.auth_calls.append(refresh_token)
        if self.auth_should_raise:
            raise PixivError("invalid_grant")
        return SimpleNamespace(response=SimpleNamespace())

    # -- bookmarks ------------------------------------------------------

    def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.bookmark_calls.append({"args": args, "kwargs": kwargs})
        illusts, next_url = self.pages[self._page_index]
        self._page_index += 1
        return SimpleNamespace(illusts=illusts, next_url=next_url)

    def parse_qs(self, next_url: str | None) -> dict[str, Any] | None:
        if not next_url:
            return None
        return {"user_id": SELF_ID, "restrict": "public", "max_bookmark_id": "1000"}

    def illust_bookmark_detail(self, illust_id: int) -> SimpleNamespace:
        self.bookmark_detail_calls.append(illust_id)
        if self.bookmark_detail_should_raise:
            raise PixivError("rate limited")
        tags = [
            SimpleNamespace(name=name, is_registered=registered)
            for name, registered in self.bookmark_detail_tags
        ]
        return SimpleNamespace(bookmark_detail=SimpleNamespace(tags=tags))

    # -- ugoira ---------------------------------------------------------

    def ugoira_metadata(self, illust_id: int) -> SimpleNamespace:
        if self.ugoira_should_raise:
            raise PixivError("ugoira boom")
        frames = [
            SimpleNamespace(file=file, delay=delay)
            for file, delay in self.ugoira_frames
        ]
        meta = SimpleNamespace(
            frames=frames,
            zip_urls=SimpleNamespace(medium=self.ugoira_zip_url),
        )
        return SimpleNamespace(ugoira_metadata=meta)

    # -- download -------------------------------------------------------

    def download(
        self,
        url: str,
        *,
        path: str,
        name: str,
        replace: bool,
        referer: str,
    ) -> bool:
        self.download_calls.append(
            {
                "url": url,
                "path": path,
                "name": name,
                "replace": replace,
                "referer": referer,
            }
        )
        if self.download_should_raise:
            raise PixivError("download failed")
        (Path(path) / name).write_bytes(self.download_bytes)
        return True


# -- default api construction -------------------------------------------


def test_default_api_carries_request_timeout() -> None:
    # Constructing the client with no injected api builds a real AppPixivAPI
    # (no network I/O at construction time) configured with DEFAULT_TIMEOUT, so
    # every request is bounded and a hung server cannot block forever.
    client = Pixivpy3Client()
    api: Any = client._api
    assert api.requests_kwargs.get("timeout") == DEFAULT_TIMEOUT


# -- authenticate -------------------------------------------------------


def test_authenticate_returns_self_id() -> None:
    api = FakeApi(user_id="555")
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    assert client.authenticate("secret-token") == "555"
    assert api.auth_calls == ["secret-token"]


def test_authenticate_rejects_bad_token() -> None:
    api = FakeApi()
    api.auth_should_raise = True
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(AuthRejected) as excinfo:
        client.authenticate("bad-token")
    # The secret must never appear in the error surfaced to users/logs.
    assert "bad-token" not in str(excinfo.value)


# -- iter_bookmarks pagination -----------------------------------------


def test_iter_bookmarks_paginates_two_pages_then_stops() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.pages = [
        ([_make_illust(1, single_url="https://x/p0.png")], "https://next?page=2"),
        ([_make_illust(2, single_url="https://x/p0.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert [w.id for w in works] == [1, 2]
    # First call positional; second call via parsed query string kwargs.
    assert len(api.bookmark_calls) == 2
    assert api.bookmark_calls[1]["kwargs"].get("user_id") == SELF_ID


def test_iter_bookmarks_sleeps_between_pages() -> None:
    naps: list[float] = []
    api = FakeApi(user_id=SELF_ID)
    api.pages = [
        ([_make_illust(1, single_url="https://x/p0.png")], "https://next?page=2"),
        ([_make_illust(2, single_url="https://x/p0.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=naps.append, delay=0.25)
    list(client.iter_bookmarks(SELF_ID, "public"))
    # Exactly one inter-page pause for two pages.
    assert naps == [0.25]


def test_iter_bookmarks_wraps_pixiverror_in_apierror() -> None:
    class BoomApi(FakeApi):
        def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise PixivError("listing exploded")

    api = BoomApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(ApiError):
        list(client.iter_bookmarks(SELF_ID, "public"))


# -- original-URL extraction -------------------------------------------


def test_single_page_yields_one_image_from_meta_single_page() -> None:
    api = FakeApi(user_id=OTHER_ID)
    api.pages = [
        ([_make_illust(10, single_url="https://i.pximg.net/10_p0.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_bookmarks(OTHER_ID, "public")))
    assert isinstance(work, BookmarkWork)
    assert len(work.images) == 1
    assert work.images[0].page == 0
    assert work.images[0].source_url == "https://i.pximg.net/10_p0.png"


def test_multi_page_yields_one_image_per_meta_page() -> None:
    urls = [
        "https://i.pximg.net/20_p0.png",
        "https://i.pximg.net/20_p1.png",
        "https://i.pximg.net/20_p2.png",
    ]
    api = FakeApi(user_id=OTHER_ID)
    api.pages = [([_make_illust(20, page_urls=urls)], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_bookmarks(OTHER_ID, "public")))
    assert [img.page for img in work.images] == [0, 1, 2]
    assert [img.source_url for img in work.images] == urls


def test_work_tags_are_mapped_with_translations() -> None:
    api = FakeApi(user_id=OTHER_ID)
    api.pages = [
        (
            [
                _make_illust(
                    30,
                    single_url="https://x/p0.png",
                    tags=[("original", "オリジナル"), ("untranslated", None)],
                )
            ],
            None,
        )
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_bookmarks(OTHER_ID, "public")))
    assert work.work_tags[0].name == "original"
    assert work.work_tags[0].translated_name == "オリジナル"
    assert work.work_tags[1].translated_name is None


# -- bookmark (personal) tags ------------------------------------------


def test_bookmark_tags_populated_for_self_with_only_registered() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.bookmark_detail_tags = [("favorite", True), ("noise", False), ("art", True)]
    api.pages = [([_make_illust(40, single_url="https://x/p0.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_bookmarks(SELF_ID, "public")))
    assert work.bookmark_tags == ["favorite", "art"]
    assert api.bookmark_detail_calls == [40]


def test_bookmark_tags_none_for_other_user() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.bookmark_detail_tags = [("favorite", True)]
    api.pages = [([_make_illust(50, single_url="https://x/p0.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_bookmarks(OTHER_ID, "public")))
    assert work.bookmark_tags is None
    # Detail endpoint is not consulted for a non-self target.
    assert api.bookmark_detail_calls == []


def test_bookmark_tags_none_when_detail_raises() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.bookmark_detail_should_raise = True
    api.pages = [([_make_illust(60, single_url="https://x/p0.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_bookmarks(SELF_ID, "public")))
    assert work.bookmark_tags is None


# -- ugoira_metadata ----------------------------------------------------


def test_ugoira_metadata_maps_frames_and_zip_url() -> None:
    api = FakeApi()
    api.ugoira_frames = [("000000.jpg", 100), ("000001.jpg", 120)]
    api.ugoira_zip_url = "https://i.pximg.net/ugoira/frames.zip"
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    info = client.ugoira_metadata(70)
    assert isinstance(info, UgoiraInfo)
    assert info.zip_url == "https://i.pximg.net/ugoira/frames.zip"
    assert [(f.file, f.delay) for f in info.frames] == [
        ("000000.jpg", 100),
        ("000001.jpg", 120),
    ]


def test_ugoira_metadata_wraps_pixiverror() -> None:
    api = FakeApi()
    api.ugoira_should_raise = True
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(ApiError):
        client.ugoira_metadata(71)


# -- download -----------------------------------------------------------


def test_download_writes_file_and_returns_byte_count(tmp_path: Path) -> None:
    api = FakeApi()
    payload = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    api.download_bytes = payload
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    dest = tmp_path / "nested" / "image_p0.png"
    written = client.download("https://i.pximg.net/img.png", dest)
    assert written == len(payload)
    assert dest.read_bytes() == payload
    call = api.download_calls[0]
    assert call["path"] == str(dest.parent)
    assert call["name"] == "image_p0.png"
    assert call["replace"] is True
    assert call["referer"] == "https://www.pixiv.net/"


def test_download_uses_custom_referer(tmp_path: Path) -> None:
    api = FakeApi()
    api.download_bytes = b"data"
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    dest = tmp_path / "f.bin"
    client.download("https://i.pximg.net/x", dest, referer="https://example.test/")
    assert api.download_calls[0]["referer"] == "https://example.test/"


def test_download_wraps_pixiverror(tmp_path: Path) -> None:
    api = FakeApi()
    api.download_should_raise = True
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(ApiError):
        client.download("https://i.pximg.net/x", tmp_path / "out.png")
