"""Unit tests for the pure filename/path helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_bookmark_backup.naming import (
    ext_from_url,
    image_filename,
    relative_to_output,
    ugoira_filename,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a/12345678_p0.jpg", "jpg"),
        ("https://example.com/a/12345678_p0.png", "png"),
        ("https://example.com/a/12345678_p0.gif", "gif"),
        ("https://example.com/a/12345678_p0.jpg?foo=bar", "jpg"),
        ("https://example.com/a/12345678_p0.png?w=600&h=400#frag", "png"),
        ("https://example.com/a/12345678_p0.JPG", "jpg"),
        ("https://example.com/a/12345678_p0.JpG?x=1", "jpg"),
        ("https://example.com/a/no_extension_here", ""),
        ("https://example.com/a/no_extension_here?ext=.jpg", ""),
        (
            "https://i.pximg.net/img-original/img/2024/01/02/03/04/05/"
            "12345678_p0.jpg?foo=bar",
            "jpg",
        ),
    ],
)
def test_ext_from_url(url: str, expected: str) -> None:
    assert ext_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # A backslash in the extension must not leak into a filename.
        "https://example.com/a/file.jp\\g",
        # A percent-escaped slash is hostile and must be rejected.
        "https://example.com/a/file.%2Fjpg",
        # A percent-escape anywhere in the extension is rejected.
        "https://example.com/a/file.jp%67",
        # An absurdly long "extension" (>10 chars) is not a real one.
        "https://example.com/a/file." + "a" * 50,
        # No extension at all.
        "https://example.com/a/no_extension_here",
        # A trailing slash leaves an empty extension.
        "https://example.com/a/dir/",
        # A space in the extension is not alphanumeric.
        "https://example.com/a/file.jp%20g",
    ],
)
def test_ext_from_url_rejects_hostile_or_odd_extensions(url: str) -> None:
    # Any extension that is not a plain 1-10 char alphanumeric token is dropped
    # so the caller's default-extension fallback applies.
    assert ext_from_url(url) == ""


def test_image_filename_with_dropped_ext_has_no_separators() -> None:
    # When ext_from_url yields "" for a hostile URL, the resulting filename is
    # still a single safe segment (no path separators, no garbage).
    ext = ext_from_url("https://example.com/a/file.jp\\g")
    name = image_filename(12345678, 0, ext)
    assert name == "12345678_p0."
    assert "/" not in name
    assert "\\" not in name


@pytest.mark.parametrize(
    ("work_id", "page", "ext", "expected"),
    [
        (12345678, 0, "jpg", "12345678_p0.jpg"),
        (12345678, 1, "png", "12345678_p1.png"),
        (42, 0, ".jpg", "42_p0.jpg"),
        (42, 3, ".PNG", "42_p3.png"),
        (99, 0, "JPG", "99_p0.jpg"),
        (99, 10, "JpEg", "99_p10.jpeg"),
    ],
)
def test_image_filename(work_id: int, page: int, ext: str, expected: str) -> None:
    assert image_filename(work_id, page, ext) == expected


@pytest.mark.parametrize(
    ("work_id", "expected"),
    [
        (12345678, "12345678_p0.gif"),
        (42, "42_p0.gif"),
    ],
)
def test_ugoira_filename(work_id: int, expected: str) -> None:
    assert ugoira_filename(work_id) == expected


def test_relative_to_output_flat_is_just_filename(tmp_path: Path) -> None:
    output_dir = tmp_path / "backup"
    path = output_dir / "12345678_p0.jpg"
    assert relative_to_output(path, output_dir) == "12345678_p0.jpg"


def test_relative_to_output_nested_uses_forward_slashes(tmp_path: Path) -> None:
    output_dir = tmp_path / "backup"
    path = output_dir / "sub" / "dir" / "12345678_p0.jpg"
    assert relative_to_output(path, output_dir) == "sub/dir/12345678_p0.jpg"
