"""chunks table

Revision ID: 5e9379bc5e6c
Revises: 55ca5286fb5b
Create Date: 2026-08-30 15:12:47.249226

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5e9379bc5e6c"
down_revision: str | Sequence[str] | None = "55ca5286fb5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.String(length=520), nullable=False),
        sa.Column("source_id", sa.String(length=500), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=100), server_default="", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_tokens", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "chunk_id"),
    )
    op.create_index(op.f("ix_chunks_owner_id"), "chunks", ["owner_id"])
    op.create_index(op.f("ix_chunks_company_id"), "chunks", ["company_id"])
    op.create_index(op.f("ix_chunks_document_id"), "chunks", ["document_id"])
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_chunks_text_tokens_gin", "chunks", ["text_tokens"], postgresql_using="gin"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chunks_text_tokens_gin", table_name="chunks")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_index(op.f("ix_chunks_document_id"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_company_id"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_owner_id"), table_name="chunks")
    op.drop_table("chunks")
