"""Fault injection for benchmark checks, without timing or optional plugins."""

from inspect import signature

import pytest

from benchmarks import test_concurrency, test_persistence, test_reads, test_workloads
from benchmarks._support import load_cache, make_entries, write_database
from seriousdb.cache import Cache


class Rounds:
    """Exercise pedantic callbacks, including disabled mode's skipped teardown."""

    def __init__(self, disabled):
        self.disabled = disabled
        self.extra_info = {}

    def pedantic(
        self, target, args=(), setup=None, teardown=None, rounds=1, warmup_rounds=0
    ):
        for _ in range(1 if self.disabled else warmup_rounds + rounds):
            if setup:
                setup()
            result = target(*args)
            if teardown and not self.disabled:
                teardown(*args)
        return result


@pytest.mark.parametrize(
    ("bad_call", "disabled"), [(1, False), (2, False), (3, False), (1, True)]
)
@pytest.mark.parametrize(
    ("module", "scenario", "operation"),
    [
        (test_reads, "test_load_and_read_by_key", "load_and_read"),
        (test_reads, "test_load_file", "load_cache"),
        (test_reads, "test_resident_read", "read_resident"),
        (test_workloads, "test_mixed_resident", "run_operations"),
        (test_concurrency, "test_shared_cache_threads", "run_operations"),
    ],
    ids=["load-and-read", "load-file", "resident-read", "mixed", "threads"],
)
def test_every_round_rejects_wrong_read_results(
    tmp_path, monkeypatch, module, scenario, operation, bad_call, disabled
):
    path = tmp_path / "benchmark.json"
    entries = make_entries(100, 32)
    write_database(path, entries)
    cache = load_cache(str(path))
    original = getattr(module, operation)
    calls = 0

    def faulty_operation(*args):
        nonlocal calls
        calls += 1
        result = original(*args)
        if calls == bad_call:
            if isinstance(result, Cache):
                result.insert(entries[0][0], "incorrect value")
            else:
                result[0] = "incorrect value"
        return result

    monkeypatch.setattr(module, operation, faulty_operation)
    arguments = {
        "benchmark": Rounds(disabled),
        "loaded_cache": cache,
        "database_file": path,
        "entries": entries,
        "measured_rounds": 3,
        "workers": 1,
    }
    test = getattr(module, scenario)
    with pytest.raises(AssertionError):
        test(**{name: arguments[name] for name in signature(test).parameters})
    assert calls == bad_call


@pytest.mark.parametrize(
    ("bad_call", "disabled"), [(1, False), (2, False), (3, False), (1, True)]
)
def test_every_wal_write_requires_persistence(
    tmp_path, monkeypatch, bad_call, disabled
):
    path = tmp_path / "benchmark.json"
    entries = make_entries(100, 32)
    write_database(path, entries)
    cache = load_cache(str(path))
    assert cache.wal is not None
    original = cache.wal.append
    calls = 0

    def sometimes_noop(op):
        nonlocal calls
        calls += 1
        if calls != bad_call:
            original(op)

    monkeypatch.setattr(cache.wal, "append", sometimes_noop)
    with pytest.raises(AssertionError):
        test_persistence.test_flush(Rounds(disabled), cache, path, entries, 3)
    assert calls == bad_call
