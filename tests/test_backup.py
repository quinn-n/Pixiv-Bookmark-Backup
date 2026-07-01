"""Offline, deterministic tests for the backup orchestrator.

Every test runs fully offline: a hand-rolled :class:`FakePixivClient` scripts the
Pixiv surface (a fixed self id, scripted ``BookmarkWork`` pages, and optional
``ApiError`` / ``KeyboardInterrupt`` raised at a chosen point), and the per-file
outcomes are controlled by monkeypatching ``backup.download_image`` and
``backup.build_ugoira`` so no real network, zip, or image work is needed. The
manifest clock is injected as a fixed timestamp, so written ``details.json``
files are byte-stable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pixiv_bookmark_backup import backup
from pixiv_bookmark_backup.backup import BackupUsageError, run
from pixiv_bookmark_backup.config import ResolvedAuth
from pixiv_bookmark_backup.core import BackupConfig, ExitCode, decide_exit_code
from pixiv_bookmark_backup.downloader import ImageResult
from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    ImageStatus,
    UgoiraInfo,
    WorkImage,
)
from pixiv_bookmark_backup.pixiv_client import (
    ApiError,
    FollowedUser,
    PixivClient,
)
from pixiv_bookmark_backup.ugoira import UgoiraResult

FIXED_CLOCK = "2026-06-24T00:00:00+00:00"
SELF_ID = "555"
TOKEN = "refresh-token-value"


# -- fakes & helpers ----------------------------------------------------


def _make_work(
    work_id: int,
    *,
    work_type: str = "illust",
    pages: int = 1,
    posted_at: str = "2024-05-01T12:00:00+09:00",
    title: str = "A Work",
) -> BookmarkWork:
    """Build a :class:`BookmarkWork` with ``pages`` original-resolution images.

    An ``ugoira`` work is built with no ``images`` by default (the orchestrator
    derives its single GIF destination from the work id), matching the real
    client which exposes ugoira frames via metadata rather than ``images``.
    """

    images = [
        WorkImage(page=i, source_url=f"https://i.pximg.net/{work_id}_p{i}.jpg")
        for i in range(pages)
    ]
    if work_type == "ugoira":
        images = []
    return BookmarkWork(
        id=work_id,
        type=work_type,
        title=title,
        author=Author(id=7, name="Artist", account="artist_acct"),
        posted_at=posted_at,
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=max(pages, 1),
        images=images,
    )


class FakePixivClient:
    """Scriptable, fully offline stand-in for the ``PixivClient`` Protocol.

    ``authenticate`` returns a fixed self id. ``iter_bookmarks`` yields the works
    scripted for the requested ``restrict`` pass and records the call; it can be
    told to raise ``ApiError`` or ``KeyboardInterrupt`` after yielding a chosen
    number of works (to model a mid-listing failure / Ctrl-C). The
    ``ugoira_metadata`` / ``download`` methods are never reached because the
    tests monkeypatch ``download_image`` / ``build_ugoira``.
    """

    def __init__(
        self,
        works_by_restrict: dict[str, list[BookmarkWork]],
        *,
        self_id: str = SELF_ID,
        raise_after: int | None = None,
        raise_exc: type[BaseException] | BaseException | None = None,
    ) -> None:
        self._works_by_restrict = works_by_restrict
        self._self_id = self_id
        self._raise_after = raise_after
        self._raise_exc = raise_exc
        self.auth_calls: list[str] = []
        self.iter_calls: list[tuple[str, str]] = []

    def authenticate(self, refresh_token: str) -> str:
        self.auth_calls.append(refresh_token)
        return self._self_id

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        self.iter_calls.append((user_id, restrict))
        yielded = 0
        for work in self._works_by_restrict.get(restrict, []):
            if self._raise_after is not None and yielded >= self._raise_after:
                self._raise()
            yield work
            yielded += 1
        if self._raise_after is not None and yielded >= self._raise_after:
            self._raise()

    def _raise(self) -> None:
        exc = self._raise_exc
        if isinstance(exc, BaseException):
            raise exc
        if exc is not None:
            raise exc()
        raise ApiError("listing failed")

    def iter_following(  # pragma: no cover
        self, user_id: str, restrict: str
    ) -> Iterator[FollowedUser]:
        raise NotImplementedError

    def iter_user_works(  # pragma: no cover
        self, user_id: str
    ) -> Iterator[BookmarkWork]:
        raise NotImplementedError

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:  # pragma: no cover
        raise NotImplementedError

    def download(  # pragma: no cover
        self, url: str, dest_path: Path, referer: str = "https://www.pixiv.net/"
    ) -> int:
        raise NotImplementedError


# Static guarantee the fake satisfies the public Protocol.
_: type[PixivClient] = FakePixivClient


def _config(output_dir: Path, **overrides: object) -> BackupConfig:
    """Build a :class:`BackupConfig` rooted at ``output_dir`` with overrides."""

    params: dict[str, object] = {"output_dir": output_dir}
    params.update(overrides)
    return BackupConfig(**params)  # type: ignore[arg-type]


def _auth() -> ResolvedAuth:
    return ResolvedAuth(refresh_token=TOKEN)


def _patch_downloads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    image_result: ImageResult | None = None,
    image_results: dict[str, ImageResult] | None = None,
    ugoira_result: UgoiraResult | None = None,
) -> dict[str, list[object]]:
    """Replace ``download_image`` / ``build_ugoira`` with scripted stand-ins.

    Returns a ``{"images": [...], "ugoira": [...]}`` record of the calls made, so
    a test can assert whether (and how often) a real download would have run.
    ``image_results`` keys by URL for per-file control; ``image_result`` is the
    fallback. The default image outcome is a successful 10-byte download.
    """

    calls: dict[str, list[object]] = {"images": [], "ugoira": []}
    default_image = image_result or ImageResult(ImageStatus.DOWNLOADED, 10, None)

    def fake_download_image(
        client: object,
        url: str,
        dest: Path,
        *,
        max_retries: int,
        skip_existing: bool,
        **_kw: object,
    ) -> ImageResult:
        calls["images"].append((url, dest))
        if image_results is not None and url in image_results:
            return image_results[url]
        return default_image

    def fake_build_ugoira(
        client: object,
        work: object,
        dest: Path,
        *,
        skip_existing: bool,
        **_kw: object,
    ) -> UgoiraResult:
        calls["ugoira"].append(dest)
        return ugoira_result or UgoiraResult(ImageStatus.DOWNLOADED, 20, None)

    monkeypatch.setattr(backup, "download_image", fake_download_image)
    monkeypatch.setattr(backup, "build_ugoira", fake_build_ugoira)
    return calls


def _read_manifest(output_dir: Path) -> Any:
    """Return the parsed ``details.json``.

    Typed ``Any`` deliberately: tests subscript the loosely structured JSON
    freely (``manifest["works"][0]["images"]``) and ``Any`` keeps those reads
    clean for the type checker without scattering ``# type: ignore`` comments.
    """

    return json.loads((output_dir / "details.json").read_text(encoding="utf-8"))


def _run(config: BackupConfig, client: FakePixivClient) -> backup.RunSummary:
    return run(config, _auth(), client, clock=lambda: FIXED_CLOCK)


# -- success ------------------------------------------------------------


def test_success_writes_manifest_with_both_works_and_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    works = {
        "public": [
            _make_work(100, pages=2, title="Multi"),
            _make_work(200, pages=1, title="Single"),
        ]
    }
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path), client)

    assert client.auth_calls == [TOKEN]
    assert summary.works_total == 2
    assert summary.works_succeeded == 2
    assert summary.works_failed == 0
    assert summary.files_downloaded == 3  # 2 pages + 1 page
    assert summary.bytes_downloaded == 30  # 3 files * 10 bytes
    assert len(calls["images"]) == 3
    assert decide_exit_code(summary) is ExitCode.SUCCESS

    manifest = _read_manifest(tmp_path)
    assert manifest["schema_version"] == 1
    assert manifest["generated_at"] == FIXED_CLOCK
    assert manifest["source"] == {"user_id": SELF_ID, "visibility": "public"}
    recorded = {work["id"]: work for work in manifest["works"]}
    assert set(recorded) == {100, 200}
    # Paths are stored relative to the output dir (flat layout -> bare names).
    paths = [img["path"] for work in manifest["works"] for img in work["images"]]
    assert paths == ["100_p0.jpg", "100_p1.jpg", "200_p0.jpg"]


# -- partial ------------------------------------------------------------


def test_partial_when_one_image_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_url = "https://i.pximg.net/100_p1.jpg"
    _patch_downloads(
        monkeypatch,
        image_results={
            failing_url: ImageResult(ImageStatus.FAILED, None, "boom"),
        },
    )
    works = {"public": [_make_work(100, pages=2)]}
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path), client)

    assert summary.works_total == 1
    assert summary.works_failed == 1
    assert summary.files_failed == 1
    assert summary.files_downloaded == 1
    assert decide_exit_code(summary) is ExitCode.PARTIAL

    manifest = _read_manifest(tmp_path)
    statuses = [img["status"] for img in manifest["works"][0]["images"]]
    assert "failed" in statuses


# -- zero works ---------------------------------------------------------


def test_zero_works_yields_no_bookmarks_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakePixivClient({"public": []})

    summary = _run(_config(tmp_path), client)

    assert summary.works_total == 0
    assert decide_exit_code(summary) is ExitCode.NO_BOOKMARKS
    # A manifest is still written (empty works), recording the run happened.
    manifest = _read_manifest(tmp_path)
    assert manifest["works"] == []


# -- metadata-only ------------------------------------------------------


def test_metadata_only_records_entries_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    works = {"public": [_make_work(100, pages=2)]}
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path, metadata_only=True), client)

    # The download primitive is never invoked in metadata-only mode.
    assert calls["images"] == []
    assert summary.files_downloaded == 0
    assert summary.works_total == 1
    assert summary.works_succeeded == 1

    manifest = _read_manifest(tmp_path)
    statuses = {img["status"] for img in manifest["works"][0]["images"]}
    assert statuses == {"metadata-only"}
    # No image files were created on disk.
    assert not (tmp_path / "100_p0.jpg").exists()


# -- dry-run ------------------------------------------------------------


def test_dry_run_writes_nothing_but_counts_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    messages: list[str] = []
    works = {"public": [_make_work(100, pages=2), _make_work(200, pages=1)]}
    client = FakePixivClient(works)

    summary = run(
        _config(tmp_path, dry_run=True),
        _auth(),
        client,
        progress=messages.append,
        clock=lambda: FIXED_CLOCK,
    )

    # No real downloads and no manifest.
    assert calls["images"] == []
    assert calls["ugoira"] == []
    assert not (tmp_path / "details.json").exists()
    # The output dir holds no created files.
    assert list(tmp_path.iterdir()) == []
    # Works are still counted, with planned file counts.
    assert summary.works_total == 2
    assert summary.works_succeeded == 2
    assert summary.files_downloaded == 3
    # A "would create" preview line is emitted per work.
    assert any("would create" in msg for msg in messages)


# -- visibility vs self -------------------------------------------------


def test_private_bookmarks_of_other_user_raise_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakePixivClient({"private": []})

    with pytest.raises(BackupUsageError):
        _run(_config(tmp_path, user_id="999", visibility="private"), client)


def test_all_visibility_of_other_user_raises_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakePixivClient({"public": [], "private": []})

    with pytest.raises(BackupUsageError):
        _run(_config(tmp_path, user_id="999", visibility="all"), client)


# -- "all" visibility ---------------------------------------------------


def test_all_visibility_lists_both_public_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {
        "public": [_make_work(100, pages=1)],
        "private": [_make_work(200, pages=1)],
    }
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path, visibility="all"), client)

    restricts = [restrict for _user, restrict in client.iter_calls]
    assert restricts == ["public", "private"]
    assert summary.works_total == 2

    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {100, 200}


# -- ugoira -------------------------------------------------------------


def test_ugoira_work_builds_gif_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    works = {"public": [_make_work(300, work_type="ugoira")]}
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path), client)

    # build_ugoira (monkeypatched) was invoked for the GIF destination.
    assert calls["ugoira"] == [tmp_path / "300_p0.gif"]
    assert calls["images"] == []
    assert summary.works_total == 1
    assert summary.files_downloaded == 1

    manifest = _read_manifest(tmp_path)
    images = manifest["works"][0]["images"]
    assert len(images) == 1
    assert images[0]["page"] == 0
    assert images[0]["path"] == "300_p0.gif"
    assert images[0]["status"] == "downloaded"


# -- resume merge -------------------------------------------------------


def _prior_manifest_dict(work_id: int) -> dict[str, object]:
    """Return a valid schema-1 manifest containing one old work."""

    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "source": {"user_id": SELF_ID, "visibility": "public"},
        "works": [
            {
                "id": work_id,
                "type": "illust",
                "title": "Old Work",
                "author": {"id": 7, "name": "Artist", "account": "artist_acct"},
                "posted_at": "2023-01-01T00:00:00+09:00",
                "nsfw": {"x_restrict": 0, "sanity_level": 2, "is_nsfw": False},
                "work_tags": [],
                "bookmark_tags": None,
                "page_count": 1,
                "images": [
                    {
                        "page": 0,
                        "source_url": "https://i.pximg.net/old_p0.jpg",
                        "path": f"{work_id}_p0.jpg",
                        "status": "downloaded",
                        "bytes": 99,
                        "error": None,
                    }
                ],
            }
        ],
        "errors": [],
    }


def test_unavailable_work_error_dedups_across_resume_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # A non-ugoira work with no images is "unavailable": it records a work-level
    # error keyed on work_id. Running twice against the same output dir must not
    # accumulate that error -- manifest.merge dedups it by "work_id".
    unavailable = _make_work(800, pages=0)
    assert unavailable.images == []  # no downloadable pages
    client = FakePixivClient({"public": [unavailable]})

    _run(_config(tmp_path), client)
    _run(_config(tmp_path), client)

    manifest = _read_manifest(tmp_path)
    matching = [err for err in manifest["errors"] if err.get("work_id") == 800]
    assert len(matching) == 1
    assert matching[0]["reason"] == "work unavailable (no downloadable pages)"


def test_resume_merges_with_prior_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    details = tmp_path / "details.json"
    details.write_text(json.dumps(_prior_manifest_dict(900)), encoding="utf-8")
    works = {"public": [_make_work(100, pages=1)]}
    client = FakePixivClient(works)

    _run(_config(tmp_path), client)

    manifest = _read_manifest(tmp_path)
    ids = {work["id"] for work in manifest["works"]}
    # The old work is preserved and the new one merged in.
    assert ids == {900, 100}


# -- schema mismatch / corruption ---------------------------------------


def test_schema_mismatch_backs_up_and_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    details = tmp_path / "details.json"
    stale = _prior_manifest_dict(900)
    stale["schema_version"] = 0
    details.write_text(json.dumps(stale), encoding="utf-8")
    works = {"public": [_make_work(100, pages=1)]}
    client = FakePixivClient(works)

    _run(_config(tmp_path), client)

    assert (tmp_path / "details.json.bak").exists()
    manifest = _read_manifest(tmp_path)
    assert manifest["schema_version"] == 1
    # The stale work was not carried over; only the freshly backed-up one is in.
    assert {work["id"] for work in manifest["works"]} == {100}


def test_corrupt_manifest_backs_up_and_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    details = tmp_path / "details.json"
    details.write_text("this is not json {{{", encoding="utf-8")
    works = {"public": [_make_work(100, pages=1)]}
    client = FakePixivClient(works)

    _run(_config(tmp_path), client)

    assert (tmp_path / "details.json.bak").exists()
    manifest = _read_manifest(tmp_path)
    assert manifest["schema_version"] == 1
    assert {work["id"] for work in manifest["works"]} == {100}


# -- SIGINT / KeyboardInterrupt -----------------------------------------


def test_keyboard_interrupt_flushes_partial_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {"public": [_make_work(100, pages=1), _make_work(200, pages=1)]}
    client = FakePixivClient(works, raise_after=1, raise_exc=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        _run(_config(tmp_path), client)

    # A partial manifest with the one processed work was flushed before re-raise.
    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {100}


# -- network mid-run ----------------------------------------------------


def test_network_error_mid_run_writes_partial_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {"public": [_make_work(100, pages=1), _make_work(200, pages=1)]}
    client = FakePixivClient(works, raise_after=1, raise_exc=ApiError)

    summary = _run(_config(tmp_path), client)

    assert summary.works_total == 1
    assert summary.works_failed >= 1
    assert decide_exit_code(summary) is ExitCode.PARTIAL

    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {100}
    assert any("listing interrupted" in err["reason"] for err in manifest["errors"])


def test_network_error_before_any_work_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakePixivClient({"public": []}, raise_after=0, raise_exc=ApiError)

    with pytest.raises(ApiError):
        _run(_config(tmp_path), client)

    # No manifest written when the listing fails before any work is processed.
    assert not (tmp_path / "details.json").exists()


# -- limit / since ------------------------------------------------------


def test_limit_caps_processed_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {
        "public": [_make_work(wid, pages=1) for wid in (100, 200, 300, 400)],
    }
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path, limit=2), client)

    assert summary.works_total == 2
    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {100, 200}


def test_since_filters_out_older_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {
        "public": [
            _make_work(100, pages=1, posted_at="2020-01-01T00:00:00+09:00"),
            _make_work(200, pages=1, posted_at="2026-05-01T00:00:00+09:00"),
        ]
    }
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path, since=date(2025, 1, 1)), client)

    assert summary.works_total == 1
    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {200}


def test_since_keeps_work_with_unparseable_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {
        "public": [_make_work(100, pages=1, posted_at="not-a-real-date")],
    }
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path, since=date(2025, 1, 1)), client)

    # Best-effort filter: an unparseable timestamp keeps the work.
    assert summary.works_total == 1


# -- dry-run + ugoira ---------------------------------------------------


def test_dry_run_ugoira_counts_work_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=True with a ugoira work hits _planned_filenames' ugoira branch.

    _planned_filenames returns [ugoira_filename(work.id)] for ugoira works.
    The orchestrator must count the work and its planned GIF file, never call
    build_ugoira, and write nothing to disk.
    """
    calls = _patch_downloads(monkeypatch)
    messages: list[str] = []
    works = {"public": [_make_work(300, work_type="ugoira")]}
    client = FakePixivClient(works)

    summary = run(
        _config(tmp_path, dry_run=True),
        _auth(),
        client,
        progress=messages.append,
        clock=lambda: FIXED_CLOCK,
    )

    # build_ugoira must never be called in dry-run mode.
    assert calls["ugoira"] == []
    assert calls["images"] == []
    # No files written and no manifest.
    assert not (tmp_path / "details.json").exists()
    assert list(tmp_path.iterdir()) == []
    # The ugoira work and its one planned GIF file are counted.
    assert summary.works_total == 1
    assert summary.works_succeeded == 1
    assert summary.files_downloaded == 1
    # The progress sink received a "would create" line naming the GIF.
    assert any("300_p0.gif" in msg for msg in messages)


# -- SIGINT during dry-run ----------------------------------------------


def test_sigint_during_dry_run_propagates_without_writing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeyboardInterrupt in dry_run=True must re-raise without flushing.

    The `if not config.dry_run` guard in the KeyboardInterrupt handler must
    prevent _flush_manifest from being called, so no details.json is written
    even when the run is interrupted after processing at least one work.
    """
    _patch_downloads(monkeypatch)
    # Client yields one ugoira work then raises KeyboardInterrupt.
    works = {"public": [_make_work(300, work_type="ugoira"), _make_work(200, pages=1)]}
    client = FakePixivClient(works, raise_after=1, raise_exc=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        run(
            _config(tmp_path, dry_run=True),
            _auth(),
            client,
            clock=lambda: FIXED_CLOCK,
        )

    # Dry-run must not flush: no details.json regardless of interruption.
    assert not (tmp_path / "details.json").exists()
    assert list(tmp_path.iterdir()) == []


# -- skipped-file status ------------------------------------------------


def test_skipped_image_tallied_but_not_counted_as_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SKIPPED ImageResult is folded through _tally_status's skipped branch.

    When download_image returns ImageStatus.SKIPPED (skip_existing=True and the
    file already exists), the file must be counted in files_skipped, not in
    files_downloaded, and the work must still be recorded as succeeded.
    """
    _patch_downloads(
        monkeypatch,
        image_result=ImageResult(ImageStatus.SKIPPED, 10, None),
    )
    works = {"public": [_make_work(100, pages=1)]}
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path), client)

    assert summary.files_skipped == 1
    assert summary.files_downloaded == 0
    assert summary.works_succeeded == 1
    assert summary.works_failed == 0

    manifest = _read_manifest(tmp_path)
    statuses = [img["status"] for img in manifest["works"][0]["images"]]
    assert statuses == ["skipped"]


# -- metadata-only + ugoira ---------------------------------------------


def test_metadata_only_ugoira_records_entry_without_building_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """metadata_only=True with a ugoira work must not call build_ugoira.

    The `if config.metadata_only:` branch inside the ugoira arm of
    _image_entries_for_work assigns METADATA_ONLY status without invoking
    build_ugoira, writes no GIF file, and records the entry in the manifest.
    """
    calls = _patch_downloads(monkeypatch)
    works = {"public": [_make_work(300, work_type="ugoira")]}
    client = FakePixivClient(works)

    summary = _run(_config(tmp_path, metadata_only=True), client)

    # build_ugoira must never be invoked.
    assert calls["ugoira"] == []
    assert calls["images"] == []
    # No GIF file on disk.
    assert not (tmp_path / "300_p0.gif").exists()
    assert summary.works_total == 1
    assert summary.works_succeeded == 1

    manifest = _read_manifest(tmp_path)
    images = manifest["works"][0]["images"]
    assert len(images) == 1
    assert images[0]["page"] == 0
    assert images[0]["path"] == "300_p0.gif"
    assert images[0]["status"] == "metadata-only"
