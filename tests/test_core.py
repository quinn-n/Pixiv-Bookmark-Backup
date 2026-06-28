"""Unit tests for the pure core helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pixiv_bookmark_backup.core import (
    BackupConfig,
    ExitCode,
    RunSummary,
    decide_exit_code,
    describe_plan,
)


def test_output_dir_is_coerced_to_path() -> None:
    config = BackupConfig(user_id="42", output_dir="some/dir")
    assert config.output_dir == Path("some/dir")


def test_describe_plan_mentions_user_and_destination(tmp_path: Path) -> None:
    config = BackupConfig(user_id="42", output_dir=tmp_path, visibility="all")
    text = describe_plan(config)
    assert "42" in text
    assert "all" in text
    assert str(tmp_path) in text


def test_describe_plan_uses_conditional_verb(tmp_path: Path) -> None:
    real = describe_plan(BackupConfig(user_id="1", output_dir=tmp_path))
    dry = describe_plan(BackupConfig(user_id="1", output_dir=tmp_path, dry_run=True))
    assert real.startswith("Backing up")
    assert dry.startswith("Would back up")


def test_invalid_visibility_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BackupConfig(user_id="42", output_dir=tmp_path, visibility="bogus")


def test_user_id_defaults_to_none(tmp_path: Path) -> None:
    config = BackupConfig(output_dir=tmp_path)
    assert config.user_id is None


def test_describe_plan_renders_self_when_user_id_none(tmp_path: Path) -> None:
    text = describe_plan(BackupConfig(output_dir=tmp_path))
    assert "self" in text.lower()


def test_all_fields_construct_and_validate(tmp_path: Path) -> None:
    config = BackupConfig(
        output_dir=tmp_path,
        user_id="7",
        visibility="private",
        dry_run=True,
        limit=10,
        since=date(2024, 1, 1),
        skip_existing=False,
        metadata_only=True,
        json_output=True,
        verbosity=2,
        quiet=True,
        max_retries=5,
    )
    assert config.limit == 10
    assert config.since == date(2024, 1, 1)
    assert config.skip_existing is False
    assert config.metadata_only is True
    assert config.json_output is True
    assert config.verbosity == 2
    assert config.quiet is True
    assert config.max_retries == 5


def test_limit_zero_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BackupConfig(output_dir=tmp_path, limit=0)


def test_max_retries_above_ten_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BackupConfig(output_dir=tmp_path, max_retries=11)


def test_max_retries_below_zero_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BackupConfig(output_dir=tmp_path, max_retries=-1)


def test_run_summary_defaults_are_zero() -> None:
    summary = RunSummary()
    assert summary.works_total == 0
    assert summary.works_succeeded == 0
    assert summary.works_failed == 0
    assert summary.files_downloaded == 0
    assert summary.files_skipped == 0
    assert summary.files_failed == 0
    assert summary.bytes_downloaded == 0


@pytest.mark.parametrize(
    "summary, expected",
    [
        (RunSummary(works_total=0), ExitCode.NO_BOOKMARKS),
        (
            RunSummary(works_total=5, works_succeeded=5, files_downloaded=5),
            ExitCode.SUCCESS,
        ),
        (
            RunSummary(works_total=5, works_succeeded=3, works_failed=2),
            ExitCode.PARTIAL,
        ),
        (
            RunSummary(works_total=5, works_succeeded=5, files_failed=1),
            ExitCode.PARTIAL,
        ),
    ],
)
def test_decide_exit_code_table(summary: RunSummary, expected: ExitCode) -> None:
    assert decide_exit_code(summary) == expected


@pytest.mark.parametrize(
    "member, value",
    [
        (ExitCode.SUCCESS, 0),
        (ExitCode.USAGE, 2),
        (ExitCode.AUTH, 3),
        (ExitCode.NETWORK, 4),
        (ExitCode.NO_BOOKMARKS, 5),
        (ExitCode.PARTIAL, 6),
        (ExitCode.LOGIN_DRIVER, 7),
        (ExitCode.LOGIN_FAILED, 8),
        (ExitCode.CONFIG_WRITE, 9),
        (ExitCode.INTERRUPTED, 130),
    ],
)
def test_exit_code_numeric_values(member: ExitCode, value: int) -> None:
    # The numeric values are part of the CLI contract; guard against drift.
    assert member == value
    assert int(member) == value


def test_login_exit_codes_are_distinct_members() -> None:
    assert ExitCode.LOGIN_DRIVER is ExitCode(7)
    assert ExitCode.LOGIN_FAILED is ExitCode(8)
    assert ExitCode.CONFIG_WRITE is ExitCode(9)
