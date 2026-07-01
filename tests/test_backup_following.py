"""Offline, deterministic tests for the follow-backup orchestrator.

Every test runs fully offline: a hand-rolled :class:`FakeFollowingClient` scripts
the Pixiv surface (a fixed self id, scripted followed users per ``restrict`` pass,
scripted ``BookmarkWork`` lists per followed user, and optional ``ApiError`` /
``KeyboardInterrupt`` / ``AuthRejected`` raised at a chosen point), and the
per-file outcomes are controlled by monkeypatching ``backup.download_image`` and
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
from pixiv_bookmark_backup.backup import run_following
from pixiv_bookmark_backup.config import ResolvedAuth
from pixiv_bookmark_backup.core import (
    ExitCode,
    FollowingConfig,
    FollowingRunSummary,
    decide_exit_code,
)
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
    AuthRejected,
    FollowedUser,
    PixivClient,
)
from pixiv_bookmark_backup.ugoira import UgoiraResult

FIXED_CLOCK = "2026-06-24T00:00:00+00:00"
SELF_ID = "555"
TOKEN = "refresh-token-value"


# -- fakes & helpers ----------------------------------------------------


def _make_user(user_id: int, *, name: str = "Creator") -> FollowedUser:
    """Build a :class:`FollowedUser` with a deterministic account handle."""

    return FollowedUser(id=user_id, name=name, account=f"acct_{user_id}")


def _make_work(
    work_id: int,
    *,
    author_id: int,
    work_type: str = "illust",
    pages: int = 1,
    posted_at: str = "2024-05-01T12:00:00+09:00",
    title: str = "A Work",
) -> BookmarkWork:
    """Build a :class:`BookmarkWork` authored by ``author_id``.

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
        author=Author(id=author_id, name="Artist", account=f"acct_{author_id}"),
        posted_at=posted_at,
        x_restrict=0,
        sanity_level=2,
        work_tags=[],
        bookmark_tags=None,
        page_count=max(pages, 1),
        images=images,
    )


class FakeFollowingClient:
    """Scriptable, fully offline stand-in for the ``PixivClient`` Protocol.

    ``authenticate`` returns a fixed self id (or raises ``auth_raise``).
    ``iter_following`` yields the followed users scripted for the requested
    ``restrict`` pass and records the call; it can be told to raise after
    yielding a chosen number of users. ``iter_user_works`` yields the works
    scripted for the requested user and records the call; per-user it can be told
    to raise a given exception after yielding a chosen number of works (to model a
    mid-listing failure / Ctrl-C). The ``ugoira_metadata`` / ``download`` methods
    are never reached because the tests monkeypatch ``download_image`` /
    ``build_ugoira``.
    """

    def __init__(
        self,
        following_by_restrict: dict[str, list[FollowedUser]],
        works_by_user: dict[str, list[BookmarkWork]],
        *,
        self_id: str = SELF_ID,
        auth_raise: BaseException | None = None,
        following_raise_after: int | None = None,
        following_raise_exc: BaseException | None = None,
        user_raises: dict[str, tuple[int, BaseException]] | None = None,
    ) -> None:
        self._following_by_restrict = following_by_restrict
        self._works_by_user = works_by_user
        self._self_id = self_id
        self._auth_raise = auth_raise
        self._following_raise_after = following_raise_after
        self._following_raise_exc = following_raise_exc
        self._user_raises = user_raises or {}
        self.auth_calls: list[str] = []
        self.following_calls: list[tuple[str, str]] = []
        self.works_calls: list[str] = []

    def authenticate(self, refresh_token: str) -> str:
        self.auth_calls.append(refresh_token)
        if self._auth_raise is not None:
            raise self._auth_raise
        return self._self_id

    def iter_following(self, user_id: str, restrict: str) -> Iterator[FollowedUser]:
        self.following_calls.append((user_id, restrict))
        yielded = 0
        for user in self._following_by_restrict.get(restrict, []):
            if (
                self._following_raise_after is not None
                and yielded >= self._following_raise_after
            ):
                raise self._raise_following()
            yield user
            yielded += 1
        if (
            self._following_raise_after is not None
            and yielded >= self._following_raise_after
        ):
            raise self._raise_following()

    def _raise_following(self) -> BaseException:
        if self._following_raise_exc is not None:
            return self._following_raise_exc
        return ApiError("listing following failed")

    def iter_user_works(self, user_id: str) -> Iterator[BookmarkWork]:
        self.works_calls.append(user_id)
        raise_spec = self._user_raises.get(user_id)
        yielded = 0
        for work in self._works_by_user.get(user_id, []):
            if raise_spec is not None and yielded >= raise_spec[0]:
                raise raise_spec[1]
            yield work
            yielded += 1
        if raise_spec is not None and yielded >= raise_spec[0]:
            raise raise_spec[1]

    def iter_bookmarks(  # pragma: no cover
        self, user_id: str, restrict: str
    ) -> Iterator[BookmarkWork]:
        raise NotImplementedError

    def ugoira_metadata(self, work_id: int) -> UgoiraInfo:  # pragma: no cover
        raise NotImplementedError

    def download(  # pragma: no cover
        self, url: str, dest_path: Path, referer: str = "https://www.pixiv.net/"
    ) -> int:
        raise NotImplementedError


# Static guarantee the fake satisfies the public Protocol.
_: type[PixivClient] = FakeFollowingClient


def _config(output_dir: Path, **overrides: object) -> FollowingConfig:
    """Build a :class:`FollowingConfig` rooted at ``output_dir`` with overrides."""

    params: dict[str, object] = {"output_dir": output_dir}
    params.update(overrides)
    return FollowingConfig(**params)  # type: ignore[arg-type]


def _auth() -> ResolvedAuth:
    return ResolvedAuth(refresh_token=TOKEN)


def _patch_downloads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    image_result: ImageResult | None = None,
    image_results: dict[str, ImageResult] | None = None,
    ugoira_result: UgoiraResult | None = None,
) -> dict[str, list[Any]]:
    """Replace ``download_image`` / ``build_ugoira`` with scripted stand-ins.

    Returns a ``{"images": [...], "ugoira": [...]}`` record of the calls made, so
    a test can assert whether (and how often) a real download would have run, and
    with which destination path. ``image_results`` keys by URL for per-file
    control; ``image_result`` is the fallback (a successful 10-byte download).
    """

    calls: dict[str, list[Any]] = {"images": [], "ugoira": []}
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
    """Return the parsed ``details.json`` (typed ``Any`` for free subscripting)."""

    return json.loads((output_dir / "details.json").read_text(encoding="utf-8"))


def _run(config: FollowingConfig, client: FakeFollowingClient) -> FollowingRunSummary:
    return run_following(config, _auth(), client, clock=lambda: FIXED_CLOCK)


# -- success / layout ---------------------------------------------------


def test_two_users_per_author_subdirs_and_root_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20)]}
    works = {
        "10": [_make_work(101, author_id=10)],
        "20": [_make_work(201, author_id=20, pages=2)],
    }
    client = FakeFollowingClient(following, works)

    summary = _run(_config(tmp_path), client)

    assert summary.users_total == 2
    assert summary.users_with_works == 2
    assert summary.works_total == 2  # one work each; user 20's has two pages
    assert summary.works_succeeded == 2
    assert summary.files_downloaded == 3
    assert summary.following_of == SELF_ID
    assert decide_exit_code(summary) is ExitCode.SUCCESS

    # Files written under per-author subdirectories; downloads targeted them.
    dests = {str(dest) for _url, dest in calls["images"]}
    assert str(tmp_path / "10" / "101_p0.jpg") in dests
    assert str(tmp_path / "20" / "201_p0.jpg") in dests
    assert str(tmp_path / "20" / "201_p1.jpg") in dests

    # Single aggregate manifest at the output root, following-shaped source.
    manifest = _read_manifest(tmp_path)
    assert manifest["source"] == {
        "mode": "following",
        "following_of": SELF_ID,
        "follow_visibility": "public",
    }
    paths = {img["path"] for work in manifest["works"] for img in work["images"]}
    assert paths == {"10/101_p0.jpg", "20/201_p0.jpg", "20/201_p1.jpg"}


def test_mixed_kinds_single_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    works = {
        "10": [
            _make_work(1, author_id=10, work_type="illust"),
            _make_work(2, author_id=10, work_type="manga", pages=2),
            _make_work(3, author_id=10, work_type="ugoira"),
        ]
    }
    client = FakeFollowingClient(following, works)

    summary = _run(_config(tmp_path), client)

    assert summary.works_total == 3
    assert summary.works_succeeded == 3
    # The ugoira lands as <author_id>/<id>_p0.gif via build_ugoira.
    assert calls["ugoira"] == [tmp_path / "10" / "3_p0.gif"]
    manifest = _read_manifest(tmp_path)
    by_id = {work["id"]: work for work in manifest["works"]}
    assert by_id[3]["type"] == "ugoira"
    assert by_id[3]["images"][0]["path"] == "10/3_p0.gif"
    assert by_id[2]["type"] == "manga"


# -- follow passes ------------------------------------------------------


def test_follows_public_single_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakeFollowingClient({"public": [_make_user(10)]}, {"10": []})
    _run(_config(tmp_path, follows="public"), client)
    assert [restrict for _uid, restrict in client.following_calls] == ["public"]


def test_follows_private_single_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakeFollowingClient({"private": [_make_user(10)]}, {"10": []})
    _run(_config(tmp_path, follows="private"), client)
    assert [restrict for _uid, restrict in client.following_calls] == ["private"]


def test_follows_all_two_passes_dedups_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # User 10 appears in BOTH passes; it must be processed only once.
    following = {
        "public": [_make_user(10), _make_user(20)],
        "private": [_make_user(10), _make_user(30)],
    }
    works = {
        "10": [_make_work(1, author_id=10)],
        "20": [_make_work(2, author_id=20)],
        "30": [_make_work(3, author_id=30)],
    }
    client = FakeFollowingClient(following, works)

    summary = _run(_config(tmp_path, follows="all"), client)

    assert [restrict for _uid, restrict in client.following_calls] == [
        "public",
        "private",
    ]
    assert summary.users_total == 3  # 10 counted once
    assert client.works_calls.count("10") == 1
    assert summary.works_total == 3


# -- nothing found ------------------------------------------------------


def test_empty_follow_list_exit_no_bookmarks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakeFollowingClient({"public": []}, {})
    summary = _run(_config(tmp_path), client)
    assert summary.users_total == 0
    assert summary.works_total == 0
    assert decide_exit_code(summary) is ExitCode.NO_BOOKMARKS
    # Manifest is still written (empty) for a completed non-dry run.
    assert (tmp_path / "details.json").exists()
    assert _read_manifest(tmp_path)["works"] == []


def test_followed_users_all_empty_exit_no_bookmarks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20)]}
    client = FakeFollowingClient(following, {"10": [], "20": []})
    summary = _run(_config(tmp_path), client)
    assert summary.users_total == 2
    assert summary.users_with_works == 0
    assert summary.works_total == 0
    assert decide_exit_code(summary) is ExitCode.NO_BOOKMARKS


# -- limits & filters ---------------------------------------------------


def test_limit_caps_total_works_across_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20)]}
    works = {
        "10": [_make_work(1, author_id=10), _make_work(2, author_id=10)],
        "20": [_make_work(3, author_id=20)],
    }
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, limit=1), client)
    assert summary.works_total == 1
    # The second user is never listed once the global limit is hit.
    assert client.works_calls == ["10"]


def test_limit_exact_budget_does_not_list_extra_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # limit equals user 10's work count exactly: once the budget is spent the
    # next user must not be counted or listed (no spurious iter_user_works call).
    following = {"public": [_make_user(10), _make_user(20)]}
    works = {
        "10": [_make_work(1, author_id=10), _make_work(2, author_id=10)],
        "20": [_make_work(3, author_id=20)],
    }
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, limit=2), client)
    assert summary.works_total == 2
    assert summary.users_total == 1  # user 20 is never counted
    assert client.works_calls == ["10"]  # user 20 is never listed


def test_per_user_limit_counts_after_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # User 10 has 4 works; one is dropped by --since, leaving 3 eligible. With
    # per_user_limit=2 only the first two eligible works are kept: the
    # since-dropped one does not consume the per-user budget, and the cap breaks
    # before the 4th eligible work.
    works = {
        "10": [
            _make_work(1, author_id=10, posted_at="2020-01-01T00:00:00+09:00"),
            _make_work(2, author_id=10, posted_at="2024-05-01T00:00:00+09:00"),
            _make_work(3, author_id=10, posted_at="2024-06-01T00:00:00+09:00"),
            _make_work(4, author_id=10, posted_at="2024-07-01T00:00:00+09:00"),
        ]
    }
    client = FakeFollowingClient({"public": [_make_user(10)]}, works)
    summary = _run(_config(tmp_path, per_user_limit=2, since=date(2023, 1, 1)), client)
    assert summary.works_total == 2
    ids = {work["id"] for work in _read_manifest(tmp_path)["works"]}
    assert ids == {2, 3}


def test_max_users_stops_after_n(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20), _make_user(30)]}
    works = {
        "10": [_make_work(1, author_id=10)],
        "20": [_make_work(2, author_id=20)],
        "30": [_make_work(3, author_id=30)],
    }
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, max_users=2), client)
    assert summary.users_total == 2
    assert client.works_calls == ["10", "20"]  # user 30 never listed


def test_only_restricts_to_listed_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20)]}
    works = {"10": [_make_work(1, author_id=10)], "20": [_make_work(2, author_id=20)]}
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, only=("20",)), client)
    assert client.works_calls == ["20"]
    assert summary.users_total == 1


def test_exclude_skips_listed_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20)]}
    works = {"10": [_make_work(1, author_id=10)], "20": [_make_work(2, author_id=20)]}
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, exclude=("10",)), client)
    assert client.works_calls == ["20"]
    assert summary.users_total == 1


def test_since_filters_old_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    works = {
        "10": [
            _make_work(1, author_id=10, posted_at="2020-01-01T00:00:00+09:00"),
            _make_work(2, author_id=10, posted_at="2024-05-01T00:00:00+09:00"),
        ]
    }
    client = FakeFollowingClient({"public": [_make_user(10)]}, works)
    summary = _run(_config(tmp_path, since=date(2023, 1, 1)), client)
    assert summary.works_total == 1
    assert {w["id"] for w in _read_manifest(tmp_path)["works"]} == {2}


# -- dry-run & metadata-only -------------------------------------------


def test_dry_run_writes_nothing_but_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    works = {"10": [_make_work(1, author_id=10, pages=2)]}
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, dry_run=True), client)
    assert summary.works_total == 1
    assert summary.files_downloaded == 2
    assert summary.users_with_works == 1
    assert calls["images"] == []  # no real download attempted
    assert not (tmp_path / "details.json").exists()  # nothing written


def test_metadata_only_records_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    works = {"10": [_make_work(1, author_id=10)]}
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path, metadata_only=True), client)
    assert summary.works_total == 1
    assert summary.files_downloaded == 0
    assert calls["images"] == []
    manifest = _read_manifest(tmp_path)
    img = manifest["works"][0]["images"][0]
    assert img["status"] == ImageStatus.METADATA_ONLY.value
    assert img["path"] == "10/1_p0.jpg"  # nested path even in metadata-only


# -- error contract -----------------------------------------------------


def test_auth_rejected_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakeFollowingClient(
        {"public": []}, {}, auth_raise=AuthRejected("bad token")
    )
    with pytest.raises(AuthRejected):
        _run(_config(tmp_path), client)


def test_follow_list_apierror_before_any_work_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    client = FakeFollowingClient(
        {"public": []}, {}, following_raise_after=0, following_raise_exc=ApiError("x")
    )
    with pytest.raises(ApiError):
        _run(_config(tmp_path), client)
    assert not (tmp_path / "details.json").exists()


def test_first_user_works_apierror_before_any_work_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    client = FakeFollowingClient(
        following, {"10": []}, user_raises={"10": (0, ApiError("listing failed"))}
    )
    with pytest.raises(ApiError):
        _run(_config(tmp_path), client)
    assert not (tmp_path / "details.json").exists()


def test_mid_run_per_user_apierror_degrades_to_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10), _make_user(20), _make_user(30)]}
    works = {
        "10": [_make_work(1, author_id=10)],
        "20": [_make_work(2, author_id=20)],
        "30": [_make_work(3, author_id=30)],
    }
    # User 20's listing fails after some work already recorded (user 10's).
    client = FakeFollowingClient(
        following, works, user_raises={"20": (0, ApiError("user 20 down"))}
    )
    summary = _run(_config(tmp_path), client)
    assert summary.works_failed >= 1
    assert decide_exit_code(summary) is ExitCode.PARTIAL
    manifest = _read_manifest(tmp_path)
    backed_up = {work["id"] for work in manifest["works"]}
    assert backed_up == {1, 3}  # users 10 and 30 still backed up
    assert any("user 20" in err.get("reason", "") for err in manifest["errors"])


def test_follow_list_apierror_after_some_works_degrades_to_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # The follow-list pagination itself fails after yielding one user whose work
    # was already backed up -> degrade to a partial run (not a clean exit 4).
    following = {"public": [_make_user(10), _make_user(20)]}
    works = {"10": [_make_work(1, author_id=10)], "20": [_make_work(2, author_id=20)]}
    client = FakeFollowingClient(
        following,
        works,
        following_raise_after=1,
        following_raise_exc=ApiError("follow list paging failed"),
    )
    summary = _run(_config(tmp_path), client)
    assert summary.works_total == 1
    assert summary.works_failed >= 1
    assert decide_exit_code(summary) is ExitCode.PARTIAL
    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {1}
    assert any(
        "listing interrupted" in err.get("reason", "") for err in manifest["errors"]
    )


def test_corrupt_manifest_is_backed_up_and_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # A pre-existing manifest with a mismatched schema is moved aside to .bak and
    # the run starts fresh rather than crashing.
    (tmp_path / "details.json").write_text(
        json.dumps({"schema_version": 999, "generated_at": "", "source": {}}),
        encoding="utf-8",
    )
    client = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(1, author_id=10)]}
    )
    _run(_config(tmp_path), client)
    assert (tmp_path / "details.json.bak").exists()
    manifest = _read_manifest(tmp_path)
    assert manifest["schema_version"] == 1
    assert {work["id"] for work in manifest["works"]} == {1}


def test_image_failure_marks_work_failed_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(
        monkeypatch,
        image_result=ImageResult(ImageStatus.FAILED, None, "boom"),
    )
    following = {"public": [_make_user(10)]}
    works = {"10": [_make_work(1, author_id=10)]}
    client = FakeFollowingClient(following, works)
    summary = _run(_config(tmp_path), client)
    assert summary.works_failed == 1
    assert decide_exit_code(summary) is ExitCode.PARTIAL


def test_keyboard_interrupt_flushes_partial_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    works = {"10": [_make_work(1, author_id=10), _make_work(2, author_id=10)]}
    # Interrupt after the first work of user 10 is yielded/processed.
    client = FakeFollowingClient(
        following, works, user_raises={"10": (1, KeyboardInterrupt())}
    )
    with pytest.raises(KeyboardInterrupt):
        _run(_config(tmp_path), client)
    # The work backed up before the interrupt is persisted.
    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {1}


def test_dry_run_keyboard_interrupt_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    works = {"10": [_make_work(1, author_id=10), _make_work(2, author_id=10)]}
    client = FakeFollowingClient(
        following, works, user_raises={"10": (1, KeyboardInterrupt())}
    )
    with pytest.raises(KeyboardInterrupt):
        _run(_config(tmp_path, dry_run=True), client)
    assert not (tmp_path / "details.json").exists()


# -- resume & unavailable works ----------------------------------------


def test_resume_merges_with_existing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    # First run backs up user 10's work 1.
    client1 = FakeFollowingClient(
        {"public": [_make_user(10)]}, {"10": [_make_work(1, author_id=10)]}
    )
    _run(_config(tmp_path), client1)
    # Second run adds user 20's work 2; user 10's prior work must be retained.
    client2 = FakeFollowingClient(
        {"public": [_make_user(20)]}, {"20": [_make_work(2, author_id=20)]}
    )
    _run(_config(tmp_path), client2)

    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {1, 2}
    assert manifest["source"]["mode"] == "following"


def test_unavailable_work_recorded_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_downloads(monkeypatch)
    following = {"public": [_make_user(10)]}
    # A non-ugoira work with no downloadable pages.
    unavailable = _make_work(1, author_id=10, pages=0)
    good = _make_work(2, author_id=10)
    client = FakeFollowingClient(following, {"10": [unavailable, good]})
    summary = _run(_config(tmp_path, per_user_limit=5), client)
    # Only the good work is counted; the unavailable one is an error entry.
    assert summary.works_total == 1
    manifest = _read_manifest(tmp_path)
    assert {work["id"] for work in manifest["works"]} == {2}
    assert any(err.get("work_id") == 1 for err in manifest["errors"])
