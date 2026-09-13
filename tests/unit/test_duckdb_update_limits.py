"""The DuckDB behaviours the schema is shaped around, asserted as facts.

Four decisions in migrations 0019-0022 exist only because of how DuckDB handles updates
and deletes on referenced rows: `target_content_id` is not a foreign key on three tables,
the evidence-atom rule is a named check rather than a unique index, and a merged-away
error pattern is superseded rather than deleted.

Stage 5 added two more, in migration 0027: a transcript layer's "one *current* reading"
rule is a `db check` rather than a partial unique index, and the index it replaced could
not be recreated under its own name.

Those reasons live in comments, which cannot fail. This file states them as executable
facts instead, so a `duckdb` version bump that changes any of them fails here and points
at the design decision it invalidates rather than at a mystery in a service.
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest


@pytest.fixture
def connection() -> Iterator[duckdb.DuckDBPyConnection]:
    handle = duckdb.connect()
    try:
        yield handle
    finally:
        handle.close()


def _referenced_parent(
    connection: duckdb.DuckDBPyConnection,
    *,
    target_is_foreign_key: bool = False,
    unique_index_on_target: bool = False,
    multi_column_check: bool = False,
) -> None:
    """A parent row that a child references, shaped like `attempts` and `evidence`."""

    connection.execute("CREATE TABLE items (content_id VARCHAR PRIMARY KEY)")
    connection.execute("INSERT INTO items VALUES ('i1'), ('i2')")
    reference = " REFERENCES items (content_id)" if target_is_foreign_key else ""
    check = ", CHECK (target IS NOT NULL OR dimension IS NOT NULL)" if multi_column_check else ""
    connection.execute(
        "CREATE TABLE parent ("
        "  id VARCHAR NOT NULL PRIMARY KEY,"
        f"  target VARCHAR{reference},"
        "  dimension VARCHAR,"
        "  status VARCHAR"
        f"{check})"
    )
    if unique_index_on_target:
        connection.execute("CREATE UNIQUE INDEX parent_atom ON parent (target)")
    connection.execute(
        "CREATE TABLE child (cid VARCHAR PRIMARY KEY, id VARCHAR NOT NULL REFERENCES parent (id))"
    )
    connection.execute("INSERT INTO parent VALUES ('p1', 'i1', 'reading', 'observed')")
    connection.execute("INSERT INTO child VALUES ('c1', 'p1')")


def test_a_foreign_key_column_cannot_be_updated_on_a_referenced_row(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why `attempts`, `evidence`, and `error_patterns` do not declare `target_content_id`.

    `knowledge merge` repoints that column, and all three tables are referenced by
    others. The relation is carried by `integrity.ORPHAN_RELATIONS` instead.
    """

    _referenced_parent(connection, target_is_foreign_key=True)

    with pytest.raises(duckdb.ConstraintException):
        connection.execute("UPDATE parent SET target = 'i2' WHERE target = 'i1'")


def test_a_unique_indexed_column_cannot_be_updated_on_a_referenced_row(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why the evidence-atom rule is a named `db check` rather than a unique index.

    An index over `target_content_id` would block exactly the update a merge performs,
    so the "one claim per attempt per target" rule is enforced on write and checked by
    name instead.
    """

    _referenced_parent(connection, unique_index_on_target=True)

    with pytest.raises(duckdb.ConstraintException):
        connection.execute("UPDATE parent SET target = 'i2' WHERE target = 'i1'")


def test_a_plain_column_can_be_updated_on_a_referenced_row(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why `error_patterns.status` is mutable in place despite three tables referencing it."""

    _referenced_parent(connection)

    connection.execute("UPDATE parent SET status = 'active' WHERE id = 'p1'")

    assert connection.execute("SELECT status FROM parent").fetchone() == ("active",)


def test_a_multi_column_check_does_not_by_itself_block_an_update(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why `attempts` and `evidence` may keep their target-scope CHECK constraints.

    Earlier stages treated a multi-column CHECK as enough on its own to force the
    delete-and-insert rewrite. It is not: the foreign key and the unique index are.
    """

    _referenced_parent(connection, multi_column_check=True)

    connection.execute("UPDATE parent SET status = 'active' WHERE id = 'p1'")

    assert connection.execute("SELECT status FROM parent").fetchone() == ("active",)


def test_a_referenced_row_cannot_be_deleted_in_the_transaction_that_repointed_its_children(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why a re-derived error pattern is superseded rather than deleted.

    A merge has to be one transaction. Inside it, the foreign-key index still holds the
    old key even after the children were repointed, so the old row cannot go -- it is
    retained, marked `superseded`, and names its successor.
    """

    _referenced_parent(connection)
    connection.execute("BEGIN TRANSACTION")
    connection.execute("INSERT INTO parent SELECT 'p2', target, dimension, status FROM parent")
    connection.execute("UPDATE child SET id = 'p2' WHERE id = 'p1'")

    with pytest.raises(duckdb.ConstraintException):
        connection.execute("DELETE FROM parent WHERE id = 'p1'")

    connection.execute("ROLLBACK")


def test_the_same_delete_succeeds_outside_a_transaction(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """The limit is the transaction, not the statement -- which is why it cannot be avoided.

    Splitting a merge into separate autocommit statements would work, and would also
    leave a half-merged learner model behind on any failure. One transaction is the
    requirement; supersession is what makes it possible.
    """

    _referenced_parent(connection)
    connection.execute("INSERT INTO parent SELECT 'p2', target, dimension, status FROM parent")
    connection.execute("UPDATE child SET id = 'p2' WHERE id = 'p1'")
    connection.execute("DELETE FROM parent WHERE id = 'p1'")

    assert connection.execute("SELECT count(*) FROM parent").fetchone() == (1,)


def test_a_partial_unique_index_cannot_be_created(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why `revision_supersession` is a `db check` and not a constraint.

    A transcript layer may hold many readings and only one *current* one, which is exactly
    what a partial unique index expresses. Without it the rule has to be asserted over the
    data instead -- and the repository's rule is that an unenforced uniqueness requirement
    is paired with a named check, never left to convention.
    """

    connection.execute(
        "CREATE TABLE readings ("
        "  revision_id VARCHAR PRIMARY KEY,"
        "  utterance_id VARCHAR NOT NULL,"
        "  layer VARCHAR NOT NULL,"
        "  superseded_at TIMESTAMP)"
    )
    with pytest.raises(duckdb.Error) as failure:
        connection.execute(
            "CREATE UNIQUE INDEX readings_current ON readings (utterance_id, layer) "
            "WHERE superseded_at IS NULL"
        )
    assert "partial" in str(failure.value).lower()

    # The unconditional form works, which is the one 0025 had and 0027 had to drop: it
    # made a second reading impossible, so a re-review deleted its predecessor.
    connection.execute("CREATE UNIQUE INDEX readings_layer ON readings (utterance_id, layer)")


def test_an_index_cannot_be_recreated_under_its_own_name_in_one_transaction(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Why 0027 renames `utterances_external` rather than redefining it.

    A migration is one transaction, and the index it drops is still holding its name when
    the `CREATE` runs. Reusing the name failed only when the migration was applied for
    real -- a fresh in-memory build of the schema, which runs outside a transaction,
    accepted it.
    """

    connection.execute("CREATE TABLE rows_ (a VARCHAR, b VARCHAR)")
    connection.execute("CREATE UNIQUE INDEX rows_key ON rows_ (a)")
    connection.execute("BEGIN TRANSACTION")
    connection.execute("DROP INDEX rows_key")
    with pytest.raises(duckdb.Error) as failure:
        connection.execute("CREATE UNIQUE INDEX rows_key ON rows_ (a, b)")
    assert "already exists" in str(failure.value)
    connection.execute("ROLLBACK")


def test_the_pinned_duckdb_is_the_one_these_facts_were_established_on() -> None:
    """A bump past this version has to re-establish them, which the gate requires anyway."""

    major, minor, _ = duckdb.__version__.split(".", 2)

    assert (int(major), int(minor)) >= (1, 4), (
        f"duckdb {duckdb.__version__} predates the versions these limits were measured on"
    )
