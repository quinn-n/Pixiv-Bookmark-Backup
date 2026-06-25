"""Shared typed records for pixiv-bookmark-backup.

The single home for cross-module value objects: the manifest, Pixiv client,
downloader, ugoira handling, and backup orchestration all import the frozen
records defined here. Everything is a plain ``attrs`` ``@frozen`` value object
with no third-party dependencies beyond ``attrs`` (and stdlib ``enum``).
"""

from __future__ import annotations

from enum import Enum

from attrs import frozen


class ImageStatus(str, Enum):
    """Outcome of attempting to obtain a single image/work locally.

    Subclasses ``str`` so members compare and serialize as their string value
    (e.g. ``ImageStatus.DOWNLOADED == "downloaded"``).
    """

    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"
    METADATA_ONLY = "metadata-only"


@frozen
class Author:
    """The Pixiv user who created a work."""

    id: int
    name: str
    account: str


@frozen
class WorkTag:
    """A tag attached to a work, with an optional translation.

    ``translated_name`` is ``None`` when Pixiv provides no translation.
    """

    name: str
    translated_name: str | None = None


@frozen
class WorkImage:
    """One page of a work at original resolution."""

    page: int
    """Zero-based page index within the work."""

    source_url: str
    """Original-resolution image URL."""


@frozen
class UgoiraFrame:
    """A single animation frame inside an ugoira zip."""

    file: str
    """Filename of the frame within the ugoira zip."""

    delay: int
    """Frame display duration in milliseconds."""


@frozen
class UgoiraInfo:
    """Metadata needed to reconstruct an ugoira animation."""

    zip_url: str
    frames: list[UgoiraFrame]


@frozen
class BookmarkWork:
    """A bookmarked Pixiv work and the metadata needed to back it up."""

    id: int
    type: str
    """Work kind: ``"illust"``, ``"manga"``, or ``"ugoira"``."""

    title: str
    author: Author
    posted_at: str
    """Creation timestamp, ISO-8601 with timezone preserved (Pixiv ``create_date``)."""

    x_restrict: int
    sanity_level: int
    work_tags: list[WorkTag]
    bookmark_tags: list[str] | None
    """Personal bookmark tags; ``None`` when not fetched / unavailable."""

    page_count: int
    images: list[WorkImage]
    """One :class:`WorkImage` per page, at original resolution."""
