import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


DOCUMENT_STATUSES = ("queued", "parsing", "chunking", "embedding", "ready", "failed")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("company_id", "sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    object_key: Mapped[str] = mapped_column(String(600))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


RESEARCH_STATUSES = ("queued", "running", "done", "failed")


class ResearchTask(Base):
    __tablename__ = "research_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    report_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSONB, nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


MESSAGE_ROLES = ("user", "assistant")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


USAGE_KINDS = ("research", "chat")


class TokenUsage(Base):
    """一次图执行的 token 账：研究任务一行、对话一轮一行。

    ref_id 不设 FK：对话在 assistant 消息落库前失败也要记账（钱已经烧了），
    那种行的 ref_id 为空；research 侧则总能指到 research_tasks.id。
    by_node 是节点级明细，配额只用总额，明细留给 P3.6 的看板。
    """

    __tablename__ = "token_usage"
    __table_args__ = (Index("ix_token_usage_owner_created", "owner_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    missing_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    by_node: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("company_id", "chunk_id"),
        # HNSW/GIN 声明在模型上：测试库 create_all 与 dev 迁移建出同构索引
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_text_tokens_gin", "text_tokens", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id"), index=True
    )
    chunk_id: Mapped[str] = mapped_column(String(520))  # "{source_id}:{seq}"
    source_id: Mapped[str] = mapped_column(String(500))
    page: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(100), server_default="")
    text: Mapped[str] = mapped_column(Text)
    # 词法列 = jieba 预分词 + to_tsvector('simple')；向量列 = 1024 维 HNSW 余弦
    text_tokens = mapped_column(TSVECTOR, nullable=False)
    embedding = mapped_column(Vector(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
