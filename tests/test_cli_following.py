"""CLI tests for the ``backup-following`` command, driven via ``CliRunner``.

Two injection strategies mirror ``test_cli.py``:

* A fully offline :class:`FakeFollowingClient` (whose ``download`` writes a small
  real file) is patched in for ``cli.Pixivpy3Client`` to drive the *real*
  ``run_following`` orchestrator for the happy-path / dry-run cases, so we can
  assert on the per-author files and the ``details.json`` it writes.
* For deterministic exit-code mapping and option plumbing, ``cli.run_following``
  itself is monkeypatched to capture the :class:`FollowingConfig` and return a
  scripted :class:`FollowingRunSummary` or raise a scripted exception.

CliRunner in click 8.4.1 has no ``mix_stderr`` kwarg: data is asserted on
``result.stdout`` and logs/errors on ``result.stderr``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pixiv_bookmark_backup import cli as cli_module
from pixiv_bookmark_backup.cli import cli
from pixiv_bookmark_backup.core import FollowingConfig, FollowingRunSummary
from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    UgoiraInfo,
    WorkImage,
)
from pixiv_bookmark_backup.pixiv_client import (
    ApiError,
    AuthRejected,
    FollowedUser,
    PixivClient,
)

FAKE_REFRESH = "fake-x"
SELF_ID = "555"


# -- fakes & helpers ----------------------------------------------------


def _make_user(user_id: int, *, name: str = "Creator") -> FollowedUser:
    return FollowedUser(id=user_id, name=name, account=f"acct_{user_id}")


def _make_work(
    work_id: int, *, author_id: int, work_type: str = "illust", pages: int = 1
) -> BookmarkWork:
    images = [
        WorkImage(page=i, source_url=f"https://i.pximg.net/{work_id}_p{i}.jpg")
        for i in range(pages)
    ]
    if work_type == "ugoira":
        images = []
    return BookmarkWork(
        id=work_id,
        type=work_type,
        title="A Work",
        author=Author(id=author_id, name="Artist", account=f"acct_{author_id}"),
        posted_at="2024-05-01T12:00:00+09:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=max(pages, 1),
        images=images,
    )


class FakeFollowingClient:
    """Offline stand-in for :class:`PixivClient` that writes real files."""

    def __init__(
        self,
        following_by_restrict: dict[str, list[FollowedUser]],
        works_by_user: dict[str, list[BookmarkWork]],
        *,
        self_id: str = SELF_ID,
    ) -> None:
        self._following_by_restrict = following_by_restrict
        self._works_by_user = works_by_user
        self._self_id = self_id
        self.download_calls: list[str] = []

    def authenticate(self, refresh_token: str) -> str:
        return self._self_id

    def iter_following(self, user_id: str, restrict: str) -> Iterator[FollowedUser]:
        yield from self._following_by_restrict.get(restrict, [])

    def iter_user_works(self, user_id: str) -> Iterator[BookmarkWork]:
        yield from self._works_by_user.get(user_id, [])

    def iter_bookmarks(  # pragma: no cover
        self, user_id: str, restrict: str
    ) -> Iterator[BookmarkWork]:
        raise NotImplementedError

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:  # pragma: no cover
        raise NotImplementedError

    def download(
        self, url: str, dest_path: Path, referer: str = "https://www.pixiv.net/"
    ) -> int:
        self.download_calls.append(url)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        data = b"fake-image-bytes"
        dest_path.write_bytes(data)
        return len(data)


# Static guarantee the fake satisfies the public Protocol.
_: type[PixivClient] = FakeFollowingClient


def _install_client(
    monkeypatch: pytest.MonkeyPatch, client: FakeFollowingClient
) -> None:
    monkeypatch.setattr(cli_module, "Pixivpy3Client", lambda: client)


def _summary(**overrides: Any) -> FollowingRunSummary:
    params: dict[str, Any] = {
        "works_total": 1,
        "works_succeeded": 1,
        "users_total": 1,
        "users_with_works": 1,
        "files_downloaded": 1,
        "following_of": SELF_ID,
    }
    params.update(overrides)
    return FollowingRunSummary(**params)


def _install_run_following(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: FollowingRunSummary | None = None,
    raises: BaseException | None = None,
    captured: dict[str, Any] | None = None,
) -> None:
    """Patch ``cli.run_following`` to capture its config and script the result."""

    def fake_run(
        config: Any,
        auth: Any,
        client: Any,
        *,
        progress: Any = None,
        **_kw: Any,
    ) -> FollowingRunSummary:
        if captured is not None:
            captured["config"] = config
        if raises is not None:
            raise raises
        return summary if summary is not None else _summary()

    monkeypatch.setattr(cli_module, "run_following", fake_run)


def _token_env() -> dict[str, str]:
    return {"PIXIV_REFRESH_TOKEN": FAKE_REFRESH}


def _missing_config(tmp_path: Path) -> str:
    return str(tmp_path / "nope.toml")


# -- help ---------------------------------------------------------------


def test_help_lists_new_flags_and_omits_visibility(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["backup-following", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "Back up works from the Pixiv users you follow." in out
    for flag in ("--follows", "--per-user-limit", "--max-users", "--only", "--exclude"):
        assert flag in out
    assert "./backup-following" in out
    # This command has no --visibility axis and no positional USER_ID argument.
    assert "--visibility" not in out
    assert "[USER_ID]" not in out and "USER_ID]" not in out.split("Options")[0]


def test_group_help_lists_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "backup-following" in result.output


# -- happy path (real orchestrator) ------------------------------------


def test_success_end_to_end_per_author_layout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10), _make_user(20)]},
        {"10": [_make_work(101, author_id=10)], "20": [_make_work(201, author_id=20)]},
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Backed up 2/2 works (0 failed) from 2/2 users" in result.stdout
    assert (out / "details.json").exists()
    assert (out / "10" / "101_p0.jpg").exists()
    assert (out / "20" / "201_p0.jpg").exists()
    assert client.download_calls


def test_json_summary_is_clean_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
            "--json",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # stdout is exactly one JSON object
    assert payload["schema"] == 1
    assert payload["mode"] == "following"
    assert payload["following_of"] == SELF_ID
    assert payload["follow_visibility"] == "public"
    assert payload["users_total"] == 1
    assert payload["users_with_works"] == 1
    assert payload["works_total"] == 1
    assert payload["details_json"] == str(out / "details.json")
    assert payload["dry_run"] is False
    assert payload["exit_code"] == 0


def test_progress_on_stderr_summary_on_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0
    assert "Backing up works from your public follows" in result.stderr
    assert "Backing up works" not in result.stdout


def test_quiet_suppresses_banner(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
            "--quiet",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Backing up works" not in result.stderr  # banner suppressed
    assert "Backed up 1/1 works" in result.stdout  # summary still printed


def test_dry_run_writes_nothing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
            "--dry-run",
            "--json",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["details_json"] is None
    assert not (out / "details.json").exists()
    assert not client.download_calls


# -- usage errors (exit 2) ---------------------------------------------


def test_quiet_and_verbose_mutually_exclusive(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path), "-q", "-v"],
        env=_token_env(),
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_only_and_exclude_mutually_exclusive(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            "--only",
            "1",
            "--exclude",
            "2",
        ],
        env=_token_env(),
    )
    assert result.exit_code == 2
    assert "--only and --exclude are mutually exclusive" in result.stderr


def test_missing_token_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env={"PIXIV_REFRESH_TOKEN": ""},
    )
    assert result.exit_code == 2
    assert "No Pixiv refresh token found" in result.stderr


# -- option plumbing ----------------------------------------------------


def test_options_plumbed_into_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _install_run_following(monkeypatch, captured=captured)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--follows",
            "all",
            "--limit",
            "5",
            "--per-user-limit",
            "3",
            "--max-users",
            "2",
            "--only",
            "10",
            "--only",
            "20",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    config = captured["config"]
    assert isinstance(config, FollowingConfig)
    assert config.follows == "all"
    assert config.limit == 5
    assert config.per_user_limit == 3
    assert config.max_users == 2
    assert config.only == ("10", "20")
    assert config.exclude == ()


def test_default_output_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _install_run_following(monkeypatch, captured=captured)

    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].output_dir == Path("./backup-following")


# -- exit-code mapping (scripted run_following) ------------------------


def test_auth_rejected_exit_3(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(monkeypatch, raises=AuthRejected("bad token"))
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )
    assert result.exit_code == 3
    assert "bad token" in result.stderr


def test_api_error_exit_4(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(monkeypatch, raises=ApiError("network down"))
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )
    assert result.exit_code == 4
    assert "Pixiv API error" in result.stderr


def test_keyboard_interrupt_exit_130(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(monkeypatch, raises=KeyboardInterrupt())
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )
    assert result.exit_code == 130
    assert "interrupted" in result.stderr


def test_no_works_exit_5(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(
        monkeypatch, summary=_summary(works_total=0, works_succeeded=0)
    )
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )
    assert result.exit_code == 5
    assert "0/0 works" in result.stdout


def test_partial_exit_6(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(
        monkeypatch,
        summary=_summary(works_total=2, works_succeeded=1, works_failed=1),
    )
    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )
    assert result.exit_code == 6
    assert "1 failed" in result.stdout


# -- --time-per-request -------------------------------------------------


def test_time_per_request_help_lists_option_and_default(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["backup-following", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "--time-per-request" in out
    assert "SECONDS" in out
    # click renders "[default: 0.0; x>=0]"; match the safe substring.
    assert "default: 0.0" in out


def test_time_per_request_negative_is_exit_2(runner: CliRunner, tmp_path: Path) -> None:
    # click's FloatRange rejects the value before the command body runs.
    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--time-per-request",
            "-1",
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 2
    assert "range" in result.stderr


def test_time_per_request_fractional_reaches_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _install_run_following(monkeypatch, captured=captured)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--time-per-request",
            "0.5",
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].time_per_request == 0.5


def test_time_per_request_defaults_to_zero_in_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    _install_run_following(monkeypatch, captured=captured)

    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].time_per_request == 0.0


def test_time_per_request_absent_from_json_payload(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(monkeypatch)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--json",
            "--time-per-request",
            "0",
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # The summary schema is unchanged: the pacing knob is not a JSON field.
    assert payload["schema"] == 1
    assert "time_per_request" not in payload


# -- security warning ---------------------------------------------------


def test_refresh_token_on_commandline_warns(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run_following(monkeypatch)
    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            "--refresh-token",
            "secret-token",
        ],
        env={"PIXIV_REFRESH_TOKEN": ""},
    )
    assert result.exit_code == 0, result.output
    assert "visible to other processes" in result.stderr
    assert "secret-token" not in result.stderr  # the token itself is never printed


def test_insecure_config_file_warns_on_stderr(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A world-readable config supplying the token triggers a chmod warning.
    config = tmp_path / "config.toml"
    config.write_text(f'[auth]\nrefresh_token = "{FAKE_REFRESH}"\n', encoding="utf-8")
    os.chmod(config, 0o644)
    _install_run_following(monkeypatch)

    result = runner.invoke(
        cli,
        ["backup-following", "--config", str(config)],
        env={"PIXIV_REFRESH_TOKEN": ""},
    )

    assert result.exit_code == 0, result.output
    assert "readable by other users" in result.stderr
    assert "chmod 600" in result.stderr
    assert str(config) in result.stderr
