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

import random
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

#: Number of retries (after the initial attempt) for a mid-pagination listing
#: recovery in ``_fetch_page`` -- i.e. up to ``1 + this`` total tries. Mirrors
#: ``downloader.download_image``'s default so both retry policies share a shape.
FETCH_PAGE_MAX_RETRIES = 3

#: Base backoff unit (seconds) for ``_fetch_page``'s recovery retries. Heavier
#: than the downloader's 0.5s: a mid-listing re-auth failure is rarer and
#: usually means Pixiv's anti-bot layer has cut connections after sustained
#: heavy traffic, which needs more time to recover than a single-image blip.
FETCH_PAGE_BASE_DELAY = 1.0


#: Cap on how much of a Pixiv error body's repr is embedded in a failure
#: message, so a pathologically large response body can't bloat the message
#: (and, downstream, CLI stderr output and the JSON manifest's ``errors`` list).
RESPONSE_ERROR_DETAIL_MAX_LEN = 500


def _response_error_detail(error: Any) -> str | None:
    """Bounded, defensive ``repr()`` of a Pixiv ``result.error`` body.

    Returns ``None`` if there is nothing to show. Pixiv's error bodies
    (``{"error": {"user_message": ..., "message": ...}}``) never echo request
    credentials -- unlike a raised ``PixivError``, which can embed the request
    URL/headers and is therefore never surfaced (see ``_fetch_page``). Building
    the description must never itself crash a run, so it's guarded like
    ``_warn_unparsable_next_url``'s shape sketch.
    """

    if error is None:
        return None
    try:
        text = repr(error)
    except Exception:  # pragma: no cover - defensive: never crash a run
        return None
    if len(text) > RESPONSE_ERROR_DETAIL_MAX_LEN:
        text = f"{text[:RESPONSE_ERROR_DETAIL_MAX_LEN]}...(truncated)"
    return text


def _describe_response_error(result: Any) -> str:
    """Render ``result.error`` for a "last failure" style failure message."""

    detail = _response_error_detail(getattr(result, "error", None))
    if detail is None:
        return "an error response"
    return f"an error response ({detail})"


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

    All Pixiv interaction funnels through this class. The underlying ``api``,
    the ``sleep`` function, and the ``rand`` jitter source are injectable so
    tests can run fully offline and deterministically. ``delay`` is the
    politeness pause (seconds) taken between listing pages while paginating
    (bookmarks, following, and user works); ``rand`` supplies jitter for
    ``_fetch_page``'s recovery backoff.

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
        warn: Callable[[str], None] = lambda _msg: None,
        rand: Callable[[], float] = random.random,
    ) -> None:
        raw: object = api if api is not None else AppPixivAPI(timeout=DEFAULT_TIMEOUT)
        self._api: _AppApi = cast(_AppApi, raw)
        self._sleep = sleep
        self._delay = delay
        # Injectable [0.0, 1.0) jitter source for _fetch_page's recovery backoff.
        # Injected (alongside sleep) so tests exercise the retry loop
        # deterministically; mirrors downloader.download_image's rand collaborator.
        self._rand = rand
        # Injectable diagnostic sink. Called (with a one-line, secret-free
        # message) when pagination stops with a truthy but unparsable next_url --
        # Pixiv said there is more data but we cannot reach it (see
        # _warn_unparsable_next_url). Defaults to a no-op so callers that do not
        # wire it up (tests, library users) see no behaviour change; the CLI
        # points it at its stderr warning closure.
        self._warn = warn
        # Cached during authenticate() so a mid-pagination token expiry can be
        # recovered by re-authing with the same token. None until the first
        # authenticate() call (or if the client is used without one).
        self._refresh_token: str | None = None

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
        # Cache for mid-pagination re-auth on token expiry (see _fetch_page).
        self._refresh_token = refresh_token
        return str(self._api.user_id)

    def _fetch_page(self, fetch: Callable[[], Any]) -> Any:
        """Fetch one listing page, recovering from a mid-pagination failure.

        Pixiv access tokens expire after ~1 hour, so a large backup run commonly
        outlives its token. When that happens, ``pixivpy3`` does *not* raise:
        ``parse_result`` never checks the HTTP status, and ``JsonDict``'s
        attribute access returns ``None`` for any missing key -- so an error body
        (``{"error": {...}}``) is indistinguishable from a genuinely exhausted
        list (``illusts``/``next_url`` both ``None``), silently truncating the
        run. This helper closes that gap: it inspects the response for an
        ``error`` key *before* the caller reads ``illusts``/``next_url``, and on
        an error re-authenticates with the cached refresh token and retries the
        *same* page (never skipping a page).

        Recovery is retried with **exponential backoff plus jitter**, mirroring
        :func:`~pixiv_bookmark_backup.downloader.download_image`. Two failure
        modes are both retried within the same budget:

        * The page fetch returns an ``error``-shaped response (token expiry, or a
          transient server-side error). Rather than classify Pixiv's opaque error
          bodies (fragile, and we have no stable sample), *any* error page is a
          re-auth-and-retry.
        * The re-auth call itself raises :class:`~pixivpy3.PixivError` -- e.g. the
          field-observed connection reset when Pixiv's anti-bot layer cuts
          connections after sustained heavy traffic. A single re-auth attempt is
          not enough to ride out such a transient reset, so it is backed off and
          retried too.

        Up to :data:`FETCH_PAGE_MAX_RETRIES` recoveries are attempted after the
        initial fetch (so ``FETCH_PAGE_MAX_RETRIES + 1`` total tries), backing off
        ``FETCH_PAGE_BASE_DELAY * (2 ** attempt) + rand() * FETCH_PAGE_BASE_DELAY``
        seconds between them via the injected ``sleep``/``rand``. The attempt
        budget is a local variable, not state on ``self`` -- so each page recovers
        independently (a run can outlive its token more than once) while a
        persistent failure can never loop forever.

        Once the budget is exhausted, an :class:`ApiError` is raised (the CLI's
        partial-run exit-code contract is unchanged). Its message states how many
        attempts were made and describes the last concrete failure: for an
        error-shaped response, Pixiv's own ``error`` body (via
        :func:`_describe_response_error`) -- these bodies never echo request
        credentials, unlike a raised ``PixivError``, whose text can embed the
        request URL/headers and is therefore deliberately never surfaced (a
        network-error give-up mirrors :meth:`authenticate`'s static message).

        Raises:
            ApiError: If no refresh token was cached, or the failure persists
                after the full retry budget of re-authentications.
        """

        result = fetch()
        if getattr(result, "error", None) is None:
            return result
        if self._refresh_token is None:
            detail = _response_error_detail(getattr(result, "error", None))
            suffix = f" ({detail})" if detail is not None else ""
            raise ApiError(
                "Pixiv returned an error response and no refresh token is "
                f"available to re-authenticate{suffix}."
            )

        total_tries = FETCH_PAGE_MAX_RETRIES + 1
        # Tracks how the most recent recovery attempt failed, so the final
        # give-up message reflects the last concrete failure without embedding
        # any raw PixivError text. Reset each attempt.
        last_failure = _describe_response_error(result)
        for attempt in range(FETCH_PAGE_MAX_RETRIES):
            # Back off before each recovery: exponential + jitter, matching
            # download_image. This also supplies the politeness pause the
            # same-page retry previously lacked.
            self._sleep(
                FETCH_PAGE_BASE_DELAY * (2**attempt)
                + self._rand() * FETCH_PAGE_BASE_DELAY
            )
            try:
                self._api.auth(refresh_token=self._refresh_token)
            except PixivError:
                # Re-auth itself failed at the network level (e.g. a connection
                # reset). Note the failure kind -- never the raw exception text --
                # and retry until the budget is spent.
                last_failure = "a network error"
                continue
            result = fetch()
            if getattr(result, "error", None) is None:
                return result
            last_failure = _describe_response_error(result)

        raise ApiError(
            f"Pixiv request recovery failed after {total_tries} attempts "
            f"(last failure: {last_failure})."
        )

    def _warn_unparsable_next_url(
        self,
        result: Any,
        *,
        pages_so_far: int,
        items_so_far: int,
        context: str,
    ) -> None:
        """Emit a diagnostic ``warn`` when a non-empty ``next_url`` is unusable.

        Called at the single pagination termination point that has no benign
        interpretation: Pixiv's response carried a *truthy* ``next_url`` -- an
        explicit "there is more data" pointer -- but :meth:`parse_qs` could not
        extract usable query parameters from it, so pagination cannot continue
        even though the API says it should. A genuine end-of-data response never
        carries a ``next_url`` at all (a falsy ``next_url`` is Pixiv's normal
        "no more data" signal and is handled silently by the callers), so this
        combination always warrants a warning; it is not gated on page size or
        emptiness.

        The message is one line, human-readable, and never contains the
        refresh/access token. It carries how much was yielded so far plus a
        shape sketch of the raw response -- its ``error`` content if present, or
        else its top-level keys -- to aid diagnosis. Building the sketch must
        never crash the run, so key extraction is defensively guarded (Pixiv
        results subclass ``dict`` and expose ``keys()``; test doubles may not).
        """

        error = getattr(result, "error", None)
        if error is not None:
            shape = f"error={error!r}"
        elif hasattr(result, "keys"):
            try:
                shape = f"top-level keys={sorted(map(str, result.keys()))}"
            except Exception:  # pragma: no cover - defensive: never crash a run
                shape = f"response type={type(result).__name__}"
        else:
            shape = f"response type={type(result).__name__}"

        self._warn(
            f"{context} stopped early: Pixiv returned a next_url pointing to more "
            f"data, but its query string could not be parsed, so pagination cannot "
            f"continue -- after yielding {items_so_far} item(s) across "
            f"{pages_so_far} page(s); {shape}."
        )

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        """Yield each bookmarked :class:`BookmarkWork` for ``user_id``.

        Pages are fetched lazily; the injected ``sleep`` is invoked for
        ``delay`` seconds between successive pages. Personal bookmark tags are
        fetched only when backing up the authenticated user's own bookmarks.

        Raises:
            ApiError: If listing bookmarks fails.
        """

        fetch_bookmark_tags = str(user_id) == str(self._api.user_id)
        pages_so_far = 0
        items_so_far = 0
        try:
            result = self._fetch_page(
                lambda: self._api.user_bookmarks_illust(user_id, restrict)
            )
            while True:
                page_items = result.illusts or []
                page_item_count = len(page_items)
                pages_so_far += 1
                for illust in page_items:
                    yield self._to_work(illust, fetch_bookmark_tags)
                items_so_far += page_item_count
                next_url = result.next_url
                if not next_url:
                    return
                qs = self._api.parse_qs(next_url)
                if not qs:
                    self._warn_unparsable_next_url(
                        result,
                        pages_so_far=pages_so_far,
                        items_so_far=items_so_far,
                        context="Bookmark listing",
                    )
                    return
                self._sleep(self._delay)
                result = self._fetch_page(
                    lambda qs=qs: self._api.user_bookmarks_illust(**qs)
                )
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

        pages_so_far = 0
        items_so_far = 0
        try:
            result = self._fetch_page(
                lambda: self._api.user_following(user_id, restrict)
            )
            while True:
                page_items = result.user_previews or []
                page_item_count = len(page_items)
                pages_so_far += 1
                for preview in page_items:
                    yield FollowedUser(
                        id=preview.user.id,
                        name=preview.user.name,
                        account=preview.user.account,
                    )
                items_so_far += page_item_count
                next_url = result.next_url
                if not next_url:
                    return
                qs = self._api.parse_qs(next_url)
                if not qs:
                    self._warn_unparsable_next_url(
                        result,
                        pages_so_far=pages_so_far,
                        items_so_far=items_so_far,
                        context="Following listing",
                    )
                    return
                self._sleep(self._delay)
                result = self._fetch_page(lambda qs=qs: self._api.user_following(**qs))
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

        pages_so_far = 0
        items_so_far = 0
        try:
            result = self._fetch_page(lambda: self._api.user_illusts(user_id, ""))
            while True:
                page_items = result.illusts or []
                page_item_count = len(page_items)
                pages_so_far += 1
                for illust in page_items:
                    yield self._to_work(illust, fetch_bookmark_tags=False)
                items_so_far += page_item_count
                next_url = result.next_url
                if not next_url:
                    return
                qs = self._api.parse_qs(next_url)
                if not qs:
                    self._warn_unparsable_next_url(
                        result,
                        pages_so_far=pages_so_far,
                        items_so_far=items_so_far,
                        context=f"Works listing for {user_id}",
                    )
                    return
                self._sleep(self._delay)
                result = self._fetch_page(lambda qs=qs: self._api.user_illusts(**qs))
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

        Routed through :meth:`_fetch_page` so a mid-run token expiry recovers via
        the same re-auth-with-backoff path as the listing endpoints: an
        error-shaped detail response (whose missing ``bookmark_detail`` key would
        otherwise resolve to ``None`` via ``JsonDict.__getattr__``, silently
        yielding "no tags") triggers re-authentication and a re-fetch instead of
        returning bad data.

        Fetching a single work's tags is *not* worth aborting a large run over, so
        this call site degrades gracefully rather than propagating the
        :class:`ApiError` that :meth:`_fetch_page` raises when its bounded
        re-auth/retry budget is exhausted -- unlike the listing endpoints, where a
        failed page means lost works and must abort. Two non-fatal outcomes return
        ``None`` for just this work:

        * A :class:`~pixivpy3.PixivError` on the detail call -- a transient
          single-work network blip, or an absent/empty ``bookmark_detail``.
        * :meth:`_fetch_page` exhausting its budget (:class:`ApiError`). This only
          fires *after* the bounded re-auth/retry already ran, so it means either
          this specific work is permanently unreadable (deleted/restricted
          bookmark -- common on a large, long-lived account) or a systemic token
          failure. The latter is still backstopped: the next *listing* page's own
          :meth:`_fetch_page` will abort the run properly. Either way the skip is
          surfaced via ``warn`` (never silent), and the bounded recovery was still
          attempted first -- so this is a graceful give-up, not the original
          silently-return-``None`` defect.
        """

        try:
            result = self._fetch_page(
                lambda: self._api.illust_bookmark_detail(illust_id)
            )
        except PixivError:
            return None
        except ApiError:
            # Bounded re-auth/retry already ran and still failed for this one
            # work. Skip its tags (non-fatal) rather than abort the whole run,
            # but make the skip visible -- silence was the original bug.
            self._warn(
                f"Could not fetch bookmark tags for work {illust_id} after "
                f"re-authenticating; skipping its tags for this run."
            )
            return None
        detail = result.bookmark_detail
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
