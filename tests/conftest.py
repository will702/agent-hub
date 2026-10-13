import asyncio
import os
import re
import uuid

# Tests run against their own database so they never pollute the real hub. This must happen
# before agent_hub is imported, because the engine is created at import time.
_base = os.environ.get("DATABASE_URL") or re.search(
    r"^DATABASE_URL=(.+)$", open(os.path.join(os.path.dirname(__file__), "..", ".env")).read(), re.M
).group(1).strip()
_TEST_DB = "agenthub_test"
os.environ["DATABASE_URL"] = re.sub(r"/[^/]+$", f"/{_TEST_DB}", _base)


def _bootstrap_test_db() -> None:
    import asyncpg
    from alembic import command

    from agent_hub.cli import _alembic_cfg

    async def create() -> None:
        dsn = re.sub(r"^postgresql\+asyncpg", "postgresql", re.sub(r"/[^/]+$", "/postgres", _base))
        conn = await asyncpg.connect(dsn)
        try:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB):
                await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
        finally:
            await conn.close()

    asyncio.run(create())
    command.upgrade(_alembic_cfg(), "head")


_bootstrap_test_db()

import pytest
import pytest_asyncio

from agent_hub.config import settings
from agent_hub.db.session import Session
from agent_hub.services import auth


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    monkeypatch.setattr(settings, "embedder", "hash")


def uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture
async def env():
    """Fresh project + agents per test (committed; unique names so no teardown needed)."""
    async with Session() as s:
        project = await auth.get_or_create_project(s, uniq("proj"))
        other = await auth.get_or_create_project(s, uniq("projB"))
        dev, _ = await auth.create_agent(s, uniq("dev"), "custom", ["developer"])
        dev2, _ = await auth.create_agent(s, uniq("dev2"), "custom", ["developer"])
        rev, _ = await auth.create_agent(s, uniq("rev"), "custom", ["reviewer"])
        human, _ = await auth.create_agent(s, uniq("human"), "human", ["human"])
        for a in (dev, dev2, rev):
            await auth.grant(s, a.id, project.id, ["read", "write"])
        await s.commit()
    return type("Env", (), dict(project=project, other=other, dev=dev, dev2=dev2, rev=rev, human=human))
