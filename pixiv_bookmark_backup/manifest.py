"""The ``details.json`` aggregate manifest for pixiv-bookmark-backup.

A single ``details.json`` lives at the output root and records every backed-up
work plus per-image outcomes. It is written *atomically* (temp file in the same
directory, then :func:`os.replace`) and *merged on resume* so an interrupted run
can be continued without losing previously recorded results.

All image paths stored here are **relative** to the output directory, so a
backup folder stays portable. The records are plain ``attrs`` ``@frozen`` value
objects; serialization to/from JSON-ready ``dict``\\s is explicit (the
``ImageEntry.bytes_`` attribute maps to the JSON key ``"bytes"``).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from attrs import field, frozen

from pixiv_bookmark_backup.models import BookmarkWork, ImageStatus

#: Version of the ``details.json`` layout. Bumped on any breaking change; the
#: caller compares this against a loaded file's ``schema_version`` and, on a
#: mismatch, backs the old file up and regenerates.
SCHEMA_VERSION: int = 1


@frozen
class ImageEntry:
    """The recorded outcome for one page/image of a work.

    ``path`` is relative to the output directory. ``bytes_`` is the downloaded
    size in bytes (``None`` when not downloaded); it is named with a trailing
    underscore to avoid shadowing the builtin and serializes to the JSON key
    ``"bytes"``. ``error`` carries a short message for failed images.
    """

    page: int
    source_url: str
    path: str
    status: str
    bytes_: int | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready ``dict``; ``bytes_`` becomes the key ``"bytes"``."""

        return {
            "page": self.page,
            "source_url": self.source_url,
            "path": self.path,
            "status": self.status,
            "bytes": self.bytes_,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageEntry:
        """Build an :class:`ImageEntry` from a JSON ``dict`` (``"bytes"`` key)."""

        return cls(
            page=data["page"],
            source_url=data["source_url"],
            path=data["path"],
            status=data["status"],
            bytes_=data.get("bytes"),
            error=data.get("error"),
        )


@frozen
class WorkEntry:
    """The recorded state of a single backed-up work.

    ``author``, ``nsfw``, and each ``work_tags`` element are plain ``dict``\\s so
    the entry serializes directly to JSON. ``bookmark_tags`` is copied verbatim
    from the source and may be ``None`` when not fetched.
    """

    id: int
    type: str
    title: str
    author: dict[str, Any]
    posted_at: str
    nsfw: dict[str, Any]
    work_tags: list[dict[str, Any]]
    bookmark_tags: list[str] | None
    page_count: int
    images: list[ImageEntry]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready ``dict`` with each image serialized in turn."""

        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "author": dict(self.author),
            "posted_at": self.posted_at,
            "nsfw": dict(self.nsfw),
            "work_tags": [dict(tag) for tag in self.work_tags],
            "bookmark_tags": (
                None if self.bookmark_tags is None else list(self.bookmark_tags)
            ),
            "page_count": self.page_count,
            "images": [image.to_dict() for image in self.images],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkEntry:
        """Build a :class:`WorkEntry` from a JSON ``dict``."""

        return cls(
            id=data["id"],
            type=data["type"],
            title=data["title"],
            author=dict(data["author"]),
            posted_at=data["posted_at"],
            nsfw=dict(data["nsfw"]),
            work_tags=[dict(tag) for tag in data["work_tags"]],
            bookmark_tags=(
                None
                if data.get("bookmark_tags") is None
                else list(data["bookmark_tags"])
            ),
            page_count=data["page_count"],
            images=[ImageEntry.from_dict(image) for image in data["images"]],
        )


@frozen
class Manifest:
    """The full ``details.json`` document.

    ``source`` records what was backed up (``{"user_id": ..., "visibility":
    ...}``); ``errors`` holds work-level failure records that are not tied to a
    single downloaded image.
    """

    schema_version: int
    generated_at: str
    source: dict[str, Any]
    works: list[WorkEntry] = field(factory=list)
    errors: list[dict[str, Any]] = field(factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready ``dict`` form of the whole manifest."""

        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "source": dict(self.source),
            "works": [work.to_dict() for work in self.works],
            "errors": [dict(error) for error in self.errors],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        """Build a :class:`Manifest` from a parsed ``details.json`` ``dict``."""

        return cls(
            schema_version=data["schema_version"],
            generated_at=data["generated_at"],
            source=dict(data["source"]),
            works=[WorkEntry.from_dict(work) for work in data.get("works", [])],
            errors=[dict(error) for error in data.get("errors", [])],
        )


def derive_nsfw(x_restrict: int, sanity_level: int) -> dict[str, Any]:
    """Return the ``nsfw`` block derived from Pixiv's restriction fields.

    A work is treated as NSFW when ``x_restrict`` is positive (``1`` for R-18,
    ``2`` for R-18G); ``sanity_level`` is carried through unchanged for callers
    that want a finer-grained signal.
    """

    return {
        "x_restrict": x_restrict,
        "sanity_level": sanity_level,
        "is_nsfw": x_restrict > 0,
    }


def make_image_entry(
    page: int,
    source_url: str,
    path: str,
    status: ImageStatus | str,
    bytes_: int | None,
    error: str | None,
) -> ImageEntry:
    """Build an :class:`ImageEntry`, normalizing ``status`` to its string value.

    ``status`` may be an :class:`~pixiv_bookmark_backup.models.ImageStatus`
    member or its plain string value; the stored value is always the string.
    """

    status_value = status.value if isinstance(status, ImageStatus) else status
    return ImageEntry(
        page=page,
        source_url=source_url,
        path=path,
        status=status_value,
        bytes_=bytes_,
        error=error,
    )


def build_work_entry(work: BookmarkWork, images: list[ImageEntry]) -> WorkEntry:
    """Assemble a :class:`WorkEntry` from a ``BookmarkWork`` and its images.

    The author is nested as ``{"id", "name", "account"}``, the ``nsfw`` block is
    derived via :func:`derive_nsfw`, work tags become ``{"name",
    "translated_name"}`` dicts, and ``bookmark_tags`` is copied verbatim (it may
    be ``None``). ``images`` is the list of already-built per-page entries.
    """

    return WorkEntry(
        id=work.id,
        type=work.type,
        title=work.title,
        author={
            "id": work.author.id,
            "name": work.author.name,
            "account": work.author.account,
        },
        posted_at=work.posted_at,
        nsfw=derive_nsfw(work.x_restrict, work.sanity_level),
        work_tags=[
            {"name": tag.name, "translated_name": tag.translated_name}
            for tag in work.work_tags
        ],
        bookmark_tags=(
            None if work.bookmark_tags is None else list(work.bookmark_tags)
        ),
        page_count=work.page_count,
        images=list(images),
    )


def new_manifest(
    user_id: str | None,
    visibility: str,
    generated_at: str,
    works: list[WorkEntry],
    errors: list[dict[str, Any]],
) -> Manifest:
    """Build a :class:`Manifest`, stamping the current :data:`SCHEMA_VERSION`.

    ``generated_at`` is supplied by the caller (the orchestrator injects the
    timestamp) rather than read from the clock here, which keeps the result
    deterministic and easy to test.
    """

    return Manifest(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        source={"user_id": user_id, "visibility": visibility},
        works=list(works),
        errors=list(errors),
    )


def new_following_manifest(
    following_of: str | None,
    follow_visibility: str,
    generated_at: str,
    works: list[WorkEntry],
    errors: list[dict[str, Any]],
) -> Manifest:
    """Build a :class:`Manifest` for a ``backup-following`` run.

    Identical to :func:`new_manifest` -- same :data:`SCHEMA_VERSION`, and the
    same ``generated_at``/``works``/``errors`` wiring -- except the ``source``
    block is *following*-shaped (``{"mode": "following", "following_of": ...,
    "follow_visibility": ...}``) instead of bookmark-shaped, so a following
    backup's ``details.json`` records which followed-user feed it came from.

    ``generated_at`` is supplied by the caller (the orchestrator injects the
    timestamp) rather than read from the clock here, which keeps the result
    deterministic and easy to test.
    """

    return Manifest(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        source={
            "mode": "following",
            "following_of": following_of,
            "follow_visibility": follow_visibility,
        },
        works=list(works),
        errors=list(errors),
    )


def merge(existing: Manifest, new: Manifest) -> Manifest:
    """Merge a fresh run's ``new`` manifest over the ``existing`` one for resume.

    Works are keyed by ``id``: a work present in ``new`` replaces the existing
    record of the same id, brand-new works are appended (after the preserved
    ones), and works present only in ``existing`` are kept. Existing order is
    preserved for retained works; new-only works follow in ``new``'s order.

    Errors are unioned by work id with the same rule -- an error whose
    ``"work_id"`` appears in ``new`` is taken from ``new``; existing errors for
    ids absent from ``new`` are kept; new errors for unseen ids are appended.
    Errors without a ``"work_id"`` are treated as new-only and appended.

    ``new``'s ``generated_at`` and ``source`` win, since they describe the run
    that produced the merged result.
    """

    new_by_id = {work.id: work for work in new.works}
    merged_works: list[WorkEntry] = []
    seen_ids: set[int] = set()
    for work in existing.works:
        merged_works.append(new_by_id.get(work.id, work))
        seen_ids.add(work.id)
    for work in new.works:
        if work.id not in seen_ids:
            merged_works.append(work)
            seen_ids.add(work.id)

    new_error_ids = {
        error["work_id"] for error in new.errors if error.get("work_id") is not None
    }
    merged_errors: list[dict[str, Any]] = []
    for error in existing.errors:
        work_id = error.get("work_id")
        if work_id is not None and work_id in new_error_ids:
            continue
        merged_errors.append(dict(error))
    for error in new.errors:
        merged_errors.append(dict(error))

    return Manifest(
        schema_version=new.schema_version,
        generated_at=new.generated_at,
        source=dict(new.source),
        works=merged_works,
        errors=merged_errors,
    )


def load_or_init(path: Path) -> tuple[Manifest, bool]:
    """Load an existing manifest at ``path`` or return a fresh empty one.

    Returns ``(manifest, schema_mismatch)``. When ``path`` is missing, returns
    an empty manifest (no works, no errors, current :data:`SCHEMA_VERSION`) with
    ``schema_mismatch`` ``False``.

    When the file exists and parses, ``schema_mismatch`` is
    ``data["schema_version"] != SCHEMA_VERSION``. On a *matching* schema the
    parsed manifest is returned; on a *mismatch* an empty manifest is returned
    (so the caller never tries to interpret a foreign layout) -- the caller is
    responsible for writing the ``.bak`` and regenerating.
    """

    if not path.exists():
        return _empty_manifest(), False

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    schema_mismatch = data.get("schema_version") != SCHEMA_VERSION
    if schema_mismatch:
        return _empty_manifest(), True
    return Manifest.from_dict(data), False


def write_atomic(path: Path, manifest: Manifest) -> None:
    """Write ``manifest`` to ``path`` atomically and human-readably.

    The JSON is dumped (``indent=2``, ``ensure_ascii=False`` so Japanese tags
    stay readable) to a temporary file created via :func:`tempfile.mkstemp` in
    the *same* directory -- an unpredictable name with mode ``0o600`` (so the
    token-bearing manifest is never group/other-readable and the predictable
    sibling name cannot be pre-created as a symlink) -- then :func:`os.replace`
    renames it onto ``path``. That rename is atomic on the same filesystem, so a
    crash mid-write can never leave a truncated ``details.json``. The temp file
    is removed if anything fails before the rename. The parent directory is
    created if needed.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest.to_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        # On any failure (including the os.replace), leave no temp artifact.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _empty_manifest() -> Manifest:
    """Return a fresh manifest with no works/errors and an empty source block."""

    return Manifest(
        schema_version=SCHEMA_VERSION,
        generated_at="",
        source={},
        works=[],
        errors=[],
    )
