"""Read-only process scaling and an opt-in concurrent write failure probe."""

import json
from pathlib import Path

import pytest

from ._processes import WorkerResult, process_pool, run_workers
from ._support import WARMUP_ROUNDS, Entries, write_database


def _verify_reads(
    results: list[WorkerResult], chunks: list[Entries], key_count: int
) -> None:
    assert len(results) == len(chunks)
    assert len({result.pid for result in results}) == len(chunks)
    assert [result.values for result in results] == [
        [value for _, value in chunk] for chunk in chunks
    ]
    assert all(result.key_count == key_count for result in results)


@pytest.mark.parametrize("mode", ["load-and-read", "resident-read"])
@pytest.mark.benchmark(group="seriousdb-process-reads")
def test_process_reads(
    benchmark,
    database_file: Path,
    entries: Entries,
    measured_rounds: int,
    processes: int,
    mode: str,
) -> None:
    write_database(database_file, entries)
    original = database_file.read_bytes()
    chunks = [entries[index::processes] for index in range(processes)]
    results: list[WorkerResult] = []
    benchmark.extra_info.update(
        workers=processes,
        concurrency="spawned processes with independent caches, shared file",
        start_method="spawn",
        file_bytes=len(original),
        reads=len(entries),
        loads_per_round=processes if mode == "load-and-read" else 0,
        workload="fixed total reads split across processes",
        cache_state="fresh Cache per round" if mode == "load-and-read" else "resident",
        timing="dispatch, synchronization, cache operations and result IPC; excludes startup",
    )

    with process_pool(processes) as pool:
        # Wait for every child to start (and optionally load) outside timing.
        run_workers(
            pool,
            database_file,
            chunks,
            "preload" if mode == "resident-read" else "ready",
        )

        def read():
            nonlocal results
            results = run_workers(pool, database_file, chunks, mode)

        def verify():
            _verify_reads(results, chunks, len(entries))
            assert database_file.read_bytes() == original

        benchmark.pedantic(
            read,
            teardown=verify,
            rounds=measured_rounds,
            warmup_rounds=WARMUP_ROUNDS,
        )
        # --benchmark-disable skips teardown.
        verify()


@pytest.mark.benchmark(group="seriousdb-process-writes-experimental")
def test_process_writes(
    benchmark,
    database_file: Path,
    entries: Entries,
    measured_rounds: int,
    processes: int,
    request,
) -> None:
    if not request.config.getoption("--multiprocess-writes"):
        pytest.skip(
            "opt in with --multiprocess-writes; concurrent writes may lose data"
        )

    updates = tuple((key, value[::-1]) for key, value in entries)
    chunks = [updates[index::processes] for index in range(processes)]
    results: list[WorkerResult] = []
    failures: list[str] = []
    benchmark.extra_info.update(
        workers=processes,
        concurrency="experimental shared-file writes from independent processes",
        start_method="spawn",
        writes=len(entries),
        flushes=min(processes, len(entries)),
        persistence="Cache.flush with file fsync and atomic replacement; concurrent writes unsupported",
        timing="load snapshots, synchronize, write, flush, read back locally and result IPC; excludes startup",
    )

    with process_pool(processes) as pool:
        run_workers(pool, database_file, chunks, "ready")

        def restore():
            # Replace directly: never load or repair a previous corrupt result.
            database_file.write_text(json.dumps(dict(entries)), encoding="utf-8")

        def write():
            nonlocal results
            results = run_workers(pool, database_file, chunks, "write")

        def verify():
            _verify_reads(results, chunks, len(entries))
            # Inspect raw JSON so Cache.load cannot hide corruption by repairing it.
            write_errors = [
                result.write_error for result in results if result.write_error
            ]
            if write_errors:
                if processes == 1:
                    pytest.fail(
                        "Single-process write failed: " + "; ".join(write_errors)
                    )
                failures.extend(write_errors)
            try:
                persisted = json.loads(database_file.read_bytes())
            except (ValueError, UnicodeDecodeError):
                failure = "invalid persisted JSON"
            else:
                failure = (
                    "lost or incorrect persisted updates"
                    if persisted != dict(updates)
                    else ""
                )
            if failure:
                if processes == 1:
                    pytest.fail(f"Single-process write failed: {failure}")
                failures.append(failure)

        benchmark.pedantic(
            write,
            setup=restore,
            teardown=verify,
            rounds=measured_rounds,
            warmup_rounds=WARMUP_ROUNDS,
        )
        verify()

    benchmark.extra_info.update(
        correctness="failed" if failures else "passed",
        persistence_failures=sorted(set(failures)),
        file_bytes=database_file.stat().st_size,
    )
    if failures:
        pytest.xfail(
            "Concurrent writes are unsupported: " + "; ".join(sorted(set(failures)))
        )
