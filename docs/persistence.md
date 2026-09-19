# Persistence

Data is stored in a local file named `.sdb` in the process working directory, alongside a write-ahead log file named `.sdb.wal`

The `.sdb` file contains a serialized Python dictionary written with the standard-library `json` module. On first startup, the application creates it with:

```python
{}
```

The database is loaded into memory once at startup. Each write (`PUT` or `DELETE`) updates the in-memory dictionary and immediately appends a record of the change to `.sdb.wal`, which is flushed and fsynced before the request completes, so a write is durable the moment it succeeds, even if the process crashes immediately after.

The `.sdb` file itself is not rewritten on every write. Instead, once a fixed number of writes have accumulated in the WAL (see `COMPACTION_THRESHOLD` in `cache.py`), the current in-memory state is written atomically (via a temporary file and rename), so an interruption during compaction leaves either the previous snapshot with its WAL intact, or the new snapshot with an empty WAL, and never a partially written or corrupted file.

On startup, `.sdb` is loaded first, then any entries remaining in `.sdb.wal` are replayed on top of it, recovering writes made since the last compaction. If the WAL's last entry is incomplete (if for example the process with interrupted mid-write), replay stops at that entry and everything recorded before it is still recovered

## Current constraints

- Both files are local to the machine running the server.
- Requests use the complete in-memory dictionary rather than a database engine.
- Concurrent writes and multi-process access to the same `.sdb`/`.sdb.wal` pair are not currently coordinated. The in-process lock only protects multiple threads within a single running server.
