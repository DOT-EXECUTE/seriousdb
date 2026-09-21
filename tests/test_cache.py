"""Tests for the Cache interface and basic database operations."""

import json

import pytest

from seriousdb.cache import Cache, require_db
from seriousdb.exceptions import ResourceNotFoundError, ServiceUnavailableError


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / ".sdb"


@pytest.fixture
def cache(db_path):
    cache = Cache()
    cache.load(str(db_path))
    return cache


def test_cache_starts_unloaded():
    cache = Cache()

    assert cache.db is None
    assert cache.filename is None


def test_insert_stores_new_value(cache):
    value, is_new_key = cache.insert("name", "Alice")

    assert value == "Alice"
    assert is_new_key
    assert cache.db == {"name": "Alice"}


def test_insert_overwrites_existing_value(cache):
    cache.insert("name", "Alice")

    value, is_new_key = cache.insert("name", "Bob")

    assert value == "Bob"
    assert not is_new_key
    assert cache.db == {"name": "Bob"}


def test_select_returns_stored_value(cache):
    cache.insert("name", "Alice")

    assert cache.select("name") == "Alice"


def test_select_missing_key_raises(cache):
    with pytest.raises(
        ResourceNotFoundError,
        match="No value set for key missing",
    ):
        cache.select("missing")


def test_delete_returns_previous_value_and_removes_key(cache):
    cache.insert("name", "Alice")

    value = cache.delete("name")

    assert value == "Alice"
    assert cache.db == {}


def test_delete_missing_key_raises(cache):
    with pytest.raises(ResourceNotFoundError, match="No value set for key missing"):
        cache.delete("missing")


def test_operations_require_loaded_database():
    cache = Cache()

    with pytest.raises(ServiceUnavailableError):
        cache.insert("name", "Alice")

    with pytest.raises(ServiceUnavailableError):
        cache.select("name")

    with pytest.raises(ServiceUnavailableError):
        cache.delete("name")


def test_load_creates_missing_database(db_path):
    cache = Cache()

    cache.load(str(db_path))

    assert db_path.exists()
    assert cache.filename == str(db_path)
    assert cache.db == {}


def test_load_reads_existing_database(db_path):
    db_path.write_text(json.dumps({"name": "Alice"}))

    cache = Cache()
    cache.load(str(db_path))

    assert cache.db == {"name": "Alice"}
    assert cache.filename == str(db_path)


def test_load_replaces_current_data(cache, tmp_path):
    cache.insert("name", "Alice")

    other_path = tmp_path / "other.sdb"
    other_path.write_text(json.dumps({"language": "Python"}))

    cache.load(str(other_path))

    assert cache.db == {"language": "Python"}
    assert cache.filename == str(other_path)


def test_load_corrupt_database_starts_fresh(db_path):
    db_path.write_bytes(b"not valid json")

    cache = Cache()
    cache.load(str(db_path))

    assert cache.db == {}
    assert db_path.exists()
    assert list(db_path.parent.glob(".sdb.corrupt-*"))


def test_load_rejects_non_object_json(db_path):
    db_path.write_text(json.dumps(["not", "a", "database"]))

    cache = Cache()
    cache.load(str(db_path))

    assert cache.db == {}
    assert db_path.exists()
    assert list(db_path.parent.glob(".sdb.corrupt-*"))


def test_require_db_returns_loaded_database(cache):
    assert require_db(cache) is cache.db


def test_require_db_raises_when_database_is_not_loaded():
    cache = Cache()
    cache.filename = ".sdb"

    with pytest.raises(
        ServiceUnavailableError,
        match=r"\.sdb",
    ):
        require_db(cache)
