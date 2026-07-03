"""CLI tests driven through click's ``CliRunner``.

These exercise the thin click layer end to end. Two injection strategies are
used:

* A fully offline :class:`FakePixivClient` (whose ``download`` writes a small
  real file) is patched in for ``cli.Pixivpy3Client`` to drive the *real*
  orchestrator for the happy-path / dry-run / metadata-only cases, so we can
  assert on the ``details.json`` it writes.
* For deterministic exit-code mapping (auth/network/interrupt/partial/empty),
  ``cli.run_backup`` itself is monkeypatched to return a scripted
  :class:`RunSummary` or raise a scripted exception.

CliRunner in click 8.4.1 has no ``mix_stderr`` kwarg: data is asserted on
``result.stdout`` and logs/errors on ``result.stderr``; ``result.output`` is the
combined stream and is therefore not used to prove a clean stdout.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO, cast

import pytest
from click.testing import CliRunner, _NamedTextIOWrapper

from pixiv_bookmark_backup import __version__, cli as cli_module
from pixiv_bookmark_backup.backup import BackupUsageError
from pixiv_bookmark_backup.cli import cli
from pixiv_bookmark_backup.core import ProgressEvent, ProgressKind, RunSummary
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

# A deliberately fake, non-secret placeholder: any non-empty string lets
# resolve_auth() succeed so the offline tests can run without real credentials.
FAKE_REFRESH = "fake-x"
SELF_ID = "555"


# -- fakes & helpers ----------------------------------------------------


def _make_work(
    work_id: int,
    *,
    work_type: str = "illust",
    pages: int = 1,
    title: str = "A Work",
) -> BookmarkWork:
    """Build a :class:`BookmarkWork` with ``pages`` original-resolution images."""

    images = [
        WorkImage(page=i, source_url=f"https://i.pximg.net/{work_id}_p{i}.jpg")
        for i in range(pages)
    ]
    if work_type == "ugoira":
        images = []
    return BookmarkWork(
        id=work_id,
        type=work_type,
        title=title,
        author=Author(id=7, name="Artist", account="artist_acct"),
        posted_at="2024-05-01T12:00:00+09:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=max(pages, 1),
        images=images,
    )


class FakePixivClient:
    """Offline stand-in for :class:`PixivClient` that writes real files.

    ``authenticate`` returns a fixed self id. ``iter_bookmarks`` yields the
    scripted works for the requested ``restrict`` pass. ``download`` writes a
    few bytes to ``dest_path`` and returns the byte count, so a real
    orchestrator run produces image files and a ``details.json`` recording
    ``downloaded`` statuses. ``ugoira_metadata`` is unused by the illust-only
    fixtures here.
    """

    def __init__(
        self,
        works_by_restrict: dict[str, list[BookmarkWork]],
        *,
        self_id: str = SELF_ID,
    ) -> None:
        self._works_by_restrict = works_by_restrict
        self._self_id = self_id
        self.download_calls: list[str] = []
        # Track API activity so a test can prove a corrupt-manifest run fails
        # fast (pre-flight) without authenticating or listing anything.
        self.authenticate_calls = 0
        self.list_calls = 0

    def authenticate(self, refresh_token: str) -> str:
        self.authenticate_calls += 1
        return self._self_id

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        self.list_calls += 1
        yield from self._works_by_restrict.get(restrict, [])

    def iter_following(  # pragma: no cover
        self, user_id: str, restrict: str
    ) -> Iterator[FollowedUser]:
        raise NotImplementedError

    def iter_user_works(  # pragma: no cover
        self, user_id: str
    ) -> Iterator[BookmarkWork]:
        raise NotImplementedError

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:  # pragma: no cover
        raise NotImplementedError

    def download(
        self,
        url: str,
        dest_path: Path,
        referer: str = "https://www.pixiv.net/",
    ) -> int:
        self.download_calls.append(url)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        data = b"fake-image-bytes"
        dest_path.write_bytes(data)
        return len(data)


# Static guarantee the fake satisfies the public Protocol.
_: type[PixivClient] = FakePixivClient


def _install_client(monkeypatch: pytest.MonkeyPatch, client: FakePixivClient) -> None:
    """Patch ``cli.Pixivpy3Client`` so constructing it returns ``client``."""

    monkeypatch.setattr(cli_module, "Pixivpy3Client", lambda: client)


def _install_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: RunSummary | None = None,
    raises: BaseException | None = None,
    captured: dict[str, Any] | None = None,
) -> None:
    """Patch ``cli.run_backup`` to return ``summary`` or raise ``raises``.

    When ``captured`` is provided, the received :class:`BackupConfig` is stored
    under ``captured["config"]`` so tests can assert on option plumbing.
    """

    def fake_run(
        config: Any,
        auth: Any,
        client: Any,
        *,
        progress: Any = None,
        **_kw: Any,
    ) -> RunSummary:
        if captured is not None:
            captured["config"] = config
        if raises is not None:
            raise raises
        assert summary is not None
        return summary

    monkeypatch.setattr(cli_module, "run_backup", fake_run)


def _token_env() -> dict[str, str]:
    """Environment providing a refresh token via the env var."""

    return {"PIXIV_REFRESH_TOKEN": FAKE_REFRESH}


def _no_token_env() -> dict[str, str]:
    """Environment with the refresh-token env var explicitly cleared."""

    return {"PIXIV_REFRESH_TOKEN": ""}


def _missing_config(tmp_path: Path) -> str:
    """Path to a nonexistent config file under ``tmp_path``.

    Pointing ``--config`` here keeps auth resolution deterministic: it never
    falls through to the user's real default config file.
    """

    return str(tmp_path / "nope.toml")


# -- preserved smoke tests ---------------------------------------------


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_summary(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Back up your Pixiv bookmarks" in result.output


# -- exit 0: success ----------------------------------------------------


def test_success_end_to_end_writes_manifest(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        ["backup", "--output-dir", str(out), "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Backed up 1/1 works" in result.stdout
    # The orchestrator wrote its manifest and the downloaded images.
    assert (out / "details.json").exists()
    assert (out / "100_p0.jpg").exists()
    assert client.download_calls  # real downloads were attempted


def test_success_human_summary_via_scripted_run(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(
        monkeypatch,
        summary=RunSummary(
            works_total=3, works_succeeded=3, files_downloaded=5, bytes_downloaded=99
        ),
    )

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Backed up 3/3 works (0 failed), 5 files" in result.stdout


# -- exit 2: usage errors ----------------------------------------------


def test_no_token_anywhere_is_usage_error(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Never construct a client / call run if auth resolution fails first.
    _install_run(monkeypatch, raises=AssertionError("must not run"))

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_no_token_env(),
    )

    assert result.exit_code == 2
    assert "No Pixiv refresh token found" in result.stderr


def test_quiet_and_verbose_are_mutually_exclusive(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli,
        ["backup", "--quiet", "--verbose", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_rejects_unknown_visibility(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["backup", "--visibility", "bogus", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 2  # click Choice rejects it before we run


def test_rejects_bad_since_date(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["backup", "--since", "2025/01/01", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.stderr


def test_backup_usage_error_from_run_is_exit_2(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(
        monkeypatch,
        raises=BackupUsageError("private bookmarks of another user are not accessible"),
    )

    result = runner.invoke(
        cli,
        [
            "backup",
            "999",
            "--visibility",
            "private",
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 2
    assert "another user" in result.stderr


# -- exit 3 / 4 / 130 ---------------------------------------------------


def test_auth_rejected_is_exit_3(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(monkeypatch, raises=AuthRejected("Pixiv rejected the refresh token"))

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 3
    assert "Pixiv rejected the refresh token" in result.stderr


def test_api_error_is_exit_4(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(monkeypatch, raises=ApiError("Failed to list bookmarks: boom"))

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 4
    assert "Pixiv API error" in result.stderr


def test_keyboard_interrupt_is_exit_130(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(monkeypatch, raises=KeyboardInterrupt())

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 130
    assert "interrupted" in result.stderr


# -- exit 5 / 6 (decided from the summary) ------------------------------


def test_zero_works_is_exit_5(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(monkeypatch, summary=RunSummary(works_total=0))

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 5
    assert "Backed up 0/0 works" in result.stdout


def test_partial_run_is_exit_6(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(
        monkeypatch,
        summary=RunSummary(
            works_total=2,
            works_succeeded=1,
            works_failed=1,
            files_downloaded=1,
        ),
    )

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 6
    assert "(1 failed)" in result.stdout


# -- --json -------------------------------------------------------------


def test_json_output_is_single_object_on_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    _install_run(
        monkeypatch,
        summary=RunSummary(
            works_total=2,
            works_succeeded=2,
            files_downloaded=4,
            bytes_downloaded=128,
        ),
    )

    result = runner.invoke(
        cli,
        [
            "backup",
            "12345",
            "--json",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    # stdout is exactly one JSON object and nothing else (logs are on stderr).
    payload = json.loads(result.stdout)
    # The forward-compat schema marker is present (and first) for scripts.
    assert payload["schema"] == 1
    assert next(iter(payload)) == "schema"
    assert payload["user_id"] == "12345"
    assert payload["visibility"] == "public"
    assert payload["output_dir"] == str(out)
    assert payload["works_total"] == 2
    assert payload["works_succeeded"] == 2
    assert payload["files_downloaded"] == 4
    assert payload["bytes_downloaded"] == 128
    assert payload["dry_run"] is False
    assert payload["details_json"] == str(out / "details.json")
    assert payload["exit_code"] == 0


def test_json_output_keeps_logs_off_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakePixivClient({"public": [_make_work(100, pages=1)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
            "--json",
            "--output-dir",
            str(tmp_path / "out"),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    # The "Backing up ..." progress line must not pollute stdout.
    assert "Backing up" not in result.stdout
    assert "Backing up" in result.stderr
    json.loads(result.stdout)  # stdout parses cleanly as one object


# -- --dry-run ----------------------------------------------------------


def test_dry_run_writes_nothing_and_says_would(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
            "--dry-run",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Would back up" in result.stdout
    # Nothing was downloaded and no manifest / output dir was created.
    assert client.download_calls == []
    assert not (out / "details.json").exists()
    assert not out.exists() or list(out.iterdir()) == []


def test_dry_run_json_has_null_details(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(
        monkeypatch,
        summary=RunSummary(works_total=1, works_succeeded=1, files_downloaded=2),
    )

    result = runner.invoke(
        cli,
        ["backup", "--dry-run", "--json", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["details_json"] is None


# -- --metadata-only ----------------------------------------------------


def test_metadata_only_records_without_downloading(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
            "--metadata-only",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    # No image bytes fetched, but the manifest records metadata-only entries.
    assert client.download_calls == []
    assert not (out / "100_p0.jpg").exists()
    manifest = json.loads((out / "details.json").read_text(encoding="utf-8"))
    statuses = {img["status"] for work in manifest["works"] for img in work["images"]}
    assert statuses == {"metadata-only"}


# -- ascii-only stream detection (spinner alphabet) ---------------------


def _stream_with_encoding(encoding: str | None) -> TextIO:
    """A minimal stand-in stream exposing just an ``encoding`` attribute."""

    return cast(TextIO, SimpleNamespace(encoding=encoding))


def test_stream_is_ascii_only_utf8_stream() -> None:
    assert cli_module._stream_is_ascii_only(_stream_with_encoding("utf-8")) is False


def test_stream_is_ascii_only_missing_encoding() -> None:
    # A stream with no usable encoding cannot promise Unicode, so play it safe.
    assert cli_module._stream_is_ascii_only(_stream_with_encoding(None)) is True


def test_stream_is_ascii_only_ascii_codec() -> None:
    # An ASCII (or otherwise Braille-incapable) encoding forces ASCII frames.
    assert cli_module._stream_is_ascii_only(_stream_with_encoding("ascii")) is True


# -- progress reporting (verbosity / mode auto-detection) ---------------


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the command see an interactive (TTY) stderr during ``invoke``.

    ``CliRunner`` swaps the standard streams for ``_NamedTextIOWrapper``s whose
    inherited ``isatty`` returns ``False``. Patching the class to report a TTY
    drives the auto-detected ``BAR`` path that the no-tty default cannot reach.
    """

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)


def _install_run_emitting(
    monkeypatch: pytest.MonkeyPatch,
    events: list[ProgressEvent],
    *,
    summary: RunSummary | None = None,
    raises: BaseException | None = None,
) -> None:
    """Patch ``cli.run_backup`` to emit ``events`` before returning/raising."""

    def fake_run(
        config: Any,
        auth: Any,
        client: Any,
        *,
        progress: Any = None,
        **_kw: Any,
    ) -> RunSummary:
        for event in events:
            progress(event)
        if raises is not None:
            raise raises
        assert summary is not None
        return summary

    monkeypatch.setattr(cli_module, "run_backup", fake_run)


def _work_events(count: int) -> list[ProgressEvent]:
    """A run's worth of ``WORK`` events with cumulative ``works_total``."""

    return [
        ProgressEvent(
            kind=ProgressKind.WORK,
            works_total=i,
            files_downloaded=i,
            line=f"[{i}] {100 + i} 'A Work' — 1 page(s)",
        )
        for i in range(1, count + 1)
    ]


def test_default_v0_nontty_has_no_per_work_lines(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CliRunner default stderr is not a TTY, so v0 auto-selects MILESTONE:
    # the old per-work verbose lines must be gone (the key regression guard).
    out = tmp_path / "out"
    client = FakePixivClient(
        {"public": [_make_work(100), _make_work(101), _make_work(102)]}
    )
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        ["backup", "--output-dir", str(out), "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "page(s)" not in result.stderr
    assert "[1]" not in result.stderr
    # A milestone summary line is emitted instead of per-work lines.
    assert "..." in result.stderr


def test_verbose_shows_per_work_lines(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
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
    assert "[1]" in result.stderr


def test_dry_run_v0_still_shows_would_create(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The v0 dry-run exemption: a preview stays verbose (with planned lines)
    # rather than collapsing to a bar/milestone.
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
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
    # it (VERBOSE "would create" lines, never a \r-redrawn bar). On the default
    # non-TTY stream both paths independently pick VERBOSE, so this can only be
    # distinguished here.
    _force_tty(monkeypatch)
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
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


def test_vv_dry_run_shows_per_work_and_would_create(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
            "-vv",
            "--dry-run",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "page(s)" in result.stderr
    assert "would create" in result.stderr


def test_quiet_has_no_progress_content(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    client = FakePixivClient({"public": [_make_work(100, pages=2)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        [
            "backup",
            "--quiet",
            "--output-dir",
            str(out),
            "--config",
            _missing_config(tmp_path),
        ],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Backing up" not in result.stderr  # banner suppressed
    assert "page(s)" not in result.stderr
    assert "..." not in result.stderr
    assert "\r" not in result.stderr
    assert "Backed up 1/1 works" in result.stdout  # summary still printed


def test_tty_draws_bar_and_finishes_cleanly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A simulated TTY auto-selects BAR: a carriage-return-redrawn line appears,
    # and finish() terminates it so stderr does not end on a bare, unfinished
    # bar fragment. Four events guarantee the first-draw event threshold trips.
    _force_tty(monkeypatch)
    _install_run_emitting(
        monkeypatch,
        _work_events(4),
        summary=RunSummary(works_total=4, works_succeeded=4, files_downloaded=4),
    )

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "\r" in result.stderr  # an in-place bar line was drawn
    # finish() erases with a trailing newline, so stderr ends cleanly.
    assert result.stderr.endswith("\n")
    assert not result.stderr.rstrip("\n").endswith("\r")


def test_interrupt_message_on_clean_line_after_bar(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ordering guard: finish() erases the bar before the interrupt message, so
    # "interrupted..." lands on its own clean line, never glued to a bar
    # fragment. Emit >=3 events first so a bar was actually drawn.
    _force_tty(monkeypatch)
    _install_run_emitting(monkeypatch, _work_events(4), raises=KeyboardInterrupt())

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 130
    lines = result.stderr.splitlines()
    assert "interrupted; partial backup saved" in lines


# -- security warnings (token visibility, insecure config) --------------


def test_refresh_token_on_command_line_warns_on_stderr(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A token passed as a flag is visible in ps/proc, so the CLI warns. The
    # scripted run keeps it fully offline; the token must never be echoed.
    _install_run(monkeypatch, summary=RunSummary(works_total=1, works_succeeded=1))

    result = runner.invoke(
        cli,
        [
            "backup",
            "--refresh-token",
            FAKE_REFRESH,
            "--config",
            _missing_config(tmp_path),
        ],
        env=_no_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "visible to other processes" in result.stderr
    assert "PIXIV_REFRESH_TOKEN" in result.stderr
    # The secret itself is never printed (not on stdout, not on stderr).
    assert FAKE_REFRESH not in result.stdout
    assert FAKE_REFRESH not in result.stderr


def test_refresh_token_from_env_does_not_warn(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An env-var-sourced token is not visible in the process args, so no warning.
    _install_run(monkeypatch, summary=RunSummary(works_total=1, works_succeeded=1))

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "visible to other processes" not in result.stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_insecure_config_file_warns_on_stderr(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A world-readable config supplying the token triggers a chmod warning.
    config = tmp_path / "config.toml"
    config.write_text(f'[auth]\nrefresh_token = "{FAKE_REFRESH}"\n', encoding="utf-8")
    os.chmod(config, 0o644)
    _install_run(monkeypatch, summary=RunSummary(works_total=1, works_succeeded=1))

    result = runner.invoke(
        cli,
        ["backup", "--config", str(config)],
        env=_no_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "readable by other users" in result.stderr
    assert "chmod 600" in result.stderr
    assert str(config) in result.stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_secure_config_file_does_not_warn(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[auth]\nrefresh_token = "{FAKE_REFRESH}"\n', encoding="utf-8")
    os.chmod(config, 0o600)
    _install_run(monkeypatch, summary=RunSummary(works_total=1, works_succeeded=1))

    result = runner.invoke(
        cli,
        ["backup", "--config", str(config)],
        env=_no_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert "readable by other users" not in result.stderr


# -- --time-per-request -------------------------------------------------


def test_time_per_request_help_lists_option_and_default(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["backup", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "--time-per-request" in out
    assert "SECONDS" in out
    # click renders "[default: 0.0; x>=0]"; match the safe substring.
    assert "default: 0.0" in out


def test_time_per_request_negative_is_exit_2(runner: CliRunner, tmp_path: Path) -> None:
    # click's FloatRange rejects the value before the command body runs, so no
    # valid token is required to reach the error.
    result = runner.invoke(
        cli,
        [
            "backup",
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
    _install_run(
        monkeypatch,
        summary=RunSummary(works_total=1, works_succeeded=1),
        captured=captured,
    )

    result = runner.invoke(
        cli,
        [
            "backup",
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
    _install_run(
        monkeypatch,
        summary=RunSummary(works_total=1, works_succeeded=1),
        captured=captured,
    )

    result = runner.invoke(
        cli,
        ["backup", "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].time_per_request == 0.0


def test_time_per_request_absent_from_json_payload(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_run(
        monkeypatch,
        summary=RunSummary(works_total=1, works_succeeded=1),
    )

    result = runner.invoke(
        cli,
        [
            "backup",
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
    client = FakePixivClient({"public": [_make_work(100, pages=1)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        ["backup", "--output-dir", str(out), "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    # Clean, teaching exit -- not a traceback, not a silent regenerate.
    assert result.exit_code == 2
    assert "not valid JSON" in result.stderr
    assert "rename or delete it" in result.stderr
    assert str(out / "details.json") in result.stderr
    assert "Traceback" not in result.output
    # No summary line reached stdout.
    assert "Backed up" not in result.stdout
    # Fail FAST: the corrupt manifest is caught up front, before any API work --
    # nothing was authenticated, listed, or downloaded.
    assert client.authenticate_calls == 0
    assert client.list_calls == 0
    assert client.download_calls == []
    # The corrupt file is left in place; nothing moved aside.
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
    client = FakePixivClient({"public": [_make_work(100, pages=1)]})
    _install_client(monkeypatch, client)

    result = runner.invoke(
        cli,
        ["backup", "--output-dir", str(out), "--config", _missing_config(tmp_path)],
        env=_token_env(),
    )

    assert result.exit_code == 0, result.output
    # The regenerate still happens...
    assert (out / "details.json.bak").exists()
    manifest = json.loads(details.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    # ...but is now announced on stderr, naming the .bak file.
    assert "Warning:" in result.stderr
    assert str(out / "details.json.bak") in result.stderr
    # The run summary still lands on stdout.
    assert "Backed up 1/1 works" in result.stdout
