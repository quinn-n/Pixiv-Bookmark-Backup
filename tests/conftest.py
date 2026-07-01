"""Shared pytest fixtures and test helpers."""

from __future__ import annotations

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    """A click test runner for invoking the CLI in-process."""

    return CliRunner()


class ScriptedMonotonic:
    """A ``monotonic()`` stand-in that pops scripted values in sequence.

    ``RequestPacer.tick()`` reads the clock once per tick when it does not sleep
    and twice when it does (once to measure elapsed, once after sleeping), so a
    test scripts exactly as many values as the tick pattern under test consumes.
    ``calls`` records how many reads happened (so a test can assert a disabled
    pacer never reads the clock); exhausting the script is a bug in the test's
    accounting and raises loudly.
    """

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if not self._values:  # pragma: no cover - guards test mis-scripting
            raise AssertionError("scripted monotonic exhausted")
        return self._values.pop(0)


class SpySleep:
    """A ``sleep()`` stand-in that records each requested duration.

    Every requested duration is appended to ``durations``. With
    ``raise_on_first=True`` the first call raises ``KeyboardInterrupt`` (after
    recording it) to drive the interrupt-during-sleep path and prove the pacer
    does not swallow it.
    """

    def __init__(self, *, raise_on_first: bool = False) -> None:
        self.durations: list[float] = []
        self._raise_on_first = raise_on_first

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        if self._raise_on_first and len(self.durations) == 1:
            raise KeyboardInterrupt
