"""Small, additive schema migrations for installations created before Alembic.

The project historically used ``Base.metadata.create_all`` at startup.  That is
enough for a new database, but it deliberately does not add columns or indexes
to an existing table.  This module keeps that lightweight deployment model
while making additive model changes safe and repeatable:

* missing tables are created from the current SQLAlchemy metadata;
* missing columns are added without rebuilding or dropping a table;
* declared indexes are created with ``checkfirst`` semantics; and
* PostgreSQL startup races are serialized with a transaction advisory lock.

This is intentionally not a general replacement for Alembic.  Renames, type
changes, removals, backfills that need business logic, and new constraints on
existing columns require an explicit versioned migration.  A required column
can only be added here when its model supplies a scalar/default SQL value; the
runner fails before changing the schema rather than inventing data.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from importlib import import_module
from threading import RLock
from typing import Iterable

from sqlalchemy import Connection, Engine, MetaData, inspect, literal, select, text
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import Column, CreateColumn, Table

from app.db import Base, engine as default_engine


_DEFAULT_MODEL_MODULES = (
    "app.models",
    "app.extended_models",
    "app.itinerary_models",
    "app.agent_models",
)
_POSTGRES_LOCK_NAME = "cloudbali_additive_schema_migrations_v1"
_process_lock = RLock()


class MigrationError(RuntimeError):
    """Base exception for an additive migration that cannot be completed."""


class UnsafeMigrationError(MigrationError):
    """Raised before DDL when metadata would require inventing legacy data."""


@dataclass(frozen=True)
class MigrationReport:
    """Names of schema objects added by one migration invocation."""

    added_tables: tuple[str, ...] = ()
    added_columns: tuple[str, ...] = ()
    added_indexes: tuple[str, ...] = ()


def _load_current_metadata(extra_model_modules: Iterable[str]) -> MetaData:
    # Importing the modules registers their mapped tables on the shared Base.
    # Callers that place models in another module can pass that module name to
    # ``run_migrations(model_modules=(... ,))`` without changing this runner.
    for module_name in (*_DEFAULT_MODEL_MODULES, *tuple(extra_model_modules)):
        import_module(module_name)
    return Base.metadata


def _qualified_name(table: Table) -> str:
    return table.fullname


def _has_table(connection: Connection, table: Table) -> bool:
    return inspect(connection).has_table(table.name, schema=table.schema)


def _column_names(connection: Connection, table: Table) -> set[str]:
    return {
        column["name"]
        for column in inspect(connection).get_columns(table.name, schema=table.schema)
    }


def _index_names(connection: Connection, table: Table) -> set[str]:
    if not _has_table(connection, table):
        return set()
    return {
        index["name"]
        for index in inspect(connection).get_indexes(table.name, schema=table.schema)
        if index.get("name")
    }


def _python_default_sql(column: Column, dialect: Dialect) -> str | None:
    """Render a trusted scalar Python-side default as a DDL literal.

    ``mapped_column(default=...)`` normally runs in the ORM and therefore does
    not appear in ``ALTER TABLE`` DDL.  Rendering scalar defaults here lets a
    required additive field (for example ``visibility='private'``) preserve and
    backfill legacy rows.  Callable defaults are deliberately rejected because
    their value may vary by row or require application context.
    """

    default = column.default
    if default is None or not default.is_scalar:
        return None
    return str(
        literal(default.arg, type_=column.type).compile(
            dialect=dialect,
            compile_kwargs={"literal_binds": True},
        )
    )


def _has_usable_default(column: Column, dialect: Dialect) -> bool:
    return column.server_default is not None or _python_default_sql(column, dialect) is not None


def _table_has_rows(connection: Connection, table: Table) -> bool:
    query = select(literal(1)).select_from(table).limit(1)
    return connection.execute(query).first() is not None


def _validate_additive_columns(connection: Connection, metadata: MetaData) -> None:
    """Validate every existing table before issuing any project DDL."""

    for table in metadata.sorted_tables:
        if not _has_table(connection, table):
            continue
        existing_columns = _column_names(connection, table)
        missing_columns = [column for column in table.columns if column.name not in existing_columns]
        if not missing_columns:
            continue

        has_rows: bool | None = None
        for column in missing_columns:
            object_name = f"{_qualified_name(table)}.{column.name}"
            if column.primary_key:
                raise UnsafeMigrationError(
                    f"Cannot add missing primary-key column {object_name!r} additively"
                )
            if column.nullable or _has_usable_default(column, connection.dialect):
                continue
            if has_rows is None:
                has_rows = _table_has_rows(connection, table)
            row_detail = "a populated" if has_rows else "an existing"
            raise UnsafeMigrationError(
                f"Cannot add required column {object_name!r} to {row_detail} table without "
                "a scalar default or server_default"
            )


def _column_ddl(column: Column, dialect: Dialect) -> str:
    definition = str(CreateColumn(column).compile(dialect=dialect))
    # SQLAlchemy renders a ForeignKeyConstraint at table level, so
    # CreateColumn alone omits REFERENCES.  A one-column FK can safely be
    # expressed inline by both PostgreSQL and SQLite's ADD COLUMN syntax.
    # Multi-column constraints need an explicit versioned migration and are
    # intentionally not weakened into independent single-column constraints.
    preparer = dialect.identifier_preparer
    for foreign_key in sorted(column.foreign_keys, key=lambda value: value.target_fullname):
        if len(foreign_key.constraint.elements) != 1:
            continue
        target_column = foreign_key.column
        constraint_name = foreign_key.constraint.name
        if constraint_name:
            definition += f" CONSTRAINT {preparer.quote(constraint_name)}"
        definition += (
            f" REFERENCES {preparer.format_table(target_column.table)}"
            f" ({preparer.quote(target_column.name)})"
        )
        if foreign_key.ondelete:
            definition += f" ON DELETE {foreign_key.ondelete}"
        if foreign_key.onupdate:
            definition += f" ON UPDATE {foreign_key.onupdate}"
        if foreign_key.deferrable is not None:
            definition += " DEFERRABLE" if foreign_key.deferrable else " NOT DEFERRABLE"
        if foreign_key.initially:
            definition += f" INITIALLY {foreign_key.initially}"
    # CreateColumn includes server defaults, but intentionally ignores ORM-only
    # defaults.  Append a scalar ORM default so existing rows receive a value.
    if column.server_default is None:
        default_sql = _python_default_sql(column, dialect)
        if default_sql is not None:
            definition += f" DEFAULT {default_sql}"
    return definition


def _add_missing_columns(connection: Connection, metadata: MetaData) -> list[str]:
    added: list[str] = []
    preparer = connection.dialect.identifier_preparer
    for table in metadata.sorted_tables:
        if not _has_table(connection, table):
            # create_all should already have created it; this guard makes an
            # unsupported schema/permission failure explicit and easy to trace.
            raise MigrationError(f"Table {_qualified_name(table)!r} was not created")
        existing_columns = _column_names(connection, table)
        table_sql = preparer.format_table(table)
        for column in table.columns:
            if column.name in existing_columns:
                continue
            column_sql = _column_ddl(column, connection.dialect)
            connection.execute(text(f"ALTER TABLE {table_sql} ADD COLUMN {column_sql}"))
            added.append(f"{_qualified_name(table)}.{column.name}")
            existing_columns.add(column.name)
    return added


def _create_declared_indexes(connection: Connection, metadata: MetaData) -> None:
    for table in metadata.sorted_tables:
        for index in sorted(table.indexes, key=lambda value: value.name or ""):
            index.create(bind=connection, checkfirst=True)


def _run(connection: Connection, metadata: MetaData) -> MigrationReport:
    if connection.dialect.name == "postgresql":
        # All application instances use the same transaction-scoped lock, so a
        # rolling ECS deployment cannot race on ADD COLUMN / CREATE INDEX.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
            {"lock_name": _POSTGRES_LOCK_NAME},
        )

    tables_before = {
        _qualified_name(table)
        for table in metadata.sorted_tables
        if _has_table(connection, table)
    }
    indexes_before = {
        _qualified_name(table): _index_names(connection, table)
        for table in metadata.sorted_tables
    }

    # Preflight first so an unsafe new field does not leave SQLite with a
    # partially upgraded schema (some SQLite DDL cannot be rolled back in every
    # driver/configuration combination).
    _validate_additive_columns(connection, metadata)
    metadata.create_all(bind=connection, checkfirst=True)
    added_columns = _add_missing_columns(connection, metadata)
    _create_declared_indexes(connection, metadata)

    tables_after = {
        _qualified_name(table)
        for table in metadata.sorted_tables
        if _has_table(connection, table)
    }
    added_indexes: list[str] = []
    for table in metadata.sorted_tables:
        before = indexes_before.get(_qualified_name(table), set())
        for index_name in sorted(_index_names(connection, table) - before):
            added_indexes.append(f"{_qualified_name(table)}.{index_name}")

    return MigrationReport(
        added_tables=tuple(sorted(tables_after - tables_before)),
        added_columns=tuple(sorted(added_columns)),
        added_indexes=tuple(sorted(added_indexes)),
    )


def run_migrations(
    bind: Engine | Connection | None = None,
    *,
    metadata: MetaData | None = None,
    model_modules: Iterable[str] = (),
) -> MigrationReport:
    """Apply all safe additive changes and return what this call created.

    ``metadata`` is injectable for tests and maintenance tooling.  Normal app
    startup should omit it so all current mapped model modules are registered.
    Passing an existing ``Connection`` honors its transaction; passing an
    ``Engine`` (or nothing) runs the migration in a new transaction.
    """

    target_metadata = metadata or _load_current_metadata(model_modules)
    target_bind = bind or default_engine

    with _process_lock:
        if isinstance(target_bind, Connection):
            context = nullcontext(target_bind)
        else:
            context = target_bind.begin()
        with context as connection:
            return _run(connection, target_metadata)
