"""Resilient single-image download with skip-existing and retry/backoff.

This module sits above the one-shot :meth:`PixivClient.download` primitive
(which performs exactly one attempt and always overwrites its target). It owns
two policies the client deliberately does not:

* **Skip-existing.** Whether a file already on disk should be left untouched.
  The decision is ours and is made by *file size*: a present, non-empty file is
  considered complete; a zero-byte or partially written file from an
  interrupted run is treated as missing and re-downloaded.
* **Retry/backoff.** Transient failures (network blips, rate limits) are
  retried with exponential backoff plus jitter.

The function is pure and fully injected (``sleep`` / ``rand``) so tests run
offline and deterministically. A failed image is returned as a recorded
:class:`ImageResult`, never raised -- the orchestrator maps that into the
manifest. Only genuinely unexpected exceptions are allowed to propagate.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from pathlib import Path

from attrs import frozen

from pixiv_bookmark_backup.models import ImageStatus
from pixiv_bookmark_backup.pixiv_client import PIXIV_REFERER, ApiError, PixivClient


@frozen
class ImageResult:
    """Outcome of attempting to obtain a single image locally.

    A lightweight per-image record the orchestrator maps into the manifest.

    Attributes:
        status: How the attempt resolved.
        bytes_: Size of the file in bytes on success/skip, ``None`` on failure.
        error: Human-readable failure reason, ``None`` unless ``status`` is
            :attr:`ImageStatus.FAILED`.
    """

    status: ImageStatus
    bytes_: int | None
    error: str | None


def should_skip(path: Path) -> bool:
    """Return whether ``path`` is an already-complete download to leave alone.

    A file is skippable only when it exists *and* is non-empty. A zero-byte
    file (a common artefact of a run interrupted mid-write) is **not** skipped,
    so it gets re-downloaded rather than silently kept as a truncated image.
    """

    return path.exists() and path.stat().st_size > 0


def download_image(
    client: PixivClient,
    url: str,
    dest: Path,
    *,
    max_retries: int,
    skip_existing: bool,
    referer: str = PIXIV_REFERER,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    base_delay: float = 0.5,
) -> ImageResult:
    """Download ``url`` to ``dest``, applying skip-existing and retry/backoff.

    If ``skip_existing`` is set and ``dest`` is already a complete file (see
    :func:`should_skip`), no download is attempted and the existing file's size
    is reported.

    Otherwise the download is attempted up to ``max_retries + 1`` times (so
    ``max_retries=3`` means one initial try plus three retries). Each attempt
    calls :meth:`PixivClient.download`, which overwrites ``dest``. On an
    :class:`ApiError` with attempts remaining, the call backs off for
    ``base_delay * (2 ** attempt) + rand() * base_delay`` seconds (exponential
    backoff with jitter) before retrying; once attempts are exhausted a
    :class:`ImageResult` with :attr:`ImageStatus.FAILED` is returned.

    Args:
        client: The Pixiv download primitive (one attempt per call).
        url: Original-resolution image URL to fetch.
        dest: Destination path; its parent is created by the client.
        max_retries: Number of retries after the initial attempt.
        skip_existing: When true, leave an already-complete ``dest`` untouched.
        referer: Referer header value forwarded to the client.
        sleep: Injected sleep function (seconds); defaults to :func:`time.sleep`.
        rand: Injected ``[0.0, 1.0)`` source for jitter; defaults to
            :func:`random.random`.
        base_delay: Base backoff unit in seconds.

    Returns:
        An :class:`ImageResult` describing the outcome. This function never
        raises :class:`ApiError`; truly unexpected exceptions propagate.
    """

    if skip_existing and should_skip(dest):
        return ImageResult(ImageStatus.SKIPPED, dest.stat().st_size, None)

    # Attempts 0..max_retries inclusive: one initial try plus max_retries retries.
    for attempt in range(max_retries + 1):
        try:
            written = client.download(url, dest, referer)
        except ApiError as err:
            # v1 treats every ApiError as retryable. A future refinement could
            # classify by HTTP status and honor Retry-After, but the one-shot
            # client surface gives us neither here, so we do not pretend to.
            if attempt < max_retries:
                sleep(base_delay * (2**attempt) + rand() * base_delay)
                continue
            return ImageResult(ImageStatus.FAILED, None, str(err))
        else:
            return ImageResult(ImageStatus.DOWNLOADED, written, None)

    # Unreachable: the loop always returns. Present to satisfy the type checker
    # for the degenerate (negative max_retries) input.
    return ImageResult(ImageStatus.FAILED, None, "no download attempt was made")
