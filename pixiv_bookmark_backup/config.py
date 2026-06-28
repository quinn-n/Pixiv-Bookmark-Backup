"""Authentication/config resolution for pixiv-bookmark-backup.

The Pixiv API is driven by a long-lived **refresh token**. This module resolves
that token from several sources, in precedence order (first non-empty wins):

1. The ``--refresh-token`` CLI flag (``flag_token``).
2. The ``PIXIV_REFRESH_TOKEN`` environment variable.
3. The ``[auth].refresh_token`` value in the TOML config file, if it exists.

Empty or whitespace-only values are treated as absent and fall through to the
next source. If no source yields a token, :class:`NoTokenError` is raised with a
message teaching the user how to provide one. The resolved token is wrapped in a
:class:`ResolvedAuth` value object whose ``repr`` deliberately omits the secret,
and the token is never logged or printed by this module.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomlkit
import tomlkit.exceptions
from attrs import field, frozen
from tomlkit.items import Table

#: Environment variable consulted for the refresh token.
ENV_VAR = "PIXIV_REFRESH_TOKEN"


class ConfigError(Exception):
    """Base error for configuration problems (e.g. malformed config file).

    The CLI maps this (and its subclasses) to exit code 2.
    """


class NoTokenError(ConfigError):
    """Raised when no refresh token can be resolved from any source."""


class ConfigWriteError(ConfigError):
    """Raised when the config file cannot be written/updated.

    Covers malformed existing TOML that cannot be safely edited and filesystem
    failures during the atomic write. The CLI maps this to exit code 9. The
    refresh token is never included in the error message.
    """


@frozen
class ResolvedAuth:
    """Resolved Pixiv credentials.

    ``refresh_token`` is excluded from ``repr`` (``field(repr=False)``) so the
    secret never leaks into logs, tracebacks, or debugging output.
    """

    refresh_token: str = field(repr=False)


def is_insecure_config(path: Path) -> bool:
    """Return whether the config file at ``path`` is readable by other users.

    A pure predicate (no printing): returns ``True`` only when the file exists
    *and* any group/other permission bit is set (``st_mode & 0o077``), i.e. the
    token-bearing file is not owner-only ``0o600``. A missing file returns
    ``False`` (there is nothing to protect). On Windows the POSIX mode bits are
    not meaningful but the bitmask simply yields ``False`` for typical modes, so
    the caller's warning stays POSIX-relevant without a platform guard here.
    """

    if not path.exists():
        return False
    return os.stat(path).st_mode & 0o077 != 0


def default_config_path() -> Path:
    """Return the default config file path.

    Resolves to ``${XDG_CONFIG_HOME:-~/.config}/pixiv-bookmark-backup/config.toml``,
    honoring the ``XDG_CONFIG_HOME`` environment variable when it is set to a
    non-empty value, and falling back to ``~/.config`` otherwise.
    """

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "pixiv-bookmark-backup" / "config.toml"


def _clean(token: str | None) -> str | None:
    """Return the token stripped of surrounding whitespace, or ``None`` if empty."""

    if token is None:
        return None
    stripped = token.strip()
    return stripped or None


def _token_from_config(config_path: Path) -> str | None:
    """Read ``[auth].refresh_token`` from ``config_path`` if the file exists.

    Returns the cleaned token, or ``None`` when the file is absent or the key is
    missing/empty. Raises :class:`ConfigError` if the file exists but is not
    valid TOML.
    """

    if not config_path.exists():
        return None

    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Config file {config_path} is not valid TOML: {exc}"
        ) from exc

    auth = data.get("auth")
    if not isinstance(auth, Mapping):
        return None

    token = auth.get("refresh_token")
    if not isinstance(token, str):
        return None
    return _clean(token)


def resolve_auth(
    flag_token: str | None,
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> ResolvedAuth:
    """Resolve Pixiv credentials from flag, environment, and config file.

    Precedence (first non-empty value wins): ``flag_token``, then
    ``env[PIXIV_REFRESH_TOKEN]``, then ``[auth].refresh_token`` from the TOML
    file at ``config_path`` (only consulted if that file exists).

    Args:
        flag_token: Token supplied via the CLI flag, or ``None``.
        env: Environment mapping to read; defaults to :data:`os.environ`.
        config_path: Config file to read; defaults to :func:`default_config_path`.

    Returns:
        A :class:`ResolvedAuth` holding the resolved token.

    Raises:
        ConfigError: If the config file exists but is not valid TOML.
        NoTokenError: If no source yields a non-empty token.
    """

    if env is None:
        env = os.environ
    if config_path is None:
        config_path = default_config_path()

    token = _clean(flag_token)
    if token is None:
        token = _clean(env.get(ENV_VAR))
    if token is None:
        token = _token_from_config(config_path)

    if token is None:
        raise NoTokenError(
            "No Pixiv refresh token found. Provide one by setting the "
            f"{ENV_VAR} environment variable, passing --refresh-token, or adding "
            f"a [auth] refresh_token entry to the config file at {config_path}. "
            "Or run 'pixiv-bookmark-backup login' to log in and save one "
            "automatically."
        )

    return ResolvedAuth(refresh_token=token)


def write_refresh_token(config_path: Path, token: str) -> None:
    """Write ``token`` into ``[auth].refresh_token`` at ``config_path``.

    The update is surgical: an existing file is parsed with :mod:`tomlkit` so
    that comments, formatting, and unrelated keys/tables are preserved; only the
    ``[auth].refresh_token`` value is set (the ``[auth]`` table is created if
    absent). A missing file (and any missing parent directories) is created.

    The write is atomic and owner-only: the new contents are written to a
    temporary file in the same directory (created ``0o600`` by
    :func:`tempfile.mkstemp`), flushed, then :func:`os.replace`-d over the
    destination, after which the mode is re-asserted to ``0o600`` to cover the
    case of overwriting a previously group/other-readable file.

    Args:
        config_path: Path to the TOML config file to create or update.
        token: The refresh token to store. Never appears in error messages.

    Raises:
        ConfigWriteError: If the existing file is not valid TOML, or a
            filesystem error occurs while writing or replacing the file.
    """

    # Create the parent owner-only (0o700): it holds a 0o600 secret file. ``mode``
    # applies only to directories actually created here, so an existing dir such as
    # ``~/.config`` keeps its own permissions.
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        try:
            doc = tomlkit.parse(text)
        except tomlkit.exceptions.ParseError as exc:
            raise ConfigWriteError(
                f"Config file {config_path} is not valid TOML and cannot be "
                f"updated: {exc}"
            ) from exc
    else:
        doc = tomlkit.document()

    existing = doc.get("auth")
    if isinstance(existing, Table):
        auth = existing
    else:
        auth = tomlkit.table()
        doc["auth"] = auth
    auth["refresh_token"] = token

    serialized = tomlkit.dumps(doc).encode("utf-8")

    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(config_path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, config_path)
        except OSError:
            # Clean up the stray temp file on any failure before the replace.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        os.chmod(config_path, 0o600)
    except OSError as exc:
        raise ConfigWriteError(
            f"Could not write config file {config_path}: {exc}"
        ) from exc
