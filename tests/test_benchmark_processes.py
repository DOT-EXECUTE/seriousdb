"""Exercise benchmark workers with real spawned processes and isolated files."""

import json
import os
from threading import Barrier

import pytest

from benchmarks import _processes
from benchmarks._processes import process_pool, run_workers
from benchmarks._support import make_entries, write_database
from seriousdb.exceptions import ResourceNotFoundError


@pytest.mark.parametrize("mode", ["load-and-read", "resident-read"])
def test_process_reads_use_distinct_children_and_preserve_file(tmp_path, mode):
    path = tmp_path / "database.json"
    entries = make_entries(11, 32)
    chunks = [entries[index::2] for index in range(2)]
    write_database(path, entries)
    original = path.read_bytes()

    with process_pool(2) as pool:
        run_workers(
            pool, path, chunks, "preload" if mode == "resident-read" else "ready"
        )
        for _ in range(2):
            results = run_workers(pool, path, chunks, mode)
            assert len({result.pid for result in results}) == 2
            assert all(result.pid != os.getpid() for result in results)
            assert [result.values for result in results] == [
                [value for _, value in chunk] for chunk in chunks
            ]
            assert all(result.key_count == len(entries) for result in results)

    assert path.read_bytes() == original


def test_resident_reads_keep_cache_while_cold_reads_reload(tmp_path):
    path = tmp_path / "database.json"
    entries = (("key", "before"),)
    write_database(path, entries)

    with process_pool(1) as pool:
        run_workers(pool, path, [entries], "preload")
        write_database(path, (("key", "after"),))
        assert run_workers(pool, path, [entries], "resident-read")[0].values == [
            "before"
        ]
        assert run_workers(pool, path, [entries], "load-and-read")[0].values == [
            "after"
        ]


def test_worker_errors_reach_parent_and_pool_cleans_up(tmp_path):
    path = tmp_path / "database.json"
    write_database(path, (("present", "value"),))

    with process_pool(2) as pool:
        children = list(pool._pool)
        with pytest.raises(ResourceNotFoundError):
            run_workers(pool, path, [(("missing", "value"),)] * 2, "load-and-read")

    assert all(not child.is_alive() for child in children)


def test_single_process_write_probe_persists_all_updates(tmp_path):
    path = tmp_path / "database.json"
    entries = make_entries(10, 32)
    updates = tuple((key, value[::-1]) for key, value in entries)
    chunks = [updates]
    write_database(path, entries)

    with process_pool(1) as pool:
        results = run_workers(pool, path, chunks, "write")
        assert [result.values for result in results] == [
            [value for _, value in chunk] for chunk in chunks
        ]

    assert json.loads(path.read_bytes()) == dict(updates)


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_write_worker_returns_windows_replacement_conflict(
    tmp_path, monkeypatch, winerror
):
    path = tmp_path / "database.json"
    entries = (("key", "before"),)
    updates = (("key", "after"),)
    write_database(path, entries)
    error = PermissionError(13, "Access denied", "temporary-file")
    error.filename2 = str(path)
    monkeypatch.setattr(error, "winerror", winerror, raising=False)

    def failed_flush(cache, changes, _frequency):
        for key, value in changes:
            cache.insert(key, value)
        raise error

    monkeypatch.setattr(_processes, "_start", Barrier(1), raising=False)
    monkeypatch.setattr(_processes, "_cache", None)
    monkeypatch.setattr(_processes, "write_entries", failed_flush)
    result = _processes._worker(str(path), updates, "write")
    assert result.values == ["after"]
    assert result.key_count == 1
    assert result.write_error == f"atomic replacement conflict (WinError {winerror})"


@pytest.mark.parametrize("fault", ["other-destination", "other-code", "no-winerror"])
def test_write_worker_propagates_other_permission_errors(tmp_path, monkeypatch, fault):
    path = tmp_path / "database.json"
    entries = (("key", "value"),)
    write_database(path, entries)
    error = PermissionError(13, "Access denied", "temporary-file")
    error.filename2 = "other-file" if fault == "other-destination" else str(path)
    if fault != "no-winerror":
        monkeypatch.setattr(
            error, "winerror", 123 if fault == "other-code" else 5, raising=False
        )

    def failed_write(*_args):
        raise error

    monkeypatch.setattr(_processes, "_start", Barrier(1), raising=False)
    monkeypatch.setattr(_processes, "_cache", None)
    monkeypatch.setattr(_processes, "write_entries", failed_write)
    with pytest.raises(PermissionError):
        _processes._worker(str(path), entries, "write")
