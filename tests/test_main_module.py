"""Regression tests for the ``python -m pixiv_bookmark_backup`` entry point.

These deliberately spawn a *real* subprocess (``sys.executable -m
pixiv_bookmark_backup ...``) instead of driving the click ``cli`` group through
``CliRunner``. The bug being guarded against lived entirely in
``pixiv_bookmark_backup/__main__.py`` (it called the standalone ``backup``
command instead of the ``cli`` group), so a ``CliRunner`` test -- which imports
and invokes the group object directly -- would pass identically before and after
the fix and would not catch the regression. Only an actual module invocation
exercises ``__main__``.

Every case uses ``--help`` (or a bare no-args invocation) so the tests stay
deterministic and offline: no Pixiv auth, token, or network is required.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

#: How long (seconds) to let the child run. Every case only exercises
#: ``--help``/bare invocation, which returns near-instantly, so this is a wide
#: margin over normal startup while still failing fast: if the dispatch logic
#: ever regresses into a real ``backup`` run, that starts a live ~90s Pixiv API
#: call, and this timeout aborts it long before then instead of hanging.
_TIMEOUT_SECONDS = 30


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the package as ``python -m pixiv_bookmark_backup`` with *args*.

    Uses ``sys.executable`` so the interpreter (and therefore the installed
    package) is the one from the pixi env running the test suite.

    The child is isolated from any real Pixiv credentials on the host so these
    tests stay offline even if the entry-point dispatch regresses. Two sources
    can feed a refresh token (see ``pixiv_bookmark_backup.config``):

    * the ``PIXIV_REFRESH_TOKEN`` environment variable, and
    * ``[auth].refresh_token`` in the config file, which ``default_config_path``
      resolves at ``$XDG_CONFIG_HOME/pixiv-bookmark-backup/config.toml`` (falling
      back to ``~/.config`` when ``XDG_CONFIG_HOME`` is unset/empty).

    So the child env drops every ``PIXIV*`` variable (covering the token and any
    other ambient Pixiv secrets) and repoints ``XDG_CONFIG_HOME`` -- and, for
    good measure, ``HOME`` -- at an empty temp directory that holds no config,
    guaranteeing no stored token is discoverable. ``cwd`` is that same temp dir
    so a regressed backup could not litter ``./backup/`` into the repo, and a
    ``timeout`` makes any such regression fail fast rather than hang.
    """

    env = {k: v for k, v in os.environ.items() if not k.startswith("PIXIV")}
    with tempfile.TemporaryDirectory() as isolated:
        env["XDG_CONFIG_HOME"] = isolated
        env["HOME"] = isolated
        return subprocess.run(
            [sys.executable, "-m", "pixiv_bookmark_backup", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,
            timeout=_TIMEOUT_SECONDS,
        )


def test_backup_following_help_dispatches_to_subcommand() -> None:
    """``backup-following --help`` must reach that subcommand, not ``backup``.

    This is the assertion that flips on the fix. Before it, ``--help`` short-
    circuited into ``backup``'s help, whose usage line is
    ``... [OPTIONS] [USER_ID]`` and never names ``backup-following``. After it,
    the group dispatches to ``backup-following``'s own help, whose usage line
    names the subcommand and carries no ``[USER_ID]`` positional. (Note the
    body of ``backup-following``'s help legitimately uses ``USER_ID`` as the
    metavar for ``--only``/``--exclude``, so the check must be scoped to the
    usage line, not the whole page.)
    """
    result = _run_module("backup-following", "--help")

    assert result.returncode == 0, result.stderr
    usage_line = result.stdout.splitlines()[0]
    assert "backup-following" in usage_line
    assert "[USER_ID]" not in usage_line


def test_group_help_lists_all_subcommands() -> None:
    result = _run_module("--help")

    assert result.returncode == 0, result.stderr
    for command in ("backup", "backup-following", "login", "search"):
        assert command in result.stdout


def test_login_help_dispatches_to_login() -> None:
    result = _run_module("login", "--help")

    assert result.returncode == 0, result.stderr
    usage_line = result.stdout.splitlines()[0]
    assert "login" in usage_line
    # ``backup``'s help (the pre-fix fallthrough) advertised a ``[USER_ID]``
    # positional in its usage line; ``login`` has none.
    assert "[USER_ID]" not in usage_line


def test_backup_help_still_shows_user_id_argument() -> None:
    """The standalone ``backup`` subcommand still works through the group."""
    result = _run_module("backup", "--help")

    assert result.returncode == 0, result.stderr
    usage_line = result.stdout.splitlines()[0]
    assert usage_line.endswith("backup [OPTIONS] [USER_ID]")
    assert "USER_ID" in result.stdout


def test_no_args_shows_group_help_not_backup() -> None:
    """A bare invocation shows the group's help, never a backup run.

    Click groups default to ``no_args_is_help=True``, so this prints the usage
    (with the subcommand list) to stderr and exits ``2``. The key regression
    guard is that it must *not* fall through to running ``backup`` (which, given
    a config token, previously started a real backup).
    """
    result = _run_module()

    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "Usage:" in combined
    assert "Commands:" in combined
    for command in ("backup", "backup-following", "login", "search"):
        assert command in combined
