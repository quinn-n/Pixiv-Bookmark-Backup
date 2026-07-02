"""Unit tests for the strict, read-only manifest loader used by ``search``."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from pixiv_bookmark_backup.manifest import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    ManifestLoadError,
    load_manifest_strict,
    resolve_manifest_path,
)


def _valid_manifest_dict() -> dict[str, Any]:
    """Return a well-formed manifest ``dict`` with a single work and image."""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2023-01-02T03:04:05+09:00",
        "source": {"user_id": "42", "visibility": "public"},
        "works": [
            {
                "id": 123,
                "type": "illust",
                "title": "A work",
                "author": {"id": 7, "name": "Artist", "account": "artist"},
                "posted_at": "2023-01-02T03:04:05+09:00",
                "nsfw": {"x_restrict": 0, "sanity_level": 2, "is_nsfw": False},
                "work_tags": [{"name": "original", "translated_name": None}],
                "bookmark_tags": ["fav"],
                "page_count": 1,
                "images": [
                    {
                        "page": 0,
                        "source_url": "https://example.test/0.png",
                        "path": "123_p0.png",
                        "status": "downloaded",
                        "bytes": 1024,
                        "error": None,
                    }
                ],
            }
        ],
        "errors": [],
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_manifest_name_constant() -> None:
    assert MANIFEST_NAME == "details.json"


def test_resolve_manifest_path_existing_dir(tmp_path: Path) -> None:
    assert resolve_manifest_path(tmp_path) == tmp_path / MANIFEST_NAME


def test_resolve_manifest_path_existing_file(tmp_path: Path) -> None:
    file_path = tmp_path / "details.json"
    file_path.write_text("{}", encoding="utf-8")
    assert resolve_manifest_path(file_path) == file_path


def test_resolve_manifest_path_nonexistent_unchanged(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "details.json"
    assert resolve_manifest_path(missing) == missing


def test_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "details.json"
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(missing)
    message = str(exc_info.value)
    assert str(missing) in message
    assert "run a backup" in message


def test_malformed_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "details.json"
    bad.write_text("{ bad", encoding="utf-8")
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(bad)
    assert "not valid JSON" in str(exc_info.value)


def test_deeply_nested_json_raises(tmp_path: Path) -> None:
    # CPython's JSON decoder recurses; a deeply-nested document raises
    # RecursionError, which must be mapped to a clean ManifestLoadError rather
    # than surfacing as a raw traceback. json bails around depth 1000, so this
    # is fast and deterministic.
    path = tmp_path / "details.json"
    path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(path)
    assert "too deeply nested" in str(exc_info.value)


@pytest.mark.parametrize("payload", ["[]", '"foo"', "null", "42"])
def test_non_object_json_raises(tmp_path: Path, payload: str) -> None:
    # Valid JSON that is not an object must still be an explicit error, not a
    # raw AttributeError from calling .get on a list/str/int/None.
    path = tmp_path / "details.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(path)
    assert "not a manifest object" in str(exc_info.value)


def test_schema_mismatch_raises(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    data["schema_version"] = 999
    path = tmp_path / "details.json"
    _write_json(path, data)
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(path)
    assert str(SCHEMA_VERSION) in str(exc_info.value)


def test_schema_missing_raises(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    del data["schema_version"]
    path = tmp_path / "details.json"
    _write_json(path, data)
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(path)
    assert str(SCHEMA_VERSION) in str(exc_info.value)


def test_structurally_invalid_raises(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    # A work missing the required "title" key -> KeyError inside from_dict.
    del data["works"][0]["title"]
    path = tmp_path / "details.json"
    _write_json(path, data)
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(path)
    assert "structurally invalid" in str(exc_info.value)


def test_structurally_invalid_value_error_raises(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    # "author" as a list containing a 3-element tuple -> dict([(1, 2, 3)])
    # raises ValueError ("dictionary update sequence element #0 has length 3;
    # 2 is required"), not KeyError/TypeError; the widened guard must still map
    # it to a load error.
    data["works"][0]["author"] = [(1, 2, 3)]
    path = tmp_path / "details.json"
    _write_json(path, data)
    with pytest.raises(ManifestLoadError) as exc_info:
        load_manifest_strict(path)
    assert "structurally invalid" in str(exc_info.value)


def test_happy_path_returns_manifest(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    path = tmp_path / "details.json"
    _write_json(path, data)

    manifest = load_manifest_strict(path)

    assert manifest.schema_version == SCHEMA_VERSION
    assert len(manifest.works) == 1
    assert manifest.works[0].id == 123
    assert manifest.works[0].title == "A work"


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file permissions not enforced on Windows"
)
def test_unreadable_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "details.json"
    _write_json(path, _valid_manifest_dict())
    path.chmod(0)
    # Root ignores read permission bits; skip rather than assert a false result.
    if os.access(path, os.R_OK):
        pytest.skip("read permission not enforceable in this environment")
    try:
        with pytest.raises(ManifestLoadError) as exc_info:
            load_manifest_strict(path)
        assert "could not read" in str(exc_info.value)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
