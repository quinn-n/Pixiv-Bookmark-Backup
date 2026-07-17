"""CLI tests for the offline, read-only ``search`` command via ``CliRunner``.

The command never authenticates or touches the network, so these tests need no
client fake: they write a small, valid ``details.json`` into ``tmp_path`` and
drive the real ``search`` command end to end through the real ``run_search``
core.

CliRunner in click 8.4.1 has no ``mix_stderr`` kwarg: results/data are asserted
on ``result.stdout`` and the match-count summary on ``result.stderr``, never on
the interleaved ``result.output`` (used only for --help text assertions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pixiv_bookmark_backup.cli import cli
from pixiv_bookmark_backup.manifest import MANIFEST_NAME, SCHEMA_VERSION

# -- fixtures & helpers -------------------------------------------------


def _image(page: int = 0, *, path: str | None = None) -> dict[str, Any]:
    return {
        "page": page,
        "source_url": f"https://example.test/{page}.png",
        "path": path if path is not None else f"p{page}.png",
        "status": "downloaded",
        "bytes": 1024,
        "error": None,
    }


def _work(
    work_id: int,
    *,
    title: str,
    author_id: int,
    author_name: str,
    author_account: str,
    posted_at: str,
    is_nsfw: bool,
    work_tags: list[dict[str, Any]],
    bookmark_tags: list[str] | None,
    work_type: str = "illust",
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    work_images = images if images is not None else [_image()]
    return {
        "id": work_id,
        "type": work_type,
        "title": title,
        "author": {"id": author_id, "name": author_name, "account": author_account},
        "posted_at": posted_at,
        "nsfw": {
            "x_restrict": 1 if is_nsfw else 0,
            "sanity_level": 6 if is_nsfw else 2,
            "is_nsfw": is_nsfw,
        },
        "work_tags": work_tags,
        "bookmark_tags": bookmark_tags,
        "page_count": len(work_images),
        "images": work_images,
    }


def _manifest_dict() -> dict[str, Any]:
    """A manifest with a SFW/NSFW mix, two authors, and varied tag surfaces."""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            _work(
                101,
                title="Sunrise",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-01-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[
                    {"name": "touhou", "translated_name": "Touhou Project"},
                    {"name": "original", "translated_name": None},
                ],
                bookmark_tags=["fav"],
            ),
            _work(
                202,
                title="Moonlight",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-06-01T00:00:00+09:00",
                is_nsfw=True,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=None,
            ),
            _work(
                303,
                title="Zephyr",
                author_id=2,
                author_name="Bob",
                author_account="bob_draws",
                posted_at="2024-03-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "scenery", "translated_name": "landscape"}],
                bookmark_tags=["wallpaper"],
            ),
        ],
        "errors": [],
    }


def _write_manifest(directory: Path, data: dict[str, Any] | None = None) -> Path:
    """Write ``details.json`` into ``directory`` and return the directory path."""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(data if data is not None else _manifest_dict()), "utf-8")
    return directory


def _ids_in(stdout: str) -> list[int]:
    """Return the leading work ids from human-readable result lines."""

    ids: list[int] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line and line[0].isdigit():
            ids.append(int(line.split()[0]))
    return ids


# -- help ---------------------------------------------------------------


def test_help_lists_command_offline_and_exit_codes(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["search", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "offline" in out
    assert "Exit codes:" in out
    assert "0 success" in out
    for flag in (
        "--field",
        "--exact",
        "--match-all",
        "--nsfw",
        "--ugoira",
        "--count",
        "--json",
    ):
        assert flag in out


def test_group_help_lists_search(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "search" in result.output


# -- browse mode --------------------------------------------------------


def test_browse_lists_all_works(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup)])
    assert result.exit_code == 0, result.output
    assert sorted(_ids_in(result.stdout)) == [101, 202, 303]
    assert "Matched 3 / 3 works" in result.stderr


# -- keyword matching ---------------------------------------------------


def test_keyword_or_default(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "touhou", "scenery", "-i", str(backup)])
    assert result.exit_code == 0, result.output
    assert sorted(_ids_in(result.stdout)) == [101, 202, 303]


def test_match_all_narrows(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    # Only work 101 has both "touhou" and "original".
    result = runner.invoke(
        cli, ["search", "touhou", "original", "--match-all", "-i", str(backup)]
    )
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [101]


# -- stdout/stderr split & --quiet -------------------------------------


def test_stdout_stderr_split(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "touhou", "-i", str(backup)])
    assert result.exit_code == 0, result.output
    assert "Matched" not in result.stdout  # summary is not on stdout
    assert "Matched 2 / 3 works" in result.stderr
    assert sorted(_ids_in(result.stdout)) == [101, 202]


def test_quiet_removes_summary_only(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    quiet = runner.invoke(cli, ["search", "touhou", "-i", str(backup), "--quiet"])
    loud = runner.invoke(cli, ["search", "touhou", "-i", str(backup)])
    assert quiet.exit_code == 0, quiet.output
    assert "Matched" not in quiet.stderr
    assert quiet.stdout == loud.stdout  # results unchanged by --quiet


def test_verbose_emits_reading_line(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    verbose = runner.invoke(cli, ["search", "touhou", "-i", str(backup), "-v"])
    plain = runner.invoke(cli, ["search", "touhou", "-i", str(backup)])
    assert verbose.exit_code == 0, verbose.output
    # -v adds a per-input resolution line to stderr (diagnostics, not results).
    assert "Reading" in verbose.stderr
    assert "(3 works)" in verbose.stderr
    assert "Reading" not in plain.stderr
    assert verbose.stdout == plain.stdout  # results unchanged by -v


# -- --count ------------------------------------------------------------


def test_count_prints_single_integer(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "touhou", "-i", str(backup), "--count"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "2"
    # The stderr summary is governed by --quiet only; it still prints under --count.
    assert "Matched 2 / 3 works" in result.stderr


def test_count_unaffected_by_limit(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--count", "--limit", "1"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "2"  # full total, not the limited slice


def test_count_and_json_mutually_exclusive(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--count", "--json"])
    assert result.exit_code == 2
    assert "--count and --json are mutually exclusive" in result.stderr


# -- --json -------------------------------------------------------------


def test_json_is_clean_single_object(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--field", "work", "--exact", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert "Matched" not in result.stdout  # summary stays on stderr
    # The stderr summary is governed by --quiet only; it still prints under --json.
    assert "Matched 2 / 3 works" in result.stderr
    payload = json.loads(result.stdout)  # exactly one JSON object

    assert payload["schema"] == 2
    assert payload["input"] == str(backup / MANIFEST_NAME)
    assert payload["query"]["keywords"] == ["touhou"]
    assert payload["query"]["fields"] == ["work"]
    assert payload["query"]["exact"] is True
    assert payload["query"]["match_all"] is False
    assert payload["query"]["nsfw"] == "include"
    assert payload["query"]["ugoira"] == "include"
    assert payload["query"]["sort"] == "posted"
    assert payload["works_scanned"] == 3
    assert payload["match_count"] == 2
    assert len(payload["matches"]) == 2
    assert payload["exit_code"] == 0
    for match in payload["matches"]:
        assert "matched_fields" in match
        assert match["matched_fields"]


def test_json_match_count_is_pre_limit_total(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--json", "--limit", "1"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["match_count"] == 2  # true total
    assert len(payload["matches"]) == 1  # truncated to --limit
    assert payload["query"]["limit"] == 1


def test_json_bookmark_tags_preserved(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--sort", "id", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    by_id = {m["id"]: m for m in payload["matches"]}
    assert by_id[101]["bookmark_tags"] == ["fav"]
    assert by_id[202]["bookmark_tags"] is None
    assert by_id[101]["is_nsfw"] is False
    assert by_id[202]["is_nsfw"] is True


# -- --limit ------------------------------------------------------------


def test_limit_truncates_human_lines(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert len(_ids_in(result.stdout)) == 2
    assert "Matched 3 / 3 works" in result.stderr  # summary shows full total


# -- filters reach core -------------------------------------------------


def test_nsfw_only(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--nsfw", "only"])
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [202]


def test_nsfw_exclude(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--nsfw", "exclude"])
    assert result.exit_code == 0, result.output
    assert sorted(_ids_in(result.stdout)) == [101, 303]


def _ugoira_manifest_dict() -> dict[str, Any]:
    """A manifest mixing one ugoira work (id 505) with two illust works.

    Kept separate from :func:`_manifest_dict` so tests that depend on the shared
    id set ``[101, 202, 303]`` are unaffected.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            _work(
                404,
                title="Still Waters",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-01-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "scenery", "translated_name": "landscape"}],
                bookmark_tags=None,
                work_type="illust",
            ),
            _work(
                505,
                title="Waving Flag",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-02-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "scenery", "translated_name": "landscape"}],
                bookmark_tags=None,
                work_type="ugoira",
            ),
            _work(
                606,
                title="Quiet Hill",
                author_id=2,
                author_name="Bob",
                author_account="bob_draws",
                posted_at="2024-03-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "scenery", "translated_name": "landscape"}],
                bookmark_tags=None,
                work_type="illust",
            ),
        ],
        "errors": [],
    }


def test_ugoira_only_narrows_results(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup", _ugoira_manifest_dict())
    result = runner.invoke(cli, ["search", "-i", str(backup), "--ugoira", "only"])
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [505]


def test_ugoira_exclude_drops_ugoira(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup", _ugoira_manifest_dict())
    result = runner.invoke(cli, ["search", "-i", str(backup), "--ugoira", "exclude"])
    assert result.exit_code == 0, result.output
    assert sorted(_ids_in(result.stdout)) == [404, 606]


def test_ugoira_invalid_choice_exits_2(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup", _ugoira_manifest_dict())
    result = runner.invoke(cli, ["search", "-i", str(backup), "--ugoira", "bogus"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_json_ugoira_filter_echoed(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup", _ugoira_manifest_dict())
    result = runner.invoke(
        cli, ["search", "-i", str(backup), "--ugoira", "only", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["query"]["ugoira"] == "only"


def test_exact_narrows_to_whole_tag(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    # "touh" is a substring of the "touhou" tag but never a whole tag.
    substring = runner.invoke(cli, ["search", "touh", "-i", str(backup)])
    exact = runner.invoke(cli, ["search", "touh", "-i", str(backup), "--exact"])
    assert substring.exit_code == 0, substring.output
    assert sorted(_ids_in(substring.stdout)) == [101, 202]  # substring hits
    assert exact.exit_code == 0, exact.output
    assert exact.stdout.strip() == ""  # no whole tag equals "touh"


def test_field_scopes_surface(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    # "fav" exists only as a bookmark tag on 101, never as a work tag.
    all_fields = runner.invoke(cli, ["search", "fav", "-i", str(backup)])
    work_only = runner.invoke(
        cli, ["search", "fav", "-i", str(backup), "--field", "work"]
    )
    assert all_fields.exit_code == 0, all_fields.output
    assert _ids_in(all_fields.stdout) == [101]  # matched on the bookmark surface
    assert work_only.exit_code == 0, work_only.output
    assert work_only.stdout.strip() == ""  # scoped to work tags -> no hit


def test_field_bookmark_exact(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "fav", "-i", str(backup), "--field", "bookmark", "--exact"]
    )
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [101]


def test_field_translated(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    # "landscape" only exists as a translated name on work 303.
    result = runner.invoke(
        cli, ["search", "landscape", "-i", str(backup), "--field", "translated"]
    )
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [303]


def test_author_filter(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--author", "bob"])
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [303]


def test_blacklist_tag(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "-i", str(backup), "--blacklist-tag", "scenery"]
    )
    assert result.exit_code == 0, result.output
    assert sorted(_ids_in(result.stdout)) == [101, 202]


def test_blacklist_author(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "-i", str(backup), "--blacklist-author", "Alice"]
    )
    assert result.exit_code == 0, result.output
    assert _ids_in(result.stdout) == [303]


# -- usage errors (exit 2) ---------------------------------------------


def test_quiet_and_verbose_mutually_exclusive(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "-q", "-v"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_missing_dir_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = runner.invoke(cli, ["search", "-i", str(missing)])
    assert result.exit_code == 2
    assert "No manifest found" in result.stderr
    assert result.stdout == ""


def test_missing_file_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "details.json"
    result = runner.invoke(cli, ["search", "-i", str(missing)])
    assert result.exit_code == 2
    assert "No manifest found" in result.stderr
    assert result.stdout == ""


def test_malformed_json_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "details.json"
    bad.write_text("{ not json", encoding="utf-8")
    result = runner.invoke(cli, ["search", "-i", str(bad)])
    assert result.exit_code == 2
    assert "not valid JSON" in result.stderr
    assert result.stdout == ""


def test_schema_mismatch_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    data = _manifest_dict()
    data["schema_version"] = 999
    path = tmp_path / "details.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = runner.invoke(cli, ["search", "-i", str(path)])
    assert result.exit_code == 2
    assert str(SCHEMA_VERSION) in result.stderr
    assert result.stdout == ""


# -- -i dir vs file -----------------------------------------------------


def test_input_dir_resolves_to_details_json(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "-i", str(backup), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["input"] == str(backup / MANIFEST_NAME)


def test_input_file_directly(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    manifest_file = backup / MANIFEST_NAME
    result = runner.invoke(cli, ["search", "-i", str(manifest_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["input"] == str(manifest_file)


# -- zero matches is success -------------------------------------------


def test_zero_matches_human(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "nonexistent-tag", "-i", str(backup)])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""
    assert "Matched 0 / 3 works" in result.stderr


def test_zero_matches_json(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "nonexistent-tag", "-i", str(backup), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["match_count"] == 0
    assert payload["matches"] == []


def test_zero_matches_count(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_manifest(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "nonexistent-tag", "-i", str(backup), "--count"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "0"


# -- default input ------------------------------------------------------


def test_default_input_uses_backup_dir(runner: CliRunner) -> None:
    # With no -i and no ./backup present, the default is resolved and the
    # missing-manifest error references the backup path.
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["search", "touhou"])
    assert result.exit_code == 2
    assert "No manifest found at backup" in result.stderr
    assert result.stdout == ""


# -- --symlink-dir ------------------------------------------------------
#
# These tests use id-prefixed, per-work image paths (production naming, e.g.
# 101_p0.png) so link names are unique across works. The default manifest
# hard-codes "p0.png" for every work, which would collapse all works onto one
# link; the helpers below give each work its own file(s) and touch them on disk
# so plan_and_link's existence check passes.


def _linkable_manifest() -> dict[str, Any]:
    """A manifest whose works carry unique, id-prefixed image paths.

    Work 101 and 202 both match "touhou"; 303 matches "scenery". Each has one
    page named ``<id>_p0.png`` so link names never collide.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            _work(
                101,
                title="Sunrise",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-01-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=["fav"],
                images=[_image(0, path="101_p0.png")],
            ),
            _work(
                202,
                title="Moonlight",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-06-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=None,
                images=[_image(0, path="202_p0.png")],
            ),
            _work(
                303,
                title="Zephyr",
                author_id=2,
                author_name="Bob",
                author_account="bob_draws",
                posted_at="2024-03-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "scenery", "translated_name": "landscape"}],
                bookmark_tags=["wallpaper"],
                images=[_image(0, path="303_p0.png")],
            ),
        ],
        "errors": [],
    }


def _touch_images(backup: Path, data: dict[str, Any]) -> None:
    """Create every image file referenced by ``data`` under ``backup`` on disk."""

    for work in data["works"]:
        for image in work["images"]:
            target = backup / image["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-image-bytes")


def _write_linkable(backup: Path, data: dict[str, Any] | None = None) -> Path:
    """Write a linkable manifest and create its image files under ``backup``."""

    payload = data if data is not None else _linkable_manifest()
    _write_manifest(backup, payload)
    _touch_images(backup, payload)
    return backup


@pytest.mark.parametrize("flag", ["--dry-run", "--force"])
def test_symlink_flags_require_symlink_dir(
    runner: CliRunner, tmp_path: Path, flag: str
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "touhou", "-i", str(backup), flag])
    assert result.exit_code == 2
    assert f"{flag} requires --symlink-dir" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("flag", ["--all-pages", "--first-page-only"])
def test_page_flags_require_symlink_dir(
    runner: CliRunner, tmp_path: Path, flag: str
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "touhou", "-i", str(backup), flag])
    assert result.exit_code == 2
    assert "--all-pages/--first-page-only requires --symlink-dir" in result.stderr
    assert result.stdout == ""


def test_symlink_creates_resolving_links(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links)]
    )
    assert result.exit_code == 0, result.output
    # Human match lines are unchanged on stdout.
    assert sorted(_ids_in(result.stdout)) == [101, 202]
    # Two works matched -> two symlinks, each resolving to its backup image.
    for name, target_name in [
        ("101_p0.png", "101_p0.png"),
        ("202_p0.png", "202_p0.png"),
    ]:
        link = links / name
        assert link.is_symlink()
        assert link.resolve() == (backup / target_name).resolve()
    assert "Linked 2 files (2 new, 0 reused) for 2 works into" in result.stderr


def test_symlink_dry_run_creates_nothing(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not links.exists()  # nothing written
    assert "Would link 2 files (2 new, 0 reused) for 2 works into" in result.stderr


def test_symlink_json_sub_object(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # exactly one JSON object
    assert "symlink" in payload
    sym = payload["symlink"]
    assert sym["dir"] == str(links.resolve())
    assert sym["all_pages"] is True
    assert sym["dry_run"] is False
    assert sym["force"] is False
    assert sym["links_created"] == 2
    assert sym["links_reused"] == 0
    assert sym["links_replaced"] == 0
    assert sym["links_total"] == 2  # created + reused + replaced
    assert sym["works_linked"] == 2
    assert sym["works_without_files"] == 0
    assert sym["skipped_work_ids"] == []  # no works skipped in this scenario
    # linked mirrors the (link_name, target_str) pairs the run touched.
    assert sorted(pair[0] for pair in sym["linked"]) == ["101_p0.png", "202_p0.png"]
    for name, target in sym["linked"]:
        assert target == str((backup / name).resolve())


def test_json_has_no_symlink_key_without_flag(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    result = runner.invoke(cli, ["search", "touhou", "-i", str(backup), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "symlink" not in payload  # back-compat: absent without --symlink-dir


def test_symlink_foreign_file_conflict_exits_10(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    links.mkdir()
    # Pre-create a regular file at a would-be link name.
    foreign = links / "101_p0.png"
    foreign.write_text("do not touch", encoding="utf-8")
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links)]
    )
    assert result.exit_code == 10
    assert result.stdout == ""  # no partial results emitted before the error
    assert "Traceback" not in result.stderr
    assert "Refusing to overwrite" in result.stderr
    # The foreign file is untouched (still a regular file with its content).
    assert not foreign.is_symlink()
    assert foreign.read_text(encoding="utf-8") == "do not touch"


def test_symlink_dir_is_regular_file_exits_10(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("i am a file", encoding="utf-8")
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--symlink-dir", str(not_a_dir)]
    )
    assert result.exit_code == 10
    assert result.stdout == ""  # no partial results emitted before the error
    assert "Traceback" not in result.stderr
    assert "not a directory" in result.stderr
    assert not_a_dir.read_text(encoding="utf-8") == "i am a file"


def test_symlink_quiet_suppresses_link_summary(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "--quiet"],
    )
    assert result.exit_code == 0, result.output
    assert "Linked" not in result.stderr
    assert "Matched" not in result.stderr
    # Links are still created; stdout still shows the results.
    assert (links / "101_p0.png").is_symlink()
    assert sorted(_ids_in(result.stdout)) == [101, 202]


def test_symlink_respects_limit(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--limit",
            "1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["match_count"] == 2  # full total unaffected by --limit
    assert payload["symlink"]["works_linked"] == 1  # only the limited slice linked
    assert len(list(links.iterdir())) == 1


def test_symlink_count_prints_integer_and_links(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "--count"],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "2"  # only the integer on stdout
    assert (links / "101_p0.png").is_symlink()  # links still created
    assert "Linked 2 files (2 new, 0 reused) for 2 works into" in result.stderr


def _multipage_manifest() -> dict[str, Any]:
    """One work (id 101, tag "touhou") with three downloaded pages on disk."""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            _work(
                101,
                title="Triptych",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-01-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=None,
                images=[
                    _image(0, path="101_p0.png"),
                    _image(1, path="101_p1.png"),
                    _image(2, path="101_p2.png"),
                ],
            ),
        ],
        "errors": [],
    }


def test_symlink_all_pages_links_every_page(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup", _multipage_manifest())
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--all-pages",
        ],
    )
    assert result.exit_code == 0, result.output
    for page in range(3):
        link = links / f"101_p{page}.png"
        assert link.is_symlink()
        assert link.resolve() == (backup / f"101_p{page}.png").resolve()
    assert "Linked 3 files (3 new, 0 reused) for 1 work into" in result.stderr


def test_symlink_default_links_all_pages(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup", _multipage_manifest())
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links)],
    )
    assert result.exit_code == 0, result.output
    for page in range(3):
        link = links / f"101_p{page}.png"
        assert link.is_symlink()
        assert link.resolve() == (backup / f"101_p{page}.png").resolve()
    assert "Linked 3 files (3 new, 0 reused) for 1 work into" in result.stderr


def test_symlink_first_page_only_links_one_page(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup", _multipage_manifest())
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--first-page-only",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (links / "101_p0.png").is_symlink()
    assert not (links / "101_p1.png").exists()
    assert not (links / "101_p2.png").exists()
    assert "Linked 1 file (1 new, 0 reused) for 1 work into" in result.stderr


def test_symlink_first_page_only_json(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup", _multipage_manifest())
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--first-page-only",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    sym = payload["symlink"]
    assert sym["all_pages"] is False
    assert sym["links_created"] == 1
    assert sym["links_total"] == 1
    assert sym["works_linked"] == 1


def _manifest_with_a_missing_file() -> dict[str, Any]:
    """Work 101 has an on-disk image; work 202 references a file we won't create."""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            _work(
                101,
                title="Sunrise",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-01-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=None,
                images=[_image(0, path="101_p0.png")],
            ),
            _work(
                202,
                title="Moonlight",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-06-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=None,
                images=[_image(0, path="202_p0.png")],
            ),
        ],
        "errors": [],
    }


def test_symlink_reports_works_without_files(runner: CliRunner, tmp_path: Path) -> None:
    data = _manifest_with_a_missing_file()
    backup = _write_manifest(tmp_path / "backup", data)
    # Only create work 101's image on disk; work 202's file stays missing.
    (backup / "101_p0.png").write_bytes(b"fake-image-bytes")
    links = tmp_path / "links"
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links)]
    )
    assert result.exit_code == 0, result.output
    assert "Linked 1 file (1 new, 0 reused) for 1 work into" in result.stderr
    assert "(1 work skipped: no local file)" in result.stderr
    assert (links / "101_p0.png").is_symlink()
    assert not (links / "202_p0.png").exists()


def test_symlink_verbose_echoes_skipped_and_links(
    runner: CliRunner, tmp_path: Path
) -> None:
    data = _manifest_with_a_missing_file()
    backup = _write_manifest(tmp_path / "backup", data)
    (backup / "101_p0.png").write_bytes(b"fake-image-bytes")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "-vv"],
    )
    assert result.exit_code == 0, result.output
    # -v echoes the skipped work id; -vv echoes each link_name -> target pair.
    assert "skipped 202: no local file" in result.stderr
    assert "101_p0.png -> " in result.stderr


def test_symlink_verbose_single_v_no_link_arrows(
    runner: CliRunner, tmp_path: Path
) -> None:
    # Plain -v (not -vv) echoes the skipped work id but NOT the link-arrow lines,
    # which are a -vv-only diagnostic.
    data = _manifest_with_a_missing_file()
    backup = _write_manifest(tmp_path / "backup", data)
    (backup / "101_p0.png").write_bytes(b"fake-image-bytes")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "-v"],
    )
    assert result.exit_code == 0, result.output
    assert "skipped 202: no local file" in result.stderr
    assert "->" not in result.stderr  # link-arrow lines are -vv only


def test_symlink_json_reports_skipped_work_ids(
    runner: CliRunner, tmp_path: Path
) -> None:
    # A run where one matched work has no local file surfaces that work's id in
    # the JSON symlink sub-object's skipped_work_ids (and links_total drops it).
    data = _manifest_with_a_missing_file()
    backup = _write_manifest(tmp_path / "backup", data)
    (backup / "101_p0.png").write_bytes(b"fake-image-bytes")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "--json"],
    )
    assert result.exit_code == 0, result.output
    sym = json.loads(result.stdout)["symlink"]
    assert sym["skipped_work_ids"] == [202]
    assert sym["works_without_files"] == 1
    assert sym["links_total"] == 1  # only work 101 linked


def test_symlink_force_replace_summary_includes_replaced(
    runner: CliRunner, tmp_path: Path
) -> None:
    # With --force re-pointing a divergent pre-existing link, the summary spells
    # out the replaced count (the term appears only when links_replaced > 0).
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    links.mkdir()
    # Pre-point 101's link at a different (in-backup) target so --force replaces.
    (backup / "other.png").write_bytes(b"fake-image-bytes")
    (links / "101_p0.png").symlink_to(backup / "other.png")
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 replaced" in result.stderr
    assert (links / "101_p0.png").resolve() == (backup / "101_p0.png").resolve()


def test_symlink_traversal_escape_is_skipped_end_to_end(
    runner: CliRunner, tmp_path: Path
) -> None:
    # A matched work whose image path escapes the backup dir (../escape.jpg) must
    # NOT produce a symlink outside the links dir: the traversal guard applies
    # end to end through the CLI, treating the work as works_without_files.
    escape_target = tmp_path / "escape.jpg"
    escape_target.write_bytes(b"secret-outside-the-backup")
    data = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            _work(
                101,
                title="Escape",
                author_id=1,
                author_name="Alice",
                author_account="alice_art",
                posted_at="2024-01-01T00:00:00+09:00",
                is_nsfw=False,
                work_tags=[{"name": "touhou", "translated_name": "Touhou Project"}],
                bookmark_tags=None,
                images=[_image(0, path="../escape.jpg")],
            ),
        ],
        "errors": [],
    }
    backup = _write_manifest(tmp_path / "backup", data)
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "--json"],
    )
    assert result.exit_code == 0, result.output
    sym = json.loads(result.stdout)["symlink"]
    assert sym["links_total"] == 0
    assert sym["works_without_files"] == 1
    assert sym["skipped_work_ids"] == [101]
    # No symlink escaping the links dir was created.
    assert not (links / "escape.jpg").exists()
    if links.exists():
        assert list(links.iterdir()) == []


# -- --layout -----------------------------------------------------------
#
# by-author nests each link under "<account>_<author_id>/"; titled prefixes the
# link name with the sanitized work title. The linkable fixture's authors are
# Alice (account "alice_art", id 1) and Bob (account "bob_draws", id 2), so the
# by-author subdirectories are "alice_art_1" and "bob_draws_2".


def test_layout_help_shows_choices_and_default(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["search", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "--layout" in out
    assert "[flat|by-author|titled]" in out
    assert "[default: flat]" in out


def test_layout_by_author_nests_under_author_subdirs(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
        ],
    )
    assert result.exit_code == 0, result.output
    # Works 101 and 202 are both Alice's (account alice_art, id 1): one subdir.
    for basename in ("101_p0.png", "202_p0.png"):
        link = links / "alice_art_1" / basename
        assert link.is_symlink()
        assert link.resolve() == (backup / basename).resolve()
    # A single per-author directory holds both of Alice's links.
    assert sorted(p.name for p in (links / "alice_art_1").iterdir()) == [
        "101_p0.png",
        "202_p0.png",
    ]
    assert "(layout: by-author)" in result.stderr


def test_layout_by_author_multiple_authors_get_own_subdir(
    runner: CliRunner, tmp_path: Path
) -> None:
    # Browse mode (no keyword) links every work, so both authors appear.
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (links / "alice_art_1" / "101_p0.png").is_symlink()
    assert (links / "alice_art_1" / "202_p0.png").is_symlink()
    assert (links / "bob_draws_2" / "303_p0.png").is_symlink()
    assert sorted(p.name for p in links.iterdir()) == ["alice_art_1", "bob_draws_2"]


def test_layout_titled_prefixes_link_name(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "titled",
        ],
    )
    assert result.exit_code == 0, result.output
    # Work 101 is titled "Sunrise" -> "Sunrise_101_p0.png"; 202 -> "Moonlight_...".
    for title, basename in [("Sunrise", "101_p0.png"), ("Moonlight", "202_p0.png")]:
        link = links / f"{title}_{basename}"
        assert link.is_symlink()
        assert link.resolve() == (backup / basename).resolve()
    # Titled stays flat: no subdirectories are created.
    assert all(p.is_symlink() for p in links.iterdir())
    assert "(layout: titled)" in result.stderr


@pytest.mark.parametrize("layout_args", [[], ["--layout", "flat"]])
def test_layout_flat_is_byte_identical(
    runner: CliRunner, tmp_path: Path, layout_args: list[str]
) -> None:
    # Both the omitted layout and an explicit --layout flat produce bare
    # basenames directly under the link dir, with no (layout: ...) suffix.
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links)]
        + layout_args,
    )
    assert result.exit_code == 0, result.output
    for basename in ("101_p0.png", "202_p0.png"):
        link = links / basename
        assert link.is_symlink()
        assert link.resolve() == (backup / basename).resolve()
    # No nested directories under flat.
    assert all(p.is_symlink() for p in links.iterdir())
    assert "Linked 2 files (2 new, 0 reused) for 2 works into" in result.stderr
    assert "(layout:" not in result.stderr  # flat output stays suffix-free


@pytest.mark.parametrize("layout", ["flat", "by-author", "titled"])
def test_layout_requires_symlink_dir(
    runner: CliRunner, tmp_path: Path, layout: str
) -> None:
    # Even an explicit --layout flat (which still sources as COMMANDLINE) errors
    # without --symlink-dir, mirroring the page-flag guard exactly.
    backup = _write_linkable(tmp_path / "backup")
    result = runner.invoke(
        cli, ["search", "touhou", "-i", str(backup), "--layout", layout]
    )
    assert result.exit_code == 2
    assert "--layout requires --symlink-dir" in result.stderr
    assert result.stdout == ""


def test_layout_by_author_json_reports_nested_names(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Base schema is 2 (from the ugoira query key); the layout/symlink
    # sub-object is still gated on --symlink-dir and does not bump the schema.
    assert payload["schema"] == 2
    sym = payload["symlink"]
    assert sym["layout"] == "by-author"
    # linked entries are link-dir-relative and NESTED under the author subdir.
    names = sorted(pair[0] for pair in sym["linked"])
    assert names == ["alice_art_1/101_p0.png", "alice_art_1/202_p0.png"]
    for name, target in sym["linked"]:
        basename = Path(name).name
        assert target == str((backup / basename).resolve())


def test_layout_flat_json_names_stay_bare(runner: CliRunner, tmp_path: Path) -> None:
    # Back-compat: with flat (explicit or default) the linked names are bare.
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        ["search", "touhou", "-i", str(backup), "--symlink-dir", str(links), "--json"],
    )
    assert result.exit_code == 0, result.output
    sym = json.loads(result.stdout)["symlink"]
    assert sym["layout"] == "flat"
    assert sorted(pair[0] for pair in sym["linked"]) == ["101_p0.png", "202_p0.png"]


def test_layout_titled_dry_run_reports_without_writing(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "titled",
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not links.exists()  # dry-run writes nothing
    sym = json.loads(result.stdout)["symlink"]
    assert sym["layout"] == "titled"
    assert sym["dry_run"] is True
    # Names still reflect the titled layout even though nothing was written.
    assert sorted(pair[0] for pair in sym["linked"]) == [
        "Moonlight_202_p0.png",
        "Sunrise_101_p0.png",
    ]


def test_layout_by_author_dry_run_summary_reports_layout(
    runner: CliRunner, tmp_path: Path
) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not links.exists()  # dry-run creates no subdirs or links
    assert "Would link 2 files (2 new, 0 reused) for 2 works into" in result.stderr
    assert "(layout: by-author)" in result.stderr


def test_layout_by_author_first_page_only(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup", _multipage_manifest())
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
            "--first-page-only",
        ],
    )
    assert result.exit_code == 0, result.output
    # Only the first page is linked, under the author subdir.
    assert (links / "alice_art_1" / "101_p0.png").is_symlink()
    assert not (links / "alice_art_1" / "101_p1.png").exists()
    assert "Linked 1 file (1 new, 0 reused) for 1 work into" in result.stderr
    assert "(layout: by-author)" in result.stderr


def test_layout_suffix_precedes_skipped_clause(
    runner: CliRunner, tmp_path: Path
) -> None:
    # A non-flat layout with a skipped (no-local-file) work interleaves the two
    # parentheticals as "... into <dir> (layout: by-author) (1 work skipped...)".
    data = _manifest_with_a_missing_file()
    backup = _write_manifest(tmp_path / "backup", data)
    (backup / "101_p0.png").write_bytes(b"fake-image-bytes")  # 202's file stays gone
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        "into "
        + str(links.resolve())
        + " (layout: by-author) (1 work skipped: no local file)"
    ) in result.stderr


def test_layout_by_author_with_count(runner: CliRunner, tmp_path: Path) -> None:
    backup = _write_linkable(tmp_path / "backup")
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
            "--count",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "2"  # only the integer on stdout
    assert (links / "alice_art_1" / "101_p0.png").is_symlink()  # links still made
    assert "(layout: by-author)" in result.stderr


# -- malformed manifest (missing author.id, DEFERRED #8) ----------------


def _linkable_missing_author_id() -> dict[str, Any]:
    """A linkable manifest whose first matched work drops ``author.id``."""

    data = _linkable_manifest()
    del data["works"][0]["author"]["id"]
    return data


def test_missing_author_id_exits_2_plain_search(
    runner: CliRunner, tmp_path: Path
) -> None:
    # A hand-edited manifest missing author.id must fail cleanly at load time as
    # a UsageError (exit 2), not crash later in format_work_line's unknown-author
    # fallback -- even for a plain search with no --symlink-dir.
    backup = _write_linkable(tmp_path / "backup", _linkable_missing_author_id())
    result = runner.invoke(cli, ["search", "touhou", "-i", str(backup)])
    assert result.exit_code == 2
    assert "Traceback" not in result.stderr
    assert "author.id" in result.stderr
    assert result.stdout == ""


def test_missing_author_id_exits_2_by_author_layout(
    runner: CliRunner, tmp_path: Path
) -> None:
    # The originating bug: --layout by-author subscripts author['id']. The load
    # guard must fire first, so the run exits 2 (not the symlink exit 10) and
    # creates no links.
    backup = _write_linkable(tmp_path / "backup", _linkable_missing_author_id())
    links = tmp_path / "links"
    result = runner.invoke(
        cli,
        [
            "search",
            "touhou",
            "-i",
            str(backup),
            "--symlink-dir",
            str(links),
            "--layout",
            "by-author",
        ],
    )
    assert result.exit_code == 2
    assert "Traceback" not in result.stderr
    assert "author.id" in result.stderr
    assert result.stdout == ""
    assert not links.exists()  # failed before any symlink was attempted
