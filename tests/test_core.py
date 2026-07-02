"""Unit tests for the pure core helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pixiv_bookmark_backup.core import (
    BackupConfig,
    ExitCode,
    FollowingConfig,
    FollowingRunSummary,
    RequestPacer,
    RunSummary,
    decide_exit_code,
    describe_plan,
)

from conftest import ScriptedMonotonic, SpySleep


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


# ---------------------------------------------------------------------------
# RequestPacer
# ---------------------------------------------------------------------------


def test_request_pacer_period_zero_is_strict_no_op() -> None:
    # period == 0 must disable pacing entirely: no clock reads, no sleeps,
    # preserving un-paced behavior byte-for-byte.
    clock = ScriptedMonotonic([])
    sleep = SpySleep()
    pacer = RequestPacer(0, monotonic=clock, sleep=sleep)

    pacer.tick()
    pacer.tick()
    pacer.tick()

    assert clock.calls == 0
    assert sleep.durations == []


def test_request_pacer_first_tick_only_records_start() -> None:
    # The very first tick has no previous start, so it must not sleep -- it just
    # records the baseline by reading the clock exactly once.
    clock = ScriptedMonotonic([100.0])
    sleep = SpySleep()
    pacer = RequestPacer(0.5, monotonic=clock, sleep=sleep)

    pacer.tick()

    assert clock.calls == 1
    assert sleep.durations == []


def test_request_pacer_sleeps_remaining_when_elapsed_below_period() -> None:
    # First tick reads once (start = 100.0). Second tick reads once before
    # sleeping (100.2 -> elapsed 0.2, remaining 0.3) and once after sleeping.
    clock = ScriptedMonotonic([100.0, 100.2, 100.5])
    sleep = SpySleep()
    pacer = RequestPacer(0.5, monotonic=clock, sleep=sleep)

    pacer.tick()
    pacer.tick()

    assert sleep.durations == [pytest.approx(0.3)]


def test_request_pacer_does_not_sleep_when_elapsed_meets_period() -> None:
    # Second tick's elapsed is 1.3 >= period 0.5, so remaining is negative and
    # no sleep occurs; the clock is read once per tick.
    clock = ScriptedMonotonic([100.0, 101.3])
    sleep = SpySleep()
    pacer = RequestPacer(0.5, monotonic=clock, sleep=sleep)

    pacer.tick()
    pacer.tick()

    assert sleep.durations == []


def test_request_pacer_baseline_uses_post_sleep_read() -> None:
    # Regression guard for the start-to-start re-read: after sleeping, the new
    # baseline must come from the POST-sleep monotonic read, not the pre-sleep
    # timestamp.
    #
    # Scripted values:
    #   tick 1: reads 100.0                    -> _last_start = 100.0
    #   tick 2: reads 100.2 (elapsed 0.2, sleep 0.3), re-reads 100.9
    #                                          -> _last_start = 100.9 (post-sleep)
    #   tick 3: reads 101.0 (elapsed vs 100.9 = 0.1 -> sleep 0.4), re-reads 101.4
    #
    # If tick 2 had (incorrectly) kept the pre-sleep 100.2 as the baseline, then
    # tick 3's elapsed would be 101.0 - 100.2 = 0.8 >= 0.5 and it would NOT
    # sleep. The correct post-sleep baseline (100.9) makes tick 3 sleep 0.4.
    clock = ScriptedMonotonic([100.0, 100.2, 100.9, 101.0, 101.4])
    sleep = SpySleep()
    pacer = RequestPacer(0.5, monotonic=clock, sleep=sleep)

    pacer.tick()
    pacer.tick()
    pacer.tick()

    assert sleep.durations == [pytest.approx(0.3), pytest.approx(0.4)]


def test_request_pacer_fractional_period_is_accepted() -> None:
    pacer = RequestPacer(0.5)
    assert pacer.period == pytest.approx(0.5)


def test_request_pacer_negative_period_is_rejected() -> None:
    with pytest.raises((ValueError,)):
        RequestPacer(-1)


def test_request_pacer_keyboard_interrupt_from_sleep_propagates() -> None:
    # A KeyboardInterrupt raised by sleep must not be swallowed by tick().
    clock = ScriptedMonotonic([100.0, 100.2])
    sleep = SpySleep(raise_on_first=True)
    pacer = RequestPacer(0.5, monotonic=clock, sleep=sleep)

    pacer.tick()  # records baseline, no sleep
    with pytest.raises(KeyboardInterrupt):
        pacer.tick()  # elapsed 0.2 < period -> tries to sleep -> raises


@pytest.mark.parametrize(
    "elapsed, expected_sleep",
    [
        (0.2, 0.3),
        (0.5, None),
        (1.3, None),
    ],
)
def test_request_pacer_arithmetic_branches(
    elapsed: float, expected_sleep: float | None
) -> None:
    clock = ScriptedMonotonic([100.0, 100.0 + elapsed, 200.0])
    sleep = SpySleep()
    pacer = RequestPacer(0.5, monotonic=clock, sleep=sleep)

    pacer.tick()
    pacer.tick()

    if expected_sleep is None:
        assert sleep.durations == []
    else:
        assert sleep.durations == [pytest.approx(expected_sleep)]


# ---------------------------------------------------------------------------
# time_per_request config field (BackupConfig + FollowingConfig)
# ---------------------------------------------------------------------------


def test_backup_config_time_per_request_defaults_to_zero_float(tmp_path: Path) -> None:
    config = BackupConfig(output_dir=tmp_path)
    assert config.time_per_request == 0.0
    assert isinstance(config.time_per_request, float)


def test_backup_config_time_per_request_stores_value(tmp_path: Path) -> None:
    config = BackupConfig(output_dir=tmp_path, time_per_request=0.5)
    assert config.time_per_request == 0.5


def test_backup_config_time_per_request_coerces_int_to_float(tmp_path: Path) -> None:
    config = BackupConfig(output_dir=tmp_path, time_per_request=1)
    assert isinstance(config.time_per_request, float)
    assert config.time_per_request == 1.0


def test_backup_config_negative_time_per_request_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BackupConfig(output_dir=tmp_path, time_per_request=-0.5)


def test_following_config_time_per_request_defaults_to_zero_float(
    tmp_path: Path,
) -> None:
    config = FollowingConfig(output_dir=tmp_path)
    assert config.time_per_request == 0.0
    assert isinstance(config.time_per_request, float)


def test_following_config_time_per_request_stores_value(tmp_path: Path) -> None:
    config = FollowingConfig(output_dir=tmp_path, time_per_request=0.5)
    assert config.time_per_request == 0.5


def test_following_config_time_per_request_coerces_int_to_float(tmp_path: Path) -> None:
    config = FollowingConfig(output_dir=tmp_path, time_per_request=1)
    assert isinstance(config.time_per_request, float)
    assert config.time_per_request == 1.0


def test_following_config_negative_time_per_request_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FollowingConfig(output_dir=tmp_path, time_per_request=-0.5)
