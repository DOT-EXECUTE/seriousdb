"""Tests for WriteAheadLog: appending, replaying, and clearing entries."""

import json

import pytest

from seriousdb.wal import WriteAheadLog


@pytest.fixture
def wal_path(tmp_path):
    return tmp_path / "test.wal"


@pytest.fixture
def wal(wal_path):
    return WriteAheadLog(str(wal_path))


def test_replay_on_missing_file_returns_empty_list(wal):
    assert wal.replay() == []


def test_append_then_replay_returns_entries_in_order(wal):
    wal.append({"op": "set", "key": "a", "value": "1"})
    wal.append({"op": "set", "key": "b", "value": "2"})
    wal.append({"op": "delete", "key": "a"})

    assert wal.replay() == [
        {"op": "set", "key": "a", "value": "1"},
        {"op": "set", "key": "b", "value": "2"},
        {"op": "delete", "key": "a"},
    ]


def test_append_writes_one_json_line_per_entry(wal, wal_path):
    wal.append({"op": "set", "key": "a", "value": "1"})

    with open(wal_path, "rb") as f:
        content = f.read()

    assert (
        content == json.dumps({"op": "set", "key": "a", "value": "1"}).encode() + b"\n"
    )


def test_replay_drops_and_truncates_a_torn_last_entry(wal, wal_path):
    wal.append({"op": "set", "key": "a", "value": "1"})
    wal.append({"op": "set", "key": "b", "value": "2"})

    with open(wal_path, "rb") as f:
        wal_bytes = f.read()
    with open(wal_path, "wb") as f:
        f.write(wal_bytes[:-3])

    entries = wal.replay()

    assert entries == [{"op": "set", "key": "a", "value": "1"}]

    with open(wal_path, "rb") as f:
        repaired = f.read()
    assert (
        repaired == json.dumps({"op": "set", "key": "a", "value": "1"}).encode() + b"\n"
    )


def test_append_after_repaired_truncation_stays_valid(wal, wal_path):
    wal.append({"op": "set", "key": "a", "value": "1"})
    wal.append({"op": "set", "key": "b", "value": "2"})

    with open(wal_path, "rb") as f:
        wal_bytes = f.read()
    with open(wal_path, "wb") as f:
        f.write(wal_bytes[:-3])

    wal.replay()
    wal.append({"op": "set", "key": "c", "value": "3"})

    assert wal.replay() == [
        {"op": "set", "key": "a", "value": "1"},
        {"op": "set", "key": "c", "value": "3"},
    ]


def test_clear_empties_the_file(wal, wal_path):
    wal.append({"op": "set", "key": "a", "value": "1"})

    wal.clear()

    with open(wal_path, "rb") as f:
        assert f.read() == b""
    assert wal.replay() == []


def test_clear_on_nonexistent_file_creates_empty_file(tmp_path):
    wal_path = tmp_path / "fresh.wal"
    wal = WriteAheadLog(str(wal_path))

    wal.clear()

    assert wal_path.exists()
    assert wal.replay() == []


def test_append_repairs_leftover_bytes_from_a_previous_failed_write(wal, wal_path):
    wal.append({"op": "set", "key": "a", "value": "1"})

    # Simulate a write that raised before finishing
    with open(wal_path, "ab") as f:
        f.write(b'{"op": "set", "key": "b')

    wal.append({"op": "set", "key": "c", "value": "3"})

    assert wal.replay() == [
        {"op": "set", "key": "a", "value": "1"},
        {"op": "set", "key": "c", "value": "3"},
    ]
