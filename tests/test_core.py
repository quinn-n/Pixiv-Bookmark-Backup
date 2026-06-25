"""Unit tests for the pure core helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_bookmark_backup.core import BackupConfig, describe_plan


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
