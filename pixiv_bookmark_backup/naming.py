"""Pure filename and path helpers for pixiv-bookmark-backup.

The on-disk *download* layout is flat: every image lives directly under the
output directory and is named with Pixiv's ``{work_id}_p{page}.{ext}``
convention (:func:`image_filename`, :func:`ugoira_filename`,
:func:`ext_from_url`, :func:`relative_to_output`); the downloader and manifest
code import these rather than re-deriving names.

This module also owns the sanitization and name-builder helpers that turn
*untrusted* Pixiv metadata into safe symlink names for the ``--layout`` feature:
:func:`strip_control` and :func:`sanitize_component` are the security core
(dropping control bytes, neutralizing path separators, rejecting empty/traversal
names, byte-capping), and :func:`author_dir_name`, :func:`titled_link_name`, and
:func:`safe_basename` build the per-layout link names on top of them.
``linking.py`` and ``core.py`` consume these builders so raw title/author/path
metadata never reaches a filename or the terminal unsanitized.

All helpers are stdlib-only, side-effect-free, and easily tested.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from urllib.parse import urlsplit

from pixiv_bookmark_backup.manifest import ImageEntry, WorkEntry

#: A safe filename extension: 1-10 lowercase alphanumerics. Anything else (a
#: backslash, percent-escape, control char, or absurdly long run) is rejected so
#: a hostile URL cannot inject path separators or garbage into a filename.
_SAFE_EXT = re.compile(r"[a-z0-9]{1,10}")

#: Translation table dropping ASCII control characters (C0 range ``\x00``-``\x1f``
#: and DEL ``\x7f``). Untrusted Pixiv metadata (titles, author names, tags) could
#: otherwise smuggle ANSI escape / control bytes into a terminal or a filename.
_CONTROL_CHARS = {code: None for code in range(0x20)} | {0x7F: None}

#: Translation table mapping Windows-reserved filename characters to ``_``. These
#: are illegal in filenames on Windows and best avoided cross-platform, so a
#: sanitized path component never carries one.
_RESERVED_CHARS = {ord(char): "_" for char in ':*?"<>|'}

#: Maximum byte length of a single link/directory name. Most filesystems cap a
#: path component at 255 bytes, so a sanitized name (including its id/basename
#: suffix) is byte-budgeted to stay under this.
_LINK_BYTE_LIMIT = 255

#: Maximum byte length reserved for a sanitized author id in a per-author
#: directory name. A real Pixiv id is a short integer; the cap only bounds a
#: doctored, oversized id so it can never crowd out the account/name budget.
_AUTHOR_ID_BYTE_CAP = 32


def strip_control(text: str) -> str:
    """Return ``text`` with ASCII control characters removed.

    Drops the C0 range (``\\x00``-``\\x1f``) and DEL (``\\x7f``) so untrusted
    title/author/tag metadata cannot inject terminal escape sequences into the
    human-readable output or filenames. Printable text (including any residue
    left after an escape byte is stripped) is preserved.
    """

    return text.translate(_CONTROL_CHARS)


def sanitize_component(text: str, *, placeholder: str, byte_budget: int) -> str:
    """Return ``text`` cleaned into a single safe path component.

    Applies, in order: control-character stripping; unconditional replacement of
    both ``/`` and ``\\`` separators with ``_`` on every platform; mapping of the
    Windows-reserved characters ``:*?"<>|`` to ``_``; and surrounding-whitespace
    stripping. A component that is empty, ``"."``, or ``".."`` after that (any of
    which would be a meaningless or traversing name) yields ``placeholder``.
    Finally the result is capped to ``byte_budget`` UTF-8 bytes, truncating on a
    codepoint boundary (a trailing partial multibyte sequence is dropped cleanly)
    and re-stripping. The empty/traversal guard is applied *again* after
    truncation, since capping an over-budget input can itself produce ``""``,
    ``"."``, or ``".."`` -- so the result is never a meaningless or traversing
    name regardless of ``byte_budget``.

    ``byte_budget`` is normally positive (callers reserve room for a short ASCII
    id/basename suffix). A non-positive budget is clamped to ``0``, which yields
    the placeholder, so the function fails safe rather than relying on Python's
    negative-slice semantics (which would truncate from the *end*).
    """

    # 1. Drop control bytes that could manipulate a terminal or a filename.
    text = strip_control(text)
    # 2. Neutralize path separators on every platform (never nest).
    text = text.replace("/", "_").replace("\\", "_")
    # 3. Map Windows-reserved characters so the name is portable.
    text = text.translate(_RESERVED_CHARS)
    # 4. Trim surrounding whitespace before the empty/dot check.
    text = text.strip()

    # 5. Reject empty and the traversal names "." / ".." with the placeholder.
    if text in ("", ".", ".."):
        return placeholder

    # 6. Byte-cap on a codepoint boundary; drop any trailing partial sequence.
    #    Clamp a non-positive budget to 0 so we never negative-slice (which would
    #    truncate from the end) -- budget 0 empties the name and hits the guard.
    budget = max(0, byte_budget)
    encoded = text.encode("utf-8")
    if len(encoded) > budget:
        text = encoded[:budget].decode("utf-8", errors="ignore").strip()
        # Truncation can produce "", ".", or ".." from a longer input, so the
        # empty/traversal guard must run again to keep the contract self-contained.
        if text in ("", ".", ".."):
            return placeholder
    return text


def author_dir_name(work: WorkEntry) -> str:
    """Return the per-author directory name for ``work`` in a titled layout.

    The name is ``{account_or_name}_{author_id}``: the author's ``account`` is
    preferred, falling back to the display ``name`` when the account is empty or
    control-only, and to the literal ``"unknown"`` when both are empty. The
    ``_{author_id}`` suffix is always appended (so the directory is unique even
    when two authors share a name), and the leading component is byte-budgeted so
    the whole name fits within :data:`_LINK_BYTE_LIMIT` UTF-8 bytes.

    ``author_id`` is untrusted manifest data, so it is run through
    :func:`sanitize_component` (dropping control/separator bytes, rejecting
    ``""``/``"."``/``".."`` via the ``"0"`` placeholder) and capped to
    :data:`_AUTHOR_ID_BYTE_CAP` bytes *before* the account/name budget is
    derived. A normal integer id -- the overwhelmingly common case -- passes
    through unchanged, so ``alice_art_1`` is byte-identical to before.
    """

    author_id = sanitize_component(
        str(work.author["id"]), placeholder="0", byte_budget=_AUTHOR_ID_BYTE_CAP
    )
    suffix = f"_{author_id}"
    budget = _LINK_BYTE_LIMIT - len(suffix.encode("utf-8"))

    # Empty placeholders so a blank/control-only field falls through to the next
    # candidate; only substitute "unknown" when both account and name are empty.
    chosen = sanitize_component(
        str(work.author.get("account", "")), placeholder="", byte_budget=budget
    )
    if not chosen:
        chosen = sanitize_component(
            str(work.author.get("name", "")), placeholder="", byte_budget=budget
        )
    if not chosen:
        chosen = "unknown"

    return f"{chosen}{suffix}"


def safe_basename(image_path: str) -> str:
    """Return the sanitized bare basename of an untrusted ``image_path``.

    Takes ``Path(image_path).name`` (so a nested manifest path like
    ``"<uid>/<file>"`` contributes only its final segment), strips ASCII control
    bytes so a doctored path cannot smuggle a terminal escape into a link name,
    and maps the degenerate results ``""``/``"."``/``".."`` (e.g. a crafted
    ``"sub/.."`` whose ``.name`` is ``".."``) to the safe placeholder ``"_"``.

    For a real manifest the basename is clean ASCII ``<id>_p<n>.<ext>``, so this
    is a no-op there and flat/titled link names stay byte-identical.
    """

    basename = strip_control(Path(image_path).name)
    if basename in ("", ".", ".."):
        return "_"
    return basename


def titled_link_name(image: ImageEntry, work: WorkEntry) -> str:
    """Return the ``{title}_{basename}`` link name for ``image`` under ``work``.

    The link name pairs a sanitized work title with the image's bare filename so
    a titled layout stays human-readable while remaining unique per page. The
    basename is derived via :func:`safe_basename` (bare ``Path.name``, control
    bytes stripped, degenerate ``.``/``..``/empty replaced) because ``image.path``
    is untrusted and may be nested (e.g. ``"<uid>/<file>"`` in a backup-following
    manifest). The title prefix is byte-budgeted so the whole name fits within
    :data:`_LINK_BYTE_LIMIT` UTF-8 bytes, and an empty title becomes
    ``"untitled"``.
    """

    basename = safe_basename(image.path)
    budget = _LINK_BYTE_LIMIT - len(("_" + basename).encode("utf-8"))
    prefix = sanitize_component(work.title, placeholder="untitled", byte_budget=budget)
    return f"{prefix}_{basename}"


def _normalize_ext(ext: str) -> str:
    """Return a bare, lowercased extension (no leading dot) for ``ext``."""

    return ext.lstrip(".").lower()


def image_filename(work_id: int, page: int, ext: str) -> str:
    """Return the flat-layout filename for one page of a work.

    Follows Pixiv's ``{work_id}_p{page}.{ext}`` convention with a zero-based
    ``page``. ``ext`` is the bare extension; a leading dot is stripped and the
    result is lowercased defensively, so ``"JPG"`` and ``".jpg"`` both yield
    ``jpg``.
    """

    return f"{work_id}_p{page}.{_normalize_ext(ext)}"


def ext_from_url(url: str) -> str:
    """Return the bare, lowercased file extension parsed from ``url``.

    The query string and fragment are ignored, so
    ``.../12345678_p0.jpg?foo=bar`` yields ``"jpg"``. Returns ``""`` when the
    URL path has no extension *or* when the parsed extension is not a plain
    1-10 character alphanumeric token -- a hostile or malformed URL (a
    backslash, ``%2F``, control characters, or an absurdly long run) yields
    ``""`` so it can never inject path separators or garbage into a filename
    (the caller falls back to a default extension).
    """

    path = urlsplit(url).path
    ext = _normalize_ext(posixpath.splitext(path)[1])
    if not _SAFE_EXT.fullmatch(ext):
        return ""
    return ext


def ugoira_filename(work_id: int) -> str:
    """Return the flat-layout filename for a converted ugoira animation.

    Ugoira works are stored as a single animated GIF named like the first
    page of any other work, i.e. ``{work_id}_p0.gif``.
    """

    return f"{work_id}_p0.gif"


def relative_to_output(path: Path, output_dir: Path) -> str:
    """Return ``path`` relative to ``output_dir`` as a forward-slash string.

    Suitable for embedding in ``details.json`` regardless of platform: the
    result always uses ``/`` separators. In the flat layout this is typically
    just the bare filename, but the helper handles nested paths and paths that
    are not under ``output_dir`` (which yield ``..`` segments).
    """

    rel = os.path.relpath(path, output_dir)
    return Path(rel).as_posix()
