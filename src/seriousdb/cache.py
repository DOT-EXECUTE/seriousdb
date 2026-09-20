"""In-memory key-value cache backed by a JSON file and a write-ahead log.

The whole database is held in memory as a ``dict``. Writes are durably
appeneded to a write-ahead log (WAL) before returning, and periodically
compacted into the full JSON snapshot file (see :data:`COMPACTION_THRESHOLD`).
All access to the data is guarded by a lock, so a single :class:`Cache` can
be shared between request handlers.
"""

import json
import logging
import os
import tempfile
import time
from threading import Lock

from .exceptions import ResourceNotFoundError, ServiceUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_DB = {}
COMPACTION_THRESHOLD = 50


class Cache:
    """Thread-safe in-memory key-value store persisted to a JSON file.

    A new cache holds no data. Call :meth:`load` before using it; until then
    every data access raises
    :class:`~seriousdb.exceptions.ServiceUnavailableError`.

    Attributes
    ----------
    filename : str or None
        Path of the database file, or ``None`` if nothing has been loaded.
    wal_filename: str or None
        Path of the database WAL file, or ``None`` if nothing has been loaded.
    db : dict of str to str or None
        The stored key-value pairs, or ``None`` if nothing has been loaded.
    lock : threading.Lock
        Lock that must be held while reading or changing `db`.
    _writes_since_compact: int
        Counter for number of writes since last compaction.
    """

    def __init__(self):
        self.filename: str | None = None
        self.wal_filename: str | None = None
        self.db: dict[str, str] | None = None
        self.lock = Lock()
        self._writes_since_compact: int = 0

    def insert(self, key: str, value: str) -> tuple[str, bool]:
        """Store `value` under `key`, replacing any existing value.

        The change is appended to the write-ahead log (WAL) and must succeed there before
        it is applied in memory, so a failed write never leaves the live cache disagreeing
        with what is durable. The full database snapshot file is only rewritten periodically,
        by :meth:`_compact`.

        Parameters
        ----------
        key : str
            Key to store the value under.
        value : str
            Value to store.

        Returns
        -------
        value : str
            The stored value.
        is_new_key : bool
            ``True`` if `key` did not exist before, ``False`` if an existing
            value was replaced.

        Raises
        ------
        ServiceUnavailableError
            If no database has been loaded.
        OSError
            If the write-ahead log cannot be written. `self.db` is left unchanged in this case.
        """
        with self.lock:
            db = require_db(self)
            is_new_key = key not in db
            self._record_write({"op": "set", "key": key, "value": value})
            db[key] = value
            self._maybe_compact()
        return value, is_new_key

    def select(self, key: str) -> str:
        """Return the value stored under `key`.

        Parameters
        ----------
        key : str
            Key to look up.

        Returns
        -------
        str
            The value stored under `key`.

        Raises
        ------
        ResourceNotFoundError
            If `key` does not exist.
        ServiceUnavailableError
            If no database has been loaded.
        """
        with self.lock:
            val = require_db(self).get(key, None)
        if val is None:
            logger.debug("Key not found: %s", key)
            raise ResourceNotFoundError(f"No value set for key {key}")
        return val

    def delete(self, key: str) -> str:
        """Remove `key` and return the value it had.

        If `key` exists, its removal is appended to the write-ahead log (WAL) and
        must succeed there before it is applied in memory, so a failed write never
        leaves the live cache disagreeing with what is durable.


        Parameters
        ----------
        key : str
            Key to remove.

        Returns
        -------
        str
            The value `key` had before it was removed.

        Raises
        ------
        ResourceNotFoundError
            If `key` does not exist.
        ServiceUnavailableError
            If no database has been loaded.
        OSError
            If the write-ahead log cannot be written. `self.db` is left unchanged in this case.
        """
        with self.lock:
            db = require_db(self)
            val = db.get(key, None)
            if val is not None:
                self._record_write({"op": "delete", "key": key})
                db.pop(key, None)
                self._maybe_compact()
        if val is None:
            logger.debug("Key not found: %s", key)
            raise ResourceNotFoundError(f"No value set for key {key}")
        return val

    def load(self, filename: str) -> None:
        """Load the database from `filename`, replacing the current data.

        If the file does not exist, it is created with an empty database.
        If it is not valid UTF-8 JSON or does not contain a JSON object, it is
        renamed to ``<filename>.corrupt-<unix timestamp>``. If that backup
        already exists, a numeric suffix is appended (such as ``-1``, ``-2``,
        etc.) to avoid overwriting it. A warning is logged, and a new file with
        an empty database is created in its place.

        After the snapshot is loaded, any entries in the write-ahead log
        (``<filename>.wal``) are replayed on top of it, recovering writes
        that happened after the last compaction. If the log ends with an
        incomplete or corrupt entry, it is truncated back to the last known-good
        so that future writes never get appended on top of leftover garbage.

        Parameters
        ----------
        filename : str
            Path of the database file.

        Raises
        ------
        OSError
            If the file cannot be read, renamed or written.
        """
        with self.lock:
            if not os.path.isfile(filename):
                logger.info(
                    "Database file %s does not exist; creating a new database",
                    filename,
                )
                self.db = _write_default(filename)
            else:
                try:
                    with open(filename, "rb") as f:
                        self.db = json.loads(f.read().decode())
                        if not isinstance(self.db, dict):
                            raise TypeError(
                                f"expected dict, got {type(self.db).__name__}"
                            )
                        logger.info("Loaded database from %s", filename)

                except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
                    backup = _generate_corrupt_backup_path(filename)
                    os.replace(filename, backup)
                    logger.warning(
                        "Corrupt database file %s (%s); moved to %s and starting fresh",
                        filename,
                        e,
                        backup,
                    )
                    self.db = _write_default(filename)
            self.filename = filename
            self.wal_filename = f"{filename}.wal"
            self._writes_since_compact = 0
            self._replay_wal()

    def flush(self) -> None:
        """No-op, kept for backward compatibility.

        Durability is now handled per-write via the write-ahead log (see :meth:`_append_wal`),
        so nothing needs to happen here.
        """
        return

    # ------ Write-ahead log internals -----------------------------------------#

    def _record_write(self, op: dict) -> None:
        """Append `op` to the write-ahead log and bump the write counter.

        Must be called, and must succeed, before `op` is applied to `self.db`,
        a failed append must never leave memory and the WAL disagreeing about
        what happened

        Parameters
        ----------
        op : dict
            A JSON-serializable write operation.

        Raises
        ------
        OSError
            If the WAL file cannot be written.
        """
        self._append_wal(op)
        self._writes_since_compact += 1

    def _maybe_compact(self) -> None:
        """Compact if the write count has reached :data:`COMPACTION_THRESHOLD`.

        Called after a write has already been applied to `self.db` and
        durably appended to the WAL, so a compaction failure here does not
        affect the durability of the write that just happened, it is already
        safe in the WAL regardless.
        """
        if self._writes_since_compact >= COMPACTION_THRESHOLD:
            self._compact()

    def _append_wal(self, op: dict) -> None:
        """Append `op` to the write-ahead log file and fsync it.

        Does nothing if no database has been loaded.

        Parameters
        ----------
        op : dict
            A JSON-serializable write operation.

        Raises
        ------
        OSError
            If the WAL file cannot be written.
        """
        if self.wal_filename is None:
            return
        with open(self.wal_filename, "ab") as f:
            f.write((json.dumps(op) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())

    def _replay_wal(self) -> None:
        """Apply every entry in the write-ahead log to `self.db`.

        Must be called after `self.db` and `self.wal_filename` are set.

        Only complete, new-line terminated entries are trusted, an entry
        cut short by a crash mid-write has no way to prove it was fully
        flushed to the disk, since `_append_wal` always writes an entry and
        its trailing newline in a single write. Replay stops at the first entry
        that isn't newline-terminated or doesn't parse. The WAL file is then
        truncated to just after the last trusted entry, so that any later write
        appends onto clean content instead of onto leftover garbage from incomplete entry.
        """
        if self.wal_filename is None or not os.path.isfile(self.wal_filename):
            return

        good_offset = 0
        found_bad_entry = False
        with open(self.wal_filename, "rb") as f:
            for raw_line in f:
                if not raw_line.endswith(b"\n"):
                    found_bad_entry = True
                    break
                line = raw_line.strip()
                if line:
                    try:
                        op = json.loads(line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        logger.warning(
                            "Stopping WAL replay at truncated/corrupt entry in %s (%s)",
                            self.wal_filename,
                            e,
                        )
                        found_bad_entry = True
                        break
                    self._apply_op(op)
                good_offset += len(raw_line)

        if found_bad_entry:
            logger.warning(
                "Truncating write-ahead log %s to its last known-good entry",
                self.wal_filename,
            )
            with open(self.wal_filename, "r+b") as f:
                f.truncate(good_offset)

    def _apply_op(self, op: dict) -> None:
        """Apply a single decoded write-ahead log entry to `self.db`.

        Parameters
        ----------
        op : dict
            A decoded WAL entry, as produced by :meth:`_append_wal`.
        """
        db = require_db(self)
        db_op = op.get("op")
        if db_op == "set":
            db[op["key"]] = op["value"]
        elif db_op == "delete":
            db.pop(op["key"], None)

    def _compact(self) -> None:
        """Write `self.db` to `self.filename` and clear the write-ahead log.

        Both the snapshot and the emptied WAL are written atomically via a temporary
        file and `os.replace`, in that order, so a crash at any point during compaction
        leaves either the old snapshot with a non-empty WAL, or the new snapshot with an
        empty WAL, and never a lost or corrupted state. Replaying the same WAL entry twice is harmless,
        since ``set``/``delete`` are overlayable.

        Raises
        ------
        OSError
            If the temporary or final files cannot be written.
        """
        if self.db is None or self.filename is None:
            return

        dir_name = os.path.dirname(self.filename) or "."
        with tempfile.NamedTemporaryFile("wb", dir=dir_name, delete=False) as tmp_file:
            tmp_file.write(json.dumps(self.db).encode())
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_file.name, self.filename)
        logger.info("Compacted database into %s", self.filename)

        if self.wal_filename is not None:
            wal_dir = os.path.dirname(self.wal_filename) or "."
            with tempfile.NamedTemporaryFile(
                "wb", dir=wal_dir, delete=False
            ) as tmp_wal:
                pass
            os.replace(tmp_wal.name, self.wal_filename)

        self._writes_since_compact = 0


def _write_default(filename: str) -> dict[str, str]:
    with open(filename, "wb") as f:
        f.write(json.dumps(DEFAULT_DB).encode())
    return dict(DEFAULT_DB)


def _generate_corrupt_backup_path(filename: str) -> str:
    """Generate an unused backup path for a corrupt database file.

    The first backup uses ``<filename>.corrupt-<unix timestamp>``.
    If that path already exists, numeric suffixes such as ``-1``,
    ``-2`` and so on are tried until an unused path is found.

    Parameters
    ----------
    filename : str
        Path of the database file.

    Returns
    -------
    str
        Unused backup path.
    """
    base = f"{filename}.corrupt-{int(time.time())}"
    if not os.path.lexists(base):
        return base
    counter = 1
    while os.path.lexists(f"{base}-{counter}"):
        counter += 1
    return f"{base}-{counter}"


def require_db(cache: Cache) -> dict[str, str]:
    """Return the loaded data of `cache`.

    The caller must hold ``cache.lock`` while using the returned ``dict``.

    Parameters
    ----------
    cache : Cache
        Cache to read the data from.

    Returns
    -------
    dict of str to str
        The loaded key-value pairs. This is the cache's own ``dict``, not a
        copy.

    Raises
    ------
    ServiceUnavailableError
        If `cache` has no database loaded.
    """
    if cache.db is None:
        logger.error("Database unavailable: %s", cache.filename)
        raise ServiceUnavailableError(
            f"Database file {cache.filename} could not be opened and loaded"
        )

    return cache.db
