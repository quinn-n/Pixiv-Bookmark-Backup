"""CLI smoke tests driven through click's CliRunner."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pixiv_bookmark_backup import __version__
from pixiv_bookmark_backup.cli import cli


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_summary(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Back up your Pixiv bookmarks" in result.output


def test_backup_dry_run_succeeds(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["backup", "12345", "--output-dir", str(tmp_path), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "12345" in result.output
    assert "Would back up" in result.output


def test_backup_without_dry_run_is_not_implemented(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli,
        ["backup", "12345", "--output-dir", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_backup_rejects_unknown_visibility(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["backup", "12345", "-o", str(tmp_path), "--visibility", "bogus"],
    )
    assert result.exit_code == 2  # click usage error
