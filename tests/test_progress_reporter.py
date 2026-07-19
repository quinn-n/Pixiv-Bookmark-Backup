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
    ProgressUnit,
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
    unit: ProgressUnit = "works",
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


def test_verbose_prints_skipped_line() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.VERBOSE, clock, show_planned=False)

    # A skip line is normal informational verbose output -- printed verbatim and
    # NOT gated by show_planned (which only governs dry-run WORK_PLANNED previews).
    reporter.handle(
        ProgressEvent(
            kind=ProgressKind.WORK_SKIPPED, line="skipped 123: already scanned"
        )
    )
    assert "skipped 123: already scanned\n" in stream.getvalue()


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


# -- WORK_SKIPPED live redraws (Option B) ----------------------------------


def skip_event(
    *,
    works_total: int = 0,
    works_skipped: int = 0,
    users_total: int = 0,
    files_downloaded: int = 0,
    bytes_downloaded: int = 0,
    line: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        kind=ProgressKind.WORK_SKIPPED,
        works_total=works_total,
        works_skipped=works_skipped,
        users_total=users_total,
        files_downloaded=files_downloaded,
        bytes_downloaded=bytes_downloaded,
        line=line,
    )


def test_bar_skip_redraws_without_ticking_cadence() -> None:
    # A WORK_SKIPPED event now redraws the bar with a running skip count, but it
    # must not advance the spinner, tick ``_event_count``, or touch the
    # real-progress throttle (``_last_draw``).
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    # Two real draws advance the spinner and set the real throttle timestamp.
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    clock.advance(THROTTLE_INTERVAL)
    reporter.handle(work_event(works_total=2))

    spin_before = reporter._spin_idx
    last_draw_before = reporter._last_draw
    count_before = reporter._event_count
    frame_before = reporter._last_frame
    redraws_before = stream.getvalue().count("\r")

    # Advance past the throttle so the skip redraw is not itself throttled.
    clock.advance(THROTTLE_INTERVAL)
    reporter.handle(skip_event(works_total=2, works_skipped=3))

    out = stream.getvalue()
    assert out.count("\r") == redraws_before + 1  # a new redraw happened
    assert "3 skipped" in out  # with the updated skip count
    # ...but none of the real-progress cadence state moved.
    assert reporter._spin_idx == spin_before
    assert reporter._last_draw == last_draw_before
    assert reporter._event_count == count_before
    # The drawn glyph is exactly the last real draw's glyph, not advanced.
    assert f"{frame_before} 2 works" in out


def test_milestone_skip_emits_without_ticking_cadence() -> None:
    # A WORK_SKIPPED event now emits a milestone line with the skip count, but it
    # must not tick ``_event_count`` or the real milestone cadence
    # (``_last_milestone``).
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)

    # A real event emits the first (time-cadence) milestone and sets state.
    reporter.handle(work_event(works_total=1, files_downloaded=1))
    count_before = reporter._event_count
    last_milestone_before = reporter._last_milestone
    lines_before = len([ln for ln in stream.getvalue().splitlines() if ln])

    # First skip milestone is due immediately (its own None-gated cadence).
    reporter.handle(skip_event(works_total=1, works_skipped=4, files_downloaded=1))

    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    assert len(lines) == lines_before + 1
    assert "4 skipped" in lines[-1]
    assert reporter._event_count == count_before
    assert reporter._last_milestone == last_milestone_before


# -- byte-identical regression anchors (works_skipped == 0) -----------------

# A fresh reporter's first draw has ``_last_line_len == 0`` so the wire output
# for a single draw is exactly ``"\r" + line`` (no trailing pad). These literals
# capture the pre-change formats so ``works_skipped == 0`` stays byte-identical.


def test_bar_zero_skipped_byte_identical_determinate_braille() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream, ReporterMode.BAR, clock, total=50, unit="works", ascii_only=False
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(
        work_event(works_total=3, files_downloaded=8, bytes_downloaded=1_234_567)
    )
    assert stream.getvalue() == "\r⠋ 3/50 works · 8 files · 1.2 MB"


def test_bar_zero_skipped_byte_identical_indeterminate_braille() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream, ReporterMode.BAR, clock, unit="works", ascii_only=False
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(
        work_event(works_total=12, files_downloaded=8, bytes_downloaded=1_234_567)
    )
    assert stream.getvalue() == "\r⠋ 12 works · 8 files · 1.2 MB"


def test_bar_zero_skipped_byte_identical_determinate_ascii() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream, ReporterMode.BAR, clock, total=50, unit="works", ascii_only=True
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(
        work_event(works_total=3, files_downloaded=8, bytes_downloaded=1_234_567)
    )
    assert stream.getvalue() == "\r- 3/50 works, 8 files, 1.2 MB"


def test_bar_zero_skipped_byte_identical_indeterminate_ascii() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream, ReporterMode.BAR, clock, unit="works", ascii_only=True
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(
        work_event(works_total=12, files_downloaded=8, bytes_downloaded=1_234_567)
    )
    assert stream.getvalue() == "\r- 12 works, 8 files, 1.2 MB"


def test_milestone_zero_skipped_byte_identical() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)
    reporter.handle(
        work_event(works_total=12, files_downloaded=8, bytes_downloaded=1_234_567)
    )
    assert stream.getvalue() == "... 12 works, 8 files, 1.2 MB\n"


@given(
    works_total=st.integers(min_value=0, max_value=10_000),
    files_downloaded=st.integers(min_value=0, max_value=10_000),
    bytes_downloaded=st.integers(min_value=0, max_value=10_000_000_000),
    ascii_only=st.booleans(),
    total=st.one_of(st.none(), st.integers(min_value=1, max_value=10_000)),
)
def test_bar_zero_skipped_never_contains_skip_fragment(
    works_total: int,
    files_downloaded: int,
    bytes_downloaded: int,
    ascii_only: bool,
    total: int | None,
) -> None:
    # Property: for ANY other field values, works_skipped == 0 (the default on
    # ``work_event``) never emits a "skipped" fragment.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream,
        ReporterMode.BAR,
        clock,
        total=total,
        unit="works",
        ascii_only=ascii_only,
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(
        work_event(
            works_total=works_total,
            files_downloaded=files_downloaded,
            bytes_downloaded=bytes_downloaded,
        )
    )
    assert "skipped" not in stream.getvalue()


# -- skip-fragment rendering & positioning ---------------------------------


@pytest.mark.parametrize("ascii_only", [True, False])
@pytest.mark.parametrize("total", [None, 50])
def test_bar_skip_fragment_positioned_after_head(
    ascii_only: bool, total: int | None
) -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream,
        ReporterMode.BAR,
        clock,
        total=total,
        unit="works",
        ascii_only=ascii_only,
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(skip_event(works_total=12, works_skipped=3, files_downloaded=8))
    out = stream.getvalue()
    sep = ", " if ascii_only else " · "
    head = "12/50 works" if total is not None else "12 works"
    # Fragment sits immediately after head, joined by the active separator.
    assert f"{head}{sep}3 skipped{sep}" in out
    # Not pluralized as a countable noun.
    assert "3 skippeds" not in out


def test_bar_skip_fragment_shows_works_count_in_users_run() -> None:
    # Following-backup case: the headline counts users but the skip fragment
    # still reports the *works* skip count.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream, ReporterMode.BAR, clock, total=5, unit="users", ascii_only=True
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(skip_event(users_total=2, works_skipped=7, files_downloaded=1))
    out = stream.getvalue()
    assert "2/5 users, 7 skipped, " in out


@pytest.mark.parametrize("ascii_only", [True, False])
def test_bar_real_event_shows_running_skip_count(ascii_only: bool) -> None:
    # The primary Option B line: a normal WORK download tick that also carries a
    # running skip count renders the fragment AND advances the spinner (the
    # advance_frame=True path), unlike a WORK_SKIPPED redraw.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(
        stream, ReporterMode.BAR, clock, total=50, unit="works", ascii_only=ascii_only
    )
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(
        ProgressEvent(
            kind=ProgressKind.WORK,
            works_total=12,
            works_skipped=3,
            files_downloaded=8,
            bytes_downloaded=1_234_567,
        )
    )
    out = stream.getvalue()
    sep = ", " if ascii_only else " · "
    assert f"12/50 works{sep}3 skipped{sep}8 file" in out
    assert reporter._spin_idx == 1  # a real draw DID advance the spinner
    assert reporter._event_count == 1


def test_milestone_skip_fragment_positioned_after_head() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)
    reporter.handle(skip_event(works_total=12, works_skipped=3, files_downloaded=8))
    out = stream.getvalue()
    # MILESTONE always uses literal ", " regardless of ascii_only.
    assert "... 12 works, 3 skipped, 8 files, " in out


# -- skip throttle / cadence isolation -------------------------------------


def test_bar_skip_throttle_isolated_and_never_spins() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    # Get past the first-draw delay with a real draw so the spinner is at a
    # known frame and _emitted_anything is set.
    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(work_event(works_total=1))
    spin_after_real = reporter._spin_idx
    frame_after_real = reporter._last_frame

    # Two skips within THROTTLE_INTERVAL: only the first (past throttle) redraws.
    clock.advance(THROTTLE_INTERVAL)
    reporter.handle(skip_event(works_total=1, works_skipped=1))
    redraws = stream.getvalue().count("\r")
    clock.advance(THROTTLE_INTERVAL / 2)
    reporter.handle(skip_event(works_total=1, works_skipped=2))
    assert stream.getvalue().count("\r") == redraws  # throttled, no new redraw

    # Advancing past the interval lets the next skip redraw.
    clock.advance(THROTTLE_INTERVAL)
    reporter.handle(skip_event(works_total=1, works_skipped=3))
    assert stream.getvalue().count("\r") == redraws + 1

    # Spinner never advanced across any skip redraw.
    assert reporter._spin_idx == spin_after_real
    assert reporter._last_frame == frame_after_real
    assert reporter._event_count == 1  # only the one real event ticked


def test_milestone_skip_cadence_isolated_from_count() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.MILESTONE, clock)

    # First skip is due immediately (None gate).
    reporter.handle(skip_event(works_total=0, works_skipped=1))
    lines = len([ln for ln in stream.getvalue().splitlines() if ln])
    assert lines == 1

    # Many more skips within MILESTONE_INTERVAL: no count-based cadence for
    # skips, so nothing emits while the clock is frozen.
    for i in range(2, 2 * MILESTONE_EVERY_N):
        reporter.handle(skip_event(works_total=0, works_skipped=i))
    assert len([ln for ln in stream.getvalue().splitlines() if ln]) == 1

    # Advancing past the interval lets the next skip emit via time cadence.
    clock.advance(MILESTONE_INTERVAL)
    reporter.handle(skip_event(works_total=0, works_skipped=99))
    assert len([ln for ln in stream.getvalue().splitlines() if ln]) == 2
    assert reporter._event_count == 0  # skips never ticked the counter


# -- all-skip first draw ----------------------------------------------------


def test_bar_all_skip_first_draw_after_delay() -> None:
    # A fresh reporter with only skips draws once FIRST_DRAW_DELAY has elapsed.
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(skip_event(works_total=0, works_skipped=5, files_downloaded=0))
    out = stream.getvalue()
    assert out != ""
    assert "5 skipped" in out
    assert reporter._event_count == 0


def test_bar_skip_before_delay_is_suppressed() -> None:
    # A skip fired before FIRST_DRAW_DELAY on a fresh reporter draws nothing:
    # first-draw suppression still holds (event-count branch is inert for skips).
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    reporter.handle(skip_event(works_total=0, works_skipped=1))
    assert stream.getvalue() == ""
    assert reporter._event_count == 0


# -- finish() after a skip-only run ----------------------------------------


def test_finish_after_skip_only_run_erases_line() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock)

    clock.advance(FIRST_DRAW_DELAY)
    reporter.handle(skip_event(works_total=0, works_skipped=2))
    drawn = stream.getvalue()
    assert drawn != ""  # a bar was drawn on skips alone

    reporter.finish()
    erase = stream.getvalue()[len(drawn) :]
    assert erase == "\r" + " " * reporter._last_line_len + "\r\n"


# -- frame-reuse correctness ------------------------------------------------


def test_bar_skip_reuses_exact_last_frame() -> None:
    # After several real draws advance the spinner through multiple frames, a
    # skip redraw's glyph equals _last_frame exactly (not frames[0], not +1).
    stream = io.StringIO()
    clock = FakeClock()
    reporter = make_reporter(stream, ReporterMode.BAR, clock, ascii_only=False)

    clock.advance(FIRST_DRAW_DELAY)
    for i in range(1, 5):
        reporter.handle(work_event(works_total=i))
        clock.advance(THROTTLE_INTERVAL)

    last_frame = reporter._last_frame
    assert last_frame != BRAILLE_FRAMES[0]  # spinner has moved off frame 0
    spin_before = reporter._spin_idx

    reporter.handle(skip_event(works_total=4, works_skipped=1))
    # The most recent write (the skip redraw) uses exactly _last_frame.
    last_write = stream.getvalue().rsplit("\r", 1)[-1]
    assert last_write.startswith(f"{last_frame} ")
    assert reporter._spin_idx == spin_before  # unchanged by the skip
