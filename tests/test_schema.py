"""Schema-level checks that need no database connection."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import configure_mappers

from app.core.config import Settings
from app.db.models import Base

EXPECTED_TABLES = {
    "audit_logs",
    "collections",
    "conversations",
    "document_chunks",
    "documents",
    "ingestion_jobs",
    "messages",
    "namespace_members",
    "namespaces",
    "users",
}


def test_every_model_is_registered() -> None:
    """A model missing from `app.db.models` is invisible to autogenerate."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_relationships_resolve() -> None:
    """Fails loudly on a typo in any back_populates or primaryjoin."""
    configure_mappers()


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_tables_have_uuid_primary_key_and_timestamps(table: str) -> None:
    columns = Base.metadata.tables[table].columns
    assert columns["id"].primary_key
    assert columns["id"].type.python_type.__name__ == "UUID"
    assert "created_at" in columns


def test_tenant_scoped_tables_reference_a_namespace() -> None:
    """Every tenant-owned table must be filterable by namespace."""
    tenant_tables = {"collections", "documents", "conversations"}
    for table in tenant_tables:
        assert "namespace_id" in Base.metadata.tables[table].columns


def test_documents_namespace_cannot_drift_from_its_collection() -> None:
    """The composite foreign key is what makes the denormalised column safe."""
    constraints = {
        tuple(sorted(fk.column.name for fk in constraint.elements))
        for constraint in Base.metadata.tables["documents"].foreign_key_constraints
    }
    assert ("id", "namespace_id") in constraints


def test_transaction_pooler_is_detected_from_the_url() -> None:
    pooled = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
    )
    direct = Settings(
        _env_file=None, DATABASE_URL="postgresql://u:p@db.project.supabase.co:5432/postgres"
    )

    assert pooled.uses_transaction_pooler
    # Prepared statements cannot survive transaction-mode pooling.
    assert pooled.db_connect_args["prepare_threshold"] is None
    assert not direct.uses_transaction_pooler
    assert "prepare_threshold" not in direct.db_connect_args


def test_migrations_use_the_direct_endpoint_when_configured() -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
        DATABASE_DIRECT_URL="postgresql://u:p@db.project.supabase.co:5432/postgres",
    )
    assert settings.migration_database_url.startswith("postgresql+psycopg://")
    assert "db.project.supabase.co:5432" in settings.migration_database_url


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"SECRET_KEY": "change-me-in-production"}, "SECRET_KEY"),
        ({"SECRET_KEY": "too-short"}, "SECRET_KEY"),
        ({"DEBUG": True}, "DEBUG"),
        ({"CORS_ORIGINS": "*"}, "CORS_ORIGINS"),
    ],
)
def test_production_refuses_an_unsafe_configuration(
    overrides: dict[str, object], expected: str
) -> None:
    """Startup must fail loudly rather than serve with a weak configuration."""
    config: dict[str, object] = {
        "DATABASE_URL": "postgresql://u:p@localhost:5432/postgres",
        "ENVIRONMENT": "production",
        "SECRET_KEY": "a-sufficiently-long-production-secret-key",
        "CORS_ORIGINS": "https://app.example.com",
        "DEBUG": False,
    }
    config.update(overrides)

    with pytest.raises(ValueError, match=expected):
        Settings(_env_file=None, **config)
