"""Core logic for pixiv-bookmark-backup.

Pure, testable functions and value objects live here; the click layer in
``cli.py`` stays thin and delegates to this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import NamedTuple, Protocol

from attrs import define, field, frozen
from attrs.validators import ge, in_, instance_of, le, optional

from pixiv_bookmark_backup.manifest import WorkEntry
from pixiv_bookmark_backup.naming import strip_control

#: Bookmark visibilities the tool understands.
VISIBILITIES = ("public", "private", "all")

#: Tag surfaces the ``search`` command can match keywords against.
SEARCH_FIELDS = ("work", "bookmark", "translated")

#: NSFW filter modes for ``search``: keep all, drop NSFW, or keep only NSFW.
NSFW_MODES = ("include", "exclude", "only")

#: Ugoira filter modes for ``search``: keep all, drop ugoira, or keep only ugoira.
UGOIRA_MODES = ("include", "exclude", "only")

#: Sort keys for ``search`` results.
SORT_KEYS = ("posted", "id", "title")


def _validate_fields(
    _instance: object, _attribute: object, value: tuple[str, ...]
) -> None:
    """Reject an empty ``fields`` selection or any surface outside SEARCH_FIELDS.

    The CLI normalizes ``()`` to all three surfaces via :func:`normalize_fields`
    *before* constructing a :class:`SearchConfig`, so a constructed config always
    carries a non-empty, valid ``fields`` tuple.
    """

    if not value:
        raise ValueError("fields must not be empty")
    unknown = [item for item in value if item not in SEARCH_FIELDS]
    if unknown:
        raise ValueError(
            f"unknown search field(s): {', '.join(unknown)}; "
            f"choose from {', '.join(SEARCH_FIELDS)}"
        )


@frozen
class BackupConfig:
    """Parameters controlling a single bookmark backup run.

    A frozen ``attrs`` value object: validated once at construction, then
    safe to pass around. ``output_dir`` is coerced to a :class:`~pathlib.Path`
    so callers may pass a plain string. The refresh token is intentionally
    *not* stored here -- it is handled separately from run parameters.
    """

    output_dir: Path = field(converter=Path)
    user_id: str | None = field(default=None, validator=optional(instance_of(str)))
    visibility: str = field(default="public", validator=in_(VISIBILITIES))
    dry_run: bool = field(default=False, validator=instance_of(bool))
    limit: int | None = field(
        default=None, validator=optional([instance_of(int), ge(1)])
    )
    since: date | None = field(default=None, validator=optional(instance_of(date)))
    skip_existing: bool = field(default=True, validator=instance_of(bool))
    metadata_only: bool = field(default=False, validator=instance_of(bool))
    json_output: bool = field(default=False, validator=instance_of(bool))
    verbosity: int = field(default=0, validator=instance_of(int))
    quiet: bool = field(default=False, validator=instance_of(bool))
    max_retries: int = field(default=3, validator=[instance_of(int), ge(0), le(10)])
    time_per_request: float = field(default=0.0, converter=float, validator=ge(0))


@frozen
class RunSummary:
    """Tallies collected over a backup run.

    A plain frozen value object: every counter defaults to ``0`` so a fresh
    summary represents "nothing happened yet". The orchestrator accumulates
    these counts and hands the final summary to :func:`decide_exit_code`.
    """

    works_total: int = 0
    works_succeeded: int = 0
    works_failed: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_downloaded: int = 0


@frozen
class FollowingConfig:
    """Parameters controlling a single follow-backup run.

    A frozen ``attrs`` value object: validated once at construction, then
    safe to pass around. ``output_dir`` is coerced to a :class:`~pathlib.Path`
    so callers may pass a plain string. The refresh token is intentionally
    *not* stored here -- it is handled separately from run parameters.

    Unlike :class:`BackupConfig`, this has no ``user_id`` or ``visibility``:
    the works backed up are those of the authenticated user's followed
    creators, selected via ``follow_visibility``.
    """

    output_dir: Path = field(converter=Path)
    follow_visibility: str = field(default="public", validator=in_(VISIBILITIES))
    dry_run: bool = field(default=False, validator=instance_of(bool))
    limit: int | None = field(
        default=None, validator=optional([instance_of(int), ge(1)])
    )
    since: date | None = field(default=None, validator=optional(instance_of(date)))
    skip_existing: bool = field(default=True, validator=instance_of(bool))
    metadata_only: bool = field(default=False, validator=instance_of(bool))
    json_output: bool = field(default=False, validator=instance_of(bool))
    verbosity: int = field(default=0, validator=instance_of(int))
    quiet: bool = field(default=False, validator=instance_of(bool))
    max_retries: int = field(default=3, validator=[instance_of(int), ge(0), le(10)])
    time_per_request: float = field(default=0.0, converter=float, validator=ge(0))
    per_user_limit: int | None = field(
        default=None, validator=optional([instance_of(int), ge(1)])
    )
    max_users: int | None = field(
        default=None, validator=optional([instance_of(int), ge(1)])
    )
    only: tuple[str, ...] = field(factory=tuple)
    exclude: tuple[str, ...] = field(factory=tuple)


@frozen
class FollowingRunSummary:
    """Tallies collected over a follow-backup run.

    A plain frozen value object: every counter defaults to ``0`` so a fresh
    summary represents "nothing happened yet". The orchestrator accumulates
    these counts and hands the final summary to :func:`decide_exit_code`.

    Extends the per-work counters of :class:`RunSummary` with per-user tallies.
    ``following_of`` is not a counter: it records the authenticated self id so
    the CLI can include it in the JSON summary.
    """

    works_total: int = 0
    works_succeeded: int = 0
    works_failed: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_downloaded: int = 0
    users_total: int = 0
    users_with_works: int = 0
    following_of: str | None = None


class ProgressKind(Enum):
    """The category of a :class:`ProgressEvent`.

    Lets a renderer switch on the event type without inspecting which fields
    happen to be populated.
    """

    RUN_START = "run_start"
    USER = "user"
    WORK = "work"
    WORK_PLANNED = "work_planned"


@frozen
class ProgressEvent:
    """A structured progress notification emitted by the orchestrator.

    A click-agnostic value object: the orchestrator in ``backup.py`` emits
    these through a ``progress`` callback so it never touches any terminal or
    rendering concern. ``line`` carries the ready-made human-readable text (for
    a verbose mode to print verbatim); the other fields carry structured
    counts/context so a progress-bar renderer can use them without parsing
    strings. Unused fields keep their zero/``None`` defaults.
    """

    kind: ProgressKind
    works_total: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_downloaded: int = 0
    users_total: int = 0
    index: int = 0
    work_id: int | None = None
    title: str | None = None
    page_count: int | None = None
    user_id: str | None = None
    user_name: str | None = None
    planned: tuple[str, ...] = field(factory=tuple)
    line: str | None = None


class ExitCode(IntEnum):
    """Process exit codes for the CLI.

    The numeric values are part of the CLI contract -- scripts depend on them,
    so they must not change without an approved plan.

    Codes 7, 8, and 9 are specific to the interactive ``login`` command:

    * :attr:`LOGIN_DRIVER` (7) -- the interactive-login browser/driver is
      unavailable (e.g. gppt not installed, or the Playwright browser binary
      is missing).
    * :attr:`LOGIN_FAILED` (8) -- the login flow ran but did not yield a token
      (login failed, timed out, or credentials were rejected).
    * :attr:`CONFIG_WRITE` (9) -- a token was obtained but writing it to the
      config file failed.

    Code 10 is specific to the ``search --symlink-dir`` command:

    * :attr:`SYMLINK` (10) -- a ``search --symlink-dir`` run failed to create
      symlinks (a filesystem or platform error, e.g. a name conflict with
      existing content, an unwritable directory, or a platform without symlink
      support).
    """

    SUCCESS = 0
    USAGE = 2
    AUTH = 3
    NETWORK = 4
    NO_BOOKMARKS = 5
    PARTIAL = 6
    LOGIN_DRIVER = 7
    LOGIN_FAILED = 8
    CONFIG_WRITE = 9
    SYMLINK = 10
    INTERRUPTED = 130


class _ExitDecidable(Protocol):
    """Structural type for summaries that :func:`decide_exit_code` can rank.

    Both :class:`RunSummary` and :class:`FollowingRunSummary` satisfy it.
    The members are read-only so that ``@frozen`` value objects match.
    """

    @property
    def works_total(self) -> int: ...

    @property
    def works_failed(self) -> int: ...

    @property
    def files_failed(self) -> int: ...


@define
class RequestPacer:
    """Enforce a minimum start-to-start interval between processed works.

    Client-side rate limiter for Pixiv: constructed once per run from
    ``config.time_per_request`` and ``tick()``-ed at the top of each processed
    work. Measures wall-clock elapsed since the previous work *started* and
    sleeps only the remainder of ``period`` (never a negative amount), so a
    work that already took ``>= period`` incurs no delay. ``period == 0``
    disables it (``tick()`` is a strict no-op -- no clock reads, no sleeps),
    preserving un-paced behavior byte-for-byte.

    ``monotonic`` and ``sleep`` are injectable (defaulting to the stdlib
    real-time sources) so tests can drive pacing deterministically and offline.
    A ``KeyboardInterrupt`` raised by ``sleep`` propagates untouched, so the
    orchestrator's interrupt handling (flush partial manifest, exit 130) still
    holds.
    """

    period: float = field(converter=float, validator=ge(0))
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last_start: float | None = field(default=None, init=False)

    def tick(self) -> None:
        """Pace one processed work: sleep the remainder of ``period``, if any."""

        if self.period == 0:
            return
        now = self.monotonic()
        if self._last_start is not None:
            remaining = self.period - (now - self._last_start)
            if remaining > 0:
                self.sleep(remaining)
                now = self.monotonic()
        self._last_start = now


def decide_exit_code(summary: _ExitDecidable) -> ExitCode:
    """Map a completed run's summary to an :class:`ExitCode`.

    Accepts either a :class:`RunSummary` or a :class:`FollowingRunSummary`.

    This is a pure, count-based decision for runs that reached completion:

    * no works at all -> :attr:`ExitCode.NO_BOOKMARKS`
    * works processed with no failures -> :attr:`ExitCode.SUCCESS`
    * works processed with any failure -> :attr:`ExitCode.PARTIAL`

    The :attr:`ExitCode.AUTH`, :attr:`ExitCode.NETWORK`, and
    :attr:`ExitCode.INTERRUPTED` outcomes are *not* decided here. They are
    determined by the orchestrator's control flow (caught exceptions and
    signal handling), because no completed summary exists in those cases.
    """

    if summary.works_total == 0:
        return ExitCode.NO_BOOKMARKS
    if summary.works_failed == 0 and summary.files_failed == 0:
        return ExitCode.SUCCESS
    return ExitCode.PARTIAL


def describe_plan(config: BackupConfig) -> str:
    """Return a human-readable, one-line summary of what a run would do.

    This is the kind of pure, easily tested helper that the CLI command
    formats and prints; it performs no I/O.
    """

    verb = "Would back up" if config.dry_run else "Backing up"
    target = "your own (self)" if config.user_id is None else f"user {config.user_id}"
    return (
        f"{verb} {config.visibility} bookmarks for {target} "
        f"into {config.output_dir}"
    )


@frozen
class SearchConfig:
    """Parameters controlling a single offline ``search`` run.

    A frozen ``attrs`` value object: validated once at construction, then safe
    to pass around. ``input_path`` is coerced to a :class:`~pathlib.Path` so
    callers may pass a plain string. The command is read-only and offline. This
    is the single config the CLI builds and threads through the run: the query
    fields (keywords, filters, blacklists, nsfw, ugoira, sort, limit) drive the pure
    search functions, while ``count``/``json_output``/``verbosity``/``quiet``
    are presentation state the CLI reads when emitting results. The CLI layer is
    responsible for loading the manifest and normalizing options before
    constructing it.

    ``fields`` selects which tag surfaces keywords match against; it is always a
    non-empty subset of :data:`SEARCH_FIELDS` (the CLI normalizes an empty
    selection to all three via :func:`normalize_fields` before construction).
    """

    input_path: Path = field(converter=Path)
    keywords: tuple[str, ...] = field(factory=tuple)
    fields: tuple[str, ...] = field(default=SEARCH_FIELDS, validator=_validate_fields)
    exact: bool = field(default=False, validator=instance_of(bool))
    match_all: bool = field(default=False, validator=instance_of(bool))
    authors: tuple[str, ...] = field(factory=tuple)
    blacklist_tags: tuple[str, ...] = field(factory=tuple)
    blacklist_authors: tuple[str, ...] = field(factory=tuple)
    nsfw: str = field(default="include", validator=in_(NSFW_MODES))
    ugoira: str = field(default="include", validator=in_(UGOIRA_MODES))
    sort: str = field(default="posted", validator=in_(SORT_KEYS))
    limit: int | None = field(
        default=None, validator=optional([instance_of(int), ge(1)])
    )
    count: bool = field(default=False, validator=instance_of(bool))
    json_output: bool = field(default=False, validator=instance_of(bool))
    verbosity: int = field(default=0, validator=instance_of(int))
    quiet: bool = field(default=False, validator=instance_of(bool))


class SearchMatch(NamedTuple):
    """A surviving work plus the surfaces its keywords matched on.

    ``matched_fields`` is a subset of the run's ``fields`` (or the full field
    list in browse mode); it drives verbose per-hit reporting in the CLI.
    """

    work: WorkEntry
    matched_fields: list[str]


def normalize_fields(fields: tuple[str, ...]) -> tuple[str, ...]:
    """Return ``fields`` unchanged, or all of :data:`SEARCH_FIELDS` when empty.

    The CLI calls this on the raw ``--field`` selection so that "no field given"
    means "search every surface". A non-empty selection is returned as-is.
    """

    return SEARCH_FIELDS if not fields else fields


def _surface_tags(work: WorkEntry, surface: str) -> list[str]:
    """Return the tag strings on one surface of ``work`` (``None``-safe).

    ``"work"`` yields raw tag names, ``"translated"`` yields translated names
    (skipping any that are ``None``), and ``"bookmark"`` yields the bookmark
    tags (an empty list when they were never fetched).
    """

    if surface == "work":
        return [tag["name"] for tag in work.work_tags]
    if surface == "translated":
        return [
            tag["translated_name"]
            for tag in work.work_tags
            if tag["translated_name"] is not None
        ]
    return list(work.bookmark_tags or [])


def _keyword_hits(work: WorkEntry, keyword: str, surface: str, exact: bool) -> bool:
    """Return whether ``keyword`` hits any tag on ``surface`` of ``work``.

    Matching is case-insensitive via :meth:`str.casefold`: ``exact`` requires
    equality, otherwise the keyword must be a substring of the tag.
    """

    needle = keyword.casefold()
    for tag in _surface_tags(work, surface):
        hay = tag.casefold()
        if (needle == hay) if exact else (needle in hay):
            return True
    return False


def matched_fields(work: WorkEntry, config: SearchConfig) -> list[str]:
    """Return the ``config.fields`` surfaces on which ``work`` matched.

    With zero keywords this is *browse mode*: every selected field is reported,
    in ``config.fields`` order. Otherwise a surface is included when any keyword
    hits any of its tags, and the result is the sorted, distinct subset of
    ``config.fields`` that produced a hit.
    """

    if not config.keywords:
        return list(config.fields)
    hit: set[str] = set()
    for surface in config.fields:
        for keyword in config.keywords:
            if _keyword_hits(work, keyword, surface, config.exact):
                hit.add(surface)
                break
    return sorted(hit)


def is_keyword_match(work: WorkEntry, config: SearchConfig) -> bool:
    """Return whether ``work`` satisfies the keyword predicate.

    Zero keywords always match. A keyword "hits" when it matches on any of the
    selected ``config.fields``. With ``match_all`` every keyword must hit (AND);
    otherwise any single keyword hitting is enough (OR).
    """

    if not config.keywords:
        return True

    def keyword_hits_any_field(keyword: str) -> bool:
        return any(
            _keyword_hits(work, keyword, surface, config.exact)
            for surface in config.fields
        )

    if config.match_all:
        return all(keyword_hits_any_field(kw) for kw in config.keywords)
    return any(keyword_hits_any_field(kw) for kw in config.keywords)


def author_ok(work: WorkEntry, config: SearchConfig) -> bool:
    """Return whether ``work`` passes the ``--author`` allow-filter.

    With no author terms every work passes. Otherwise the work is kept when any
    term is a case-insensitive substring of the author's display name *or*
    account handle.
    """

    if not config.authors:
        return True
    name = str(work.author.get("name", "")).casefold()
    account = str(work.author.get("account", "")).casefold()
    return any(
        term.casefold() in name or term.casefold() in account for term in config.authors
    )


def blacklisted(work: WorkEntry, config: SearchConfig) -> bool:
    """Return whether ``work`` is excluded by a blacklist term.

    Unlike the keyword filters, blacklist tag matching ignores ``config.fields``
    and checks *all* surfaces (work name, translated name, bookmark tag): a work
    is blacklisted when any ``blacklist_tags`` term equals (case-insensitively)
    any of those tags, or when any ``blacklist_authors`` term is a
    case-insensitive substring of the author name or account.
    """

    if config.blacklist_tags:
        all_tags = [
            tag.casefold()
            for surface in SEARCH_FIELDS
            for tag in _surface_tags(work, surface)
        ]
        for term in config.blacklist_tags:
            if term.casefold() in all_tags:
                return True

    if config.blacklist_authors:
        name = str(work.author.get("name", "")).casefold()
        account = str(work.author.get("account", "")).casefold()
        for term in config.blacklist_authors:
            needle = term.casefold()
            if needle in name or needle in account:
                return True

    return False


def nsfw_ok(work: WorkEntry, config: SearchConfig) -> bool:
    """Return whether ``work`` passes the ``--nsfw`` tri-state filter.

    ``include`` keeps everything, ``exclude`` drops NSFW works, and ``only``
    keeps *only* NSFW works. The NSFW flag is read defensively from
    ``work.nsfw["is_nsfw"]``.
    """

    flag = bool(work.nsfw.get("is_nsfw"))
    if config.nsfw == "exclude":
        return not flag
    if config.nsfw == "only":
        return flag
    return True


def ugoira_ok(work: WorkEntry, config: SearchConfig) -> bool:
    """Return whether ``work`` passes the ``--ugoira`` tri-state filter.

    ``include`` keeps everything, ``exclude`` drops ugoira (animated) works, and
    ``only`` keeps *only* ugoira works. A work is ugoira when its
    ``work.type`` is ``"ugoira"``.
    """

    if config.ugoira == "exclude":
        return work.type != "ugoira"
    if config.ugoira == "only":
        return work.type == "ugoira"
    return True


def _posted_sort_key(work: WorkEntry) -> float:
    """Return a POSIX-timestamp sort key for ``work``'s posted time.

    Parsing goes through :func:`datetime.fromisoformat` and the aware datetime
    is reduced to a POSIX float so results with *mixed* UTC offsets compare
    correctly and no aware/naive comparison ever arises. A malformed timestamp
    never raises: it falls back to ``-inf`` so it sorts last under the
    newest-first ordering.
    """

    try:
        return datetime.fromisoformat(work.posted_at).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return float("-inf")


def sort_works(works: list[WorkEntry], sort: str) -> list[WorkEntry]:
    """Return ``works`` sorted by ``sort`` with a stable, total ordering.

    ``"posted"`` sorts by parsed timestamp, newest first (descending);
    ``"id"`` sorts by ascending integer id; ``"title"`` sorts by ascending
    case-folded title. All keys are total and never raise, so a malformed
    ``posted_at`` cannot crash the run.
    """

    if sort == "id":
        return sorted(works, key=lambda work: work.id)
    if sort == "title":
        return sorted(works, key=lambda work: work.title.casefold())
    return sorted(works, key=_posted_sort_key, reverse=True)


def run_search(
    works: list[WorkEntry], config: SearchConfig
) -> tuple[list[SearchMatch], int]:
    """Filter, annotate, and sort ``works`` for a search run.

    A work survives when it satisfies *all* of: the keyword predicate
    (:func:`is_keyword_match`), the author allow-filter (:func:`author_ok`), not
    being blacklisted (:func:`blacklisted`), the NSFW filter
    (:func:`nsfw_ok`), and the ugoira filter (:func:`ugoira_ok`). Survivors are
    sorted by ``config.sort`` and paired with their :func:`matched_fields`.

    Returns ``(matches, works_scanned)`` where ``works_scanned`` is the total
    number of input works. ``--limit`` is deliberately *not* applied here: the
    CLI truncates for display, but the true pre-limit total must remain
    available to callers (e.g. for ``--count``).
    """

    survivors = [
        work
        for work in works
        if is_keyword_match(work, config)
        and author_ok(work, config)
        and not blacklisted(work, config)
        and nsfw_ok(work, config)
        and ugoira_ok(work, config)
    ]
    ordered = sort_works(survivors, config.sort)
    matches = [
        SearchMatch(work=work, matched_fields=matched_fields(work, config))
        for work in ordered
    ]
    return matches, len(works)


def format_work_line(match: SearchMatch) -> str:
    """Return a one-line, human-readable summary of a matched work.

    The line carries the work id, type, an ``NSFW``/``SFW`` marker, the quoted
    title, the author's name and account, and the comma-joined work-tag names
    (``(none)`` when the work has no tags).

    Untrusted text fields (title, author name/account, tag names) are stripped
    of ASCII control characters via
    :func:`~pixiv_bookmark_backup.naming.strip_control` before interpolation
    so hostile metadata cannot manipulate the terminal. Blank fields render
    identifiable placeholders: an empty title becomes ``"(untitled)"``, and a
    work whose author name *and* account are both empty renders
    ``by (unknown, id <author_id>)`` so the line is never anonymous.
    """

    work = match.work
    rating = "NSFW" if bool(work.nsfw.get("is_nsfw")) else "SFW"
    names = [strip_control(tag["name"]) for tag in work.work_tags]
    tags = ", ".join(names) if names else "(none)"

    title = strip_control(work.title).strip() or "(untitled)"
    name = strip_control(str(work.author.get("name", ""))).strip()
    account = strip_control(str(work.author.get("account", ""))).strip()
    if not name and not account:
        author = f"(unknown, id {work.author['id']})"
    else:
        author = f"{name} (@{account})"

    return f'{work.id}  {work.type}  {rating}  "{title}"  ' f"by {author}  tags: {tags}"
