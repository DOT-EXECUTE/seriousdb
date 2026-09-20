"""Tests for Cache's write-ahea log: replay, compaction, and crash recovery."""

import json

import pytest

from seriousdb.cache import COMPACTION_THRESHOLD, Cache


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / ".sdb"


def test_replay_recorvers_writes_after_reload(db_path):
    """Writes not yet compacted into database are still recovered by a fresh Cache.load()."""
    cache = Cache()
    cache.load(str(db_path))
    cache.insert("name", "Alice")
    cache.insert("language", "Python")
    cache.delete("language")

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db == {"name": "Alice"}


def test_compaction_triggers_after_threshold_writes(db_path):
    """Once COMPACTION_THRESHOLD writes have happened, the snapshot file
    reflects all of them and the WAL is emptied."""
    cache = Cache()
    cache.load(str(db_path))

    for i in range(COMPACTION_THRESHOLD):
        cache.insert(f"key_{i}", f"value_{i}")

    assert cache.wal_filename is not None

    with open(db_path, "rb") as f:
        on_disk = json.loads(f.read().decode())
    assert on_disk == {f"key_{i}": f"value_{i}" for i in range(COMPACTION_THRESHOLD)}

    with open(cache.wal_filename, "rb") as f:
        assert f.read() == b""


def test_compaction_does_not_trigger_before_threshold(db_path):
    """Fewer than COMPACTION_THRESHOLD writes leave the snapshot untouched,
    with entries only in the WAL."""
    cache = Cache()
    cache.load(str(db_path))

    for i in range(COMPACTION_THRESHOLD - 1):
        cache.insert(f"key_{i}", f"value_{i}")

    assert cache.wal_filename is not None

    with open(db_path, "rb") as f:
        on_disk = json.loads(f.read().decode())
    assert on_disk == {}

    with open(cache.wal_filename, "rb") as f:
        wal_lines = f.read().decode().splitlines()
    assert len(wal_lines) == COMPACTION_THRESHOLD - 1


def test_compaction_counter_resets_after_compacting(db_path):
    """After a compaction, the counter must reset so the WAL genuinely batches
    writes again, instead of recompacting on every write after the first threshold
    is hit."""
    cache = Cache()
    cache.load(str(db_path))

    for i in range(COMPACTION_THRESHOLD):
        cache.insert(f"key_{i}", f"value_{i}")
    cache.insert("one_more", "value")  # one write since the first compaction

    assert cache.wal_filename is not None

    with open(cache.wal_filename, "rb") as f:
        wal_lines = f.read().decode().splitlines()

    # If the counter didn't reset, this single write would have immediately
    # triggered another compaction, leaving the WAL empty again.
    assert wal_lines == [json.dumps({"op": "set", "key": "one_more", "value": "value"})]


def test_replay_recovers_from_torn_last_wal_entry(db_path):
    """A crash mid-appened leaves a truncated last line. Replay recovers everything
    before it and does not raise."""
    cache = Cache()
    cache.load(str(db_path))
    cache.insert("a", "1")
    cache.insert("b", "2")

    assert cache.wal_filename is not None

    # Simulate a crash mid-appened by chopping bytes off the end, landing
    # inside the last JSON line without touching the complete first one.
    with open(cache.wal_filename, "rb") as f:
        wal_bytes = f.read()
    with open(cache.wal_filename, "wb") as f:
        f.write(wal_bytes[:-3])

    reloaded = Cache()
    reloaded.load(str(db_path))  # must not raise

    assert reloaded.db == {"a": "1"}


def test_replay_is_overlayable_after_interrupted_compaction(db_path):
    """If a crash happens between the snapshot replace and the WAL clear,
    the WAL still has entries the snapshot already contains. Replaying
    them again on the next load must not corrupt state."""
    cache = Cache()
    cache.load(str(db_path))
    cache.insert("a", "1")
    cache.insert("b", "2")

    # Manually perform what _compact does, but stop right after replacing
    # the snapshot. Simulating a crash mid-compaction.
    with open(db_path, "wb") as f:
        f.write(json.dumps(cache.db).encode())
    # cache.wal_filename still holds the original two entries.

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db == {"a": "1", "b": "2"}


def test_insert_leaves_state_unchanged_when_wal_append_fails(db_path, monkeypatch):
    """A failed WAL append must not leave the in-memory cache disagreeing with what's
    actually durable."""
    cache = Cache()
    cache.load(str(db_path))
    assert cache.db is not None

    def boom(op):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cache, "_append_wal", boom)

    with pytest.raises(OSError):
        cache.insert("name", "Alice")

    assert "name" not in cache.db

    reloaded = Cache()
    reloaded.load(str(db_path))
    assert reloaded.db is not None
    assert "name" not in reloaded.db


def test_deletes_leaves_state_unchanged_when_wal_append_fails(db_path, monkeypatch):
    cache = Cache()
    cache.load(str(db_path))
    assert cache.db is not None
    cache.insert("name", "Alice")

    def boom(op):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cache, "_append_wal", boom)

    with pytest.raises(OSError):
        cache.delete("name")

    assert cache.db["name"] == "Alice"

    reloaded = Cache()
    reloaded.load(str(db_path))
    assert reloaded.db is not None
    assert reloaded.db["name"] == "Alice"


def test_replay_repairs_wal_so_later_appends_stay_clean(db_path):
    """A torn last entry must be truncated from the WAL file during replay, not just skipped
    in memory, otherwise a live process resuming writes after recovery corrupts everything appended
    from then on."""
    cache = Cache()
    cache.load(str(db_path))
    assert cache.wal_filename is not None
    cache.insert("a", "1")
    cache.insert("b", "2")

    with open(cache.wal_filename, "rb") as f:
        wal_bytes = f.read()
    with open(cache.wal_filename, "wb") as f:
        f.write(wal_bytes[:-3])

    recovered = Cache()
    recovered.load(str(db_path))
    assert recovered.wal_filename is not None
    assert recovered.db == {"a": "1"}

    with open(recovered.wal_filename, "rb") as f:
        repaired_bytes = f.read()
    assert repaired_bytes == b"" or repaired_bytes.endswith(b"\n")

    recovered.insert("c", "3")

    final = Cache()
    final.load(str(db_path))
    assert final.db == {"a": "1", "c": "3"}
