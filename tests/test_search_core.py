"""Unit tests for the offline ``search`` core logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pixiv_bookmark_backup.core import (
    NSFW_MODES,
    SEARCH_FIELDS,
    SORT_KEYS,
    SearchConfig,
    SearchMatch,
    author_ok,
    blacklisted,
    format_work_line,
    is_keyword_match,
    matched_fields,
    normalize_fields,
    nsfw_ok,
    run_search,
    sort_works,
)
from pixiv_bookmark_backup.manifest import WorkEntry


def _work(
    *,
    work_id: int = 1,
    type_: str = "illust",
    title: str = "Sunset",
    author_name: str = "Alice",
    author_account: str = "alice_art",
    posted_at: str = "2024-05-01T12:00:00+09:00",
    is_nsfw: bool = False,
    work_tags: list[tuple[str, str | None]] | None = None,
    bookmark_tags: list[str] | None = None,
) -> WorkEntry:
    """Build a :class:`WorkEntry` with the fields the search logic reads.

    ``work_tags`` is a list of ``(name, translated_name)`` pairs (translated may
    be ``None``); ``bookmark_tags`` may be ``None`` to exercise the not-fetched
    case. Only the attributes the search functions touch are populated.
    """

    if work_tags is None:
        work_tags = [("scenery", "風景")]
    return WorkEntry(
        id=work_id,
        type=type_,
        title=title,
        author={"id": 100, "name": author_name, "account": author_account},
        posted_at=posted_at,
        nsfw={"x_restrict": 1 if is_nsfw else 0, "sanity_level": 2, "is_nsfw": is_nsfw},
        work_tags=[{"name": n, "translated_name": t} for n, t in work_tags],
        bookmark_tags=bookmark_tags,
        page_count=1,
        images=[],
    )


# --------------------------------------------------------------------------- #
# SearchConfig validation
# --------------------------------------------------------------------------- #


def test_search_config_defaults(tmp_path: Path) -> None:
    config = SearchConfig(input_path=tmp_path)
    assert config.keywords == ()
    assert config.fields == SEARCH_FIELDS
    assert config.exact is False
    assert config.match_all is False
    assert config.nsfw == "include"
    assert config.sort == "posted"
    assert config.limit is None
    assert config.count is False


def test_search_config_input_path_is_coerced() -> None:
    config = SearchConfig(input_path="some/details.json")
    assert config.input_path == Path("some/details.json")


def test_search_config_rejects_bad_nsfw(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SearchConfig(input_path=tmp_path, nsfw="maybe")


def test_search_config_rejects_bad_sort(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SearchConfig(input_path=tmp_path, sort="random")


def test_search_config_rejects_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SearchConfig(input_path=tmp_path, fields=("work", "bogus"))


def test_search_config_rejects_empty_fields(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SearchConfig(input_path=tmp_path, fields=())


def test_search_config_accepts_full_field_triple(tmp_path: Path) -> None:
    config = SearchConfig(input_path=tmp_path, fields=SEARCH_FIELDS)
    assert config.fields == SEARCH_FIELDS


def test_search_config_rejects_limit_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SearchConfig(input_path=tmp_path, limit=0)


def test_search_config_allows_limit_none(tmp_path: Path) -> None:
    assert SearchConfig(input_path=tmp_path, limit=None).limit is None


def test_module_constants_are_stable() -> None:
    assert SEARCH_FIELDS == ("work", "bookmark", "translated")
    assert NSFW_MODES == ("include", "exclude", "only")
    assert SORT_KEYS == ("posted", "id", "title")


# --------------------------------------------------------------------------- #
# normalize_fields
# --------------------------------------------------------------------------- #


def test_normalize_fields_empty_becomes_all() -> None:
    assert normalize_fields(()) == SEARCH_FIELDS


def test_normalize_fields_subset_unchanged() -> None:
    assert normalize_fields(("bookmark",)) == ("bookmark",)


# --------------------------------------------------------------------------- #
# Keyword matching: OR / AND / exact vs substring
# --------------------------------------------------------------------------- #


def _config(tmp_path: Path, **kwargs: Any) -> SearchConfig:
    return SearchConfig(input_path=tmp_path, **kwargs)


def test_keyword_or_matches_any(tmp_path: Path) -> None:
    work = _work(work_tags=[("cat", None)])
    config = _config(tmp_path, keywords=("cat", "dog"))
    assert is_keyword_match(work, config) is True


def test_keyword_or_no_match(tmp_path: Path) -> None:
    work = _work(work_tags=[("cat", None)])
    config = _config(tmp_path, keywords=("dog", "fish"))
    assert is_keyword_match(work, config) is False


def test_keyword_and_requires_all(tmp_path: Path) -> None:
    work = _work(work_tags=[("cat", None), ("dog", None)])
    both = _config(tmp_path, keywords=("cat", "dog"), match_all=True)
    missing = _config(tmp_path, keywords=("cat", "bird"), match_all=True)
    assert is_keyword_match(work, both) is True
    assert is_keyword_match(work, missing) is False


def test_zero_keywords_always_matches(tmp_path: Path) -> None:
    assert is_keyword_match(_work(), _config(tmp_path)) is True


@pytest.mark.parametrize("keyword", ["CAT", "cat", "Cat"])
def test_substring_is_case_insensitive(tmp_path: Path, keyword: str) -> None:
    work = _work(work_tags=[("Category", None)])
    config = _config(tmp_path, keywords=(keyword,))
    assert is_keyword_match(work, config) is True


@pytest.mark.parametrize("keyword", ["CAT", "cat", "Cat"])
def test_exact_is_case_insensitive(tmp_path: Path, keyword: str) -> None:
    work = _work(work_tags=[("Cat", None)])
    match = _config(tmp_path, keywords=(keyword,), exact=True)
    assert is_keyword_match(work, match) is True


def test_exact_rejects_substring(tmp_path: Path) -> None:
    work = _work(work_tags=[("Category", None)])
    config = _config(tmp_path, keywords=("cat",), exact=True)
    assert is_keyword_match(work, config) is False


# --------------------------------------------------------------------------- #
# Field scoping
# --------------------------------------------------------------------------- #


def test_field_scoping_bookmark_only(tmp_path: Path) -> None:
    work = _work(work_tags=[("scenery", None)], bookmark_tags=["favorite"])
    in_bookmark = _config(tmp_path, keywords=("favorite",), fields=("bookmark",))
    in_work = _config(tmp_path, keywords=("favorite",), fields=("work",))
    assert is_keyword_match(work, in_bookmark) is True
    assert is_keyword_match(work, in_work) is False


def test_translated_matches_translated_name_only(tmp_path: Path) -> None:
    work = _work(work_tags=[("風景", "scenery")])
    translated = _config(tmp_path, keywords=("scenery",), fields=("translated",))
    raw = _config(tmp_path, keywords=("scenery",), fields=("work",))
    assert is_keyword_match(work, translated) is True
    assert is_keyword_match(work, raw) is False


# --------------------------------------------------------------------------- #
# matched_fields
# --------------------------------------------------------------------------- #


def test_matched_fields_sorted_distinct_subset(tmp_path: Path) -> None:
    work = _work(work_tags=[("cat", "cat-t")], bookmark_tags=["cat"])
    config = _config(tmp_path, keywords=("cat",))
    result = matched_fields(work, config)
    assert result == ["bookmark", "translated", "work"]
    assert result == sorted(set(result))


def test_matched_fields_only_hit_surfaces(tmp_path: Path) -> None:
    work = _work(work_tags=[("scenery", None)], bookmark_tags=["mine"])
    config = _config(tmp_path, keywords=("mine",))
    assert matched_fields(work, config) == ["bookmark"]


def test_matched_fields_browse_mode_returns_fields(tmp_path: Path) -> None:
    config = _config(tmp_path, fields=("work", "bookmark"))
    # Browse mode preserves fields order, does not sort.
    assert matched_fields(_work(), config) == ["work", "bookmark"]


# --------------------------------------------------------------------------- #
# Author filter
# --------------------------------------------------------------------------- #


def test_author_ok_no_filter(tmp_path: Path) -> None:
    assert author_ok(_work(), _config(tmp_path)) is True


def test_author_ok_substring_name(tmp_path: Path) -> None:
    work = _work(author_name="Alice Wonderland")
    assert author_ok(work, _config(tmp_path, authors=("wonder",))) is True


def test_author_ok_substring_account(tmp_path: Path) -> None:
    work = _work(author_account="alice_art_99")
    assert author_ok(work, _config(tmp_path, authors=("art",))) is True


def test_author_ok_multiple_terms_or(tmp_path: Path) -> None:
    work = _work(author_name="Alice")
    assert author_ok(work, _config(tmp_path, authors=("bob", "alice"))) is True


def test_author_ok_no_match(tmp_path: Path) -> None:
    work = _work(author_name="Alice", author_account="alice_art")
    assert author_ok(work, _config(tmp_path, authors=("zephyr",))) is False


def test_author_anded_with_keyword_in_run_search(tmp_path: Path) -> None:
    match = _work(work_id=1, author_name="Alice", work_tags=[("cat", None)])
    wrong_author = _work(
        work_id=2,
        author_name="Bob",
        author_account="bob_draws",
        work_tags=[("cat", None)],
    )
    wrong_kw = _work(work_id=3, author_name="Alice", work_tags=[("dog", None)])
    config = _config(tmp_path, keywords=("cat",), authors=("alice",))
    matches, scanned = run_search([match, wrong_author, wrong_kw], config)
    assert scanned == 3
    assert [m.work.id for m in matches] == [1]


# --------------------------------------------------------------------------- #
# Blacklists
# --------------------------------------------------------------------------- #


def test_blacklist_tag_exact_case_insensitive(tmp_path: Path) -> None:
    work = _work(work_tags=[("Gore", None)])
    assert blacklisted(work, _config(tmp_path, blacklist_tags=("gore",))) is True


def test_blacklist_tag_substring_near_miss_not_excluded(tmp_path: Path) -> None:
    work = _work(work_tags=[("category", None)])
    # Blacklist is EXACT on tags: "cat" must not blacklist "category".
    assert blacklisted(work, _config(tmp_path, blacklist_tags=("cat",))) is False


def test_blacklist_tag_hits_bookmark_surface(tmp_path: Path) -> None:
    work = _work(work_tags=[("scenery", None)], bookmark_tags=["Spoiler"])
    assert blacklisted(work, _config(tmp_path, blacklist_tags=("spoiler",))) is True


def test_blacklist_tag_ignores_fields_scope(tmp_path: Path) -> None:
    # Even with fields=("work",), a bookmark-surface blacklist tag still excludes.
    work = _work(work_tags=[("scenery", None)], bookmark_tags=["nope"])
    config = _config(tmp_path, fields=("work",), blacklist_tags=("nope",))
    assert blacklisted(work, config) is True


def test_blacklist_tag_hits_translated_surface(tmp_path: Path) -> None:
    work = _work(work_tags=[("風景", "gore")])
    assert blacklisted(work, _config(tmp_path, blacklist_tags=("gore",))) is True


def test_blacklist_author_substring(tmp_path: Path) -> None:
    work = _work(author_name="Bad Actor", author_account="villain")
    assert blacklisted(work, _config(tmp_path, blacklist_authors=("villain",))) is True
    assert blacklisted(work, _config(tmp_path, blacklist_authors=("bad",))) is True


def test_blacklist_none_configured(tmp_path: Path) -> None:
    assert blacklisted(_work(), _config(tmp_path)) is False


# --------------------------------------------------------------------------- #
# NSFW tri-state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("is_nsfw", "mode", "expected"),
    [
        (True, "include", True),
        (False, "include", True),
        (True, "exclude", False),
        (False, "exclude", True),
        (True, "only", True),
        (False, "only", False),
    ],
)
def test_nsfw_tristate(
    tmp_path: Path, is_nsfw: bool, mode: str, expected: bool
) -> None:
    work = _work(is_nsfw=is_nsfw)
    assert nsfw_ok(work, _config(tmp_path, nsfw=mode)) is expected


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #


def test_sort_posted_descending_mixed_offsets_and_garbage() -> None:
    # 2024-05-01T12:00:00+09:00 == 03:00 UTC; 2024-05-01T10:00:00+00:00 later.
    older = _work(work_id=1, posted_at="2024-05-01T12:00:00+09:00")
    newer = _work(work_id=2, posted_at="2024-05-01T10:00:00+00:00")
    garbage = _work(work_id=3, posted_at="not-a-real-timestamp")
    result = sort_works([older, garbage, newer], "posted")
    # Newest first; garbage sorts last, and nothing raises.
    assert [w.id for w in result] == [2, 1, 3]


def test_sort_posted_garbage_only_does_not_raise() -> None:
    works = [_work(work_id=1, posted_at="bogus"), _work(work_id=2, posted_at="")]
    assert len(sort_works(works, "posted")) == 2


def test_sort_id_ascending() -> None:
    works = [_work(work_id=3), _work(work_id=1), _work(work_id=2)]
    assert [w.id for w in sort_works(works, "id")] == [1, 2, 3]


def test_sort_title_ascending_casefold() -> None:
    works = [_work(title="banana"), _work(title="Apple"), _work(title="cherry")]
    assert [w.title for w in sort_works(works, "title")] == [
        "Apple",
        "banana",
        "cherry",
    ]


# --------------------------------------------------------------------------- #
# run_search
# --------------------------------------------------------------------------- #


def test_run_search_does_not_apply_limit(tmp_path: Path) -> None:
    works = [_work(work_id=i, work_tags=[("cat", None)]) for i in range(5)]
    config = _config(tmp_path, keywords=("cat",), limit=2)
    matches, scanned = run_search(works, config)
    assert scanned == 5
    assert len(matches) == 5  # limit is NOT applied here


def test_run_search_filters_and_sorts(tmp_path: Path) -> None:
    keep_a = _work(
        work_id=10,
        posted_at="2024-01-01T00:00:00+00:00",
        work_tags=[("cat", None)],
    )
    keep_b = _work(
        work_id=20,
        posted_at="2024-06-01T00:00:00+00:00",
        work_tags=[("cat", None)],
    )
    drop_kw = _work(work_id=30, work_tags=[("dog", None)])
    drop_nsfw = _work(work_id=40, is_nsfw=True, work_tags=[("cat", None)])
    config = _config(tmp_path, keywords=("cat",), nsfw="exclude", sort="posted")
    matches, scanned = run_search([keep_a, keep_b, drop_kw, drop_nsfw], config)
    assert scanned == 4
    # Newest first: keep_b before keep_a.
    assert [m.work.id for m in matches] == [20, 10]
    assert all(isinstance(m, SearchMatch) for m in matches)


def test_run_search_browse_mode_returns_all_sorted(tmp_path: Path) -> None:
    works = [_work(work_id=3), _work(work_id=1), _work(work_id=2)]
    config = _config(tmp_path, sort="id")
    matches, scanned = run_search(works, config)
    assert scanned == 3
    assert [m.work.id for m in matches] == [1, 2, 3]
    assert matches[0].matched_fields == list(config.fields)


# --------------------------------------------------------------------------- #
# None-safety
# --------------------------------------------------------------------------- #


def test_none_safety_everywhere(tmp_path: Path) -> None:
    work = _work(work_tags=[("風景", None)], bookmark_tags=None)
    config = _config(
        tmp_path,
        keywords=("風景",),
        blacklist_tags=("nope",),
        blacklist_authors=("nobody",),
        authors=(),
    )
    # None bookmark_tags and None translated_name must not raise anywhere.
    assert is_keyword_match(work, config) is True
    assert matched_fields(work, config) == ["work"]
    assert author_ok(work, config) is True
    assert blacklisted(work, config) is False
    assert nsfw_ok(work, config) is True
    matches, scanned = run_search([work], config)
    assert scanned == 1
    assert len(matches) == 1


# --------------------------------------------------------------------------- #
# format_work_line
# --------------------------------------------------------------------------- #


def test_format_work_line_contains_all_parts(tmp_path: Path) -> None:
    work = _work(
        work_id=42,
        type_="manga",
        title="My Title",
        author_name="Alice",
        author_account="alice_art",
        is_nsfw=True,
        work_tags=[("cat", None), ("dog", None)],
    )
    line = format_work_line(SearchMatch(work=work, matched_fields=["work"]))
    assert "42" in line
    assert "manga" in line
    assert "NSFW" in line
    assert '"My Title"' in line
    assert "by Alice (@alice_art)" in line
    assert "tags: cat, dog" in line


def test_format_work_line_sfw_and_no_tags() -> None:
    work = _work(is_nsfw=False, work_tags=[])
    line = format_work_line(SearchMatch(work=work, matched_fields=[]))
    assert "SFW" in line
    assert "NSFW" not in line
    assert "tags: (none)" in line


def test_format_work_line_strips_control_chars(tmp_path: Path) -> None:
    # Untrusted title/author/tag metadata containing ASCII control / ANSI escape
    # bytes must not reach the terminal raw. The ESC (\x1b) and BEL (\x07) bytes
    # are removed; printable text (including the now-defanged "[31m" residue and
    # the word "red") stays intact.
    work = _work(
        title="\x1b[31mred\x1b[0m\x07",
        author_name="evil\x1bname",
        author_account="acc\x07ount",
        work_tags=[("ta\x1bg", None)],
    )
    line = format_work_line(SearchMatch(work=work, matched_fields=[]))
    # The control bytes are gone entirely; the surrounding printable text is
    # preserved, now safely spliced together.
    assert "\x1b" not in line
    assert "\x07" not in line
    assert "red" in line
    assert "evilname" in line
    assert "account" in line
    assert "tag" in line


def test_format_work_line_untitled_when_title_empty() -> None:
    work = _work(title="", author_name="Alice", author_account="alice_art")
    line = format_work_line(SearchMatch(work=work, matched_fields=[]))
    assert '"(untitled)"' in line
    assert "by Alice (@alice_art)" in line


def test_format_work_line_all_blank_metadata() -> None:
    # Deleted/restricted works can have empty title AND empty author name AND
    # empty account; the line must still be identifiable via id and author id.
    work = _work(
        work_id=146401615,
        title="",
        author_name="",
        author_account="",
        work_tags=[],
    )
    line = format_work_line(SearchMatch(work=work, matched_fields=[]))
    assert '"(untitled)"' in line
    assert "by (unknown, id 100)" in line
    assert "(@)" not in line
