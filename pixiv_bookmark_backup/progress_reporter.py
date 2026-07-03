"""Terminal rendering for the live backup progress indicator.

This module owns *all* terminal-drawing concerns (spinners, in-place redraws,
throttling, milestone lines) and is kept deliberately separate from the
click-agnostic orchestrator (``backup.py``) and the thin click layer
(``cli.py``). It imports nothing from click so it can be unit-tested with a
plain :class:`io.StringIO` and a fake clock.

The reporter never sniffs the terminal itself: whether output is a TTY, what
encoding the stream uses, and how wide the terminal is are all resolved by the
*caller*, which then injects the results (``mode``, ``ascii_only``, ``stream``,
``total``). This dependency-injection keeps the class testable without a real
terminal.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable, TextIO

from attrs import define, field

from pixiv_bookmark_backup.core import ProgressEvent, ProgressKind

#: Spinner frames used when the stream cannot render Unicode.
ASCII_FRAMES: tuple[str, ...] = ("-", "\\", "|", "/")

#: Braille spinner frames used when the stream can render Unicode.
BRAILLE_FRAMES: tuple[str, ...] = (
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
)

#: Minimum seconds between two bar redraws (~10 Hz).
THROTTLE_INTERVAL = 0.1

#: Suppress the bar for this long after start to avoid a flash on fast runs.
FIRST_DRAW_DELAY = 0.15

#: ...unless this many events arrive first (whichever comes first).
FIRST_DRAW_EVENT_COUNT = 3

#: Emit a milestone line every this many events...
MILESTONE_EVERY_N = 10

#: ...or at least this often by the clock, whichever comes first.
MILESTONE_INTERVAL = 2.5


def _pluralize(n: int, singular: str) -> str:
    """Render ``n`` with ``singular`` pluralised by an ``s`` unless ``n == 1``.

    e.g. ``_pluralize(1, "work") == "1 work"`` and ``_pluralize(2, "work") ==
    "2 works"``. Only for the free-standing "N units" headline/fragment forms;
    the determinate ratio form (``"3/50 works"``) stays plural regardless.
    """

    return f"{n} {singular}" if n == 1 else f"{n} {singular}s"


class ReporterMode(Enum):
    """How progress should be rendered to the stream.

    ``DISABLED`` renders nothing, ``VERBOSE`` prints one line per event,
    ``BAR`` draws an in-place spinner/progress bar, and ``MILESTONE`` prints a
    periodic summary line (useful when output is redirected to a file).
    """

    DISABLED = "disabled"
    VERBOSE = "verbose"
    BAR = "bar"
    MILESTONE = "milestone"


@define
class ProgressReporter:
    """Renders :class:`ProgressEvent`s to a text stream.

    All environment resolution (TTY detection, encoding, terminal width) is
    done by the caller and injected here, so the reporter is a pure function of
    its inputs and can be driven from tests with a :class:`io.StringIO` and a
    fake ``clock``.

    ``_format_bytes`` renders sizes in decimal megabytes (bytes / 1_000_000)
    with one decimal place, e.g. ``"1.2 MB"``; ``0`` renders as ``"0.0 MB"``.
    """

    stream: TextIO
    """Destination stream (e.g. an injected ``sys.stderr`` or a ``StringIO``)."""

    mode: ReporterMode
    """Which rendering strategy to use."""

    show_planned: bool
    """Whether ``WORK_PLANNED`` lines are printed in ``VERBOSE`` mode."""

    total: int | None
    """Determinate denominator (e.g. ``--limit``), or ``None`` if unknown."""

    unit: str
    """What ``total`` counts: ``"works"`` or ``"users"``."""

    ascii_only: bool
    """Use ASCII spinner frames when ``True``, Braille frames when ``False``."""

    clock: Callable[[], float] = field(default=time.monotonic)
    """Monotonic time source. Stored *unevaluated* via ``default=`` so it can be
    called repeatedly; ``factory=`` would wrongly store a single float."""

    _last_draw: float | None = field(init=False, default=None)
    _started: float | None = field(init=False, default=None)
    _created: float = field(init=False, default=0.0)
    _spin_idx: int = field(init=False, default=0)
    _finished: bool = field(init=False, default=False)
    _emitted_anything: bool = field(init=False, default=False)
    _event_count: int = field(init=False, default=0)
    _last_line_len: int = field(init=False, default=0)
    _last_milestone: float | None = field(init=False, default=None)

    def __attrs_post_init__(self) -> None:
        # Capture a construction timestamp so the first-draw delay works even
        # when no RUN_START event is ever handled.
        self._created = self.clock()

    # -- public API --------------------------------------------------------

    def handle(self, event: ProgressEvent) -> None:
        """Render a single progress event according to the current mode."""
        if self.mode is ReporterMode.DISABLED:
            return
        if self.mode is ReporterMode.VERBOSE:
            self._handle_verbose(event)
        elif self.mode is ReporterMode.BAR:
            self._handle_bar(event)
        elif self.mode is ReporterMode.MILESTONE:
            self._handle_milestone(event)

    def finish(self) -> None:
        """Finalize output. Idempotent and safe to call after any mode.

        In ``BAR`` mode, if a bar was ever drawn, erase the dangling
        ``\\r``-terminated line and move to a fresh line. Other modes already
        write complete newline-terminated lines, so nothing is needed.
        """
        if self._finished:
            return
        if self.mode is ReporterMode.BAR and self._emitted_anything:
            self._write("\r" + " " * self._last_line_len + "\r", nl=True)
        self._finished = True

    # -- per-mode handlers -------------------------------------------------

    def _handle_verbose(self, event: ProgressEvent) -> None:
        if event.kind in (ProgressKind.WORK, ProgressKind.USER):
            if event.line is not None:
                self._write(event.line, nl=True)
        elif event.kind is ProgressKind.WORK_PLANNED:
            if self.show_planned and event.line is not None:
                self._write(event.line, nl=True)
        # RUN_START is ignored in verbose mode.

    def _handle_bar(self, event: ProgressEvent) -> None:
        if event.kind is ProgressKind.RUN_START:
            if self._started is None:
                self._started = self.clock()
            return
        # WORK_PLANNED is a VERBOSE-only concept; ignore it (and anything else).
        if event.kind not in (ProgressKind.WORK, ProgressKind.USER):
            return

        self._event_count += 1
        now = self.clock()

        if not self._emitted_anything:
            base = self._started if self._started is not None else self._created
            waited_enough = (now - base) >= FIRST_DRAW_DELAY
            seen_enough = self._event_count >= FIRST_DRAW_EVENT_COUNT
            if not (waited_enough or seen_enough):
                return
        else:
            if (
                self._last_draw is not None
                and (now - self._last_draw) < THROTTLE_INTERVAL
            ):
                return

        self._draw_bar(event, now)

    def _handle_milestone(self, event: ProgressEvent) -> None:
        if event.kind not in (ProgressKind.WORK, ProgressKind.USER):
            return

        self._event_count += 1
        now = self.clock()
        due_by_count = self._event_count % MILESTONE_EVERY_N == 0
        due_by_time = (
            self._last_milestone is None
            or (now - self._last_milestone) >= MILESTONE_INTERVAL
        )
        if not (due_by_count or due_by_time):
            return

        head = self._headline_count(event)
        line = (
            f"... {head}, {_pluralize(event.files_downloaded, 'file')}, "
            f"{self._format_bytes(event.bytes_downloaded)}"
        )
        self._write(line, nl=True)
        self._last_milestone = now

    # -- drawing helpers ---------------------------------------------------

    def _draw_bar(self, event: ProgressEvent, now: float) -> None:
        frame = self._spinner_frame()
        if self.unit == "users":
            numerator = event.users_total
        else:
            numerator = event.works_total

        if self.total is not None:
            head = f"{numerator}/{self.total} {self.unit}"
        else:
            head = self._headline_count(event)

        # Keep every glyph inside the stream's encoding: ``ascii_only`` means
        # the caller has told us this stream cannot render non-ASCII, so a
        # Unicode middot separator would raise ``UnicodeEncodeError`` on write.
        # The ASCII fallback reuses the milestone line's ", " (not " - ") so a
        # frozen "-" spinner frame cannot blur into the separator.
        sep = ", " if self.ascii_only else " · "
        line = (
            f"{frame} {head}{sep}{_pluralize(event.files_downloaded, 'file')}{sep}"
            f"{self._format_bytes(event.bytes_downloaded)}"
        )
        pad = max(0, self._last_line_len - len(line))
        self._write("\r" + line + " " * pad, nl=False)

        self._last_line_len = len(line)
        self._last_draw = now
        self._emitted_anything = True

    def _headline_count(self, event: ProgressEvent) -> str:
        if self.unit == "users":
            return _pluralize(event.users_total, "user")
        return _pluralize(event.works_total, "work")

    def _spinner_frame(self) -> str:
        frames = ASCII_FRAMES if self.ascii_only else BRAILLE_FRAMES
        frame = frames[self._spin_idx % len(frames)]
        self._spin_idx += 1
        return frame

    def _format_bytes(self, n: int) -> str:
        """Format a byte count as decimal megabytes, e.g. ``"1.2 MB"``.

        Uses the decimal convention (1 MB == 1_000_000 bytes) with a single
        decimal place. ``0`` renders as ``"0.0 MB"``.
        """
        return f"{n / 1_000_000:.1f} MB"

    def _write(self, text: str, *, nl: bool) -> None:
        self.stream.write(text + "\n" if nl else text)
        flush = getattr(self.stream, "flush", None)
        if callable(flush):
            flush()
