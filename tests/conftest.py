import uuid

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
