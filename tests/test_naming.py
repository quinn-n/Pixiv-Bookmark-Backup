"""Unit tests for the pure filename/path helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_bookmark_backup.manifest import ImageEntry, WorkEntry
from pixiv_bookmark_backup.naming import (
    author_dir_name,
    ext_from_url,
    image_filename,
    relative_to_output,
    safe_basename,
    sanitize_component,
    strip_control,
    titled_link_name,
    ugoira_filename,
)


def _work_entry(
    *,
    title: str = "Title",
    author_id: object = 1,
    name: str = "Name",
    account: str = "account",
) -> WorkEntry:
    """Build a minimal :class:`WorkEntry` for name-builder tests.

    ``author_id`` is typed ``object`` so a test may pass a hostile *string* id
    (control bytes, separators) matching the untrusted ``dict[str, Any]`` shape
    ``details.json`` deserializes into.
    """

    return WorkEntry(
        id=100,
        type="illust",
        title=title,
        author={"id": author_id, "name": name, "account": account},
        posted_at="2024-01-01T00:00:00+00:00",
        nsfw={"x_restrict": 0, "sanity_level": 2, "is_nsfw": False},
        work_tags=[],
        bookmark_tags=None,
        page_count=1,
        images=[],
    )


def _image_entry(path: str) -> ImageEntry:
    """Build a minimal :class:`ImageEntry` with the given ``path``."""

    return ImageEntry(
        page=0,
        source_url="https://example.com/x.jpg",
        path=path,
        status="downloaded",
        bytes_=1,
        error=None,
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


# --- strip_control -------------------------------------------------------


def test_strip_control_drops_c0_and_del() -> None:
    # An ANSI escape sequence (ESC = \x1b) is stripped of its control byte,
    # leaving only the printable residue; DEL (\x7f) is dropped too.
    assert strip_control("\x1b[31mred\x1b[0m") == "[31mred[0m"
    assert strip_control("a\x7fb") == "ab"
    assert strip_control("\x00\x01\x1f") == ""


def test_strip_control_preserves_printable_and_multibyte() -> None:
    assert strip_control("hello world") == "hello world"
    # Japanese text carries no ASCII control bytes and is untouched.
    assert strip_control("日本語") == "日本語"


# --- sanitize_component --------------------------------------------------


def test_sanitize_component_strips_control_chars() -> None:
    assert sanitize_component("a\x1bb\x7fc", placeholder="x", byte_budget=255) == "abc"


def test_sanitize_component_replaces_both_separators() -> None:
    # Both forward slash and backslash become "_" on every platform, so a
    # sanitized component can never introduce a nested path.
    result = sanitize_component("a/b\\c", placeholder="x", byte_budget=255)
    assert result == "a_b_c"
    assert "/" not in result
    assert "\\" not in result


@pytest.mark.parametrize("text", ["", ".", "..", "   ", "\t\n", " . "])
def test_sanitize_component_meaningless_names_yield_placeholder(text: str) -> None:
    assert sanitize_component(text, placeholder="PLACE", byte_budget=255) == "PLACE"


def test_sanitize_component_maps_windows_reserved_chars() -> None:
    result = sanitize_component('a:b*c?d"e<f>g|h', placeholder="x", byte_budget=255)
    assert result == "a_b_c_d_e_f_g_h"


def test_sanitize_component_dot_runs_and_leading_dot_preserved() -> None:
    # Only the exact names ""/"."/".." are rejected; a longer dot run like "..."
    # and a leading-dot filename like ".foo" are preserved verbatim (leading
    # dots are allowed for cross-platform simplicity).
    assert sanitize_component("...", placeholder="P", byte_budget=255) == "..."
    assert sanitize_component(".foo", placeholder="P", byte_budget=255) == ".foo"


def test_sanitize_component_bytecap_multibyte_on_boundary() -> None:
    # "日本語" is 9 UTF-8 bytes (3 each). A budget of 7 must not split the third
    # codepoint: it truncates cleanly to "日本" (6 bytes), no replacement char.
    result = sanitize_component("日本語", placeholder="P", byte_budget=7)
    assert result == "日本"
    assert len(result.encode("utf-8")) <= 7
    assert "�" not in result


def test_sanitize_component_emoji_budget_edges() -> None:
    # "😀" is a single 4-byte codepoint. At budget 3 it cannot fit at all, so
    # truncation empties the string and the placeholder is returned.
    assert sanitize_component("😀", placeholder="P", byte_budget=3) == "P"
    # At budget 4 it fits exactly.
    assert sanitize_component("😀", placeholder="P", byte_budget=4) == "😀"


def test_sanitize_component_restrips_after_truncation() -> None:
    # Budget of 2 keeps "a " -> re-stripped to "a".
    assert sanitize_component("a bbbb", placeholder="P", byte_budget=2) == "a"


def test_sanitize_component_truncation_that_empties_yields_placeholder() -> None:
    # The only way truncation empties a component is when the budget is smaller
    # than the sole (multibyte) codepoint, so it drops entirely -> placeholder.
    assert sanitize_component("😀", placeholder="P", byte_budget=1) == "P"


def test_sanitize_component_truncation_restrips_trailing_space() -> None:
    # Truncating "ab cd" at 3 bytes yields "ab " which is re-stripped to "ab".
    assert sanitize_component("ab cd", placeholder="P", byte_budget=3) == "ab"


def test_sanitize_component_truncation_to_dotdot_yields_placeholder() -> None:
    # Regression: an over-budget input that TRUNCATES to exactly ".." must still
    # be rejected -- the empty/traversal guard is re-applied after the byte-cap,
    # so the sanitizer never returns a traversal name regardless of budget.
    assert sanitize_component("..xxxxxx", placeholder="SAFE", byte_budget=2) == "SAFE"


def test_sanitize_component_truncation_to_dot_yields_placeholder() -> None:
    # Truncating ".xxxxxx" to 1 byte yields ".", which is rejected.
    assert sanitize_component(".xxxxxx", placeholder="SAFE", byte_budget=1) == "SAFE"


def test_sanitize_component_truncation_to_empty_yields_placeholder() -> None:
    # A sole 4-byte emoji truncated below its width drops entirely -> placeholder.
    assert sanitize_component("😀", placeholder="SAFE", byte_budget=2) == "SAFE"


def test_sanitize_component_nonpositive_budget_fails_safe() -> None:
    # A non-positive budget is clamped to 0 (not negative-sliced from the end),
    # so any non-trivial name empties to the placeholder rather than leaking a
    # truncated-from-the-end fragment.
    assert (
        sanitize_component("hello world", placeholder="SAFE", byte_budget=0) == "SAFE"
    )
    assert (
        sanitize_component("hello world", placeholder="SAFE", byte_budget=-10) == "SAFE"
    )


def test_sanitize_component_large_budget_preserves_dot_runs() -> None:
    # Normal (large-budget) behavior is unchanged: no truncation, so a longer
    # dot run like ".." + text stays verbatim (only exact "."/".."/"" reject).
    assert (
        sanitize_component("..xxxxxx", placeholder="SAFE", byte_budget=255)
        == "..xxxxxx"
    )


def test_sanitize_component_ascii_exact_budget_untouched() -> None:
    assert sanitize_component("abcde", placeholder="P", byte_budget=5) == "abcde"


def test_sanitize_component_ascii_over_budget_truncated() -> None:
    assert sanitize_component("abcdef", placeholder="P", byte_budget=5) == "abcde"


# --- author_dir_name -----------------------------------------------------


def test_author_dir_name_prefers_account() -> None:
    work = _work_entry(author_id=1, name="Alice", account="alice_art")
    assert author_dir_name(work) == "alice_art_1"


@pytest.mark.parametrize("account", ["", "   ", "\x00"])
def test_author_dir_name_falls_back_to_name(account: str) -> None:
    # An empty, blank, or control-only account falls through to the name.
    work = _work_entry(author_id=2, name="Bob", account=account)
    assert author_dir_name(work) == "Bob_2"


def test_author_dir_name_unknown_when_both_empty() -> None:
    work = _work_entry(author_id=7, name="", account="")
    assert author_dir_name(work) == "unknown_7"


def test_author_dir_name_always_ends_with_id() -> None:
    work = _work_entry(author_id=42, name="", account="")
    assert author_dir_name(work).endswith("_42")


def test_author_dir_name_long_multibyte_account_survives_id() -> None:
    work = _work_entry(author_id=99, name="", account="あ" * 200)
    result = author_dir_name(work)
    assert result.endswith("_99")
    assert len(result.encode("utf-8")) <= 255


def test_author_dir_name_separator_in_account_not_nested() -> None:
    work = _work_entry(author_id=5, name="", account="a/b")
    result = author_dir_name(work)
    assert result == "a_b_5"
    assert "/" not in result


def test_author_dir_name_normal_int_id_unchanged() -> None:
    # Back-compat: a normal integer id sanitizes to itself, so the whole name is
    # byte-identical to the pre-sanitization behavior.
    work = _work_entry(author_id=1, name="Alice", account="alice_art")
    assert author_dir_name(work) == "alice_art_1"


def test_author_dir_name_strips_control_char_from_id() -> None:
    # SECURITY: a doctored author.id carrying an ANSI escape (ESC = \x1b) must
    # not leak the control byte into the subdir name (terminal-escape injection).
    work = _work_entry(author_id="\x1b[31mR", name="", account="")
    result = author_dir_name(work)
    assert "\x1b" not in result
    # The stripped residue is retained as the (single-component) id suffix.
    assert result == "unknown_[31mR"


def test_author_dir_name_control_only_id_uses_placeholder() -> None:
    # An id that is entirely control bytes sanitizes to "" and hits the "0"
    # placeholder, so the suffix is always present and never empty.
    work = _work_entry(author_id="\x00\x1f", name="", account="")
    assert author_dir_name(work) == "unknown_0"


def test_author_dir_name_separator_and_traversal_id_stays_single_component() -> None:
    # SECURITY: a crafted id with separators / traversal segments is collapsed to
    # a single sanitized component -- no "/" survives to nest or escape.
    work = _work_entry(author_id="../../../x", name="", account="")
    result = author_dir_name(work)
    assert "/" not in result
    assert result == "unknown_.._.._.._x"


def test_author_dir_name_oversized_id_capped_but_account_survives() -> None:
    # A doctored, oversized id is byte-capped so it cannot crowd the account out;
    # both the (truncated) id and the account remain present, name within limit.
    work = _work_entry(author_id="9" * 1000, name="", account="alice_art")
    result = author_dir_name(work)
    assert result.startswith("alice_art_")
    assert len(result.encode("utf-8")) <= 255


# --- titled_link_name ----------------------------------------------------


def test_titled_link_name_basic() -> None:
    work = _work_entry(title="My Title")
    image = _image_entry("144036249_p0.jpg")
    result = titled_link_name(image, work)
    # Spaces are preserved (only separators/reserved chars are replaced).
    assert result == "My Title_144036249_p0.jpg"
    assert result.endswith("_144036249_p0.jpg")


def test_titled_link_name_empty_title_uses_placeholder() -> None:
    work = _work_entry(title="")
    image = _image_entry("144036249_p0.jpg")
    assert titled_link_name(image, work) == "untitled_144036249_p0.jpg"


def test_titled_link_name_long_multibyte_title_keeps_basename() -> None:
    work = _work_entry(title="あ" * 300)
    image = _image_entry("144036249_p0.jpg")
    result = titled_link_name(image, work)
    assert result.endswith("_144036249_p0.jpg")
    assert len(result.encode("utf-8")) <= 255


def test_titled_link_name_nested_path_uses_basename_only() -> None:
    # image.path may be "<uid>/<file>" for a backup-following manifest; the link
    # name must use only the basename, never leak the nested "123/" segment.
    work = _work_entry(title="My Title")
    image = _image_entry("123/144036249_p0.jpg")
    result = titled_link_name(image, work)
    assert result.endswith("_144036249_p0.jpg")
    assert "123/" not in result
    assert "/" not in result


def test_titled_link_name_strips_control_char_from_basename() -> None:
    # SECURITY: a doctored image.path carrying an ANSI escape (ESC = \x1b) must
    # not leak the control byte into the titled link name.
    work = _work_entry(title="My Title")
    image = _image_entry("evil\x1b[31m_p0.jpg")
    result = titled_link_name(image, work)
    assert "\x1b" not in result
    assert result == "My Title_evil[31m_p0.jpg"


def test_titled_link_name_dotdot_basename_uses_placeholder() -> None:
    # A crafted "sub/.." has ``.name == ".."``; the degenerate basename must
    # become the safe "_" placeholder, never a traversal segment in the name.
    work = _work_entry(title="My Title")
    image = _image_entry("sub/..")
    assert titled_link_name(image, work) == "My Title__"


# --- safe_basename -------------------------------------------------------


def test_safe_basename_clean_ascii_is_noop() -> None:
    # Back-compat: a real manifest's ``<id>_p<n>.<ext>`` passes through verbatim.
    assert safe_basename("144036249_p0.jpg") == "144036249_p0.jpg"


def test_safe_basename_nested_path_returns_bare_name() -> None:
    assert safe_basename("123/144036249_p0.jpg") == "144036249_p0.jpg"


def test_safe_basename_strips_control_chars() -> None:
    # SECURITY: control bytes in the basename are stripped (terminal-escape
    # injection defense) while the printable residue is retained.
    assert safe_basename("evil\x1b[31m_p0.jpg") == "evil[31m_p0.jpg"


@pytest.mark.parametrize("path", ["sub/..", "..", ".", ""])
def test_safe_basename_degenerate_basename_uses_placeholder(path: str) -> None:
    # A basename of ""/"."/".." (e.g. crafted "sub/.." -> "..") is replaced with
    # the safe "_" placeholder rather than a meaningless or traversing name.
    # (``Path("sub/.").name`` is ``"sub"``, a valid name, so it is NOT degenerate
    # and is excluded here.)
    assert safe_basename(path) == "_"
