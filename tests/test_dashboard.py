import httpx
import pytest

from agent_hub.dashboard import create_app
from agent_hub.db.session import Session
from agent_hub.services import auth, memory, tasks

from conftest import uniq


@pytest.fixture
async def dash(env):
    async with Session() as s:
        _, token = await auth.create_agent(s, uniq("dashhuman"), "human", ["human"])
        await s.commit()
    app = create_app(token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        c.csrf = app.state.csrf
        yield c


async def test_pages_render_and_escape(env, dash):
    async with Session() as s:
        await tasks.create(s, env.dev, env.project.id, "<script>alert(1)</script>")
        await s.commit()
    for path in ("/", "/approvals", "/memory", "/events", "/agents"):
        assert (await dash.get(path)).status_code == 200
    body = (await dash.get("/")).text
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


async def test_approve_requires_csrf_and_works(env, dash):
    async with Session() as s:
        t = await tasks.create(s, env.dev, env.project.id, "gated", requires_approval=True)
        await tasks.claim(s, env.dev, env.project.id)
        await tasks.update(s, env.dev, t["id"], "done")
        await s.commit()
    assert (await dash.post(f"/tasks/{t['id']}/approve", data={"csrf": "wrong"})).status_code == 403
    r = await dash.post(f"/tasks/{t['id']}/approve", data={"csrf": dash.csrf})
    assert r.status_code == 303
    async with Session() as s:
        from sqlalchemy import text

        assert (await s.execute(text("SELECT status FROM tasks WHERE id=:i"), {"i": t["id"]})).scalar_one() == "done"


async def test_promote_memory(env, dash):
    async with Session() as s:
        m = await memory.write(s, env.dev, env.project.id, "fact", "dashboard promote me")
        await s.commit()
    r = await dash.post(f"/memory/{m.id}/promote", data={"csrf": dash.csrf})
    assert r.status_code == 303


async def test_bad_host_and_non_human_token(env):
    async with Session() as s:
        _, tok = await auth.create_agent(s, uniq("notahuman"), "custom", ["developer"])
        await s.commit()
    app = create_app(tok)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://evil.example") as c:
        assert (await c.get("/")).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        assert (await c.get("/")).status_code == 500
