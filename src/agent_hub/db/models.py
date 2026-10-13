import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent_hub.config import settings


class Base(DeclarativeBase):
    pass


def _uuid():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = _uuid()
    slug: Mapped[str] = mapped_column(String, unique=True)
    root_path: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _now()


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[uuid.UUID] = _uuid()
    name: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)  # claude-code|codex|agy|kiro|zcode|mlx|custom|human
    model: Mapped[str | None] = mapped_column(String)
    token_hash: Mapped[str | None] = mapped_column(String, unique=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    created_at: Mapped[datetime] = _now()


class AgentProjectAccess(Base):
    __tablename__ = "agent_project_access"
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    perms: Mapped[list[str]] = mapped_column(ARRAY(String), default=lambda: ["read", "write"])


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[uuid.UUID] = _uuid()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String, default="note")  # fact|decision|lesson|note
    content: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(settings.embed_dim))
    source_agent: Mapped[str | None] = mapped_column(String)
    source_ref: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|verified|superseded
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id"))
    created_at: Mapped[datetime] = _now()
    promoted_by: Mapped[str | None] = mapped_column(String)
    tsv = mapped_column(TSVECTOR)  # maintained by trigger in migration

    __table_args__ = (
        Index("ix_memories_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_memories_embedding", "embedding", postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = _uuid()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String)
    spec: Mapped[str] = mapped_column(Text, default="")
    role_required: Mapped[str] = mapped_column(String, default="developer")
    # pending|ready|claimed|in_progress|review|done|failed|blocked|awaiting_approval
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    result: Mapped[str | None] = mapped_column(Text)
    worktree_path: Mapped[str | None] = mapped_column(String)
    branch: Mapped[str | None] = mapped_column(String)
    requires_approval: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TaskDep(Base):
    __tablename__ = "task_deps"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    depends_on: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _now()
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    agent_name: Mapped[str | None] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class SkillIndex(Base):
    __tablename__ = "skills"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
