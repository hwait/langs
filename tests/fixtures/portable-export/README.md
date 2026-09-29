# Retained portable-export fixtures

Each `schema-<version>/` directory records one synthetic portable export exactly as it
was produced by the release that introduced that database schema version: the real
`metadata.json`, plus one JSON row file per typed table. `metadata.json` records the
DuckDB version that produced it, as provenance.

`tests/support/portable_fixture.py` materializes the directory back into a real Parquet
export using **only** the column names and types recorded in `metadata.json`.

## What these fixtures do and do not prove

They pin the **shape** of an export — table set, column names, column types — against the
current `db restore` path, so a migration that renames or retypes a column without a
matching export change fails the suite.

They do **not** prove cross-version DuckDB compatibility: the Parquet is rebuilt by
whichever DuckDB is installed, so a fixture can never contain bytes an older DuckDB
wrote. Presenting them as a storage-format upgrade gate would be misleading.

## The actual DuckDB upgrade gate

`scripts/check_duckdb_upgrade.py` installs two DuckDB versions for real, writes learner
data with the outgoing one, and then requires the candidate to open the native database,
re-export it, and restore both the native and portable backups:

```bash
uv run python scripts/check_duckdb_upgrade.py --to <candidate version>
uv run python scripts/verify.py --duckdb-upgrade <candidate version>
```

A `duckdb` bump in `pyproject.toml` is accepted only after that passes. Never edit a
retained fixture; add a new `schema-<version>/` directory instead.
