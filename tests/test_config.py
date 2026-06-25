"""Unit tests for auth/config resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pixiv_bookmark_backup.config import (
    ENV_VAR,
    ConfigError,
    NoTokenError,
    ResolvedAuth,
    default_config_path,
    is_insecure_config,
    resolve_auth,
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
