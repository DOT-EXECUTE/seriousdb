# Testing

The project uses `pytest` for automated testing.

## Running the Tests

Run the regular test suite in `tests/` with:

```bash
uv run pytest
```

The test suite covers:

- Public Python API behavior (`seriousdb.api`)
- CRUD operations
- Error handling
- Concurrent database access
- Logging

Each test uses an isolated temporary database so the test suite does not modify the local `.sdb`
database.

The benchmark scenarios run separately using the commands below.

## Performance benchmarks

From the repository root, run the benchmark suite with compact output and saved results:

```bash
uv run --locked --group benchmark -m benchmarks
```

See the [benchmark guide](../benchmarks/README.md) for all scenarios, implementation details,
datasets, command options, correctness checks and guidance on comparing results.
