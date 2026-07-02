"""Unit tests for the pure core helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import attrs.exceptions
import pytest

from pixiv_bookmark_backup.core import (
    BackupConfig,
    ExitCode,
    FollowingConfig,
    FollowingRunSummary,
    ProgressEvent,
    ProgressKind,
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


def test_following_run_summary_defaults() -> None:
    summary = FollowingRunSummary()
    assert summary.works_total == 0
    assert summary.works_succeeded == 0
    assert summary.works_failed == 0
    assert summary.files_downloaded == 0
    assert summary.files_skipped == 0
    assert summary.files_failed == 0
    assert summary.bytes_downloaded == 0
    assert summary.users_total == 0
    assert summary.users_with_works == 0
    assert summary.following_of is None


def test_following_config_defaults(tmp_path: Path) -> None:
    config = FollowingConfig(output_dir=tmp_path)
    assert config.follows == "public"
    assert config.dry_run is False
    assert config.limit is None
    assert config.since is None
    assert config.skip_existing is True
    assert config.metadata_only is False
    assert config.json_output is False
    assert config.verbosity == 0
    assert config.quiet is False
    assert config.max_retries == 3
    assert config.per_user_limit is None
    assert config.max_users is None
    assert config.only == ()
    assert config.exclude == ()


def test_following_config_output_dir_is_coerced_to_path() -> None:
    config = FollowingConfig(output_dir="some/dir")
    assert config.output_dir == Path("some/dir")


def test_following_config_all_fields_construct_and_validate(tmp_path: Path) -> None:
    config = FollowingConfig(
        output_dir=tmp_path,
        follows="private",
        dry_run=True,
        limit=10,
        since=date(2024, 1, 1),
        skip_existing=False,
        metadata_only=True,
        json_output=True,
        verbosity=2,
        quiet=True,
        max_retries=5,
        per_user_limit=4,
        max_users=20,
        only=("illust",),
        exclude=("manga",),
    )
    assert config.follows == "private"
    assert config.per_user_limit == 4
    assert config.max_users == 20
    assert config.only == ("illust",)
    assert config.exclude == ("manga",)


def test_following_config_invalid_follows_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FollowingConfig(output_dir=tmp_path, follows="bogus")


def test_following_config_limit_zero_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FollowingConfig(output_dir=tmp_path, limit=0)


def test_following_config_per_user_limit_zero_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FollowingConfig(output_dir=tmp_path, per_user_limit=0)


def test_following_config_max_users_zero_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FollowingConfig(output_dir=tmp_path, max_users=0)


@pytest.mark.parametrize(
    "summary, expected",
    [
        (FollowingRunSummary(works_total=0), ExitCode.NO_BOOKMARKS),
        (
            FollowingRunSummary(works_total=2, works_succeeded=2, files_downloaded=2),
            ExitCode.SUCCESS,
        ),
        (
            FollowingRunSummary(works_total=2, works_succeeded=1, works_failed=1),
            ExitCode.PARTIAL,
        ),
        (
            FollowingRunSummary(works_total=2, works_succeeded=2, files_failed=1),
            ExitCode.PARTIAL,
        ),
    ],
)
def test_decide_exit_code_following_table(
    summary: FollowingRunSummary, expected: ExitCode
) -> None:
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


def test_progress_event_defaults() -> None:
    event = ProgressEvent(kind=ProgressKind.WORK)
    assert event.kind is ProgressKind.WORK
    assert event.works_total == 0
    assert event.files_downloaded == 0
    assert event.files_skipped == 0
    assert event.files_failed == 0
    assert event.bytes_downloaded == 0
    assert event.users_total == 0
    assert event.index == 0
    assert event.work_id is None
    assert event.title is None
    assert event.page_count is None
    assert event.user_id is None
    assert event.user_name is None
    assert event.planned == ()
    assert event.line is None


def test_progress_event_is_frozen() -> None:
    event = ProgressEvent(kind=ProgressKind.WORK)
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        event.index = 5  # type: ignore[misc]


def test_progress_event_planned_default_is_empty_tuple() -> None:
    first = ProgressEvent(kind=ProgressKind.WORK)
    second = ProgressEvent(kind=ProgressKind.WORK)
    assert first.planned == ()
    assert second.planned == ()


def test_progress_event_all_fields_round_trip() -> None:
    event = ProgressEvent(
        kind=ProgressKind.WORK_PLANNED,
        works_total=42,
        files_downloaded=10,
        files_skipped=3,
        files_failed=1,
        bytes_downloaded=2048,
        users_total=7,
        index=4,
        work_id=123456,
        title="A Title",
        page_count=5,
        user_id="999",
        user_name="An Artist",
        planned=("page1.jpg", "page2.jpg"),
        line="Planned 5 pages for work 123456",
    )
    assert event.kind is ProgressKind.WORK_PLANNED
    assert event.works_total == 42
    assert event.files_downloaded == 10
    assert event.files_skipped == 3
    assert event.files_failed == 1
    assert event.bytes_downloaded == 2048
    assert event.users_total == 7
    assert event.index == 4
    assert event.work_id == 123456
    assert event.title == "A Title"
    assert event.page_count == 5
    assert event.user_id == "999"
    assert event.user_name == "An Artist"
    assert event.planned == ("page1.jpg", "page2.jpg")
    assert event.line == "Planned 5 pages for work 123456"
