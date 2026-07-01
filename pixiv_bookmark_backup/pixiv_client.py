"""Typed wrapper around the Pixiv app API.

This is the **only** module that imports :mod:`pixivpy3`. The rest of the
codebase talks to Pixiv exclusively through the :class:`PixivClient` Protocol
and receives fully typed value objects from :mod:`pixiv_bookmark_backup.models`.

The third-party client returns ``JsonDict`` objects whose attribute access is
typed ``Any``. All such loosely typed access is confined to the private
``_to_*`` helpers below, so the public surface of this module is fully typed and
basedpyright-clean. The refresh token is never logged, printed, or embedded in
any exception message raised from here.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from attrs import field, frozen
from attrs.validators import instance_of
from pixivpy3 import AppPixivAPI, PixivError

from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    UgoiraFrame,
    UgoiraInfo,
    WorkImage,
    WorkTag,
)

#: Referer Pixiv expects when fetching original images from ``i.pximg.net``.
PIXIV_REFERER = "https://www.pixiv.net/"

#: Default ``(connect, read)`` timeout (seconds) applied to every Pixiv request.
#: ``pixivpy3``'s ``BasePixivAPI(**requests_kwargs)`` stores these kwargs and
#: forwards them to every ``requests`` call, so passing ``timeout=`` to
#: ``AppPixivAPI`` bounds both connect and read so a hung server cannot block the
#: run forever.
DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 60.0)


@frozen
class FollowedUser:
    """A Pixiv user followed by the authenticated account.

    ``id`` is validated as an ``int`` at the boundary: it is later stringified
    into the per-author output subdirectory (``output_dir / str(id)``), so
    enforcing the integer type guarantees the path component is purely numeric
    and can never carry a separator or ``..``.
    """

    id: int = field(validator=instance_of(int))
    name: str = field(validator=instance_of(str))
    account: str = field(validator=instance_of(str))


class AuthRejected(Exception):
    """Pixiv refused the supplied refresh token.

    The CLI maps this to exit code 3. The message intentionally never contains
    the refresh token, so it is safe to surface to the user and logs.
    """


class ApiError(Exception):
    """A Pixiv API call failed (network error, unexpected response, etc.).

    The CLI maps this to exit code 4.
    """


@runtime_checkable
class PixivClient(Protocol):
    """The narrow Pixiv surface the rest of the tool depends on.

    Implementations translate Pixiv's loosely typed JSON into the frozen value
    objects from :mod:`pixiv_bookmark_backup.models` and raise the typed
    :class:`AuthRejected` / :class:`ApiError` exceptions instead of leaking
    third-party error types.
    """

    def authenticate(self, refresh_token: str) -> str:
        """Authenticate and return the authenticated user's own id."""
        ...

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        """Yield every bookmarked work for ``user_id`` at the given visibility."""
        ...

    def iter_following(self, user_id: str, restrict: str) -> Iterator[FollowedUser]:
        """Yield every user ``user_id`` follows at the given visibility."""
        ...

    def iter_user_works(self, user_id: str) -> Iterator[BookmarkWork]:
        """Yield every work (illust, manga, and ugoira) posted by ``user_id``."""
        ...

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:
        """Return ugoira frame metadata and the frames-zip URL for ``work_id``."""
        ...

    def download(self, url: str, dest_path: Path, referer: str = PIXIV_REFERER) -> int:
        """Download ``url`` to ``dest_path`` in one attempt, returning bytes written."""
        ...


class _AppApi(Protocol):
    """Internal structural view of the ``pixivpy3`` methods we call.

    Used purely as a ``cast`` target so attribute access on the injected client
    is typed (returning ``Any`` exactly where Pixiv's ``JsonDict`` does). The
    real :class:`~pixivpy3.AppPixivAPI` satisfies this at runtime; the cast keeps
    the call sites typed without spreading ``Any`` across the public surface.
    """

    user_id: int | str

    def auth(self, *, refresh_token: str) -> Any: ...

    def user_bookmarks_illust(self, user_id: int | str, restrict: str) -> Any: ...

    def user_following(self, user_id: int | str, restrict: str) -> Any: ...

    def user_illusts(self, user_id: int | str, type: str) -> Any: ...

    def parse_qs(self, next_url: str | None) -> dict[str, Any] | None: ...

    def illust_bookmark_detail(self, illust_id: int) -> Any: ...

    def ugoira_metadata(self, illust_id: int) -> Any: ...

    def download(
        self,
        url: str,
        *,
        path: str,
        name: str,
        replace: bool,
        referer: str,
    ) -> bool: ...


class Pixivpy3Client:
    """:class:`PixivClient` backed by :class:`pixivpy3.AppPixivAPI`.

    All Pixiv interaction funnels through this class. The underlying ``api`` and
    the ``sleep`` function are injectable so tests can run fully offline and
    deterministically. ``delay`` is the politeness pause (seconds) taken between
    bookmark pages while paginating.

    When no ``api`` is injected, the default :class:`~pixivpy3.AppPixivAPI` is
    constructed with :data:`DEFAULT_TIMEOUT`, which ``pixivpy3`` forwards to
    every request so a hung server cannot block the run indefinitely. An injected
    ``api`` is used verbatim (tests supply their own), so it is each caller's
    responsibility to set a timeout there.
    """

    def __init__(
        self,
        api: object | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = 0.5,
    ) -> None:
        raw: object = api if api is not None else AppPixivAPI(timeout=DEFAULT_TIMEOUT)
        self._api: _AppApi = cast(_AppApi, raw)
        self._sleep = sleep
        self._delay = delay

    # -- public, fully typed surface ------------------------------------

    def authenticate(self, refresh_token: str) -> str:
        """Authenticate with ``refresh_token`` and return the self user id.

        Raises:
            AuthRejected: If Pixiv rejects the token (e.g. expired or revoked).
        """

        try:
            self._api.auth(refresh_token=refresh_token)
        except PixivError as exc:
            # Deliberately omit the token from the message.
            raise AuthRejected(
                "Pixiv rejected the refresh token (it may be expired or "
                "revoked). Generate a fresh token and try again."
            ) from exc
        return str(self._api.user_id)

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        """Yield each bookmarked :class:`BookmarkWork` for ``user_id``.

        Pages are fetched lazily; the injected ``sleep`` is invoked for
        ``delay`` seconds between successive pages. Personal bookmark tags are
        fetched only when backing up the authenticated user's own bookmarks.

        Raises:
            ApiError: If listing bookmarks fails.
        """

        fetch_bookmark_tags = str(user_id) == str(self._api.user_id)
        try:
            result = self._api.user_bookmarks_illust(user_id, restrict)
            while True:
                for illust in result.illusts or []:
                    yield self._to_work(illust, fetch_bookmark_tags)
                next_url = result.next_url
                if not next_url:
                    return
                qs = self._api.parse_qs(next_url)
                if not qs:
                    return
                self._sleep(self._delay)
                result = self._api.user_bookmarks_illust(**qs)
        except PixivError as exc:
            raise ApiError(f"Failed to list bookmarks: {exc}") from exc

    def iter_following(self, user_id: str, restrict: str) -> Iterator[FollowedUser]:
        """Yield each :class:`FollowedUser` that ``user_id`` follows.

        Pages are fetched lazily; the injected ``sleep`` is invoked for
        ``delay`` seconds between successive pages. ``restrict`` selects the
        visibility of the follow list (``"public"`` or ``"private"``).

        Raises:
            ApiError: If listing the follow list fails.
        """

        try:
            result = self._api.user_following(user_id, restrict)
            while True:
                for preview in result.user_previews or []:
                    yield FollowedUser(
                        id=preview.user.id,
                        name=preview.user.name,
                        account=preview.user.account,
                    )
                next_url = result.next_url
                if not next_url:
                    return
                qs = self._api.parse_qs(next_url)
                if not qs:
                    return
                self._sleep(self._delay)
                result = self._api.user_following(**qs)
        except PixivError as exc:
            raise ApiError(f"Failed to list following: {exc}") from exc

    def iter_user_works(self, user_id: str) -> Iterator[BookmarkWork]:
        """Yield each :class:`BookmarkWork` posted by ``user_id``.

        A single empty-string ``type`` pass returns every kind of work
        (illust, manga, and ugoira) in one listing. Pages are fetched lazily;
        the injected ``sleep`` is invoked for ``delay`` seconds between
        successive pages. Personal bookmark tags are never fetched here -- these
        are another user's works, not the caller's bookmarks.

        Raises:
            ApiError: If listing the user's works fails.
        """

        try:
            result = self._api.user_illusts(user_id, "")
            while True:
                for illust in result.illusts or []:
                    yield self._to_work(illust, fetch_bookmark_tags=False)
                next_url = result.next_url
                if not next_url:
                    return
                qs = self._api.parse_qs(next_url)
                if not qs:
                    return
                self._sleep(self._delay)
                result = self._api.user_illusts(**qs)
        except PixivError as exc:
            raise ApiError(f"Failed to list works for {user_id}: {exc}") from exc

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:
        """Return :class:`UgoiraInfo` for the ugoira work ``work_id``.

        Raises:
            ApiError: If the metadata request fails.
        """

        try:
            meta = self._api.ugoira_metadata(work_id).ugoira_metadata
        except PixivError as exc:
            raise ApiError(
                f"Failed to fetch ugoira metadata for {work_id}: {exc}"
            ) from exc
        return self._to_ugoira(meta)

    def download(self, url: str, dest_path: Path, referer: str = PIXIV_REFERER) -> int:
        """Download ``url`` to ``dest_path`` (single attempt), returning bytes.

        The parent directory is created if needed. Returns the size of the
        written file in bytes.

        Raises:
            ApiError: If the download fails.
        """

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._api.download(
                url,
                path=str(dest_path.parent),
                name=dest_path.name,
                replace=True,
                referer=referer,
            )
        except PixivError as exc:
            raise ApiError(f"Failed to download {url}: {exc}") from exc
        return dest_path.stat().st_size

    # -- private helpers: all JsonDict/Any access lives here ------------

    def _to_work(self, illust: Any, fetch_bookmark_tags: bool) -> BookmarkWork:
        """Map a Pixiv illust ``JsonDict`` to a typed :class:`BookmarkWork`."""

        author = Author(
            id=illust.user.id,
            name=illust.user.name,
            account=illust.user.account,
        )
        work_tags = [
            WorkTag(name=tag.name, translated_name=tag.translated_name)
            for tag in illust.tags
        ]
        bookmark_tags = (
            self._fetch_bookmark_tags(illust.id) if fetch_bookmark_tags else None
        )
        return BookmarkWork(
            id=illust.id,
            type=illust.type,
            title=illust.title,
            author=author,
            posted_at=illust.create_date,
            x_restrict=illust.x_restrict,
            sanity_level=illust.sanity_level,
            work_tags=work_tags,
            bookmark_tags=bookmark_tags,
            page_count=illust.page_count,
            images=self._extract_images(illust),
        )

    def _extract_images(self, illust: Any) -> list[WorkImage]:
        """Collect original-resolution image URLs from a Pixiv illust.

        Single-page works expose the URL via ``meta_single_page``; multi-page
        works expose one URL per entry in ``meta_pages``.
        """

        meta_pages = illust.meta_pages
        if meta_pages:
            return [
                WorkImage(page=index, source_url=page.image_urls.original)
                for index, page in enumerate(meta_pages)
            ]
        single = illust.meta_single_page.original_image_url
        if not single:
            return []
        return [WorkImage(page=0, source_url=single)]

    def _fetch_bookmark_tags(self, illust_id: int) -> list[str] | None:
        """Return the authenticated user's registered bookmark tags for a work.

        Returns ``None`` if Pixiv fails to provide the detail (the run is not
        aborted for this).
        """

        try:
            detail = self._api.illust_bookmark_detail(illust_id).bookmark_detail
        except PixivError:
            return None
        if detail is None:
            return None
        return [tag.name for tag in detail.tags if tag.is_registered]

    def _to_ugoira(self, meta: Any) -> UgoiraInfo:
        """Map Pixiv ugoira metadata ``JsonDict`` to a typed :class:`UgoiraInfo`."""

        frames = [
            UgoiraFrame(file=frame.file, delay=frame.delay) for frame in meta.frames
        ]
        return UgoiraInfo(zip_url=meta.zip_urls.medium, frames=frames)


# Static guarantee that the concrete client satisfies the public Protocol.
_: type[PixivClient] = Pixivpy3Client
