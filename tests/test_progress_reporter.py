"""Unit tests for the terminal progress reporter.

Every test injects an :class:`io.StringIO` as the output stream and a
:class:`FakeClock` as the time source, so no real terminal or wall clock is
involved -- exactly what the injected-dependency design in
``progress_reporter`` is meant to enable.
"""

from __future__ import annotations

import io

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pixiv_bookmark_backup.core import ProgressEvent, ProgressKind
from pixiv_bookmark_backup.progress_reporter import (
    ASCII_FRAMES,
    BRAILLE_FRAMES,
    FIRST_DRAW_DELAY,
    MILESTONE_EVERY_N,
    MILESTONE_INTERVAL,
    THROTTLE_INTERVAL,
    ProgressReporter,
    ReporterMode,
    _pluralize,
)


class FakeClock:
    """A controllable monotonic clock: reads and advances ``value`` on call."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


def make_reporter(
    stream: io.StringIO,
    mode: ReporterMode,
    clock: FakeClock,
    *,
    show_planned: bool = False,
    total: int | None = None,
    unit: str = "works",
    ascii_only: bool = True,
) -> ProgressReporter:
    return ProgressReporter(
        stream=stream,
        mode=mode,
        show_planned=show_planned,
        total=total,
        unit=unit,
        ascii_only=ascii_only,
        clock=clock,
    )


def work_event(
    *,
    works_total: int = 0,
    files_downloaded: int = 0,
    bytes_downloaded: int = 0,
    line: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        kind=ProgressKind.WORK,
        works_total=works_total,
        files_downloaded=files_downloaded,
        bytes_downloaded=bytes_downloaded,
        line=line,
    )


# -- _format_bytes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0.0 MB"),
        (500_000, "0.5 MB"),
        (1_234_567, "1.2 MB"),
        (104_857_600, "104.9 MB"),
    ],
)
def test_format_bytes(n: int, expected: str) -> None:
    reporter = make_reporter(io.StringIO(), ReporterMode.BAR, FakeClock())
    assert reporter._format_bytes(n) == expected


# -- spinner frame selection ----------------------------------------------


def test_ascii_only_uses_ascii_frames() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, ascii_only=True)
    # Force a first draw immediately via the event-count path.
    for _ in range(3):
        reporter.handle(work_event(works_total=1))
    output = stream.getvalue()
    assert any(frame in output for frame in ASCII_FRAMES)
    assert not any(frame in output for frame in BRAILLE_FRAMES)


def test_ascii_only_output_is_ascii_encodable() -> None:
    # ``ascii_only`` means the stream cannot render non-ASCII; every glyph the
    # bar writes (spinner frame AND separators) must stay inside ASCII.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, ascii_only=True)
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1, files_downloaded=2, bytes_downloaded=5))
    stream.getvalue().encode("ascii")  # raises UnicodeEncodeError on regression


def test_unicode_uses_braille_frames() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, ascii_only=False)
    for _ in range(3):
        reporter.handle(work_event(works_total=1))
    output = stream.getvalue()
    assert any(frame in output for frame in BRAILLE_FRAMES)


# -- BAR throttling --------------------------------------------------------


def test_bar_throttles_within_interval() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    # Get past the first-draw delay so the first draw is guaranteed.
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    assert stream.getvalue().count("\r") == 1

    # A second event within the throttle interval must NOT redraw.
    clock.advance(THROTTLE_INTERVAL / 2)
    reporter.handle(work_event(works_total=2))
    assert stream.getvalue().count("\r") == 1

    # Advancing past the interval allows the next draw.
    clock.advance(THROTTLE_INTERVAL)
    reporter.handle(work_event(works_total=3))
    assert stream.getvalue().count("\r") == 2


# -- BAR first-draw delay --------------------------------------------------


def test_bar_first_draw_delay_suppresses_immediate_draw() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    # No time elapsed and only one event: below both thresholds -> no draw.
    reporter.handle(work_event(works_total=1))
    assert stream.getvalue() == ""


def test_bar_first_draw_after_delay_elapsed() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    assert stream.getvalue() != ""


def test_bar_first_draw_after_enough_events() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    # Clock never advances, but the event-count threshold forces a draw.
    for _ in range(2):
        reporter.handle(work_event(works_total=1))
    assert stream.getvalue() == ""
    reporter.handle(work_event(works_total=1))
    assert stream.getvalue() != ""


# -- MILESTONE cadence -----------------------------------------------------


def test_milestone_does_not_write_every_event() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)

    # Clock frozen so only the count-based cadence can trigger. The first event
    # triggers (no prior milestone), then nothing until the Nth event.
    for i in range(1, MILESTONE_EVERY_N):
        reporter.handle(work_event(works_total=i))
    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    assert len(lines) == 1  # only the very first event produced a line

    # The Nth event triggers the count-based milestone.
    reporter.handle(work_event(works_total=MILESTONE_EVERY_N))
    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    assert len(lines) == 2


def test_milestone_line_content() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)
    reporter.handle(work_event(works_total=5, files_downloaded=12, bytes_downloaded=0))
    out = stream.getvalue()
    assert "5 works" in out
    assert "12 files" in out
    assert out.endswith("\n")


def test_milestone_time_based_trigger_independent_of_count() -> None:
    # The first event always emits (no prior milestone). A handful more events
    # -- fewer than MILESTONE_EVERY_N -- are silent while the clock is frozen.
    # Advancing past MILESTONE_INTERVAL must fire a second milestone on the next
    # event via the *time* cadence alone, without reaching the count threshold.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)

    reporter.handle(work_event(works_total=1))  # first event -> milestone
    reporter.handle(work_event(works_total=2))  # frozen clock, below count -> silent
    assert len([ln for ln in stream.getvalue().splitlines() if ln]) == 1

    clock.advance(MILESTONE_INTERVAL)
    reporter.handle(work_event(works_total=3))  # 3rd event (< N) but time is due
    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    assert len(lines) == 2  # the time cadence fired, not the count cadence


# -- finish() idempotence --------------------------------------------------


def test_finish_is_idempotent() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    reporter.finish()
    after_first = stream.getvalue()
    reporter.finish()
    assert stream.getvalue() == after_first


def test_finish_without_draw_writes_nothing_extra() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    # No draw ever happened -> finish must not emit a spurious blank line.
    reporter.finish()
    assert stream.getvalue() == ""


def test_finish_disabled_mode_writes_nothing() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.DISABLED, clock)
    reporter.handle(work_event(works_total=1))
    reporter.finish()
    assert stream.getvalue() == ""


# -- VERBOSE mode ----------------------------------------------------------


def test_verbose_prints_work_and_user_lines() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.VERBOSE, clock)

    reporter.handle(work_event(line="downloaded work 42"))
    reporter.handle(ProgressEvent(kind=ProgressKind.USER, line="user alice"))
    out = stream.getvalue()
    assert "downloaded work 42\n" in out
    assert "user alice\n" in out


def test_verbose_planned_hidden_by_default() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.VERBOSE, clock, show_planned=False)
    reporter.handle(
        ProgressEvent(kind=ProgressKind.WORK_PLANNED, line="will fetch: a, b")
    )
    assert stream.getvalue() == ""


def test_verbose_planned_shown_when_enabled() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.VERBOSE, clock, show_planned=True)
    reporter.handle(
        ProgressEvent(kind=ProgressKind.WORK_PLANNED, line="will fetch: a, b")
    )
    assert "will fetch: a, b\n" in stream.getvalue()


def test_verbose_ignores_run_start() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.VERBOSE, clock)
    reporter.handle(ProgressEvent(kind=ProgressKind.RUN_START, line="starting"))
    assert stream.getvalue() == ""


# -- DISABLED mode ---------------------------------------------------------


def test_disabled_writes_nothing() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.DISABLED, clock)

    for kind in ProgressKind:
        reporter.handle(ProgressEvent(kind=kind, line="x", works_total=1))
    assert stream.getvalue() == ""


# -- determinate BAR rendering ---------------------------------------------


def test_bar_determinate_shows_numerator_over_total() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, total=10, unit="works")
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=3))
    assert "3/10 works" in stream.getvalue()


def test_bar_determinate_users_unit() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, total=5, unit="users")
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(ProgressEvent(kind=ProgressKind.USER, users_total=2))
    assert "2/5 users" in stream.getvalue()


def test_bar_indeterminate_users_headline() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, unit="users")
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(ProgressEvent(kind=ProgressKind.USER, users_total=4))
    assert "4 users" in stream.getvalue()


# -- pluralization ---------------------------------------------------------


def test_bar_headline_singular_at_one_work() -> None:
    # Indeterminate headline: "1 work" (singular), not "1 works".
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, unit="works")
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1, files_downloaded=1))
    out = stream.getvalue()
    assert "1 work" in out
    assert "1 works" not in out
    assert "1 file" in out  # files fragment also pluralizes
    assert "1 files" not in out


def test_bar_headline_singular_at_one_user() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, unit="users")
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(ProgressEvent(kind=ProgressKind.USER, users_total=1))
    out = stream.getvalue()
    assert "1 user" in out
    assert "1 users" not in out


def test_bar_headline_plural_at_zero_and_two() -> None:
    # Everything other than exactly 1 stays plural.
    for count in (0, 2):
        stream = io.StringIO()
        clock = FakeClock()
        reporter = make_reporter(stream, ReporterMode.BAR, clock, unit="works")
        clock.advance(FIRST_DRAW_DELAY)
        reporter.handle(work_event(works_total=count, files_downloaded=count))
        out = stream.getvalue()
        assert f"{count} works" in out
        assert f"{count} files" in out


def test_bar_determinate_ratio_stays_plural_at_one() -> None:
    # The determinate ratio form keeps the plural unit even at a 1 numerator.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, total=50, unit="works")
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    assert "1/50 works" in stream.getvalue()


@given(
    n=st.integers(min_value=0, max_value=10_000),
    singular=st.sampled_from(["work", "user", "file"]),
)
def test_pluralize_matches_singular_iff_count_is_one(n: int, singular: str) -> None:
    # Property, not just the n=0/1/2 examples above: for ANY non-negative
    # count, the singular form appears iff n == 1, and n is always echoed
    # verbatim as the leading token.
    result = _pluralize(n, singular)
    expected = f"{n} {singular}" if n == 1 else f"{n} {singular}s"
    assert result == expected


def test_milestone_singular_at_one() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)
    reporter.handle(work_event(works_total=1, files_downloaded=1))
    out = stream.getvalue()
    assert "1 work" in out
    assert "1 works" not in out
    assert "1 file" in out
    assert "1 files" not in out


# -- ASCII separator -------------------------------------------------------


def test_ascii_bar_uses_comma_separator_not_dash() -> None:
    # The ASCII fallback separator is ", " (matching MILESTONE), so a frozen
    # "-" spinner frame cannot blur into a " - " separator.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, ascii_only=True)
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=2, files_downloaded=3, bytes_downloaded=5))
    out = stream.getvalue()
    assert ", " in out
    assert " - " not in out


# -- BAR event routing -----------------------------------------------------


def test_bar_run_start_records_start_without_drawing() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    reporter.handle(ProgressEvent(kind=ProgressKind.RUN_START))
    assert stream.getvalue() == ""

    # The first-draw delay is measured from the recorded start, so a WORK
    # event past the delay draws immediately.
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    assert stream.getvalue() != ""


def test_bar_ignores_work_planned() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(ProgressEvent(kind=ProgressKind.WORK_PLANNED, line="will fetch: a"))
    assert stream.getvalue() == ""


def test_milestone_ignores_non_progress_kinds() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)

    reporter.handle(ProgressEvent(kind=ProgressKind.RUN_START))
    reporter.handle(ProgressEvent(kind=ProgressKind.WORK_PLANNED, line="will fetch: a"))
    assert stream.getvalue() == ""
