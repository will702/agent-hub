"""initial schema

Revision ID: 0001
"""
import sqlalchemy as sa
from alembic import op

from agent_hub.db.models import Base

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    Base.metadata.create_all(op.get_bind())

    # keep memories.tsv in sync with content
    op.execute("""
        CREATE FUNCTION memories_tsv_update() RETURNS trigger AS $$
        BEGIN NEW.tsv := to_tsvector('english', coalesce(NEW.content,'')); RETURN NEW; END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER memories_tsv BEFORE INSERT OR UPDATE OF content ON memories
        FOR EACH ROW EXECUTE FUNCTION memories_tsv_update();
    """)

    # events are append-only
    op.execute("""
        CREATE FUNCTION events_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'events is append-only'; END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER events_no_update BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION events_immutable();
    """)

    # push new events to listeners (dashboard SSE)
    op.execute("""
        CREATE FUNCTION events_notify() RETURNS trigger AS $$
        BEGIN PERFORM pg_notify('events', NEW.id::text); RETURN NEW; END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER events_notify AFTER INSERT ON events
        FOR EACH ROW EXECUTE FUNCTION events_notify();
    """)


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
    op.execute("DROP FUNCTION IF EXISTS memories_tsv_update CASCADE")
    op.execute("DROP FUNCTION IF EXISTS events_immutable CASCADE")
    op.execute("DROP FUNCTION IF EXISTS events_notify CASCADE")
