"""Append-only write-ahead log for durable, incremental persistence."""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


class WriteAheadLog:
    """An append-only, crash-safe log of JSON-serializable write operations.

    Each entry is appended as one JSON-encoded line, flushed and fsynced
    before :meth:`append` returns, so an entry is durable the moment the
    call succeeds. :meth:`replay` reads back every entry that was safely
    written, repairing the file on disk if the last entry was left
    incomplete by a crash mid-write, so a later :meth:`append` never lands
    on top of leftover garbage.

    Attributes
    ----------
    filename : str
        Path of the log file.
    """

    def __init__(self, filename: str):
        self.filename = filename

    def append(self, op: dict) -> None:
        """Append `op` to the log and fsync it.

        Parameters
        ----------
        op : dict
            A JSON-serializable write operation.

        Raises
        ------
        OSError
            If the log file cannot be written.
        """
        with open(self.filename, "ab") as f:
            f.write((json.dumps(op) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())

    def replay(self) -> list[dict]:
        """Return every entry durably written to the log, oldest first.

        Only complete, newline-terminated entries are trusted, an entry
        cut short by a crash mid-write has no way to prove it was fully
        flushed to disk, since :meth:`append` always writes an entry and
        its trailing newline in a single write. If the log ends with an
        entry that is not newline-terminated or does not parse, it is
        dropped, and the file is truncated on disk to just after the last
        trusted entry, so a later :meth:`append` lands on clean content
        instead of onto leftover garbage.

        Returns
        -------
        list of dict
            The decoded entries, in the order they were appended. Empty if
            the log file does not exist yet.
        """
        if not os.path.isfile(self.filename):
            return []

        entries: list[dict] = []
        good_offset = 0
        found_bad_entry = False
        with open(self.filename, "rb") as f:
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
                            "Corrupt entry in write-ahead log %s (%s)",
                            self.filename,
                            e,
                        )
                        found_bad_entry = True
                        break
                    entries.append(op)
                good_offset += len(raw_line)

        if found_bad_entry:
            logger.warning(
                "Truncating write-ahead log %s to its last known-good entry",
                self.filename,
            )
            with open(self.filename, "r+b") as f:
                f.truncate(good_offset)

        return entries

    def clear(self) -> None:
        """Atomically replace the log with an empty file.

        Raises
        ------
        OSError
            If the temporary or final files cannot be written.
        """
        dir_name = os.path.dirname(self.filename) or "."
        with tempfile.NamedTemporaryFile("wb", dir=dir_name, delete=False) as tmp_file:
            pass
        os.replace(tmp_file.name, self.filename)
