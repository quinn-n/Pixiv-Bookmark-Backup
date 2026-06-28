"""Offline unit tests for the interactive Pixiv login wrapper.

Every test runs with NEITHER ``gppt`` NOR ``playwright`` installed (the default
environment lacks both). A fake ``gppt`` -- and, where needed, a fake
``playwright.async_api`` -- is injected into :data:`sys.modules` so the lazy
imports inside :class:`GpptLogin.login` resolve to our stubs. No network I/O and
no real browser are ever touched.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable
from typing import Any

import pytest

from pixiv_bookmark_backup.auth_login import (
    GpptLogin,
    LoginDriverError,
    LoginFailed,
    LoginFlow,
    LoginResult,
)

SECRET = "secret-refresh-token-xyz"


def _make_login_info(refresh_token: str = "tok") -> dict[str, Any]:
    """Build a ``gppt``-shaped ``LoginInfo`` dict (a TypedDict at runtime)."""

    return {
        "refresh_token": refresh_token,
        "access_token": "access-abc",
        "user": {},
    }


def _install_fake_gppt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_impl: Callable[..., Any],
    record: dict[str, Any] | None = None,
) -> None:
    """Insert a fake ``gppt`` module exposing ``GetPixivToken`` into sys.modules.

    ``login_impl`` is an ``async def`` used as the instance ``login`` coroutine.
    If ``record`` is given, the constructor kwargs are stored under
    ``record['ctor_kwargs']`` so a test can assert what reached the constructor.
    """

    class FakeGetPixivToken:
        def __init__(self, **kwargs: Any) -> None:
            if record is not None:
                record["ctor_kwargs"] = kwargs

        login = login_impl

    module = type(sys)("gppt")
    module.GetPixivToken = FakeGetPixivToken  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gppt", module)


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Insert a fake ``playwright.async_api`` exposing an ``Error`` class.

    Returns the fake ``Error`` type so a test's fake ``login`` can raise an
    instance of exactly the type the wrapper's guarded import will resolve.
    """

    class FakePlaywrightError(Exception):
        """Stand-in for ``playwright.async_api.Error`` (a plain Exception)."""

    playwright_pkg = type(sys)("playwright")
    async_api = type(sys)("playwright.async_api")
    async_api.Error = FakePlaywrightError  # type: ignore[attr-defined]
    playwright_pkg.async_api = async_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    return FakePlaywrightError


def test_login_result_repr_hides_token() -> None:
    """The refresh token must never appear in ``repr(LoginResult)``."""

    rendered = repr(LoginResult(SECRET))
    assert SECRET not in rendered
    assert "LoginResult" in rendered


def test_gppt_login_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful flow returns the token and routes creds to the constructor."""

    record: dict[str, Any] = {}

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        return _make_login_info("tok")

    _install_fake_gppt(monkeypatch, login_impl=fake_login, record=record)

    result = GpptLogin().login(headless=False, username="u", password="p", timeout=120)

    assert result == LoginResult("tok")
    assert result.refresh_token == "tok"
    # Credentials and headless reached the CONSTRUCTOR (gppt's documented API).
    assert record["ctor_kwargs"] == {
        "headless": False,
        "username": "u",
        "password": "p",
    }


def test_gppt_not_installed_raises_login_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing gppt yields a teaching LoginDriverError, not a raw traceback."""

    # Setting the module to None makes ``import gppt`` raise ImportError.
    monkeypatch.setitem(sys.modules, "gppt", None)

    with pytest.raises(LoginDriverError) as excinfo:
        GpptLogin().login(headless=True, username="u", password="p", timeout=120)

    message = str(excinfo.value)
    assert "pixi add --pypi --feature login gppt" in message
    assert "playwright install chromium" in message


def test_gppt_playwright_browser_missing_raises_login_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Playwright Error (missing browser binary) maps to LoginDriverError."""

    playwright_error = _install_fake_playwright(monkeypatch)

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        raise playwright_error("Executable doesn't exist; run playwright install")

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    with pytest.raises(LoginDriverError) as excinfo:
        GpptLogin().login(headless=True, username="u", password="p", timeout=120)

    assert "playwright install chromium" in str(excinfo.value)


def test_gppt_login_failure_raises_login_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gppt's ValueError (any login failure) folds into LoginFailed; no secret."""

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("bad creds")

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    with pytest.raises(LoginFailed) as excinfo:
        GpptLogin().login(headless=True, username="user", password=SECRET, timeout=120)

    assert SECRET not in str(excinfo.value)


def test_gppt_timeout_raises_login_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login that never completes within the timeout raises LoginFailed."""

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(10)
        return _make_login_info()

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    with pytest.raises(LoginFailed) as excinfo:
        # A zero timeout forces asyncio.wait_for to raise immediately.
        GpptLogin().login(headless=True, username="u", password="p", timeout=0)

    assert "timed out" in str(excinfo.value).lower()


def test_gppt_missing_refresh_token_raises_login_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful-but-malformed result without 'refresh_token' -> LoginFailed.

    Regression: the key extraction must not raise a bare ``KeyError`` (which the
    CLI does not catch, producing an unhandled traceback and a leaked exit 1).
    """

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        # gppt returned a dict but, unexpectedly, with no refresh_token key.
        return {"access_token": "a"}

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    with pytest.raises(LoginFailed) as excinfo:
        GpptLogin().login(headless=True, username="u", password=SECRET, timeout=120)

    message = str(excinfo.value)
    assert "refresh token" in message.lower()
    # No secret or raw result material leaks into the user-facing message.
    assert SECRET not in message
    assert "access_token" not in message


def test_gppt_empty_refresh_token_raises_login_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string refresh_token is treated as failure, not a usable token."""

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        return _make_login_info(refresh_token="")

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    with pytest.raises(LoginFailed):
        GpptLogin().login(headless=True, username="u", password="p", timeout=120)


def test_gppt_unexpected_error_is_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmapped exception type propagates unchanged (no silent swallowing)."""

    class WeirdError(Exception):
        """Not a TimeoutError, PlaywrightError, or ValueError."""

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        raise WeirdError("something unexpected")

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    with pytest.raises(WeirdError):
        GpptLogin().login(headless=True, username="u", password="p", timeout=120)


def test_module_import_does_not_import_gppt_or_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the module must not pull in gppt or playwright (offline-safe)."""

    # Start from a clean slate so a prior test's injected fakes do not pollute
    # this assertion, then force a fresh import of the module under test.
    for name in list(sys.modules):
        if name == "gppt" or name == "playwright" or name.startswith("playwright."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "pixiv_bookmark_backup.auth_login", raising=False)

    importlib.import_module("pixiv_bookmark_backup.auth_login")

    assert "gppt" not in sys.modules
    assert "playwright" not in sys.modules


def test_protocol_conformance() -> None:
    """GpptLogin satisfies the runtime-checkable LoginFlow Protocol."""

    assert isinstance(GpptLogin(), LoginFlow)


def test_login_drives_the_coroutine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper awaits gppt's async login and reads the kwargs it forwards."""

    seen: dict[str, Any] = {}

    async def fake_login(self: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return _make_login_info("from-coroutine")

    _install_fake_gppt(monkeypatch, login_impl=fake_login)

    result = GpptLogin().login(headless=True, username="u2", password="p2", timeout=30)

    assert result == LoginResult("from-coroutine")
    # The coroutine actually ran and received the same creds/headless.
    assert seen == {"headless": True, "username": "u2", "password": "p2"}


# Type-checker exercise: ``GpptLogin`` is assignable to the Protocol type. This
# mirrors the module's bottom-of-file ``_: type[LoginFlow] = GpptLogin`` static
# guarantee (the module importing without error is itself evidence of it).
_flow: type[LoginFlow] = GpptLogin
