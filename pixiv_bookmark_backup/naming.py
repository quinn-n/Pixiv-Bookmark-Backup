"""Pure filename and path helpers for pixiv-bookmark-backup.

The on-disk layout is *flat*: every image lives directly under the output
directory and is named with Pixiv's ``{work_id}_p{page}.{ext}`` convention.
These helpers are stdlib-only, side-effect-free, and easily tested; the
downloader and manifest code import them rather than re-deriving names.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from urllib.parse import urlsplit

#: A safe filename extension: 1-10 lowercase alphanumerics. Anything else (a
#: backslash, percent-escape, control char, or absurdly long run) is rejected so
#: a hostile URL cannot inject path separators or garbage into a filename.
_SAFE_EXT = re.compile(r"[a-z0-9]{1,10}")


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
