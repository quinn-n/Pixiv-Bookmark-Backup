"""Unit tests for auth/config resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pixiv_bookmark_backup import config as config_module
from pixiv_bookmark_backup.config import (
    ENV_VAR,
    ConfigError,
    ConfigWriteError,
    NoTokenError,
    ResolvedAuth,
    _token_from_config,
    default_config_path,
    is_insecure_config,
    resolve_auth,
    write_refresh_token,
)


def _write_config(tmp_path: Path, token: str) -> Path:
    """Write a config file with ``[auth].refresh_token`` and return its path."""

    config = tmp_path / "config.toml"
    config.write_text(f'[auth]\nrefresh_token = "{token}"\n', encoding="utf-8")
    return config


def test_flag_beats_env_beats_toml(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "from-toml")
    env = {ENV_VAR: "from-env"}
    resolved = resolve_auth("from-flag", env=env, config_path=config)
    assert resolved.refresh_token == "from-flag"


def test_env_beats_toml(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "from-toml")
    env = {ENV_VAR: "from-env"}
    resolved = resolve_auth(None, env=env, config_path=config)
    assert resolved.refresh_token == "from-env"


def test_env_source_alone_resolves(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    resolved = resolve_auth(None, env={ENV_VAR: "from-env"}, config_path=missing)
    assert resolved.refresh_token == "from-env"


def test_toml_source_alone_resolves(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "from-toml")
    resolved = resolve_auth(None, env={}, config_path=config)
    assert resolved.refresh_token == "from-toml"


def test_default_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
    expected = Path("/custom/xdg/pixiv-bookmark-backup/config.toml")
    assert default_config_path() == expected


def test_default_config_path_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    expected = Path("/home/tester/.config/pixiv-bookmark-backup/config.toml")
    assert default_config_path() == expected


def test_default_config_path_treats_empty_xdg_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    expected = Path("/home/tester/.config/pixiv-bookmark-backup/config.toml")
    assert default_config_path() == expected


def test_no_token_anywhere_raises_no_token_error(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    with pytest.raises(NoTokenError) as excinfo:
        resolve_auth(None, env={}, config_path=missing)
    assert isinstance(excinfo.value, ConfigError)
    assert ENV_VAR in str(excinfo.value)


def test_no_token_message_mentions_flag_and_config_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    with pytest.raises(NoTokenError) as excinfo:
        resolve_auth(None, env={}, config_path=missing)
    message = str(excinfo.value)
    assert "--refresh-token" in message
    assert str(missing) in message


def test_no_token_message_points_to_login_command(tmp_path: Path) -> None:
    """The no-token message keeps its original lead and points to ``login``.

    The leading sentence ``"No Pixiv refresh token found."`` is unchanged (an
    existing CLI test substring-matches it), and the message now also teaches the
    easy onboarding path: running the ``login`` subcommand.
    """
    missing = tmp_path / "absent.toml"
    with pytest.raises(NoTokenError) as excinfo:
        resolve_auth(None, env={}, config_path=missing)
    message = str(excinfo.value)
    assert message.startswith("No Pixiv refresh token found.")
    assert "pixiv-bookmark-backup login" in message


def test_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("this is = not valid = toml", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        resolve_auth(None, env={}, config_path=config)
    assert not isinstance(excinfo.value, NoTokenError)
    assert str(config) in str(excinfo.value)


def test_config_without_auth_section_falls_through(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[other]\nkey = "value"\n', encoding="utf-8")
    with pytest.raises(NoTokenError):
        resolve_auth(None, env={}, config_path=config)


def test_config_with_empty_token_falls_through(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "   ")
    with pytest.raises(NoTokenError):
        resolve_auth(None, env={}, config_path=config)


def test_empty_flag_falls_through_to_env(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    resolved = resolve_auth("   ", env={ENV_VAR: "from-env"}, config_path=missing)
    assert resolved.refresh_token == "from-env"


def test_empty_env_falls_through_to_toml(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "from-toml")
    resolved = resolve_auth(None, env={ENV_VAR: "  "}, config_path=config)
    assert resolved.refresh_token == "from-toml"


def test_whitespace_token_is_stripped(tmp_path: Path) -> None:
    resolved = resolve_auth(
        "  padded-token  ", env={}, config_path=tmp_path / "absent.toml"
    )
    assert resolved.refresh_token == "padded-token"


def test_repr_does_not_leak_token() -> None:
    auth = ResolvedAuth("secret-token-value")
    assert "secret-token-value" not in repr(auth)


def test_env_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "from-os-environ")
    # Point config at a guaranteed-missing path so only env can satisfy it.
    resolved = resolve_auth(None, config_path=Path("/nonexistent/config.toml"))
    assert resolved.refresh_token == "from-os-environ"


# --------------------------------------------------------------------------- #
# is_insecure_config
# --------------------------------------------------------------------------- #


def test_is_insecure_config_missing_file_is_secure(tmp_path: Path) -> None:
    # Nothing to protect: a missing config is not flagged.
    assert is_insecure_config(tmp_path / "absent.toml") is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_is_insecure_config_owner_only_is_secure(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nrefresh_token = "x"\n', encoding="utf-8")
    os.chmod(config, 0o600)
    assert is_insecure_config(config) is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_is_insecure_config_group_other_readable_is_insecure(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nrefresh_token = "x"\n', encoding="utf-8")
    os.chmod(config, 0o644)
    assert is_insecure_config(config) is True


# --------------------------------------------------------------------------- #
# Non-string refresh_token in TOML
# --------------------------------------------------------------------------- #


def test_non_string_refresh_token_in_toml_falls_through_to_no_token_error(
    tmp_path: Path,
) -> None:
    """A TOML config with refresh_token = 123 (integer) must not be accepted.

    _token_from_config checks `isinstance(token, str)` and returns None when
    the key holds a non-string value, causing resolve_auth to fall through all
    sources and raise NoTokenError.
    """
    config = tmp_path / "config.toml"
    config.write_text("[auth]\nrefresh_token = 123\n", encoding="utf-8")

    with pytest.raises(NoTokenError):
        resolve_auth(None, env={}, config_path=config)


# --------------------------------------------------------------------------- #
# resolve_auth default config_path branch
# --------------------------------------------------------------------------- #


def test_resolve_auth_uses_default_config_path_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling resolve_auth(config_path=None) triggers the default-path branch.

    When config_path is None, resolve_auth assigns config_path =
    default_config_path() before consulting the file. Because the env var is
    set here, the token resolves from the environment before the file is read,
    so the test does not depend on any real config file existing at the default
    path on this machine.
    """
    monkeypatch.setenv(ENV_VAR, "env-token")
    # config_path=None exercises the `config_path = default_config_path()` assignment.
    resolved = resolve_auth(None, config_path=None)
    assert resolved.refresh_token == "env-token"


# --------------------------------------------------------------------------- #
# write_refresh_token
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_write_creates_file_owner_only_0o600(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_refresh_token(config, "fresh-token")
    assert os.stat(config).st_mode & 0o777 == 0o600
    assert _token_from_config(config) == "fresh-token"


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    config = tmp_path / "a" / "b" / "config.toml"
    write_refresh_token(config, "nested-token")
    assert config.exists()
    assert config.parent.is_dir()
    assert _token_from_config(config) == "nested-token"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_write_creates_parent_dir_owner_only_0o700(tmp_path: Path) -> None:
    """A newly created parent dir holding the 0o600 secret is owner-only 0o700.

    ``mkdir(mode=0o700)`` is masked by the process umask, so a loose umask can
    only narrow (never widen) the bits. Pinning the process umask to 0 for the
    duration of the write makes the exact-0o700 assertion deterministic; the
    secret file itself is 0o600 regardless of umask (mkstemp + chmod).
    """

    config = tmp_path / "newdir" / "config.toml"

    old_umask = os.umask(0)
    try:
        write_refresh_token(config, "nested-token")
    finally:
        os.umask(old_umask)

    assert config.parent.is_dir()
    assert os.stat(config.parent).st_mode & 0o777 == 0o700
    # The secret file remains owner-only too.
    assert os.stat(config).st_mode & 0o777 == 0o600


def test_write_preserves_other_keys_tables_and_comments(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "# leading comment\n"
        "[other]\n"
        'key = "value"\n'
        "\n"
        "[auth]\n"
        'note = "x"\n'
        'refresh_token = "old"\n',
        encoding="utf-8",
    )
    write_refresh_token(config, "new-token")

    raw = config.read_text(encoding="utf-8")
    assert "# leading comment" in raw
    assert "[other]" in raw
    assert 'key = "value"' in raw
    assert 'note = "x"' in raw
    assert _token_from_config(config) == "new-token"


def test_write_replaces_existing_token(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nrefresh_token = "old"\n', encoding="utf-8")
    write_refresh_token(config, "new")
    assert _token_from_config(config) == "new"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits are not meaningful on Windows.",
)
def test_write_reasserts_0o600_on_loose_existing_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nrefresh_token = "old"\n', encoding="utf-8")
    os.chmod(config, 0o644)
    write_refresh_token(config, "new")
    assert os.stat(config).st_mode & 0o777 == 0o600


def test_write_malformed_existing_toml_raises_config_write_error(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("x = =", encoding="utf-8")
    with pytest.raises(ConfigWriteError) as excinfo:
        write_refresh_token(config, "sentinel-write-token")
    assert isinstance(excinfo.value, ConfigError)
    message = str(excinfo.value)
    assert str(config) in message
    assert "sentinel-write-token" not in message


def test_write_atomic_no_partial_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nrefresh_token = "old"\n', encoding="utf-8")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(ConfigWriteError):
        write_refresh_token(config, "new")

    # Destination unchanged.
    assert _token_from_config(config) == "old"
    # No leftover temp file from mkstemp remains in the directory.
    assert {p.name for p in tmp_path.iterdir()} == {"config.toml"}


def test_write_cleanup_failure_still_raises_config_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both os.replace and the temp-file cleanup os.unlink fail, the original
    replace error still surfaces as ConfigWriteError.

    This exercises the inner ``except OSError: pass`` best-effort cleanup branch:
    the failed cleanup is swallowed so it cannot mask the original replace
    failure, which is then re-raised and wrapped.
    """
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nrefresh_token = "old"\n', encoding="utf-8")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    def fail_unlink(*_args: object, **_kwargs: object) -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)
    monkeypatch.setattr(config_module.os, "unlink", fail_unlink)

    with pytest.raises(ConfigWriteError) as excinfo:
        write_refresh_token(config, "sentinel-must-not-leak")

    # The original replace failure surfaces, not the cleanup failure.
    assert "replace failed" in str(excinfo.value)
    assert "sentinel-must-not-leak" not in str(excinfo.value)
    # The destination file is unchanged by the failed write.
    assert _token_from_config(config) == "old"


def test_write_token_not_in_exception_message(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("x = =", encoding="utf-8")
    # A unique sentinel string standing in for the refresh token; the test
    # proves this value never appears in the raised exception message.
    sentinel = "sentinel-must-not-leak"
    with pytest.raises(ConfigWriteError) as excinfo:
        write_refresh_token(config, sentinel)
    assert sentinel not in str(excinfo.value)
