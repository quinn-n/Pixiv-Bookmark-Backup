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
    FETCH_PAGE_BASE_DELAY,
    FETCH_PAGE_MAX_RETRIES,
    ApiError,
    AuthRejected,
    FollowedUser,
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


def _make_user_preview(
    user_id: int,
    *,
    name: str = "Followed",
    account: str = "followed_acct",
) -> SimpleNamespace:
    """Build a ``user_previews`` entry as returned by ``user_following``."""

    return SimpleNamespace(user=SimpleNamespace(id=user_id, name=name, account=account))


class FakeApi:
    """Minimal stand-in for ``pixivpy3.AppPixivAPI`` used in tests.

    Records calls and returns canned attribute-bag responses. Individual tests
    tweak the public attributes (``pages``, ``bookmark_detail_response``, etc.)
    to drive specific behaviours. The ``user_following`` and ``user_illusts``
    endpoints keep their own pagination state, independent of the bookmark
    pagination, so tests for one never cross-contaminate another.
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
        # Follow-list pagination: (user_previews, next_url) tuples consumed in
        # order as ``user_following`` is re-invoked.
        self.following_pages: list[tuple[list[SimpleNamespace], str | None]] = []
        self._following_index = 0
        self.following_calls: list[dict[str, Any]] = []
        # User-works pagination: (illusts, next_url) tuples consumed in order
        # as ``user_illusts`` is re-invoked.
        self.works_pages: list[tuple[list[SimpleNamespace], str | None]] = []
        self._works_index = 0
        self.works_calls: list[dict[str, Any]] = []
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

    # -- following ------------------------------------------------------

    def user_following(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.following_calls.append({"args": args, "kwargs": kwargs})
        previews, next_url = self.following_pages[self._following_index]
        self._following_index += 1
        return SimpleNamespace(user_previews=previews, next_url=next_url)

    # -- user works -----------------------------------------------------

    def user_illusts(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.works_calls.append({"args": args, "kwargs": kwargs})
        illusts, next_url = self.works_pages[self._works_index]
        self._works_index += 1
        return SimpleNamespace(illusts=illusts, next_url=next_url)

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


class _ExpiringBookmarkApi(FakeApi):
    """FakeApi whose bookmark listing returns an error-shaped page until re-auth.

    Models Pixiv's expired-token behaviour: fetching the page at ``error_index``
    returns ``{"error": {...}}`` (as a SimpleNamespace with an ``error`` attr)
    until ``auth`` is re-invoked, at which point the *same* page index yields its
    real illusts. Crucially, the page index is not advanced on an error return,
    so a correct retry re-fetches the same page (no silently skipped page).
    """

    def __init__(self, *, error_index: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._error_index = error_index
        self._authed = False

    def auth(self, *, refresh_token: str) -> SimpleNamespace:
        self._authed = True
        return super().auth(refresh_token=refresh_token)

    def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.bookmark_calls.append({"args": args, "kwargs": kwargs})
        if self._page_index == self._error_index and not self._authed:
            # Token expired: error body, page index NOT advanced.
            return SimpleNamespace(
                error=SimpleNamespace(user_message="", message="invalid access token"),
                illusts=None,
                next_url=None,
            )
        illusts, next_url = self.pages[self._page_index]
        self._page_index += 1
        self._authed = False  # token can expire again on a later page
        return SimpleNamespace(illusts=illusts, next_url=next_url)


def test_iter_bookmarks_reauths_and_continues_on_expired_token() -> None:
    # Regression: an expired-token error mid-pagination previously looked like a
    # clean end-of-data (JsonDict.__getattr__ returns None for missing illusts/
    # next_url), silently truncating the run. It must re-authenticate and finish.
    api = _ExpiringBookmarkApi(user_id=SELF_ID, error_index=1)
    api.pages = [
        ([_make_illust(1, single_url="https://x/1.png")], "https://next?page=2"),
        ([_make_illust(2, single_url="https://x/2.png")], "https://next?page=3"),
        ([_make_illust(3, single_url="https://x/3.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    # Cache the refresh token via the normal initial auth.
    client.authenticate("refresh-secret")
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    # All three pages backed up, none skipped or truncated.
    assert [w.id for w in works] == [1, 2, 3]
    # Re-auth happened (initial + one recovery), bounded (not an infinite loop).
    assert api.auth_calls == ["refresh-secret", "refresh-secret"]


def test_iter_bookmarks_reauths_more_than_once_across_pages() -> None:
    # A large run can outlive the token more than once; recovery must be per-page,
    # not a single global one-shot.
    class TwiceExpiringApi(FakeApi):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._expire_at = {1, 2}
            self._authed_for: int | None = None

        def auth(self, *, refresh_token: str) -> SimpleNamespace:
            self._authed_for = self._page_index
            return super().auth(refresh_token=refresh_token)

        def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.bookmark_calls.append({"args": args, "kwargs": kwargs})
            if self._page_index in self._expire_at and (
                self._authed_for != self._page_index
            ):
                return SimpleNamespace(
                    error=SimpleNamespace(message="invalid access token"),
                    illusts=None,
                    next_url=None,
                )
            illusts, next_url = self.pages[self._page_index]
            self._page_index += 1
            return SimpleNamespace(illusts=illusts, next_url=next_url)

    api = TwiceExpiringApi(user_id=SELF_ID)
    api.pages = [
        ([_make_illust(1, single_url="https://x/1.png")], "https://n?p=2"),
        ([_make_illust(2, single_url="https://x/2.png")], "https://n?p=3"),
        ([_make_illust(3, single_url="https://x/3.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    client.authenticate("refresh-secret")
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert [w.id for w in works] == [1, 2, 3]
    # Initial auth + one recovery on page 1 + one recovery on page 2.
    assert api.auth_calls == ["refresh-secret"] * 3


def test_iter_bookmarks_raises_apierror_when_error_persists_after_reauth() -> None:
    # If the error recurs even after re-auth, the client must raise ApiError
    # (so backup.py's partial-run handling kicks in) rather than loop forever or
    # silently swallow it. Recovery is now bounded by FETCH_PAGE_MAX_RETRIES.
    class PersistentErrorApi(FakeApi):
        def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.bookmark_calls.append({"args": args, "kwargs": kwargs})
            if self._page_index == 0:
                self._page_index += 1
                return SimpleNamespace(
                    illusts=[_make_illust(1, single_url="https://x/1.png")],
                    next_url="https://next?page=2",
                )
            # Every subsequent fetch errors, even right after re-auth.
            return SimpleNamespace(
                error=SimpleNamespace(message="server error"),
                illusts=None,
                next_url=None,
            )

    api = PersistentErrorApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    client.authenticate("refresh-secret")
    with pytest.raises(ApiError) as excinfo:
        list(client.iter_bookmarks(SELF_ID, "public"))
    # Initial auth + FETCH_PAGE_MAX_RETRIES recovery re-auths before giving up,
    # proving a bounded (not infinite) retry loop.
    assert api.auth_calls == ["refresh-secret"] * (1 + FETCH_PAGE_MAX_RETRIES)
    # The final give-up message must not embed raw Pixiv error text.
    assert "server error" not in str(excinfo.value)


def test_iter_bookmarks_reauths_after_transient_reauth_network_errors() -> None:
    # Field case: the error-shaped page fires, but the *re-auth* POST to
    # oauth.secure.pixiv.net gets its connection reset a couple of times before
    # succeeding. The recovery loop must back off and keep retrying the re-auth,
    # then finish the listing -- not abort after a single failed re-auth.
    class ResetOnReauthApi(FakeApi):
        def __init__(self, *, reset_times: int, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._reset_times = reset_times
            self._authed_page = False

        def auth(self, *, refresh_token: str) -> SimpleNamespace:
            # The initial authenticate() call (page_index 0) always succeeds;
            # only the mid-pagination recovery re-auths get reset, reset_times
            # times, before one finally succeeds.
            if self._page_index >= 1 and self._reset_times > 0:
                self._reset_times -= 1
                self.auth_calls.append(refresh_token)
                raise PixivError(
                    "requests POST https://oauth.secure.pixiv.net/auth/token "
                    "error: ('Connection aborted.', "
                    "ConnectionResetError(104, 'Connection reset by peer'))"
                )
            self._authed_page = True
            return super().auth(refresh_token=refresh_token)

        def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.bookmark_calls.append({"args": args, "kwargs": kwargs})
            if self._page_index == 1 and not self._authed_page:
                return SimpleNamespace(
                    error=SimpleNamespace(message="invalid access token"),
                    illusts=None,
                    next_url=None,
                )
            illusts, next_url = self.pages[self._page_index]
            self._page_index += 1
            self._authed_page = False
            return SimpleNamespace(illusts=illusts, next_url=next_url)

    naps: list[float] = []
    rolls: list[float] = []

    def _rand() -> float:
        rolls.append(0.5)
        return 0.5

    api = ResetOnReauthApi(user_id=SELF_ID, reset_times=2)
    api.pages = [
        ([_make_illust(1, single_url="https://x/1.png")], "https://n?p=2"),
        ([_make_illust(2, single_url="https://x/2.png")], "https://n?p=3"),
        ([_make_illust(3, single_url="https://x/3.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=naps.append, delay=0, rand=_rand)
    client.authenticate("refresh-secret")
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    # The listing completed despite two connection-reset re-auth failures.
    assert [w.id for w in works] == [1, 2, 3]
    # Initial auth + two failed recovery re-auths + one successful recovery.
    assert api.auth_calls == ["refresh-secret"] * 4
    # rand() is only called on the backoff path, so its call count is a clean
    # signal that backoff ran once per recovery attempt (two resets plus the
    # successful re-auth = 3).
    assert len(rolls) == 3
    # The backoff sleeps (jitter fixed at 0.5) show exponential growth per
    # attempt 0..2, proving backoff was applied and grew between retries. The
    # zero-second inter-page politeness sleeps (delay=0) are filtered out.
    backoff_naps = [nap for nap in naps if nap > 0]
    assert backoff_naps == [
        FETCH_PAGE_BASE_DELAY * (2**attempt) + 0.5 * FETCH_PAGE_BASE_DELAY
        for attempt in range(3)
    ]


def test_iter_bookmarks_raises_static_message_when_reauth_network_error_persists() -> (
    None
):
    # If the re-auth POST keeps getting its connection reset past the retry
    # budget, the client raises ApiError (bounded) whose message describes a
    # network error but never embeds the raw PixivError/response text.
    reset_text = "Connection reset by peer secret-endpoint-detail"

    class AlwaysResetOnReauthApi(FakeApi):
        def auth(self, *, refresh_token: str) -> SimpleNamespace:
            if self._page_index >= 1:
                self.auth_calls.append(refresh_token)
                raise PixivError(reset_text)
            return super().auth(refresh_token=refresh_token)

        def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.bookmark_calls.append({"args": args, "kwargs": kwargs})
            if self._page_index == 0:
                self._page_index += 1
                return SimpleNamespace(
                    illusts=[_make_illust(1, single_url="https://x/1.png")],
                    next_url="https://n?p=2",
                )
            return SimpleNamespace(
                error=SimpleNamespace(message="invalid access token"),
                illusts=None,
                next_url=None,
            )

    api = AlwaysResetOnReauthApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    client.authenticate("refresh-secret")
    with pytest.raises(ApiError) as excinfo:
        list(client.iter_bookmarks(SELF_ID, "public"))
    # Bounded: initial auth + exactly FETCH_PAGE_MAX_RETRIES recovery re-auths.
    assert api.auth_calls == ["refresh-secret"] * (1 + FETCH_PAGE_MAX_RETRIES)
    message = str(excinfo.value)
    # The give-up message names the failure kind but never leaks raw Pixiv text.
    assert reset_text not in message
    assert "network error" in message


def test_iter_bookmarks_error_without_refresh_token_raises_apierror() -> None:
    # If an error page appears but no refresh token was cached (authenticate was
    # never called on this client), there is nothing to re-auth with: raise
    # ApiError rather than silently truncate.
    class ImmediateErrorApi(FakeApi):
        def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.bookmark_calls.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(
                error=SimpleNamespace(message="invalid access token"),
                illusts=None,
                next_url=None,
            )

    api = ImmediateErrorApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(ApiError):
        list(client.iter_bookmarks(SELF_ID, "public"))
    assert api.auth_calls == []


# -- unparsable-next_url diagnostics ------------------------------------


class _JsonDict(dict):  # type: ignore[type-arg]
    """Faithful copy of ``pixivpy3.utils.JsonDict``.

    A ``dict`` subclass whose attribute access reads the underlying key
    (returning ``None`` for a missing one). The production diagnostics inspect
    both ``result.<attr>`` and ``result.keys()``, so a double for the field-bug
    path must be dict-like *and* attribute-accessible -- exactly this shape,
    which ``SimpleNamespace`` (no ``keys()``) and a plain ``dict`` (no attribute
    access) each only half-satisfy.
    """

    def __getattr__(self, attr: str) -> Any:
        return self.get(attr)


class _DictResultApi(FakeApi):
    """FakeApi whose bookmark pages are ``JsonDict``-shaped (like real Pixiv).

    Real ``pixivpy3`` responses subclass ``dict``, so ``result.keys()`` works
    and missing attributes read back as ``None``. This double returns the same
    shape so it faithfully exercises the ``list(result.keys())`` diagnostic
    branch that fires in the field.
    """

    def user_bookmarks_illust(self, *args: Any, **kwargs: Any) -> Any:
        self.bookmark_calls.append({"args": args, "kwargs": kwargs})
        illusts, next_url = self.pages[self._page_index]
        self._page_index += 1
        return _JsonDict(illusts=illusts, next_url=next_url)


def _full_page(start: int) -> list[SimpleNamespace]:
    """Return a full Pixiv page (30 illusts) with sequential ids from ``start``."""

    return [
        _make_illust(i, single_url=f"https://x/{i}.png")
        for i in range(start, start + 30)
    ]


def test_iter_bookmarks_no_warn_on_full_page_then_empty_null_next_url() -> None:
    # Regression for the false-positive field report: an account whose total is
    # an exact multiple of the 30-item page size terminates as N full pages
    # followed by a genuinely empty page with next_url None -- Pixiv's standard
    # "no more data" signal. A falsy next_url is always a normal end and must
    # never warn, regardless of the previous page's item count.
    warnings: list[str] = []
    api = _DictResultApi(user_id=SELF_ID)
    api.pages = [
        (_full_page(1), "https://next?page=2"),
        (_full_page(31), "https://next?page=3"),
        (_full_page(61), "https://next?page=4"),
        ([], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    # Everything the API offered was yielded, no exception, and no false warning.
    assert [w.id for w in works] == list(range(1, 91))
    assert warnings == []


def test_iter_bookmarks_no_warn_when_full_page_has_falsy_next_url_directly() -> None:
    # A single full page whose next_url is already None (no trailing empty page)
    # is also a normal end: a falsy next_url is Pixiv's unambiguous end-of-data
    # signal and must never be flagged, whatever the page's item count.
    warnings: list[str] = []
    api = _DictResultApi(user_id=SELF_ID)
    api.pages = [(_full_page(1), None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert len(works) == 30
    assert warnings == []


def test_iter_bookmarks_no_warn_on_normal_short_final_page() -> None:
    # The ordinary happy path: the final page holds fewer than a full page of
    # bookmarks. That is a genuine end-of-data and must not warn (no spam).
    warnings: list[str] = []
    api = _DictResultApi(user_id=SELF_ID)
    api.pages = [
        (_full_page(1), "https://next?page=2"),
        ([_make_illust(99, single_url="https://x/99.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert len(works) == 31
    assert warnings == []


def test_iter_bookmarks_no_warn_on_empty_account() -> None:
    # A zero-bookmark account's first page is empty with next_url None. That is a
    # normal end (short/empty final page), not a suspicious truncation.
    warnings: list[str] = []
    api = _DictResultApi(user_id=SELF_ID)
    api.pages = [([], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    assert list(client.iter_bookmarks(SELF_ID, "public")) == []
    assert warnings == []


def test_iter_bookmarks_no_warn_on_successful_reauth_recovery() -> None:
    # A recovered (re-authed) page is normal-shaped, so the diagnostic warning
    # must not fire on a successful recovery -- the recovery stays silent and the
    # final short page ends the run cleanly.
    warnings: list[str] = []
    api = _ExpiringBookmarkApi(user_id=SELF_ID, error_index=1)
    api.pages = [
        (_full_page(1), "https://next?page=2"),
        ([_make_illust(200, single_url="https://x/200.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    client.authenticate("refresh-secret")
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert [w.id for w in works] == list(range(1, 31)) + [200]
    assert warnings == []


def test_iter_bookmarks_warns_when_parse_qs_returns_none_on_full_page() -> None:
    # A full page followed by a truthy next_url whose query string cannot be
    # parsed stops pagination early: Pixiv said there is more but we cannot get
    # to it. That has no benign interpretation, so it warns.
    warnings: list[str] = []

    class UnparsableDictApi(_DictResultApi):
        def parse_qs(self, next_url: str | None) -> dict[str, Any] | None:
            return None

    api = UnparsableDictApi(user_id=SELF_ID)
    api.pages = [(_full_page(1), "https://next?page=2")]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert len(works) == 30
    assert len(warnings) == 1
    assert "could not be parsed" in warnings[0]


def test_iter_bookmarks_warns_when_parse_qs_returns_none_on_short_page() -> None:
    # The unparsable-next_url warning is not gated on page size: even a short
    # final page whose truthy next_url cannot be parsed warns, because a genuine
    # end-of-data response never carries a next_url at all.
    warnings: list[str] = []

    class UnparsableDictApi(_DictResultApi):
        def parse_qs(self, next_url: str | None) -> dict[str, Any] | None:
            return None

    api = UnparsableDictApi(user_id=SELF_ID)
    api.pages = [
        ([_make_illust(1, single_url="https://x/1.png")], "https://next?page=2")
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert len(works) == 1
    assert len(warnings) == 1
    assert "could not be parsed" in warnings[0]


def test_warn_unparsable_next_url_reports_error_shape_when_present() -> None:
    # The response-shape sketch surfaces an ``error`` body when present (over the
    # top-level keys). Exercise it directly: a result carrying an error surfaces
    # that error content in the diagnostic.
    warnings: list[str] = []
    api = FakeApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    result = SimpleNamespace(error=SimpleNamespace(message="mystery"))
    client._warn_unparsable_next_url(
        result,
        pages_so_far=5,
        items_so_far=150,
        context="Bookmark listing",
    )
    assert len(warnings) == 1
    assert "error=" in warnings[0]
    assert "mystery" in warnings[0]


def test_warn_defaults_to_noop_and_diagnostics_do_not_crash() -> None:
    # The default no-op warn must not break the diagnostic path (no wiring means
    # no output, but also no exception). Exercise the one surviving trigger: a
    # full page with a truthy but unparsable next_url.
    class UnparsableDictApi(_DictResultApi):
        def parse_qs(self, next_url: str | None) -> dict[str, Any] | None:
            return None

    api = UnparsableDictApi(user_id=SELF_ID)
    api.pages = [(_full_page(1), "https://next?page=2")]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    assert len(list(client.iter_bookmarks(SELF_ID, "public"))) == 30


# -- iter_following -----------------------------------------------------


def test_followed_user_rejects_non_int_id() -> None:
    # The int validator on `id` is load-bearing: `str(id)` becomes a per-author
    # output subdirectory, so a non-int (e.g. a string) must be rejected at
    # construction rather than risk a path component carrying a separator.
    with pytest.raises(TypeError):
        FollowedUser(id="x", name="n", account="a")  # type: ignore[arg-type]


def test_iter_following_yields_followed_users() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.following_pages = [
        (
            [
                _make_user_preview(11, name="Alice", account="alice"),
                _make_user_preview(22, name="Bob", account="bob"),
            ],
            None,
        )
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    followed = list(client.iter_following(SELF_ID, "public"))
    assert all(isinstance(user, FollowedUser) for user in followed)
    assert [(u.id, u.name, u.account) for u in followed] == [
        (11, "Alice", "alice"),
        (22, "Bob", "bob"),
    ]


def test_iter_following_paginates_and_sleeps_between_pages() -> None:
    naps: list[float] = []
    api = FakeApi(user_id=SELF_ID)
    api.following_pages = [
        ([_make_user_preview(11)], "https://next?page=2"),
        ([_make_user_preview(22)], None),
    ]
    client = Pixivpy3Client(api=api, sleep=naps.append, delay=0.25)
    followed = list(client.iter_following(SELF_ID, "public"))
    assert [u.id for u in followed] == [11, 22]
    # Two pages fetched; second via parsed query-string kwargs.
    assert len(api.following_calls) == 2
    assert api.following_calls[1]["kwargs"].get("user_id") == SELF_ID
    # Exactly one inter-page pause for two pages.
    assert naps == [0.25]


def test_iter_following_forwards_restrict() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.following_pages = [([], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    list(client.iter_following(SELF_ID, "private"))
    assert api.following_calls[0]["args"] == (SELF_ID, "private")


def test_iter_following_forwards_public_restrict() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.following_pages = [([], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    list(client.iter_following(SELF_ID, "public"))
    assert api.following_calls[0]["args"] == (SELF_ID, "public")


def test_iter_following_empty_yields_nothing() -> None:
    for previews in ([], None):
        api = FakeApi(user_id=SELF_ID)
        # None models Pixiv returning no `user_previews`; exercises the `or []` guard.
        api.following_pages = [(previews, None)]  # type: ignore[list-item]
        client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
        assert list(client.iter_following(SELF_ID, "public")) == []


def test_iter_following_reauths_and_continues_on_expired_token() -> None:
    # The expired-token recovery is shared across all three paginated listings;
    # this confirms it is wired into iter_following too (not just iter_bookmarks).
    class ExpiringFollowingApi(FakeApi):
        # The token "expires" when page index 1 is first reached: the page errors
        # until a *further* auth() call re-authenticates. Gating on the auth-call
        # count (rather than a bool flag) ignores the initial authenticate() and
        # only recovers once a re-auth happens during iteration.
        def user_following(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            self.following_calls.append({"args": args, "kwargs": kwargs})
            if self._following_index == 1 and len(self.auth_calls) < 2:
                return SimpleNamespace(
                    error=SimpleNamespace(message="invalid access token"),
                    user_previews=None,
                    next_url=None,
                )
            previews, next_url = self.following_pages[self._following_index]
            self._following_index += 1
            return SimpleNamespace(user_previews=previews, next_url=next_url)

    api = ExpiringFollowingApi(user_id=SELF_ID)
    api.following_pages = [
        ([_make_user_preview(11)], "https://next?page=2"),
        ([_make_user_preview(22)], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    client.authenticate("refresh-secret")
    followed = list(client.iter_following(SELF_ID, "public"))
    assert [u.id for u in followed] == [11, 22]
    assert api.auth_calls == ["refresh-secret", "refresh-secret"]


def test_iter_following_warns_when_parse_qs_returns_none() -> None:
    # A truthy next_url whose query string cannot be parsed ends pagination
    # rather than looping forever -- and warns, because Pixiv said there was more
    # data but it could not be reached. The diagnostic is wired here too.
    warnings: list[str] = []

    class UnparsableApi(FakeApi):
        def parse_qs(self, next_url: str | None) -> dict[str, Any] | None:
            return None

    api = UnparsableApi(user_id=SELF_ID)
    api.following_pages = [([_make_user_preview(11)], "https://next?page=2")]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    followed = list(client.iter_following(SELF_ID, "public"))
    assert [u.id for u in followed] == [11]
    assert len(api.following_calls) == 1
    assert len(warnings) == 1
    assert "Following listing" in warnings[0]
    assert "could not be parsed" in warnings[0]


def test_iter_following_no_warn_on_full_page_then_null_next_url() -> None:
    # A falsy next_url is a normal end even after a full page: no false warning.
    warnings: list[str] = []
    api = FakeApi(user_id=SELF_ID)
    api.following_pages = [
        ([_make_user_preview(i) for i in range(30)], "https://next?page=2"),
        ([], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    followed = list(client.iter_following(SELF_ID, "public"))
    assert len(followed) == 30
    assert warnings == []


def test_iter_following_does_not_warn_on_normal_short_page() -> None:
    warnings: list[str] = []
    api = FakeApi(user_id=SELF_ID)
    api.following_pages = [([_make_user_preview(11), _make_user_preview(22)], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    list(client.iter_following(SELF_ID, "public"))
    assert warnings == []


def test_iter_following_wraps_pixiverror_in_apierror() -> None:
    class BoomApi(FakeApi):
        def user_following(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise PixivError("following exploded")

    api = BoomApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(ApiError):
        list(client.iter_following(SELF_ID, "public"))


# -- iter_user_works ----------------------------------------------------


def test_iter_user_works_returns_all_kinds_from_single_pass() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.works_pages = [
        (
            [
                _make_illust(1, work_type="illust", single_url="https://x/1.png"),
                _make_illust(2, work_type="manga", single_url="https://x/2.png"),
                _make_illust(3, work_type="ugoira", single_url="https://x/3.png"),
            ],
            None,
        )
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    works = list(client.iter_user_works(OTHER_ID))
    assert all(isinstance(work, BookmarkWork) for work in works)
    assert [w.type for w in works] == ["illust", "manga", "ugoira"]
    assert [w.id for w in works] == [1, 2, 3]


def test_iter_user_works_forwards_empty_string_type() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.works_pages = [([], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    list(client.iter_user_works(OTHER_ID))
    assert api.works_calls[0]["args"] == (OTHER_ID, "")


def test_iter_user_works_maps_ugoira_type() -> None:
    api = FakeApi(user_id=SELF_ID)
    api.works_pages = [
        ([_make_illust(9, work_type="ugoira", single_url="https://x/9.png")], None)
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_user_works(OTHER_ID)))
    assert work.type == "ugoira"


def test_iter_user_works_does_not_fetch_bookmark_tags() -> None:
    # Even when listing the authenticated user's own works, these are not the
    # caller's bookmarks: bookmark_tags stays None and the detail endpoint is
    # never consulted.
    api = FakeApi(user_id=SELF_ID)
    api.bookmark_detail_tags = [("favorite", True)]
    api.works_pages = [([_make_illust(40, single_url="https://x/40.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    work = next(iter(client.iter_user_works(SELF_ID)))
    assert work.bookmark_tags is None
    assert api.bookmark_detail_calls == []


def test_iter_user_works_paginates_and_sleeps_between_pages() -> None:
    naps: list[float] = []
    api = FakeApi(user_id=SELF_ID)
    api.works_pages = [
        ([_make_illust(1, single_url="https://x/1.png")], "https://next?page=2"),
        ([_make_illust(2, single_url="https://x/2.png")], None),
    ]
    client = Pixivpy3Client(api=api, sleep=naps.append, delay=0.25)
    works = list(client.iter_user_works(OTHER_ID))
    assert [w.id for w in works] == [1, 2]
    assert len(api.works_calls) == 2
    assert api.works_calls[1]["kwargs"].get("user_id") == SELF_ID
    assert naps == [0.25]


def test_iter_user_works_warns_when_parse_qs_returns_none() -> None:
    # A truthy next_url whose query string cannot be parsed ends pagination and
    # warns: Pixiv said there was more but it could not be reached.
    warnings: list[str] = []

    class UnparsableApi(FakeApi):
        def parse_qs(self, next_url: str | None) -> dict[str, Any] | None:
            return None

    api = UnparsableApi(user_id=SELF_ID)
    api.works_pages = [
        ([_make_illust(1, single_url="https://x/1.png")], "https://next?page=2")
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_user_works(OTHER_ID))
    assert [w.id for w in works] == [1]
    assert len(api.works_calls) == 1
    assert len(warnings) == 1
    assert f"Works listing for {OTHER_ID}" in warnings[0]
    assert "could not be parsed" in warnings[0]


def test_iter_user_works_no_warn_on_full_page_then_null_next_url() -> None:
    # A falsy next_url is a normal end even after a full page: no false warning.
    warnings: list[str] = []
    api = FakeApi(user_id=SELF_ID)
    api.works_pages = [
        (
            [_make_illust(i, single_url=f"https://x/{i}.png") for i in range(30)],
            "https://next?page=2",
        ),
        ([], None),
    ]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    works = list(client.iter_user_works(OTHER_ID))
    assert len(works) == 30
    assert warnings == []


def test_iter_user_works_does_not_warn_on_normal_short_page() -> None:
    warnings: list[str] = []
    api = FakeApi(user_id=SELF_ID)
    api.works_pages = [([_make_illust(1, single_url="https://x/1.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    list(client.iter_user_works(OTHER_ID))
    assert warnings == []


def test_iter_user_works_wraps_pixiverror_in_apierror() -> None:
    class BoomApi(FakeApi):
        def user_illusts(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            raise PixivError("works exploded")

    api = BoomApi(user_id=SELF_ID)
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    with pytest.raises(ApiError):
        list(client.iter_user_works(OTHER_ID))


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


class _ExpiringDetailApi(FakeApi):
    """FakeApi whose bookmark-detail endpoint mimics an expired-token error.

    Models Pixiv's expired-token behaviour on ``illust_bookmark_detail``: the
    call returns an ``{"error": {...}}``-shaped ``SimpleNamespace`` (whose missing
    ``bookmark_detail`` key resolves to ``None``, indistinguishable from "no tags"
    by attribute access alone) until ``auth`` has been re-invoked *for recovery*.
    The gate keys on ``len(auth_calls) >= 2`` -- initial ``authenticate`` plus one
    ``_fetch_page`` recovery -- so the initial auth cannot pre-satisfy it and the
    detail call genuinely errors until recovery re-auths.
    """

    def __init__(self, *, recover: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._recover = recover

    def illust_bookmark_detail(self, illust_id: int) -> SimpleNamespace:
        self.bookmark_detail_calls.append(illust_id)
        recovered = self._recover and len(self.auth_calls) >= 2
        if not recovered:
            # Token expired: error body with ``bookmark_detail`` resolving to
            # ``None`` -- mirrors pixivpy3's JsonDict, where a missing key on an
            # error response reads back as ``None`` (so a naive ``.bookmark_detail``
            # silently yields "no tags" instead of surfacing the failure).
            return SimpleNamespace(
                error=SimpleNamespace(user_message="", message="invalid access token"),
                bookmark_detail=None,
            )
        tags = [
            SimpleNamespace(name=name, is_registered=registered)
            for name, registered in self.bookmark_detail_tags
        ]
        return SimpleNamespace(bookmark_detail=SimpleNamespace(tags=tags))


def test_bookmark_tags_reauth_and_recovers_on_expired_token() -> None:
    # Regression: an expired-token error while fetching a work's personal bookmark
    # tags previously slipped past recovery -- illust_bookmark_detail was called
    # DIRECTLY (not via _fetch_page), so the error-shaped response's missing
    # bookmark_detail resolved to None and the tags came back silently empty with
    # no re-auth. Routing through _fetch_page must now re-authenticate and return
    # the real tags.
    api = _ExpiringDetailApi(user_id=SELF_ID)
    api.bookmark_detail_tags = [("favorite", True), ("noise", False), ("art", True)]
    api.pages = [([_make_illust(80, single_url="https://x/p0.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0)
    # Cache the refresh token via the normal initial auth.
    client.authenticate("refresh-secret")
    work = next(iter(client.iter_bookmarks(SELF_ID, "public")))
    # Real tags recovered, not a silent empty/None truncation.
    assert work.bookmark_tags == ["favorite", "art"]
    # Re-auth happened (initial + one recovery), bounded (not an infinite loop).
    assert api.auth_calls == ["refresh-secret", "refresh-secret"]


def test_bookmark_tags_degrade_to_none_and_warn_when_error_persists() -> None:
    # A single work whose bookmark-detail keeps erroring even after re-auth (e.g. a
    # deleted/restricted bookmark -- common on a large, long-lived account) must
    # NOT abort the whole run. _fetch_bookmark_tags catches _fetch_page's
    # exhausted-budget ApiError and degrades to None for just this work -- but only
    # AFTER the bounded re-auth/retry ran, and the skip is surfaced via warn (never
    # silent). A systemic token failure is still backstopped by the next listing
    # page's own _fetch_page; here the listing itself is healthy.
    warnings: list[str] = []
    api = _ExpiringDetailApi(user_id=SELF_ID, recover=False)
    api.bookmark_detail_tags = [("favorite", True)]
    api.pages = [([_make_illust(90, single_url="https://x/p0.png")], None)]
    client = Pixivpy3Client(api=api, sleep=_no_sleep, delay=0, warn=warnings.append)
    client.authenticate("refresh-secret")
    # The run completes: the work is still yielded, just without its tags.
    works = list(client.iter_bookmarks(SELF_ID, "public"))
    assert [w.id for w in works] == [90]
    assert works[0].bookmark_tags is None
    # Bounded recovery was still attempted -- initial auth + FETCH_PAGE_MAX_RETRIES
    # recovery re-auths -- proving "tried then gracefully gave up", NOT a reversion
    # to the original silent bug where recovery was never attempted at all.
    assert api.auth_calls == ["refresh-secret"] * (1 + FETCH_PAGE_MAX_RETRIES)
    # The skip is visible on the warn sink, names the work, and never leaks raw
    # Pixiv error text.
    assert len(warnings) == 1
    assert "90" in warnings[0]
    assert "invalid access token" not in warnings[0]


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
