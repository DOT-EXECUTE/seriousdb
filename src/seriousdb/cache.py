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

        The change is appended to the write-ahead log (WAL) before this method
        returns, so it survives a crash immediately; the full database snapshot file
        is only rewritten periodically, by :meth:`_compact`.

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
        """
        with self.lock:
            db = require_db(self)
            is_new_key = key not in db
            db[key] = value
            self._record_write({"op": "set", "key": key, "value": value})
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

        If `key` existed, the removal is appended to the write-ahead log (WAL) file
        before this method returns.

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
        """
        with self.lock:
            val = require_db(self).pop(key, None)
            if val is not None:
                self._record_write({"op": "delete", "key": key})
        if val is None:
            logger.debug("Key not found: %s", key)
            raise ResourceNotFoundError(f"No value set for key {key}")
        return val

    def load(self, filename: str) -> None:
        """Load the database from `filename`, replacing the current data.

        If the file does not exist, it is created with an empty database.
        If it is not valid UTF-8 JSON or does not contain a JSON object, it is
        renamed to ``<filename>.corrupt-<unix timestamp>``, a warning is
        logged, and a new file with an empty database is created in its
        place.

        After the snapshot is loaded, any entries in the write-ahead log
        (``<filename>.wal``) are replayed on top of it, recovering writes
        that happened after the last compaction.

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
                    backup = f"{filename}.corrupt-{int(time.time())}"
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
        so nothing needs to happen here. `main.py` still schedules this as a background task after
        each write; this method exists so that call keeps working without change.
        """
        return

    # ------ Write-ahead log internals -----------------------------------------#

    def _record_write(self, op: dict) -> None:
        """Persist `op` to the write-ahead log and compact if due.

        Parameters
        ----------
        op : dict
            A JSON-serializable write operation.
        """
        self._append_wal(op)
        self._writes_since_compact += 1
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
        Stops at the first entry that cannot be parsed; An uncomplete write
        by a crash mid-append, and logs a warning instead of raising
        every entry before it has already been applied.
        """
        if self.wal_filename is None or not os.path.isfile(self.wal_filename):
            return
        with open(self.wal_filename, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    op = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.warning(
                        "Stopping WAL replay at truncated/corrupt entry in %s (%s)",
                        self.wal_filename,
                        e,
                    )
                    break
                self._apply_op(op)

    def _apply_op(self, op: dict) -> None:
        """Apply a single decoded write-ahead log entry to `self.db`.

        Parameters
        ----------
        op : dict
            A decoded WAL entry, as produced by :meth:`_append_wal`.
        """
        db = require_db(self)
        if op.get("op") == "set":
            db[op["key"]] = op["value"]
        elif op.get("op") == "delete":
            db.pop(op["key"], None)

    def _compact(self) -> None:
        """Write `self.db` to `self.filename` and clear the write-ahead log.

        Both the snapshot and the emptied WAL are written atomically via temporary
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

        tmp_path = f"{self.filename}.tmp-{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(json.dumps(self.db).encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.filename)
        logger.info("Compacted database into %s", self.filename)

        if self.wal_filename is not None:
            tmp_wal = f"{self.wal_filename}.tmp-{os.getpid()}"
            with open(tmp_wal, "wb"):
                pass
            os.replace(tmp_wal, self.wal_filename)

        self._writes_since_compact = 0


def _write_default(filename: str) -> dict[str, str]:
    with open(filename, "wb") as f:
        f.write(json.dumps(DEFAULT_DB).encode())
    return dict(DEFAULT_DB)


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
