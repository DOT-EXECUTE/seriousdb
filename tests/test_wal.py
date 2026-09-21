"""Tests for Cache's write-ahead log: replay, compaction, and crash recovery."""

import json

import pytest

from seriousdb.cache import COMPACTION_THRESHOLD, Cache


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / ".sdb"


def test_replay_recovers_writes_after_reload(db_path):
    """Recover writes that have not yet been compacted into the database."""
    cache = Cache()
    cache.load(str(db_path))

    cache.insert("name", "Alice")
    cache.insert("language", "Python")
    cache.delete("language")

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db == {"name": "Alice"}


def test_compaction_triggers_at_write_threshold(db_path):
    """Compact the database once COMPACTION_THRESHOLD writes have occurred."""
    cache = Cache()
    cache.load(str(db_path))

    for i in range(COMPACTION_THRESHOLD):
        cache.insert(f"key_{i}", f"value_{i}")

    assert cache.wal is not None

    with open(db_path, "rb") as f:
        on_disk = json.loads(f.read().decode())

    assert on_disk == {f"key_{i}": f"value_{i}" for i in range(COMPACTION_THRESHOLD)}

    with open(cache.wal.filename, "rb") as f:
        assert f.read() == b""


def test_compaction_does_not_trigger_before_write_threshold(db_path):
    """Leave writes in the WAL until COMPACTION_THRESHOLD is reached."""
    cache = Cache()
    cache.load(str(db_path))

    for i in range(COMPACTION_THRESHOLD - 1):
        cache.insert(f"key_{i}", f"value_{i}")

    assert cache.wal is not None

    with open(db_path, "rb") as f:
        on_disk = json.loads(f.read().decode())

    assert on_disk == {}

    with open(cache.wal.filename, "rb") as f:
        wal_lines = f.read().decode().splitlines()

    assert len(wal_lines) == COMPACTION_THRESHOLD - 1


def test_compaction_counter_resets_after_compacting(db_path):
    """Reset the WAL write counter after compaction."""
    cache = Cache()
    cache.load(str(db_path))

    for i in range(COMPACTION_THRESHOLD):
        cache.insert(f"key_{i}", f"value_{i}")

    cache.insert("one_more", "value")

    assert cache.wal is not None

    with open(cache.wal.filename, "rb") as f:
        wal_lines = f.read().decode().splitlines()

    assert wal_lines == [json.dumps({"op": "set", "key": "one_more", "value": "value"})]


def test_insert_succeeds_when_compaction_fails(db_path, monkeypatch):
    """Keep a successfully appended write when a later compaction fails."""
    cache = Cache()
    cache.load(str(db_path))

    assert cache.db is not None

    def boom():
        raise OSError("simulated disk-full during compaction")

    monkeypatch.setattr(cache, "_compact", boom)
    monkeypatch.setattr(cache, "_writes_since_compact", COMPACTION_THRESHOLD)

    value, is_new_key = cache.insert("name", "Alice")

    assert value == "Alice"
    assert is_new_key
    assert cache.db["name"] == "Alice"


def test_replay_recovers_from_torn_last_wal_entry(db_path):
    """Ignore a truncated final WAL record while replaying complete records."""
    cache = Cache()
    cache.load(str(db_path))

    cache.insert("a", "1")
    cache.insert("b", "2")

    assert cache.wal is not None

    with open(cache.wal.filename, "rb") as f:
        wal_bytes = f.read()

    with open(cache.wal.filename, "wb") as f:
        f.write(wal_bytes[:-3])

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db == {"a": "1"}


def test_replay_is_idempotent_after_interrupted_compaction(db_path):
    """Replay WAL entries safely when the snapshot was replaced before WAL clear."""
    cache = Cache()
    cache.load(str(db_path))

    cache.insert("a", "1")
    cache.insert("b", "2")

    assert cache.db is not None

    with open(db_path, "wb") as f:
        f.write(json.dumps(cache.db).encode())

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db == {"a": "1", "b": "2"}


def test_insert_leaves_state_unchanged_when_wal_append_fails(db_path, monkeypatch):
    """Do not mutate memory when the WAL append fails."""
    cache = Cache()
    cache.load(str(db_path))

    assert cache.db is not None
    assert cache.wal is not None

    def boom(op):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cache.wal, "append", boom)

    with pytest.raises(OSError):
        cache.insert("name", "Alice")

    assert "name" not in cache.db

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db is not None
    assert "name" not in reloaded.db


def test_delete_leaves_state_unchanged_when_wal_append_fails(db_path, monkeypatch):
    """Do not mutate memory when the WAL append fails during deletion."""
    cache = Cache()
    cache.load(str(db_path))

    assert cache.db is not None
    assert cache.wal is not None

    cache.insert("name", "Alice")

    def boom(op):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cache.wal, "append", boom)

    with pytest.raises(OSError):
        cache.delete("name")

    assert cache.db["name"] == "Alice"

    reloaded = Cache()
    reloaded.load(str(db_path))

    assert reloaded.db is not None
    assert reloaded.db["name"] == "Alice"


def test_replay_repairs_torn_wal_before_later_appends(db_path):
    """Remove a torn final record so later WAL appends remain valid."""
    cache = Cache()
    cache.load(str(db_path))

    assert cache.wal is not None

    cache.insert("a", "1")
    cache.insert("b", "2")

    with open(cache.wal.filename, "rb") as f:
        wal_bytes = f.read()

    with open(cache.wal.filename, "wb") as f:
        f.write(wal_bytes[:-3])

    recovered = Cache()
    recovered.load(str(db_path))

    assert recovered.wal is not None
    assert recovered.db == {"a": "1"}

    with open(recovered.wal.filename, "rb") as f:
        repaired_bytes = f.read()

    assert repaired_bytes == (
        json.dumps({"op": "set", "key": "a", "value": "1"}).encode() + b"\n"
    )

    recovered.insert("c", "3")

    final = Cache()
    final.load(str(db_path))

    assert final.db == {"a": "1", "c": "3"}
