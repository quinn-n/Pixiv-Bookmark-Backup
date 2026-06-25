"""Offline unit tests for the resilient image downloader.

Every test injects a :class:`FakeClient` (a structural stand-in for the
``PixivClient`` Protocol that performs no network I/O) plus deterministic
``sleep`` / ``rand`` callables, so backoff timing and attempt counts are
asserted exactly. ``ApiError`` is imported from ``pixiv_client`` only to
construct/raise it; that import does no network work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from pixiv_bookmark_backup.downloader import (
    ImageResult,
    download_image,
    should_skip,
)
from pixiv_bookmark_backup.models import BookmarkWork, ImageStatus, UgoiraInfo
from pixiv_bookmark_backup.pixiv_client import ApiError


class FakeClient:
    """Scriptable stand-in for the ``PixivClient`` Protocol.

    Only :meth:`download` carries real behaviour; the other Protocol methods are
    stubs present purely so the class structurally satisfies ``PixivClient`` for
    the type checker. ``download`` raises :class:`ApiError` for its first
    ``fail_times`` calls, then (if ``fail_forever`` is false) writes ``payload``
    to ``dest_path`` and returns the byte count -- mirroring the real client,
    which overwrites its target. Every call is recorded in :attr:`calls`.
    """

    def __init__(
        self,
        *,
        payload: bytes = b"image-bytes",
        fail_times: int = 0,
        fail_forever: bool = False,
    ) -> None:
        self.payload = payload
        self.fail_times = fail_times
        self.fail_forever = fail_forever
        self.calls: list[tuple[str, Path, str]] = []

    def authenticate(self, refresh_token: str) -> str:
        raise NotImplementedError

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        raise NotImplementedError

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:
        raise NotImplementedError

    def download(
        self, url: str, dest_path: Path, referer: str = "https://www.pixiv.net/"
    ) -> int:
        self.calls.append((url, dest_path, referer))
        if self.fail_forever or len(self.calls) <= self.fail_times:
            raise ApiError("download failed")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(self.payload)
        return len(self.payload)


def _recording_sleep(naps: list[float]) -> Callable[[float], None]:
    """Return a sleep stub that appends each requested duration to ``naps``."""

    def _sleep(seconds: float) -> None:
        naps.append(seconds)

    return _sleep


# -- should_skip --------------------------------------------------------


def test_should_skip_true_for_existing_nonzero_file(tmp_path: Path) -> None:
    target = tmp_path / "img.png"
    target.write_bytes(b"not empty")
    assert should_skip(target) is True


def test_should_skip_false_for_zero_byte_file(tmp_path: Path) -> None:
    target = tmp_path / "img.png"
    target.write_bytes(b"")
    assert should_skip(target) is False


def test_should_skip_false_for_missing_file(tmp_path: Path) -> None:
    assert should_skip(tmp_path / "absent.png") is False


# -- skip-existing path -------------------------------------------------


def test_skip_existing_returns_skipped_without_downloading(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    dest.write_bytes(b"already here")
    client = FakeClient()
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=3,
        skip_existing=True,
    )
    assert result == ImageResult(ImageStatus.SKIPPED, len(b"already here"), None)
    assert client.calls == []


def test_skip_existing_false_redownloads_existing_file(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    dest.write_bytes(b"stale")
    client = FakeClient(payload=b"fresh-payload")
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=3,
        skip_existing=False,
    )
    assert result.status is ImageStatus.DOWNLOADED
    assert result.bytes_ == len(b"fresh-payload")
    assert len(client.calls) == 1
    assert dest.read_bytes() == b"fresh-payload"


def test_zero_byte_file_is_not_skipped(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    dest.write_bytes(b"")
    client = FakeClient(payload=b"recovered")
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=3,
        skip_existing=True,
    )
    assert result.status is ImageStatus.DOWNLOADED
    assert result.bytes_ == len(b"recovered")
    assert len(client.calls) == 1
    assert dest.read_bytes() == b"recovered"


# -- success path -------------------------------------------------------


def test_success_first_try(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "img.png"
    payload = b"\x89PNG\r\n\x1a\nbody"
    client = FakeClient(payload=payload)
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=3,
        skip_existing=True,
    )
    assert result == ImageResult(ImageStatus.DOWNLOADED, len(payload), None)
    assert dest.exists()
    assert dest.read_bytes() == payload
    assert len(client.calls) == 1


def test_default_referer_is_pixiv_referer(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    client = FakeClient()
    download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=0,
        skip_existing=False,
    )
    assert client.calls[0][2] == "https://www.pixiv.net/"


# -- retry then success -------------------------------------------------


def test_retry_then_success_with_exponential_backoff(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    naps: list[float] = []
    # Fail twice, succeed on the third attempt.
    client = FakeClient(payload=b"ok", fail_times=2)
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=5,
        skip_existing=False,
        sleep=_recording_sleep(naps),
        rand=lambda: 0.0,
        base_delay=0.5,
    )
    assert result.status is ImageStatus.DOWNLOADED
    assert result.bytes_ == len(b"ok")
    assert len(client.calls) == 3
    # Two failures -> two backoffs, doubling each time (no jitter with rand=0).
    assert naps == [0.5, 1.0]


def test_jitter_is_added_to_backoff(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    naps: list[float] = []
    client = FakeClient(payload=b"ok", fail_times=2)
    download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=5,
        skip_existing=False,
        sleep=_recording_sleep(naps),
        rand=lambda: 1.0,
        base_delay=0.5,
    )
    # rand() == 1.0 adds a full base_delay of jitter to each backoff:
    #   attempt 0: 0.5 * 1 + 1.0 * 0.5 = 1.0
    #   attempt 1: 0.5 * 2 + 1.0 * 0.5 = 1.5
    assert naps == [1.0, 1.5]


# -- retry exhausted ----------------------------------------------------


def test_retry_exhausted_returns_failed(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    naps: list[float] = []
    client = FakeClient(fail_forever=True)
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=2,
        skip_existing=False,
        sleep=_recording_sleep(naps),
        rand=lambda: 0.0,
    )
    assert result.status is ImageStatus.FAILED
    assert result.bytes_ is None
    assert result.error is not None
    assert "download failed" in result.error
    # max_retries + 1 attempts, max_retries backoffs between them.
    assert len(client.calls) == 3
    assert len(naps) == 2
    assert not dest.exists()


def test_no_retries_single_attempt_failure(tmp_path: Path) -> None:
    dest = tmp_path / "img.png"
    naps: list[float] = []
    client = FakeClient(fail_forever=True)
    result = download_image(
        client,
        "https://i.pximg.net/img.png",
        dest,
        max_retries=0,
        skip_existing=False,
        sleep=_recording_sleep(naps),
    )
    assert result.status is ImageStatus.FAILED
    assert len(client.calls) == 1
    assert naps == []
