"""Assemble animated GIFs from Pixiv ugoira (animated works).

A Pixiv *ugoira* is delivered not as a video but as a single ZIP archive of
still frames (PNG/JPEG) plus a per-frame display delay in milliseconds (see
:class:`~pixiv_bookmark_backup.models.UgoiraInfo`). This module turns that pair
into a playable animated GIF with correct per-frame timing.

It sits above the one-shot :meth:`PixivClient.download` primitive and owns the
fetch -> extract -> assemble pipeline for a single work:

* :func:`assemble_gif` is the pure rendering core -- given decoded
  ``(frame_bytes, delay_ms)`` pairs it writes a looping animated GIF, honouring
  each frame's individual duration.
* :func:`build_ugoira` orchestrates one work end to end: skip-existing,
  metadata fetch, zip download, in-memory frame extraction, assembly, and
  temp-file cleanup.

The frames ZIP is an **untrusted archive**, so extraction is deliberately
defensive: entries are read **by exact name into memory** (never
``extractall``/extract-to-disk, which avoids path traversal), the frame count is
capped, and -- the authoritative zip-bomb defense -- each frame is
*stream-decompressed* in fixed-size chunks while a running total of the actual
decompressed bytes is enforced against ``max_zip_bytes`` (so a high-ratio
crafted zip that declares a tiny size but expands to gigabytes is aborted mid
read, before it can exhaust memory). The cheap declared-size pre-check is kept
as a first filter only. Any failure of the whole pipeline is captured and
returned as a :class:`UgoiraResult` with
:attr:`~pixiv_bookmark_backup.models.ImageStatus.FAILED` -- :func:`build_ugoira`
never raises for an expected failure, so the orchestrator records the bad
ugoira and continues.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path

from attrs import frozen
from PIL import Image

from pixiv_bookmark_backup.models import BookmarkWork, ImageStatus
from pixiv_bookmark_backup.pixiv_client import PIXIV_REFERER, PixivClient

#: Chunk size (bytes) used when stream-decompressing each frame from the zip.
_CHUNK_SIZE = 65536


@frozen
class UgoiraResult:
    """Outcome of attempting to build a single ugoira animation locally.

    A lightweight per-work record the orchestrator maps into the manifest.

    Attributes:
        status: How the attempt resolved.
        bytes_: Size of the written GIF in bytes on success/skip, ``None`` on
            failure.
        error: Human-readable failure reason, ``None`` unless ``status`` is
            :attr:`~pixiv_bookmark_backup.models.ImageStatus.FAILED`.
    """

    status: ImageStatus
    bytes_: int | None
    error: str | None


def assemble_gif(frames: list[tuple[bytes, int]], dest: Path) -> None:
    """Render ``frames`` into a looping animated GIF written to ``dest``.

    Each entry is a ``(image_bytes, delay_ms)`` pair in display order; the
    bytes are an encoded still frame (PNG/JPEG) and ``delay_ms`` is how long it
    should be shown. The frames are decoded and converted to ``RGB`` (Pillow
    only honours a per-frame ``duration`` list for real raster frames), and the
    GIF is saved with ``loop=0`` (loop forever) and ``disposal=2`` (restore to
    background between frames so frames do not bleed into one another).

    The parent directory of ``dest`` is created if needed.

    Args:
        frames: Non-empty list of ``(image_bytes, delay_ms)`` pairs in order.
        dest: Path the animated GIF is written to.

    Raises:
        ValueError: If ``frames`` is empty.
    """

    if not frames:
        raise ValueError("cannot assemble an ugoira GIF from zero frames")

    dest.parent.mkdir(parents=True, exist_ok=True)

    images = [Image.open(io.BytesIO(data)).convert("RGB") for data, _ in frames]
    durations = [delay for _, delay in frames]

    first, *rest = images
    first.save(
        dest,
        save_all=True,
        append_images=rest,
        duration=durations,
        loop=0,
        disposal=2,
    )


def _read_frame_capped(
    archive: zipfile.ZipFile,
    name: str,
    *,
    remaining: int,
) -> bytes:
    """Stream-decompress entry ``name`` from ``archive``, capped at ``remaining``.

    Reads the member in fixed-size chunks via :meth:`zipfile.ZipFile.open` and
    accumulates the *actual* decompressed bytes, raising :class:`ValueError` the
    moment the running output would exceed ``remaining``. This is the
    authoritative zip-bomb guard: it never trusts the archive's declared sizes,
    so a high-ratio crafted entry is aborted before it can exhaust memory.

    Args:
        archive: The open frames archive.
        name: Exact member name to read (never an ``extractall`` path).
        remaining: Byte budget still available across all frames so far.

    Returns:
        The fully decompressed frame bytes.

    Raises:
        ValueError: If decompressing the entry would exceed ``remaining``.
    """

    buffer = io.BytesIO()
    written = 0
    with archive.open(name) as member:
        while True:
            chunk = member.read(_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > remaining:
                raise ValueError(
                    "ugoira frames exceed the decompressed-size limit "
                    "(possible zip bomb); aborting extraction"
                )
            buffer.write(chunk)
    return buffer.getvalue()


def build_ugoira(
    client: PixivClient,
    work: BookmarkWork,
    dest: Path,
    *,
    skip_existing: bool,
    referer: str = PIXIV_REFERER,
    max_zip_bytes: int = 100_000_000,
    max_frames: int = 2000,
) -> UgoiraResult:
    """Fetch, extract, and assemble one ugoira ``work`` into a GIF at ``dest``.

    The full pipeline runs under a single guard: any failure (metadata error,
    download error, a bad/garbage/oversized zip, a frame missing from the
    archive, or a render error) is captured and returned as a
    :class:`UgoiraResult` with
    :attr:`~pixiv_bookmark_backup.models.ImageStatus.FAILED`. This function
    never raises for an expected failure, so the caller can record the failed
    work and continue.

    Steps on the happy path:

    1. If ``skip_existing`` and ``dest`` already exists with non-zero size, the
       existing file is left untouched and reported as
       :attr:`~pixiv_bookmark_backup.models.ImageStatus.SKIPPED`.
    2. Fetch :class:`~pixiv_bookmark_backup.models.UgoiraInfo` for the work.
    3. Download the frames zip to a temp path beside ``dest``.
    4. Extract each declared frame **by exact name into memory** and assemble
       the GIF, honouring per-frame delays.
    5. Delete the temp zip (always, even on failure) and report
       :attr:`~pixiv_bookmark_backup.models.ImageStatus.DOWNLOADED`.

    The archive is treated as untrusted: the frame count is rejected if it
    exceeds ``max_frames``; each frame is stream-decompressed in chunks while a
    running total of *actual* decompressed bytes is enforced against
    ``max_zip_bytes`` (the authoritative zip-bomb guard, aborting mid-read on a
    high-ratio crafted entry); the cheap summed *declared* size is checked first
    as a fast pre-filter; and entries are read by exact name rather than
    extracted to disk (avoiding path traversal).

    Args:
        client: The Pixiv surface used for metadata and the zip download.
        work: The bookmarked ugoira work to back up (its ``id`` drives the
            metadata lookup).
        dest: Destination path for the assembled GIF.
        skip_existing: When true, leave an already-complete ``dest`` untouched.
        referer: Referer header value forwarded to the download.
        max_zip_bytes: Upper bound on the *actual* decompressed size summed
            across all frame entries; enforced during streaming extraction
            (zip-bomb guard) and also as a declared-size pre-check.
        max_frames: Upper bound on the number of frames accepted.

    Returns:
        A :class:`UgoiraResult` describing the outcome. Never raises for an
        expected failure.
    """

    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return UgoiraResult(ImageStatus.SKIPPED, dest.stat().st_size, None)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # An unpredictable temp name (mkstemp, mode 0o600) avoids the symlink
    # pre-create race a fixed sibling path would expose.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".ugoira.zip")
    os.close(fd)
    zip_path = Path(tmp_name)
    try:
        info = client.ugoira_metadata(work.id)

        if len(info.frames) > max_frames:
            raise ValueError(
                f"ugoira has {len(info.frames)} frames, exceeding the "
                f"limit of {max_frames}"
            )

        client.download(info.zip_url, zip_path, referer)

        frames: list[tuple[bytes, int]] = []
        with zipfile.ZipFile(zip_path) as archive:
            # Cheap first filter: reject if the *declared* uncompressed sizes
            # already blow the budget, before decompressing anything.
            declared = sum(
                archive.getinfo(frame.file).file_size for frame in info.frames
            )
            if declared > max_zip_bytes:
                raise ValueError(
                    f"ugoira frames declare {declared} uncompressed bytes, "
                    f"exceeding the limit of {max_zip_bytes}"
                )

            # Authoritative guard: stream each frame and enforce the running
            # total of *actual* decompressed bytes. Read by exact name into
            # memory -- never extractall / extract to disk, so a crafted member
            # name cannot escape the archive.
            remaining = max_zip_bytes
            for frame in info.frames:
                data = _read_frame_capped(archive, frame.file, remaining=remaining)
                remaining -= len(data)
                frames.append((data, frame.delay))

        assemble_gif(frames, dest)
    except Exception as exc:  # noqa: BLE001 -- failure is recorded, not raised.
        return UgoiraResult(ImageStatus.FAILED, None, str(exc))
    finally:
        zip_path.unlink(missing_ok=True)

    return UgoiraResult(ImageStatus.DOWNLOADED, dest.stat().st_size, None)
