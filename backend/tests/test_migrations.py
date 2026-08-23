from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)

from app.migrations import UnsafeMigrationError, run_migrations


def _target_metadata() -> MetaData:
    """A compact version of the collaboration schema added after launch."""

    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(255), nullable=False),
    )
    regions = Table(
        "regions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(100), nullable=False),
    )
    chains = Table(
        "place_chains",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("title", String(180), nullable=False),
    )
    places = Table(
        "places",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("region_id", ForeignKey(regions.c.id), nullable=False),
        Column("title", String(180), nullable=False),
        Column("chain_id", ForeignKey(chains.c.id), nullable=True, index=True),
        # A Python-side scalar default is intentionally used: the migration
        # runner must render it for legacy rows even though create_all would not.
        Column("branch_name", String(80), nullable=False, default=""),
    )
    notes = Table(
        "place_notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("place_id", ForeignKey(places.c.id), nullable=False),
        Column("content", Text, nullable=False),
        Column("visibility", String(20), nullable=False, default="shared", index=True),
    )
    Table(
        "user_messages",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", ForeignKey(users.c.id), nullable=False, index=True),
        Column("content", Text, nullable=False),
    )
    Table(
        "place_contributors",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("place_id", ForeignKey(places.c.id), nullable=False, index=True),
        Column("user_id", ForeignKey(users.c.id), nullable=False, index=True),
    )
    Table(
        "place_insights",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("place_id", ForeignKey(places.c.id), nullable=False, index=True),
        Column("kind", String(40), nullable=False),
        Column("content", Text, nullable=False),
    )
    return metadata


def _create_legacy_database(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL)"))
        connection.execute(text("CREATE TABLE regions (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL)"))
        connection.execute(
            text(
                "CREATE TABLE places ("
                "id INTEGER PRIMARY KEY, region_id INTEGER NOT NULL, title VARCHAR(180) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE place_notes ("
                "id INTEGER PRIMARY KEY, place_id INTEGER NOT NULL, content TEXT NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO users VALUES (7, 'legacy@example.com')"))
        connection.execute(text("INSERT INTO regions VALUES (3, 'Ubud')"))
        connection.execute(text("INSERT INTO places VALUES (11, 3, 'Legacy temple')"))
        connection.execute(text("INSERT INTO place_notes VALUES (13, 11, 'Keep this note')"))


def test_legacy_sqlite_is_upgraded_preserved_and_rerunnable(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    metadata = _target_metadata()
    _create_legacy_database(engine)

    first = run_migrations(engine, metadata=metadata)

    inspector = inspect(engine)
    assert {
        "place_chains",
        "user_messages",
        "place_contributors",
        "place_insights",
    } <= set(inspector.get_table_names())
    assert {column["name"] for column in inspector.get_columns("places")} >= {
        "chain_id",
        "branch_name",
    }
    assert {column["name"] for column in inspector.get_columns("place_notes")} >= {
        "visibility"
    }
    assert "place_chains" in first.added_tables
    assert "places.chain_id" in first.added_columns
    assert "places.branch_name" in first.added_columns
    assert "place_notes.visibility" in first.added_columns

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id, region_id, title, chain_id, branch_name FROM places")
        ).one() == (11, 3, "Legacy temple", None, "")
        assert connection.execute(
            text("SELECT id, place_id, content, visibility FROM place_notes")
        ).one() == (13, 11, "Keep this note", "shared")

    assert any(
        foreign_key["constrained_columns"] == ["chain_id"]
        and foreign_key["referred_table"] == "place_chains"
        for foreign_key in inspector.get_foreign_keys("places")
    )

    assert any(
        index["name"] == "ix_places_chain_id"
        for index in inspector.get_indexes("places")
    )
    assert any(
        index["name"] == "ix_place_notes_visibility"
        for index in inspector.get_indexes("place_notes")
    )

    second = run_migrations(engine, metadata=metadata)
    assert second.added_tables == ()
    assert second.added_columns == ()
    assert second.added_indexes == ()

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM places")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM place_notes")).scalar_one() == 1


def test_unsafe_required_column_fails_before_any_schema_change(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unsafe.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO items VALUES (1, 'preserve me')"))

    metadata = MetaData()
    Table(
        "items",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", Text, nullable=False),
        Column("required_owner", String(100), nullable=False),
    )
    # This table would be created by create_all if preflight happened too late.
    Table("should_not_exist", metadata, Column("id", Integer, primary_key=True))

    try:
        run_migrations(engine, metadata=metadata)
    except UnsafeMigrationError as exc:
        assert "items.required_owner" in str(exc)
    else:  # pragma: no cover - makes an unexpected success explicit
        raise AssertionError("unsafe migration unexpectedly succeeded")

    inspector = inspect(engine)
    assert "should_not_exist" not in inspector.get_table_names()
    assert "required_owner" not in {
        column["name"] for column in inspector.get_columns("items")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, value FROM items")).one() == (1, "preserve me")
