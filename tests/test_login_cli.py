"""CLI tests for the ``login`` command, driven through click's ``CliRunner``.

These exercise the thin click layer end to end while staying fully offline. The
real interactive flow (gppt + Playwright) is never imported: a :class:`FakeLogin`
implementing the :class:`LoginFlow` Protocol is injected by monkeypatching
``cli._build_login`` (mirroring how the backup tests patch ``Pixivpy3Client``).
``FakeLogin`` records the kwargs it was called with and either returns a scripted
:class:`LoginResult` or raises a scripted exception.

CliRunner in click 8.4.1 has no ``mix_stderr`` kwarg: data is asserted on
``result.stdout`` and logs/errors on ``result.stderr``; ``result.output`` (the
combined stream) is not used to prove a clean stdout. The refresh token is a
distinctive sentinel so leak assertions are meaningful: it must never appear on
stdout or stderr unless the user explicitly opts in.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result, _NamedTextIOWrapper

from pixiv_bookmark_backup import cli as cli_module
from pixiv_bookmark_backup.auth_login import (
    LoginDriverError,
    LoginFailed,
    LoginFlow,
    LoginResult,
)
from pixiv_bookmark_backup.cli import cli
from pixiv_bookmark_backup.config import (
    ConfigWriteError,
    _token_from_config,
    resolve_auth,
    write_refresh_token,
)
from pixiv_bookmark_backup.pixiv_client import ApiError, AuthRejected

#: A distinctive, non-secret sentinel so leak assertions are meaningful: it must
#: never reach stdout/stderr unless the user opts in via --print-token /
#: --stdout-only.
TOKEN = "refresh-token-sentinel-do-not-leak"

#: A distinctive password used only to assert it is never echoed.
SECRET_PASSWORD = "password-sentinel-do-not-leak"


# -- fakes & helpers ----------------------------------------------------


class FakeLogin:
    """Offline stand-in for :class:`LoginFlow`.

    Records the kwargs of the single ``login`` call it receives and then either
    returns a scripted :class:`LoginResult` or raises a scripted exception, so
    every exit-code branch can be exercised without a real browser.
    """

    def __init__(
        self,
        *,
        token: str = TOKEN,
        raises: BaseException | None = None,
    ) -> None:
        self._token = token
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    def login(
        self,
        *,
        headless: bool,
        username: str | None,
        password: str | None,
        timeout: int,
    ) -> LoginResult:
        self.calls.append(
            {
                "headless": headless,
                "username": username,
                "password": password,
                "timeout": timeout,
            }
        )
        if self._raises is not None:
            raise self._raises
        return LoginResult(refresh_token=self._token)


# Static guarantee the fake satisfies the public Protocol.
_: type[LoginFlow] = FakeLogin


def _install_login(monkeypatch: pytest.MonkeyPatch, flow: LoginFlow) -> None:
    """Patch ``cli._build_login`` so the command uses ``flow``."""

    monkeypatch.setattr(cli_module, "_build_login", lambda: flow)


def _config_path(tmp_path: Path) -> str:
    """A config file path under ``tmp_path`` (does not exist yet)."""

    return str(tmp_path / "config.toml")


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the command see an interactive stdin during ``runner.invoke``.

    ``CliRunner`` swaps ``sys.stdin`` for a ``_NamedTextIOWrapper`` over a
    ``BytesIO``, whose inherited ``isatty`` returns ``False``. The overwrite gate
    branches on ``sys.stdin.isatty()`` to decide whether to prompt, so we patch
    that wrapper class to report a tty -- exercising the interactive-confirm path
    that the no-tty CliRunner default cannot reach on its own.
    """

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)


def _assert_token_absent(result: Result) -> None:
    """Assert the sentinel token leaked to neither stdout nor stderr."""

    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr


# -- exit 0: happy write ------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_happy_write_saves_token_owner_only(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert "Saved refresh token to" in result.stdout
    assert str(config) in result.stdout
    # The file was written, is owner-only, and round-trips back to the token.
    assert config.exists()
    assert os.stat(config).st_mode & 0o777 == 0o600
    assert _token_from_config(config) == TOKEN
    # The secret itself never reaches the user's terminal.
    _assert_token_absent(result)


def test_happy_write_passes_flow_kwargs(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = FakeLogin()
    _install_login(monkeypatch, flow)

    result = runner.invoke(
        cli, ["login", "--config", _config_path(tmp_path), "--timeout", "30"]
    )

    assert result.exit_code == 0, result.output
    assert flow.calls == [
        {"headless": False, "username": None, "password": None, "timeout": 30}
    ]


# -- --json -------------------------------------------------------------


def test_json_result_is_single_object_on_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema"] == 1
    assert next(iter(payload)) == "schema"
    assert set(payload) == {
        "schema",
        "config_path",
        "wrote_config",
        "overwrote_existing",
        "dry_run",
        "refresh_token",
        "exit_code",
    }
    assert payload["config_path"] == str(config)
    assert payload["wrote_config"] is True
    assert payload["overwrote_existing"] is False
    assert payload["dry_run"] is False
    assert payload["refresh_token"] is None  # not opted in
    assert payload["exit_code"] == 0
    _assert_token_absent(result)


def test_json_print_token_includes_token_in_payload(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(
        cli,
        ["login", "--config", _config_path(tmp_path), "--json", "--print-token"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["refresh_token"] == TOKEN
    # Opted-in token may be on stdout (in the payload) but never on stderr.
    assert TOKEN not in result.stderr


def test_print_token_human_echoes_token_to_stdout_only(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The opt-in --print-token flag (without --json) appends the token to the
    # human-readable stdout, but it must never appear on stderr.
    config = tmp_path / "config.toml"
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config), "--print-token"])

    assert result.exit_code == 0, result.output
    assert "Saved refresh token to" in result.stdout
    assert f"refresh_token: {TOKEN}" in result.stdout
    assert TOKEN not in result.stderr


# -- --stdout-only ------------------------------------------------------


def test_stdout_only_prints_bare_token_and_writes_nothing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfgdir"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--stdout-only"])

    assert result.exit_code == 0, result.output
    # stdout is exactly the token and a trailing newline -- pipeable.
    assert result.stdout == f"{TOKEN}\n"
    # No config file or directory was created.
    assert not config_dir.exists()


def test_stdout_only_wins_over_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfgdir"))
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--stdout-only", "--json"])

    assert result.exit_code == 0, result.output
    # The pipeable-token contract wins: only the bare token, no JSON object.
    assert result.stdout == f"{TOKEN}\n"


# -- --dry-run ----------------------------------------------------------


def test_dry_run_writes_nothing_and_says_would(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--dry-run", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert "Would save refresh token to" in result.stdout
    assert not config.exists()
    _assert_token_absent(result)


def test_dry_run_json_reports_not_written(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(
        cli, ["login", "--dry-run", "--json", "--config", str(config)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["wrote_config"] is False
    assert payload["dry_run"] is True
    assert not config.exists()


# -- overwrite gate -----------------------------------------------------


def test_existing_token_without_force_no_tty_is_usage_error(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CliRunner provides no tty, so with neither --force nor --yes the command
    # refuses to clobber an existing token.
    config = tmp_path / "config.toml"
    write_refresh_token(config, "old-token-value")
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config)])

    assert result.exit_code == 2
    assert "pass --force" in result.stderr
    assert "--yes" in result.stderr
    # The pre-existing token is untouched.
    assert _token_from_config(config) == "old-token-value"


def test_force_overwrites_existing_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    write_refresh_token(config, "old-token-value")
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config), "--force"])

    assert result.exit_code == 0, result.output
    assert _token_from_config(config) == TOKEN


def test_force_json_reports_overwrote_existing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    write_refresh_token(config, "old-token-value")
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config), "--force", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["overwrote_existing"] is True
    assert payload["wrote_config"] is True


def test_yes_overwrites_existing_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    write_refresh_token(config, "old-token-value")
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config), "--yes"])

    assert result.exit_code == 0, result.output
    assert _token_from_config(config) == TOKEN


def test_decline_confirm_leaves_token_unchanged(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tty makes the command prompt; answering "n" aborts as a 0-exit no-op.
    config = tmp_path / "config.toml"
    write_refresh_token(config, "old-token-value")
    _install_login(monkeypatch, FakeLogin())
    _force_tty(monkeypatch)

    result = runner.invoke(cli, ["login", "--config", str(config)], input="n\n")

    assert result.exit_code == 0, result.output
    assert "existing token unchanged" in result.stderr
    assert _token_from_config(config) == "old-token-value"
    _assert_token_absent(result)


def test_confirm_yes_overwrites_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    write_refresh_token(config, "old-token-value")
    _install_login(monkeypatch, FakeLogin())
    _force_tty(monkeypatch)

    result = runner.invoke(cli, ["login", "--config", str(config)], input="y\n")

    assert result.exit_code == 0, result.output
    assert _token_from_config(config) == TOKEN


def test_malformed_existing_config_is_exit_9(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("this is = = not valid toml\n", encoding="utf-8")
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config)])

    assert result.exit_code == 9
    assert "--stdout-only" in result.stderr


# -- exit 7 / 8 / 3 / 4 / 130 / 9 (flow failures) -----------------------


def test_login_driver_error_is_exit_7(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(
        monkeypatch, FakeLogin(raises=LoginDriverError("gppt is not installed"))
    )

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 7
    assert "gppt is not installed" in result.stderr
    assert "Traceback" not in result.stderr


def test_login_failed_is_exit_8(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin(raises=LoginFailed("Login timed out")))

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 8
    assert "Login timed out" in result.stderr
    assert "Traceback" not in result.stderr


def test_auth_rejected_is_exit_3(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(
        monkeypatch, FakeLogin(raises=AuthRejected("Pixiv rejected the token"))
    )

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 3
    assert "Pixiv rejected the token" in result.stderr


def test_api_error_is_exit_4(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin(raises=ApiError("connection reset")))

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 4
    assert "Pixiv API error" in result.stderr


def test_keyboard_interrupt_is_exit_130(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin(raises=KeyboardInterrupt()))

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 130
    assert "interrupted" in result.stderr
    assert "no token saved" in result.stderr


def test_config_write_failure_is_exit_9(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flow succeeds, but persisting the token fails: map to exit 9 and
    # suggest --stdout-only, never echoing the token itself.
    _install_login(monkeypatch, FakeLogin())

    def boom(config_path: Path, token: str) -> None:
        raise ConfigWriteError("disk is full")

    monkeypatch.setattr(cli_module, "write_refresh_token", boom)

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 9
    assert "--stdout-only" in result.stderr
    _assert_token_absent(result)


# -- password visibility warning ----------------------------------------


def test_password_on_argv_warns_without_echoing_it(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(
        cli,
        [
            "login",
            "--config",
            _config_path(tmp_path),
            "--headless",
            "--username",
            "me@example.com",
            "--password",
            SECRET_PASSWORD,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "visible to other processes" in result.stderr
    assert "PIXIV_PASSWORD" in result.stderr
    # The password value is never echoed anywhere.
    assert SECRET_PASSWORD not in result.stdout
    assert SECRET_PASSWORD not in result.stderr


def test_password_from_env_does_not_warn(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin())
    monkeypatch.setenv("PIXIV_PASSWORD", SECRET_PASSWORD)

    result = runner.invoke(
        cli,
        [
            "login",
            "--config",
            _config_path(tmp_path),
            "--headless",
            "--username",
            "me@example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "visible to other processes" not in result.stderr
    assert SECRET_PASSWORD not in result.stdout
    assert SECRET_PASSWORD not in result.stderr


# -- mutual exclusions / validation -------------------------------------


def test_quiet_and_verbose_are_mutually_exclusive(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli, ["login", "--quiet", "--verbose", "--config", _config_path(tmp_path)]
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_stdout_only_and_force_are_mutually_exclusive(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["login", "--stdout-only", "--force"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_stdout_only_with_config_is_usage_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli, ["login", "--stdout-only", "--config", _config_path(tmp_path)]
    )

    assert result.exit_code == 2
    assert "--config has no effect" in result.stderr


def test_username_without_password_is_usage_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli, ["login", "--config", _config_path(tmp_path), "--username", "me"]
    )

    assert result.exit_code == 2
    assert "provide both" in result.stderr


# -- token containment / quiet ------------------------------------------


def test_token_never_on_either_stream_by_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 0, result.output
    _assert_token_absent(result)


def test_quiet_suppresses_progress_but_not_password_warning(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(
        cli,
        [
            "login",
            "--quiet",
            "--config",
            _config_path(tmp_path),
            "--headless",
            "--username",
            "me@example.com",
            "--password",
            SECRET_PASSWORD,
        ],
    )

    assert result.exit_code == 0, result.output
    # Progress lines are suppressed under --quiet ...
    assert "Logging in to Pixiv" not in result.stderr
    assert "Opening Pixiv login" not in result.stderr
    # ... but the security warning is always shown.
    assert "visible to other processes" in result.stderr
    assert SECRET_PASSWORD not in result.stderr


def test_progress_line_shown_without_quiet(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", _config_path(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Opening Pixiv login in your browser" in result.stderr


# -- help ---------------------------------------------------------------


def test_login_help_lists_flags_codes_and_examples(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["login", "--help"])

    assert result.exit_code == 0
    for flag in (
        "--config",
        "--headless",
        "--no-headless",
        "--username",
        "--password",
        "--timeout",
        "--force",
        "--print-token",
        "--stdout-only",
        "--json",
        "--yes",
        "--dry-run",
        "--quiet",
        "--verbose",
    ):
        assert flag in result.output
    assert "Exit codes:" in result.output
    assert "Examples:" in result.output


def test_group_help_lists_both_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "backup" in result.output
    assert "login" in result.output


# -- login -> backup integration ----------------------------------------


def test_login_then_backup_resolves_written_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end contract: a token saved by ``login`` is what ``backup`` resolves.

    Drives the real ``login`` command (with the offline ``FakeLogin`` injected via
    ``_build_login``) to persist a token at a tmp config path, then proves that
    ``resolve_auth`` -- the function the ``backup`` command uses to obtain its
    credentials -- reads back exactly that freshly-written token, with no flag and
    an empty environment. Fully offline and deterministic.
    """

    config = tmp_path / "config.toml"
    _install_login(monkeypatch, FakeLogin())

    result = runner.invoke(cli, ["login", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert config.exists()

    # The backup command resolves auth via resolve_auth(flag, env, config_path);
    # with no flag and a cleared env, the only source is the config login wrote.
    resolved = resolve_auth(None, env={}, config_path=config)
    assert resolved.refresh_token == TOKEN
