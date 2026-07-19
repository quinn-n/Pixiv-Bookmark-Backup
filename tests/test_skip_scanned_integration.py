"""End-to-end integration tests for the ``--skip-scanned`` feature.

These tests exercise the *fully wired* feature across module boundaries --
``backup.run`` / ``backup.run_following`` driving ``core`` (skip decision,
exit-code policy) and ``manifest`` (``load_or_init`` / ``merge`` carry-forward)
together -- rather than any one module in isolation. The per-module suites in
``test_backup.py`` / ``test_backup_following.py`` / ``test_core.py`` cover their
own module; here we prove the cross-module contract holds: a skipped work is
never re-fetched, its prior manifest entry survives the resume merge byte-for-
byte, and an all-skipped run exits ``SUCCESS`` (not ``NO_BOOKMARKS``).

Everything runs fully offline via the same fakes/helpers the sibling suites
use: a scripted ``FakePixivClient`` / ``FakeFollowingClient``, monkeypatched
``download_image`` / ``build_ugoira``, and a fixed manifest clock.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pixiv_bookmark_backup import backup, cli as cli_module
from pixiv_bookmark_backup.backup import run, run_following
from pixiv_bookmark_backup.cli import cli
from pixiv_bookmark_backup.config import ResolvedAuth
from pixiv_bookmark_backup.core import (
    BackupConfig,
    ExitCode,
    FollowingConfig,
    decide_exit_code,
)
from pixiv_bookmark_backup.manifest import Manifest, WorkEntry, load_or_init
from pixiv_bookmark_backup.models import (
    Author,
    BookmarkWork,
    ImageStatus,
    WorkImage,
)
from pixiv_bookmark_backup.pixiv_client import PixivClient

from test_backup import (
    FIXED_CLOCK,
    SELF_ID,
    TOKEN,
    FakePixivClient,
    _patch_downloads,
    _seed_manifest,
)
from test_backup_following import (
    FakeFollowingClient,
    _make_user,
)

# -- helpers ------------------------------------------------------------


def _bookmark_work(work_id: int, *, title: str = "Fresh Work") -> BookmarkWork:
    """A single-page bookmark work whose page URL encodes its id."""

    return BookmarkWork(
        id=work_id,
        type="illust",
        title=title,
        author=Author(id=7, name="Artist", account="artist_acct"),
        posted_at="2024-05-01T12:00:00+09:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=1,
        images=[WorkImage(page=0, source_url=f"https://i.pximg.net/{work_id}_p0.jpg")],
    )


def _following_work(work_id: int, *, author_id: int) -> BookmarkWork:
    """A single-page followed-user work authored by ``author_id``."""

    return BookmarkWork(
        id=work_id,
        type="illust",
        title="Fresh Work",
        author=Author(id=author_id, name="Artist", account=f"acct_{author_id}"),
        posted_at="2024-05-01T12:00:00+09:00",
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=1,
        images=[WorkImage(page=0, source_url=f"https://i.pximg.net/{work_id}_p0.jpg")],
    )


def _auth() -> ResolvedAuth:
    return ResolvedAuth(refresh_token=TOKEN)


def _load(output_dir: Path) -> Manifest:
    """Deserialize ``details.json`` through the real ``load_or_init`` path."""

    manifest, _mismatch = load_or_init(output_dir / "details.json")
    return manifest


def _entry(manifest: Manifest, work_id: int) -> WorkEntry:
    """Return the single ``WorkEntry`` with ``work_id`` (asserting uniqueness)."""

    matches = [work for work in manifest.works if work.id == work_id]
    assert len(matches) == 1, f"expected exactly one entry for {work_id}"
    return matches[0]


def _spy_image_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    """Record the ids of works actually sent through the per-work fetch.

    Wraps the real ``_image_entries_for_work`` (so ``C`` is still processed
    end-to-end) and captures ``work.id`` from the exact call-site argument
    position. This proves the *no re-fetch* contract directly, rather than
    inferring it from a download-call counter.
    """

    real = backup._image_entries_for_work
    fetched_ids: list[int] = []

    def spy(config: Any, client: Any, work: BookmarkWork, *args: Any, **kwargs: Any):
        fetched_ids.append(work.id)
        return real(config, client, work, *args, **kwargs)

    monkeypatch.setattr(backup, "_image_entries_for_work", spy)
    return fetched_ids


# -- crux 1: skip-scanned carry-forward via merge() ---------------------


def test_skip_scanned_carries_forward_prior_entry_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped work's prior manifest entry survives merge() untouched.

    The seeded manifest holds two clean works: ``A`` (id 100) which this run
    re-lists and skips, and ``B`` (id 300) which this run does *not* list at all
    (a pre-existing bookmark carried on disk from history). The fake client
    yields ``A`` (skippable) and ``C`` (id 200, brand new). After the run:

    * ``A``'s entry is byte-for-byte its prior entry -- never re-fetched, never
      re-appended, preserved by ``merge()``.
    * ``B``'s entry survives the plain un-listed carry-forward (independent of
      skip-scanned; see ``test_backup.test_resume_merges_with_prior_manifest``).
    * ``C`` is present with fresh data from this run.
    * The spy proves the per-work fetch ran for ``C`` but never for ``A``.
    """
    _patch_downloads(monkeypatch)
    _seed_manifest(tmp_path, [100, 300])
    before = _load(tmp_path)
    a_before = _entry(before, 100)
    b_before = _entry(before, 300)

    fetched_ids = _spy_image_entries(monkeypatch)
    client = FakePixivClient(
        {"public": [_bookmark_work(100), _bookmark_work(200, title="New C")]}
    )

    summary = run(
        BackupConfig(output_dir=tmp_path, skip_scanned=True),
        _auth(),
        client,
        clock=lambda: FIXED_CLOCK,
    )

    # Summary: A skipped (not processed), only C processed.
    assert summary.works_skipped == 1
    assert summary.works_total == 1

    # The per-work fetch ran for C and never for A: the no-refetch contract.
    assert fetched_ids == [200]
    # The personal bookmark-tag fetch is likewise skipped for A (own-account
    # run): it only fires for the non-skipped work C, sharing C's paced step.
    assert client.tag_fetch_calls == [200]

    after = _load(tmp_path)
    # (1) A's entry is identical to its pre-existing record (frozen ==).
    assert _entry(after, 100) == a_before
    # (2) B carried forward unchanged although it was never re-listed this run.
    assert _entry(after, 300) == b_before
    # (3) C is present with this run's fresh data.
    c_after = _entry(after, 200)
    assert c_after.title == "New C"
    assert c_after.images[0].status == ImageStatus.DOWNLOADED.value
    assert c_after.images[0].bytes_ == 10
    # No stray duplicates; exactly the three expected works.
    assert {work.id for work in after.works} == {100, 200, 300}


# -- crux 2: all-skipped run exits SUCCESS, not NO_BOOKMARKS -------------


def test_all_skipped_run_exits_success_and_preserves_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An all-skipped run is a success and carries every prior work forward.

    N clean works are pre-recorded; the fake client re-lists exactly those and
    nothing else. ``skip_scanned`` skips all of them, so ``works_total == 0``
    while ``works_skipped == N``. The load-bearing policy fix: this is
    ``ExitCode.SUCCESS`` end-to-end, *not* ``NO_BOOKMARKS`` -- proven through a
    real ``run()`` (not just ``core``'s truth table). The flushed manifest still
    holds all N prior entries, each ``WorkEntry`` untouched.
    """
    _patch_downloads(monkeypatch)
    prior_ids = [100, 200, 300]
    _seed_manifest(tmp_path, prior_ids)
    before = _load(tmp_path)
    entries_before = {wid: _entry(before, wid) for wid in prior_ids}

    fetched_ids = _spy_image_entries(monkeypatch)
    client = FakePixivClient({"public": [_bookmark_work(wid) for wid in prior_ids]})

    summary = run(
        BackupConfig(output_dir=tmp_path, skip_scanned=True),
        _auth(),
        client,
        clock=lambda: FIXED_CLOCK,
    )

    assert summary.works_total == 0
    assert summary.works_skipped == len(prior_ids)
    assert summary.works_failed == 0
    # The load-bearing fix: all-skipped is SUCCESS, not NO_BOOKMARKS.
    assert decide_exit_code(summary) is ExitCode.SUCCESS
    # Nothing was fetched at all.
    assert fetched_ids == []

    after = _load(tmp_path)
    assert {work.id for work in after.works} == set(prior_ids)
    # Each prior WorkEntry survived the flush untouched (merge writes a new
    # generated_at/source, so only the entries -- not the whole file -- are
    # asserted unchanged).
    for wid, entry_before in entries_before.items():
        assert _entry(after, wid) == entry_before


# -- crux 3: following-command carry-forward ----------------------------


def _seed_following_manifest(
    output_dir: Path,
    client: FakeFollowingClient,
) -> Manifest:
    """Seed a per-author following manifest via a real first ``run_following``.

    Following-mode manifests carry a distinct ``source`` block and nested
    ``<author_id>/<file>`` paths, so they are seeded through a genuine first run
    (the established pattern in ``test_backup_following.py``) rather than hand-
    built JSON.
    """

    run_following(
        FollowingConfig(output_dir=output_dir),
        _auth(),
        client,
        clock=lambda: FIXED_CLOCK,
    )
    return _load(output_dir)


def test_skip_scanned_following_carries_forward_prior_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped followed-user work's prior entry survives the resume merge.

    First run records user 10's work 101. The ``--skip-scanned`` re-run re-lists
    user 10 (work 101 -> skipped) and a new user 20 contributing work 201. The
    skipped user's prior entry and the following ``source`` block must survive
    ``merge()``, while user 20's new work is fetched and recorded under its
    author subdirectory.
    """
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    first = FakeFollowingClient(following, {"10": [_following_work(101, author_id=10)]})
    before = _seed_following_manifest(tmp_path, first)
    entry_101_before = _entry(before, 101)
    source_before = dict(before.source)

    fetched_ids = _spy_image_entries(monkeypatch)
    following2 = {"public": [_make_user(10), _make_user(20)]}
    works2 = {
        "10": [_following_work(101, author_id=10)],
        "20": [_following_work(201, author_id=20)],
    }
    client = FakeFollowingClient(following2, works2)

    summary = run_following(
        FollowingConfig(output_dir=tmp_path, skip_scanned=True),
        _auth(),
        client,
        clock=lambda: FIXED_CLOCK,
    )

    assert summary.works_skipped == 1
    assert summary.works_total == 1
    # Only the new user's work was fetched; the skipped one never was.
    assert fetched_ids == [201]

    after = _load(tmp_path)
    # The skipped user's prior entry survived merge() unchanged.
    assert _entry(after, 101) == entry_101_before
    # merge() always takes the new run's source block (new-wins, not a
    # preserve rule); it matches source_before here because both runs share
    # the same self_id/follow_visibility, not because merge() carried it over.
    assert after.source == source_before
    assert after.source["mode"] == "following"
    # The new user's work is recorded under its author subdirectory.
    entry_201 = _entry(after, 201)
    assert entry_201.images[0].path == "20/201_p0.jpg"
    assert entry_201.images[0].status == ImageStatus.DOWNLOADED.value
    assert {work.id for work in after.works} == {101, 201}


# -- crux 4 (optional): full CLI-level double-run ------------------------


class _CliFakePixivClient:
    """Offline ``PixivClient`` for CLI runs: ``download`` writes real bytes.

    Reused across two CLI invocations (a normal seeding run, then a
    ``--skip-scanned`` re-run) by re-pointing ``works_by_restrict`` between
    invocations, so the second run lists the prior works plus one new one.
    """

    def __init__(self, works_by_restrict: dict[str, list[BookmarkWork]]) -> None:
        self.works_by_restrict = works_by_restrict
        self.download_calls: list[str] = []

    def authenticate(self, refresh_token: str) -> str:
        return SELF_ID

    def iter_bookmarks(self, user_id: str, restrict: str) -> Iterator[BookmarkWork]:
        yield from self.works_by_restrict.get(restrict, [])

    def iter_following(  # pragma: no cover
        self, user_id: str, restrict: str
    ) -> Iterator[Any]:
        raise NotImplementedError

    def iter_user_works(  # pragma: no cover
        self, user_id: str
    ) -> Iterator[BookmarkWork]:
        raise NotImplementedError

    def ugoira_metadata(self, work_id: int) -> Any:  # pragma: no cover
        raise NotImplementedError

    def download(
        self, url: str, dest_path: Path, referer: str = "https://www.pixiv.net/"
    ) -> int:
        self.download_calls.append(url)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        data = b"fake-image-bytes"
        dest_path.write_bytes(data)
        return len(data)

    def fetch_bookmark_tags(self, illust_id: int) -> list[str] | None:
        return None


# Static guarantee the CLI fake satisfies the public Protocol.
_: type[PixivClient] = _CliFakePixivClient


def test_cli_skip_scanned_double_run_reports_skips_and_exits_zero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real CLI double-run: seed, then ``--skip-scanned`` skips + adds one new.

    Drives the actual ``backup`` command through ``CliRunner`` against a real
    filesystem: a first plain run writes ``details.json``, then a second
    ``--skip-scanned --json`` run over the same works plus one new one reports
    ``works_skipped == 1`` in its JSON payload and exits 0.
    """
    out = tmp_path / "out"
    client = _CliFakePixivClient({"public": [_bookmark_work(100)]})
    monkeypatch.setattr(cli_module, "Pixivpy3Client", lambda *args, **kwargs: client)
    config_path = str(tmp_path / "nope.toml")
    env = {"PIXIV_REFRESH_TOKEN": "fake-x"}

    first = runner.invoke(
        cli,
        ["backup", "--output-dir", str(out), "--config", config_path],
        env=env,
    )
    assert first.exit_code == 0, first.output
    assert (out / "details.json").exists()

    client.download_calls.clear()
    client.works_by_restrict = {
        "public": [_bookmark_work(100), _bookmark_work(200, title="New C")]
    }

    second = runner.invoke(
        cli,
        [
            "backup",
            "--output-dir",
            str(out),
            "--skip-scanned",
            "--json",
            "--config",
            config_path,
        ],
        env=env,
    )

    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["works_skipped"] == 1
    assert payload["works_total"] == 1
    # Only the new work was actually downloaded on the re-run.
    assert client.download_calls == ["https://i.pximg.net/200_p0.jpg"]

    manifest = json.loads((out / "details.json").read_text(encoding="utf-8"))
    assert {work["id"] for work in manifest["works"]} == {100, 200}
