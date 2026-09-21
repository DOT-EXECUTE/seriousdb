# Benchmarks

The process benchmarks exercise [`seriousdb.api`](../src/seriousdb/api.py), the
interface used by applications. The other scenarios measure
[`Cache`](../src/seriousdb/cache.py) directly. All scenarios use isolated temporary files.

## Run benchmarks

From the repository root, run the default suite with compact output and saved results:

```bash
uv run --locked --group benchmark -m benchmarks
```

The default run includes reads, persistence, mixed workloads, threads and process
reads. Experimental process writes are skipped unless explicitly enabled.
The [runner](__main__.py) saves raw samples in ignored `.benchmarks/` JSON files.

Select scenarios with pytest's `-k` filter. These commands work in PowerShell and Bash:

```bash
uv run --locked --group benchmark -m benchmarks -k "batch_write and 1000x32B"
uv run --locked --group benchmark -m benchmarks -k process_reads
```

Check correctness without timing, or compare saved runs:

```bash
uv run --locked --group benchmark pytest benchmarks --benchmark-disable
uv run --locked --group benchmark pytest-benchmark compare
```

CI runs the correctness command without performance thresholds. Performance history stays local.

## Datasets and options

The [fixtures](conftest.py) select three datasets: 100 and 1,000 entries with
32-byte values, plus 1,000 entries with 1,024-byte values. Each scenario runs one
unrecorded warmup followed by five measured rounds. The [shared helpers](_support.py)
generate deterministic string keys and values and shuffle access order with seed 212.

| Option                    | Effect                                                                                   |
|---------------------------|------------------------------------------------------------------------------------------|
| `-k "expression"`         | Select scenarios or dataset IDs, such as `1000x32B`.                                     |
| `--extended`              | Add 10,000 and 100,000 entries with 32-byte values.                                      |
| `--engine-rounds=20`      | Change the number of measured rounds; the default is 5.                                  |
| `--process-counts 1 4 16` | Choose process counts; the default is 1, 2, 4 and 8. Thread counts remain 1, 2, 4 and 8. |
| `--multiprocess-writes`   | Enable experimental writes to the same file from multiple processes.                     |

For example:

```bash
uv run --locked --group benchmark -m benchmarks -k process_reads --process-counts 1 4 16 --engine-rounds=20
```

## Scenarios and implementation

| Implementation                      | What it measures                                                                               |
|-------------------------------------|------------------------------------------------------------------------------------------------|
| [Reads](test_reads.py)              | Load and read every key; load only; reads from a resident cache.                               |
| [Persistence](test_persistence.py)  | Batch creation and persistence; flush only; 100 overwrites with individual or batched flushes. |
| [Mixed workload](test_workloads.py) | Shuffled 90% reads and 10% overwrites in one resident cache.                                   |
| [Threads](test_concurrency.py)      | The mixed workload split across threads sharing one cache.                                     |
| [Processes](test_processes.py)      | API loads and resident reads across processes, plus optional API writes to a shared file.      |

Scenarios use `benchmark.pedantic` to separate timed operations from setup and
verification. Checks run after every warmup and measured round. Explicit final
checks also cover `--benchmark-disable`, which skips teardown callbacks.

### Reads

- `test_load_and_read_by_key` times loading a fresh cache and looking up every
  key. Creating the populated file is outside timing; returned values are checked afterward.
- `test_load_file` times creating a cache and loading the file. Key lookups and
  the total key-count check happen afterward, outside timing.
- `test_resident_read` times key lookups from an already loaded cache. File creation
  and loading are setup; returned values are checked afterward.

Cold loading means a fresh application cache each round. The OS may still cache
the file, so these scenarios do not measure cold-disk performance.

### Persistence

- `test_batch_write_and_persist` removes the previous file before timing. It then
  creates a database, inserts the full dataset and flushes once. Verification reopens
  the file and checks every value and the total key count.
- `test_flush` starts with a populated cache. Each round changes one cached value
  outside timing, then times only `Cache.flush`. Reopening the file afterward proves
  the changed value was persisted.
- `test_update_and_persist` overwrites 100 existing keys, flushing either after each
  write or once for the entire batch. Original values are restored and persisted
  before each round. Both the cache and reopened file are checked afterward.

Each `Cache.flush` serializes the entire cache, writes a temporary file, calls file
`fsync` and atomically replaces the database file. These timings include that work;
they do not establish crash durability.

### Mixed workload and threads

The mixed workload performs one operation per dataset entry: 90% reads and 10%
overwrites of disjoint keys, shuffled deterministically. Each round restores the
original cached values outside timing. Returned reads and final cache contents are
checked afterward. A final flush and reopen check happen after all timed rounds.

The thread scenario distributes the same total workload across 1, 2, 4 or 8
threads sharing one `Cache`. Timings include task submission, barrier synchronization,
operations and waiting for results. Executor construction is outside timing;
`ThreadPoolExecutor` starts threads lazily, so initial thread startup occurs during
the unrecorded warmup. This scenario measures scheduling and contention on the shared cache.

### Process reads

The [worker helpers](_processes.py) use `spawn` on every platform. Each child uses
its own instance of the API module, with `api.load(filename)` selecting the shared
temporary file. Workers read through `api.get()` and check key counts through
`api.count()`. Each round splits the keys across children, keeping the total number
of lookups fixed as the process count increases.

| Mode            | Timed cache work                                                                                             |
|-----------------|--------------------------------------------------------------------------------------------------------------|
| `load-and-read` | Call `api.load()` to reload the file, then `api.get()` for assigned keys and `api.count()` in every process. |
| `resident-read` | Call `api.get()` for assigned keys and `api.count()`, with `api.load()` called once before timing.           |

Processes start and reach a synchronization point before timing begins. Timings
include task dispatch, synchronization, API operations, serialization and transfer
of results to the parent. They exclude startup, initial file creation and correctness
checks. Small datasets can be dominated by communication costs.

Every round checks returned values, total key counts, distinct worker process IDs
and an unchanged database file. Worker waits have timeouts, and children are
terminated and joined when the scenario exits, including failures.

### Experimental process writes

Enable the write probe with:

```bash
uv run --locked --group benchmark -m benchmarks -k process_writes --multiprocess-writes -rx
```

Each round restores the original JSON fixture outside timing. All children call
`api.load()` and synchronize before overwriting disjoint keys through `api.set()`.
Every `set()` persists its change before returning. The workload updates 100 keys
in total (or all keys if fewer), keeping the number of writes fixed as datasets grow.
API loading, synchronization, writes including persistence, local readback with
`api.get()`, `api.count()` and result communication are timed. Startup and
verification are excluded.

The parent checks local readback and the combined persisted updates by reading raw
JSON, without allowing a database load to repair corruption. Unchanged keys are
also checked. A single writer must pass.
Lost updates, invalid JSON or Windows atomic replacement conflicts (access denied,
sharing or lock violations on the shared destination) with multiple writers are
reported as `XFAIL` after collecting timings. Saved metadata includes
`correctness=failed` and failure reasons. Unexpected worker errors still fail the test.
After a recognized replacement conflict, the worker continues attempting the
remaining assigned writes; the round stays failed even if a later write succeeds.

Failed write timings describe an unsuccessful workload and must not be treated as
valid write throughput. Successful rounds do not establish general process safety;
the probe does not add write coordination to the engine.

## Interpret results

Saved JSON includes raw timings, revision, environment and workload metadata such
as dataset size, value size and worker count. Compare matching scenarios, datasets,
worker counts and workload versions on the same idle machine, Python version and
storage. Check revision and persistence semantics too: adding `fsync`, for example,
changes the work performed even if the scenario name stays the same.

Workload version 6 switches process benchmarks to the API, including one persisted
write per `set()` call. Start a new baseline for these results; earlier process
write samples used one flush per worker. Process results record
`interface=seriousdb.api` in their metadata.

Use the median and spread rather than a single fastest sample. Start a fresh baseline
when the workload version changes. OPS counts complete scenarios per second, not
individual reads or writes. OS caching and process communication costs affect the
results; interpret them with the timing boundaries above.
