"""Typed, offline-testable wrapper around the interactive Pixiv OAuth login.

This is the **only** module that touches :mod:`gppt` (and, transitively,
Playwright). The rest of the tool drives the login flow exclusively through the
:class:`LoginFlow` Protocol and receives a frozen :class:`LoginResult` value
object whose ``repr`` deliberately omits the refresh token.

Neither ``gppt`` nor ``playwright`` is imported at module top level: both live
behind a lazy import inside :meth:`GpptLogin.login`. This keeps the module
importable -- and every unit test runnable -- in the default environment, which
ships *without* the optional ``login`` feature (``gppt`` + ``playwright``). The
real login path is exercised manually under ``pixi run -e login``.

``gppt``'s ``login`` is an ``async`` coroutine that takes no timeout of its own,
so :meth:`GpptLogin.login` drives it with :func:`asyncio.run` and enforces our
own bound via :func:`asyncio.wait_for`. No exception raised from this module
ever contains the refresh token.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from attrs import field, frozen


class LoginDriverError(Exception):
    """The interactive login browser/driver is unavailable.

    Raised when ``gppt`` is not installed, or when the Playwright browser binary
    it relies on is missing. The CLI maps this to exit code 7. The message never
    contains the refresh token.
    """


class LoginFailed(Exception):
    """The login flow ran but produced no usable token.

    Raised when the login failed, credentials were rejected, or the flow timed
    out. ``gppt`` surfaces all of these as a plain :class:`ValueError`, so they
    fold into this single error. The CLI maps this to exit code 8. The message
    never contains the refresh token.
    """


@frozen
class LoginResult:
    """The outcome of a successful interactive login.

    ``refresh_token`` is excluded from ``repr`` (``field(repr=False)``) so the
    secret never leaks into logs, tracebacks, or debugging output.
    """

    refresh_token: str = field(repr=False)


@runtime_checkable
class LoginFlow(Protocol):
    """The narrow interactive-login surface the rest of the tool depends on.

    Implementations run the OAuth login and return a :class:`LoginResult`, or
    raise the typed :class:`LoginDriverError` / :class:`LoginFailed` instead of
    leaking third-party error types. The method is synchronous even though the
    underlying ``gppt`` call is a coroutine -- the implementation hides the async
    with :func:`asyncio.run`.
    """

    def login(
        self,
        *,
        headless: bool,
        username: str | None,
        password: str | None,
        timeout: int,
    ) -> LoginResult:
        """Run the login flow, enforcing ``timeout`` seconds, and return the token."""
        ...


class GpptLogin:
    """:class:`LoginFlow` backed by :class:`gppt.GetPixivToken`.

    All interactive-login interaction funnels through this class. ``gppt`` and
    the Playwright error type are imported lazily inside :meth:`login` so this
    module stays importable -- and unit-testable -- without the optional
    ``login`` feature installed.
    """

    def login(
        self,
        *,
        headless: bool,
        username: str | None,
        password: str | None,
        timeout: int,
    ) -> LoginResult:
        """Run the Pixiv OAuth login and return the resulting refresh token.

        ``headless=True`` with both credentials runs the flow automatically;
        ``headless=False`` with no credentials opens an interactive browser
        window. (If ``headless=True`` but a credential is ``None``, ``gppt``
        silently falls back to a headed window; the CLI's both-or-neither
        credential validation prevents reaching this half-credential case.)

        Args:
            headless: Whether to run the browser without a visible window.
            username: Pixiv username/email, or ``None`` for interactive entry.
            password: Pixiv password, or ``None`` for interactive entry.
            timeout: Maximum seconds to allow the login flow to run.

        Returns:
            A :class:`LoginResult` holding the resolved refresh token.

        Raises:
            LoginDriverError: If ``gppt`` is not installed or the Playwright
                browser binary is missing (exit code 7).
            LoginFailed: If the login failed, credentials were rejected, or the
                flow timed out (exit code 8).
        """

        try:
            # gppt is an optional ('login' feature) dependency, absent from the
            # default env basedpyright type-checks against; the runtime guard
            # below handles its absence.
            from gppt import GetPixivToken  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise LoginDriverError(
                "gppt is not installed. Install it with "
                "'pixi add --pypi --feature login gppt' (or 'pip install gppt'), "
                "then run 'playwright install chromium'."
            ) from exc

        # Guarded lazy import: a missing Playwright must never crash the wrapper,
        # so we only reference ``PlaywrightError`` in an ``except`` when it is not
        # ``None`` (i.e. when Playwright is actually installed).
        playwright_error: type[BaseException] | None
        try:
            # playwright is pulled in transitively by the optional 'login'
            # feature and is absent from the default type-check env; the
            # is-not-None guard below makes its absence safe at runtime.
            from playwright.async_api import (  # pyright: ignore[reportMissingImports]
                Error as PlaywrightError,
            )

            playwright_error = PlaywrightError
        except ImportError:
            playwright_error = None

        # gppt accepts the credentials on the GetPixivToken(...) constructor; the
        # same kwargs are forwarded to login() for cross-version compatibility.
        token_getter = GetPixivToken(
            headless=headless, username=username, password=password
        )

        try:
            result = asyncio.run(
                asyncio.wait_for(
                    token_getter.login(
                        headless=headless, username=username, password=password
                    ),
                    timeout,
                )
            )
        except asyncio.TimeoutError as exc:
            raise LoginFailed(f"Login timed out after {timeout}s.") from exc
        except Exception as exc:
            # A missing Playwright browser binary surfaces as a Playwright
            # ``Error`` (a direct ``Exception`` subclass, *not* a ``ValueError``),
            # which maps to the driver-unavailable case (exit 7). Check it first.
            if playwright_error is not None and isinstance(exc, playwright_error):
                raise LoginDriverError(
                    "The Playwright browser is not installed. Run "
                    "'playwright install chromium' and try again."
                ) from exc
            # ``gppt`` raises a plain ``ValueError`` for every login failure
            # (form not found, wrong credentials, redirect timeout). Fold them
            # all into ``LoginFailed`` without echoing any credential.
            if isinstance(exc, ValueError):
                raise LoginFailed(
                    "Login failed: Pixiv did not return a token. Check your "
                    "credentials and try again."
                ) from exc
            raise

        # gppt can return a successful-but-malformed result lacking the token;
        # read it defensively and fold a missing/empty value into LoginFailed so
        # the CLI maps it to exit 8 instead of leaking a KeyError traceback. The
        # raw result is never echoed -- it may carry other token material.
        token = result.get("refresh_token") if isinstance(result, dict) else None
        if not token:
            raise LoginFailed(
                "Pixiv returned an unexpected response without a refresh token."
            )
        return LoginResult(refresh_token=token)


# Static guarantee that the concrete login flow satisfies the public Protocol.
_: type[LoginFlow] = GpptLogin
