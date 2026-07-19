"""Offline unit tests for ugoira GIF assembly and orchestration.

Every test builds real in-memory PNG frames and real ZIP archives with Pillow
and :mod:`zipfile`, so the suite never touches the network. A hand-rolled
``FakeClient`` stands in for the Pixiv surface: its ``download`` writes a real
(or deliberately corrupt) zip to the requested path, exactly as the real client
would land the frames zip on disk.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from PIL.GifImagePlugin import GifImageFile

from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    ImageStatus,
    UgoiraFrame,
    UgoiraInfo,
)
from pixiv_bookmark_backup.pixiv_client import (
    PIXIV_REFERER,
    ApiError,
    FollowedUser,
    PixivClient,
)
from pixiv_bookmark_backup.ugoira import (
    _read_frame_capped,
    assemble_gif,
    build_ugoira,
)

# -- helpers ------------------------------------------------------------


def _png_bytes(color: str, size: int = 4) -> bytes:
    """Return the bytes of a tiny solid-colour RGB PNG."""

    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    """Return a real ZIP archive containing ``(name, data)`` members."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buf.getvalue()


def _open_gif(path: Path) -> GifImageFile:
    """Open ``path`` as a GIF, asserting it really is one (narrows the type)."""

    img = Image.open(path)
    assert isinstance(img, GifImageFile)
    return img


def _make_work(work_id: int = 100, work_type: str = "ugoira") -> BookmarkWork:
    """Build a minimal :class:`BookmarkWork` for ugoira tests."""

    return BookmarkWork(
        id=work_id,
        type=work_type,
        title="Spinny",
        author=Author(id=7, name="Artist", account="artist_acct"),
        posted_at="2024-01-02T03:04:05+09:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=1,
        images=[],
    )


class FakeClient:
    """Minimal stand-in for the Pixiv client used in ugoira tests.

    ``ugoira_metadata`` returns the configured :class:`UgoiraInfo` (or raises),
    and ``download`` writes ``zip_payload`` to the requested path (mirroring the
    real client, which lands the frames zip on disk) and returns its size.
    """

    def __init__(
        self,
        *,
        info: UgoiraInfo | None = None,
        metadata_error: Exception | None = None,
        zip_payload: bytes = b"",
    ) -> None:
        self._info = info
        self._metadata_error = metadata_error
        self._zip_payload = zip_payload
        self.metadata_calls: list[int] = []
        self.download_calls: list[tuple[str, Path, str]] = []

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:
        self.metadata_calls.append(work_id)
        if self._metadata_error is not None:
            raise self._metadata_error
        assert self._info is not None
        return self._info

    def download(self, url: str, dest_path: Path, referer: str = PIXIV_REFERER) -> int:
        self.download_calls.append((url, dest_path, referer))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(self._zip_payload)
        return len(self._zip_payload)

    # build_ugoira touches only the two methods above; the rest of the
    # PixivClient Protocol is stubbed so FakeClient satisfies it statically.
    def authenticate(self, refresh_token: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def iter_bookmarks(
        self, user_id: str, restrict: str
    ) -> Iterator[BookmarkWork]:  # pragma: no cover
        raise NotImplementedError

    def iter_following(
        self, user_id: str, restrict: str
    ) -> Iterator[FollowedUser]:  # pragma: no cover
        raise NotImplementedError

    def iter_user_works(
        self, user_id: str
    ) -> Iterator[BookmarkWork]:  # pragma: no cover
        raise NotImplementedError

    def fetch_bookmark_tags(
        self, illust_id: int
    ) -> list[str] | None:  # pragma: no cover
        raise NotImplementedError


# Static guarantee that the fake satisfies the public Protocol.
_: type[PixivClient] = FakeClient


# -- assemble_gif -------------------------------------------------------


def test_assemble_gif_writes_animated_gif_with_per_frame_durations(
    tmp_path: Path,
) -> None:
    b0, b1 = _png_bytes("red"), _png_bytes("blue")
    dest = tmp_path / "nested" / "anim.gif"

    assemble_gif([(b0, 120), (b1, 300)], dest)

    assert dest.exists()
    gif = _open_gif(dest)
    assert gif.n_frames == 2
    assert gif.is_animated
    gif.seek(0)
    assert gif.info["duration"] == pytest.approx(120, abs=10)
    gif.seek(1)
    assert gif.info["duration"] == pytest.approx(300, abs=10)


def test_assemble_gif_creates_parent_directory(tmp_path: Path) -> None:
    dest = tmp_path / "a" / "b" / "c" / "anim.gif"
    assemble_gif([(_png_bytes("green"), 50)], dest)
    assert dest.exists()


def test_assemble_gif_rejects_empty_frames(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        assemble_gif([], tmp_path / "empty.gif")


# -- build_ugoira: success ----------------------------------------------


def test_build_ugoira_success_assembles_gif_and_cleans_temp_zip(
    tmp_path: Path,
) -> None:
    info = UgoiraInfo(
        zip_url="https://i.pximg.net/ugoira/frames.zip",
        frames=[
            UgoiraFrame(file="000000.png", delay=120),
            UgoiraFrame(file="000001.png", delay=300),
        ],
    )
    payload = _zip_bytes(
        [
            ("000000.png", _png_bytes("red")),
            ("000001.png", _png_bytes("blue")),
        ]
    )
    client = FakeClient(info=info, zip_payload=payload)
    dest = tmp_path / "out" / "100.gif"

    result = build_ugoira(client, _make_work(100), dest, skip_existing=False)

    assert result.status == ImageStatus.DOWNLOADED
    assert result.error is None
    assert result.bytes_ == dest.stat().st_size
    assert dest.exists()
    gif = _open_gif(dest)
    assert gif.n_frames == 2
    # The temp frames zip (an unpredictable mkstemp name) is cleaned up: only
    # the assembled GIF is left in the destination directory.
    assert list(dest.parent.glob("*.ugoira.zip")) == []
    assert client.metadata_calls == [100]
    # The zip was downloaded to a temp path beside the destination, with an
    # unpredictable name ending in ``.ugoira.zip`` (not a fixed sibling).
    downloaded_to = client.download_calls[0][1]
    assert downloaded_to.parent == dest.parent
    assert downloaded_to.name.endswith(".ugoira.zip")
    assert downloaded_to != dest.with_suffix(".ugoira.zip")


# -- build_ugoira: skip-existing ----------------------------------------


def test_build_ugoira_skips_existing_nonempty_dest(tmp_path: Path) -> None:
    dest = tmp_path / "100.gif"
    dest.write_bytes(b"already here")
    client = FakeClient()

    result = build_ugoira(client, _make_work(100), dest, skip_existing=True)

    assert result.status == ImageStatus.SKIPPED
    assert result.bytes_ == dest.stat().st_size
    assert result.error is None
    # Skip-existing means no Pixiv calls at all.
    assert client.metadata_calls == []
    assert client.download_calls == []


def test_build_ugoira_does_not_skip_zero_byte_dest(tmp_path: Path) -> None:
    dest = tmp_path / "100.gif"
    dest.touch()  # zero bytes -> treated as missing
    info = UgoiraInfo(
        zip_url="https://x/frames.zip",
        frames=[UgoiraFrame(file="000000.png", delay=80)],
    )
    payload = _zip_bytes([("000000.png", _png_bytes("red"))])
    client = FakeClient(info=info, zip_payload=payload)

    result = build_ugoira(client, _make_work(100), dest, skip_existing=True)

    assert result.status == ImageStatus.DOWNLOADED
    assert client.metadata_calls == [100]


# -- build_ugoira: failure paths ----------------------------------------


def test_build_ugoira_metadata_error_returns_failed_without_raising(
    tmp_path: Path,
) -> None:
    client = FakeClient(metadata_error=ApiError("metadata boom"))
    dest = tmp_path / "100.gif"

    result = build_ugoira(client, _make_work(100), dest, skip_existing=False)

    assert result.status == ImageStatus.FAILED
    assert result.bytes_ is None
    assert result.error is not None
    assert not dest.exists()
    assert client.download_calls == []


def test_build_ugoira_garbage_zip_returns_failed_and_cleans_temp(
    tmp_path: Path,
) -> None:
    info = UgoiraInfo(
        zip_url="https://x/frames.zip",
        frames=[UgoiraFrame(file="000000.png", delay=80)],
    )
    client = FakeClient(info=info, zip_payload=b"this is definitely not a zip")
    dest = tmp_path / "100.gif"

    result = build_ugoira(client, _make_work(100), dest, skip_existing=False)

    assert result.status == ImageStatus.FAILED
    assert result.bytes_ is None
    assert result.error is not None
    assert not dest.exists()
    # Even a failed run cleans up the temp zip.
    assert list(dest.parent.glob("*.ugoira.zip")) == []


def test_build_ugoira_missing_frame_in_zip_returns_failed(tmp_path: Path) -> None:
    info = UgoiraInfo(
        zip_url="https://x/frames.zip",
        frames=[UgoiraFrame(file="000000.png", delay=80)],
    )
    # The archive does not contain the declared frame name.
    payload = _zip_bytes([("somethingelse.png", _png_bytes("red"))])
    client = FakeClient(info=info, zip_payload=payload)
    dest = tmp_path / "100.gif"

    result = build_ugoira(client, _make_work(100), dest, skip_existing=False)

    assert result.status == ImageStatus.FAILED
    assert result.error is not None
    assert list(dest.parent.glob("*.ugoira.zip")) == []


# -- build_ugoira: zip-bomb / abuse guards ------------------------------


def test_build_ugoira_rejects_too_many_frames_without_downloading(
    tmp_path: Path,
) -> None:
    info = UgoiraInfo(
        zip_url="https://x/frames.zip",
        frames=[
            UgoiraFrame(file="000000.png", delay=80),
            UgoiraFrame(file="000001.png", delay=80),
            UgoiraFrame(file="000002.png", delay=80),
        ],
    )
    client = FakeClient(info=info, zip_payload=b"unused")
    dest = tmp_path / "100.gif"

    result = build_ugoira(
        client, _make_work(100), dest, skip_existing=False, max_frames=2
    )

    assert result.status == ImageStatus.FAILED
    assert result.error is not None
    # The frame-count guard trips before any download is attempted.
    assert client.download_calls == []
    assert not dest.exists()


def test_build_ugoira_rejects_oversized_declared_frames(tmp_path: Path) -> None:
    frame = _png_bytes("red")
    info = UgoiraInfo(
        zip_url="https://x/frames.zip",
        frames=[UgoiraFrame(file="000000.png", delay=80)],
    )
    payload = _zip_bytes([("000000.png", frame)])
    client = FakeClient(info=info, zip_payload=payload)
    dest = tmp_path / "100.gif"

    # Cap below the single frame's uncompressed size -> rejected before read.
    result = build_ugoira(
        client, _make_work(100), dest, skip_existing=False, max_zip_bytes=1
    )

    assert result.status == ImageStatus.FAILED
    assert result.error is not None
    assert not dest.exists()
    assert list(dest.parent.glob("*.ugoira.zip")) == []


def test_build_ugoira_streaming_cap_aborts_high_ratio_zip(tmp_path: Path) -> None:
    # A frame of 2 MiB of zeros compresses to a tiny entry, so the declared
    # pre-check alone is not the only line of defense: with a 1 MB cap the
    # streaming guard must abort mid-read before the whole frame is in memory.
    payload = _zip_bytes([("000000.png", b"\x00" * (2 * 1024 * 1024))])
    info = UgoiraInfo(
        zip_url="https://x/frames.zip",
        frames=[UgoiraFrame(file="000000.png", delay=80)],
    )
    client = FakeClient(info=info, zip_payload=payload)
    dest = tmp_path / "100.gif"

    result = build_ugoira(
        client, _make_work(100), dest, skip_existing=False, max_zip_bytes=1_000_000
    )

    assert result.status == ImageStatus.FAILED
    assert result.error is not None
    # No GIF was written and the temp zip is gone.
    assert not dest.exists()
    assert list(dest.parent.glob("*.ugoira.zip")) == []


def test_read_frame_capped_streams_within_budget() -> None:
    # A 200 KiB entry read with a generous budget returns the exact bytes.
    data = b"\x00" * (200 * 1024)
    buf = io.BytesIO(_zip_bytes([("frame.bin", data)]))
    with zipfile.ZipFile(buf) as archive:
        out = _read_frame_capped(archive, "frame.bin", remaining=10_000_000)
    assert out == data


def test_read_frame_capped_aborts_when_actual_exceeds_budget() -> None:
    # The authoritative guard: even though this is an honest zip, the streaming
    # reader raises the moment the running decompressed total passes the budget,
    # never trusting any declared size. A budget smaller than the entry trips it.
    data = b"\x00" * (200 * 1024)
    buf = io.BytesIO(_zip_bytes([("frame.bin", data)]))
    with zipfile.ZipFile(buf) as archive:
        with pytest.raises(ValueError, match="decompressed-size limit"):
            _read_frame_capped(archive, "frame.bin", remaining=1024)
