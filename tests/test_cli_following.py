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
from click.testing import CliRunner, _NamedTextIOWrapper

from pixiv_bookmark_backup import cli as cli_module
from pixiv_bookmark_backup.cli import cli
from pixiv_bookmark_backup.core import (
    FollowingConfig,
    FollowingRunSummary,
    ProgressEvent,
    ProgressKind,
)
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
        # Track API activity so a test can prove a corrupt-manifest run fails
        # fast (pre-flight) without authenticating or listing anything.
        self.authenticate_calls = 0
        self.list_calls = 0

    def authenticate(self, refresh_token: str) -> str:
        self.authenticate_calls += 1
        return self._self_id

    def iter_following(self, user_id: str, restrict: str) -> Iterator[FollowedUser]:
        self.list_calls += 1
        yield from self._following_by_restrict.get(restrict, [])

    def iter_user_works(self, user_id: str) -> Iterator[BookmarkWork]:
        self.list_calls += 1
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
    # The command constructs the real client with warn= (and possibly other
    # kwargs); swallow any constructor arguments and return the fake regardless.
    monkeypatch.setattr(cli_module, "Pixivpy3Client", lambda *args, **kwargs: client)


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
    for flag in (
        "--follow-visibility",
        "--per-user-limit",
        "--max-users",
        "--only",
        "--exclude",
    ):
        assert flag in out
    assert "./backup-following" in out
    # The flag is --follow-visibility, not the old --follows spelling.
    assert "--follows" not in out
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


def test_only_matching_nobody_warns_on_stderr(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Following user 10, but --only names 999 (nobody) -> full miss.
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--only",
            "999",
        ],
        env=_token_env(),
    )

    # No user matched -> 0 works -> the existing exit 5, plus the new warning.
    assert result.exit_code == 5, result.output
    assert "Warning: no followed user matched --only for 999" in result.stderr


def test_only_partial_miss_warns_only_for_unmatched_id(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 10 is followed (and backed up), 999 matches nobody -> exit 0 with a warning
    # naming only 999.
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--only",
            "10",
            "--only",
            "999",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Warning: no followed user matched --only for 999" in result.stderr
    # The matched ID must not be named as unmatched.
    assert "for 10" not in result.stderr
    assert "10, 999" not in result.stderr


def test_exclude_matching_nobody_warns_on_stderr(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 10 is followed and backed up; --exclude 999 excludes nobody -> warning.
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--exclude",
            "999",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Warning: no followed user matched --exclude for 999" in result.stderr


def test_only_full_match_does_not_warn(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--only",
            "10",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "matched --only" not in result.stderr


def test_unmatched_warning_suppressed_when_run_truncated_by_limit(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --limit stops enumeration after the first work of user 10, before 999
    # could ever be observed. Suppress the warning to avoid a false positive.
    client = FakeFollowingClient(
        {"public": [_make_user(10)]},
        {
            "10": [
                _make_work(101, author_id=10),
                _make_work(102, author_id=10),
            ]
        },
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--only",
            "10",
            "--only",
            "999",
            "--limit",
            "1",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "matched --only" not in result.stderr


def test_unmatched_warning_suppressed_when_run_truncated_by_max_users(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --max-users stops enumeration after user 10, before 999 could be observed.
    # 10 and 20 both match --only, so the cap genuinely trips (truncated run) and
    # the warning for 999 is suppressed.
    client = FakeFollowingClient(
        {"public": [_make_user(10), _make_user(20)]},
        {
            "10": [_make_work(101, author_id=10)],
            "20": [_make_work(201, author_id=20)],
        },
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--only",
            "10",
            "--only",
            "20",
            "--only",
            "999",
            "--max-users",
            "1",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "matched --only" not in result.stderr


def test_unmatched_warning_suppressed_when_follow_list_errors_midway(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The follow-list listing fails partway (after user 10's works are recorded),
    # degrading to a partial run (exit 6). Because enumeration was truncated, the
    # never-observed 999 must not trigger a "matched nobody" warning.
    class _FollowListFailsClient(FakeFollowingClient):
        def iter_following(self, user_id: str, restrict: str) -> Iterator[FollowedUser]:
            yield _make_user(10)
            raise ApiError("follow-list listing interrupted")

    client = _FollowListFailsClient(
        {"public": []}, {"10": [_make_work(101, author_id=10)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
            "--only",
            "10",
            "--only",
            "999",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 6, result.output
    assert "matched --only" not in result.stderr


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


@pytest.mark.parametrize("flag", ["--only", "--exclude"])
def test_non_numeric_user_id_is_exit_2(
    runner: CliRunner, tmp_path: Path, flag: str
) -> None:
    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            flag,
            "abc",
        ],
        env=_token_env(),
    )
    assert result.exit_code == 2
    # Teaching BadParameter naming the offending value and the option.
    assert "'abc' is not a valid Pixiv user ID" in result.stderr
    assert flag in result.stderr


def test_non_numeric_user_id_rejected_among_valid_ones(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            "--only",
            "10",
            "--only",
            "1.5",
        ],
        env=_token_env(),
    )
    assert result.exit_code == 2
    assert "'1.5' is not a valid Pixiv user ID" in result.stderr


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
            "--follow-visibility",
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
    assert config.follow_visibility == "all"
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


def test_old_follows_flag_is_rejected(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-rename ``--follows`` spelling is gone; click rejects it (exit 2)."""
    _install_run_following(monkeypatch)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            "--follows",
            "all",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 2
    assert "No such option" in result.output
    assert "--follows" in result.output
    # Click helpfully points users at the new spelling.
    assert "--follow-visibility" in result.output


def test_follow_visibility_invalid_choice_is_rejected(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value outside VISIBILITIES is rejected by click.Choice (exit 2)."""
    _install_run_following(monkeypatch)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--config",
            _missing_config(tmp_path),
            "--follow-visibility",
            "bogus",
        ],
        env=_token_env(),
    )

    assert result.exit_code == 2
    assert "'bogus'" in result.output


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


# -- progress reporting (verbosity / mode auto-detection) ---------------


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the command see an interactive (TTY) stderr during ``invoke``."""

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)


def _install_run_following_emitting(
    monkeypatch: pytest.MonkeyPatch,
    events: list[ProgressEvent],
    *,
    summary: FollowingRunSummary | None = None,
    raises: BaseException | None = None,
    warn_message: str | None = None,
) -> None:
    """Patch ``cli.run_following`` to emit ``events`` before returning/raising.

    When ``warn_message`` is given, the fake invokes the injected ``warn``
    callback with it *after* emitting the progress events (i.e. while the live
    bar is still on screen), mirroring how the real orchestrator emits the
    unmatched-filter warning from inside the run.
    """

    def fake_run(
        config: Any,
        auth: Any,
        client: Any,
        *,
        progress: Any = None,
        warn: Any = None,
        **_kw: Any,
    ) -> FollowingRunSummary:
        for event in events:
            progress(event)
        if warn_message is not None and warn is not None:
            warn(warn_message)
        if raises is not None:
            raise raises
        return summary if summary is not None else _summary()

    monkeypatch.setattr(cli_module, "run_following", fake_run)


def _work_events(count: int) -> list[ProgressEvent]:
    """A run's worth of ``WORK`` events with cumulative ``works_total``."""

    return [
        ProgressEvent(
            kind=ProgressKind.WORK,
            works_total=i,
            files_downloaded=i,
            line=f"  {100 + i} 'A Work' — 1 page(s)",
        )
        for i in range(1, count + 1)
    ]


def test_default_v0_nontty_has_no_per_work_lines(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CliRunner default stderr is not a TTY, so v0 auto-selects MILESTONE:
    # the old per-work verbose lines must be gone (the key regression guard).
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]},
        {
            "10": [
                _make_work(101, author_id=10),
                _make_work(102, author_id=10),
                _make_work(103, author_id=10),
            ]
        },
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
    assert "page(s)" not in result.stderr
    assert "..." in result.stderr  # milestone line emitted instead


def test_verbose_shows_per_work_lines(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10, pages=2)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "-v",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "page(s)" in result.stderr


def test_dry_run_v0_still_shows_would_create(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10, pages=2)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--dry-run",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "would create" in result.stderr
    assert "page(s)" in result.stderr


def test_dry_run_on_tty_still_verbose_not_bar(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dry-run BAR exemption is only observable on a *real* TTY: force one so
    # the auto-detected BAR branch is reachable, then prove --dry-run overrides
    # it (VERBOSE "would create" lines, never a \r-redrawn bar).
    _force_tty(monkeypatch)
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10, pages=2)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--dry-run",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "would create" in result.stderr
    assert "page(s)" in result.stderr
    assert "\r" not in result.stderr  # no in-place bar redraw


def test_quiet_has_no_progress_content(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10, pages=2)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--quiet",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Backing up works" not in result.stderr  # banner suppressed
    assert "page(s)" not in result.stderr
    assert "..." not in result.stderr
    assert "\r" not in result.stderr
    assert "Backed up 1/1 works" in result.stdout  # summary still printed


def test_tty_draws_bar_and_finishes_cleanly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A simulated TTY auto-selects BAR: a carriage-return-redrawn line appears,
    # and finish() terminates it cleanly. Four events trip the first-draw
    # threshold.
    _force_tty(monkeypatch)
    _install_run_following_emitting(
        monkeypatch,
        _work_events(4),
        summary=_summary(works_total=4, works_succeeded=4),
    )

    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "\r" in result.stderr
    assert result.stderr.endswith("\n")
    assert not result.stderr.rstrip("\n").endswith("\r")


def test_tty_bar_warning_prints_on_its_own_clean_line(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In BAR mode the unmatched-filter warning is emitted from inside the run,
    # while the dangling bar line is still on screen. It must clear that line
    # first so the warning is not appended onto the spinner.
    _force_tty(monkeypatch)
    _install_run_following_emitting(
        monkeypatch,
        _work_events(4),
        summary=_summary(works_total=4, works_succeeded=4),
        warn_message="no followed user matched --only for 999",
    )

    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path), "--only", "999"],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    marker = "Warning: no followed user matched --only for 999"
    assert marker in result.stderr
    # The warning must start on a fresh line (bar cleared), never glued onto the
    # carriage-return-redrawn spinner line.
    assert result.stderr[result.stderr.index(marker) - 1] == "\n"


def test_tty_bar_counts_users_when_max_users_set(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With --max-users (and no --limit) the determinate bar counts users, so a
    # "N/2 users" fragment appears in the redrawn bar line.
    _force_tty(monkeypatch)
    events = [
        ProgressEvent(kind=ProgressKind.USER, users_total=i, line=f"user {i}")
        for i in range(1, 5)
    ]
    _install_run_following_emitting(
        monkeypatch, events, summary=_summary(users_total=2)
    )

    result = runner.invoke(
        cli,
        [
            "backup-following",
            "--max-users",
            "2",
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "users" in result.stderr
    assert "/2 users" in result.stderr


def test_interrupt_message_on_clean_line_after_bar(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ordering guard: finish() erases the bar before the interrupt message.
    _force_tty(monkeypatch)
    _install_run_following_emitting(
        monkeypatch, _work_events(4), raises=KeyboardInterrupt()
    )

    result = runner.invoke(
        cli,
        ["backup-following", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 130
    lines = result.stderr.splitlines()
    assert "interrupted; partial backup saved" in lines


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


# -- resume manifest: corrupt fails loud, schema mismatch warns ---------


def test_corrupt_manifest_fails_loud_exit_2(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "details.json").write_text("this is not json {{{", encoding="utf-8")
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

    assert result.exit_code == 2
    assert "not valid JSON" in result.stderr
    assert "rename or delete it" in result.stderr
    assert str(out / "details.json") in result.stderr
    assert "Traceback" not in result.output
    assert "Backed up" not in result.stdout
    # Fail FAST: caught up front, before any API work -- nothing authenticated,
    # listed, or downloaded.
    assert client.authenticate_calls == 0
    assert client.list_calls == 0
    assert client.download_calls == []
    assert (out / "details.json").read_text(encoding="utf-8") == "this is not json {{{"
    assert not (out / "details.json.bak").exists()


def test_schema_mismatch_regenerates_and_warns_on_stderr(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    details = out / "details.json"
    details.write_text(
        json.dumps({"schema_version": 999, "generated_at": "", "source": {}}),
        encoding="utf-8",
    )
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

    assert result.exit_code == 0, result.output
    assert (out / "details.json.bak").exists()
    manifest = json.loads(details.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert "Warning:" in result.stderr
    assert str(out / "details.json.bak") in result.stderr
    assert "Backed up 1/1 works" in result.stdout


def test_schema_mismatch_left_intact_when_run_fails_after_preflight(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The move-aside is deferred to a successful flush: if the run fails after
    # pre-flight (here, auth is rejected) the schema-mismatched details.json is
    # left in place -- not moved, no .bak, no regenerate.
    out = tmp_path / "out"
    out.mkdir()
    details = out / "details.json"
    original = json.dumps({"schema_version": 999, "generated_at": "", "source": {}})
    details.write_text(original, encoding="utf-8")
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(101, author_id=10)]}
    )

    def _reject(_token: str) -> str:
        raise AuthRejected("Pixiv rejected the refresh token")

    monkeypatch.setattr(client, "authenticate", _reject)
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

    assert result.exit_code == 3
    assert details.read_text(encoding="utf-8") == original
    assert not (out / "details.json.bak").exists()
    assert client.download_calls == []
