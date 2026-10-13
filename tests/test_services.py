import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from agent_hub.db.session import Session
from agent_hub.services import auth, events, memory, tasks


async def mk(e, title, **kw):
    async with Session() as s:
        t = await tasks.create(s, e.dev, e.project.id, title, **kw)
        await s.commit()
    return t


async def status_of(tid):
    async with Session() as s:
        return (await s.execute(text("SELECT status FROM tasks WHERE id=:i"), {"i": tid})).scalar_one()


async def test_concurrent_claim(env):
    for i in range(5):
        await mk(env, f"t{i}")

    async def go():
        async with Session() as s:
            r = await tasks.claim(s, env.dev, env.project.id)
            await s.commit()
            return r

    res = await asyncio.gather(*[go() for _ in range(20)])
    winners = [r for r in res if r]
    assert len(winners) == 5
    assert len({r["id"] for r in winners}) == 5


async def test_lease_expiry_and_max_attempts(env):
    t = await mk(env, "x", max_attempts=2)
    for attempt in (1, 2):
        async with Session() as s:
            c = await tasks.claim(s, env.dev, env.project.id)
            assert c["id"] == t["id"] and c["attempts"] == attempt
            await s.execute(
                text("UPDATE tasks SET lease_expires_at = now() - interval '1 minute' WHERE id=:i"),
                {"i": t["id"]},
            )
            await s.commit()
        async with Session() as s:
            assert await tasks.reap_expired(s) >= 1
            await s.commit()
        assert await status_of(t["id"]) == ("ready" if attempt == 1 else "failed")


async def test_heartbeat_only_claimer(env):
    await mk(env, "x")
    async with Session() as s:
        c = await tasks.claim(s, env.dev, env.project.id)
        await tasks.heartbeat(s, env.dev, c["id"])
        with pytest.raises(PermissionError):
            await tasks.heartbeat(s, env.dev2, c["id"])


async def test_dependency_promotion(env):
    a = await mk(env, "a")
    b = await mk(env, "b")
    c = await mk(env, "c", depends_on=[a["id"], b["id"]])
    assert c["status"] == "pending"
    async with Session() as s:
        for t in (a, b):
            await tasks.claim(s, env.dev, env.project.id, task_id=t["id"])
            await tasks.update(s, env.dev, t["id"], "done")
            await s.commit()
            expected = "pending" if t is a else "ready"
            assert await status_of(c["id"]) == expected


async def test_dep_validation(env):
    t = await mk(env, "a")
    async with Session() as s:
        with pytest.raises(ValueError):
            await tasks.create(s, env.dev, env.project.id, "z", depends_on=[__import__("uuid").uuid4()])
        await s.rollback()
        await auth.grant(s, env.dev.id, env.other.id, ["read", "write"])
        with pytest.raises(ValueError):
            await tasks.create(s, env.dev, env.other.id, "z", depends_on=[t["id"]])


async def test_approval_gate(env):
    t = await mk(env, "a", requires_approval=True)
    d = await mk(env, "d", depends_on=[t["id"]])
    async with Session() as s:
        await tasks.claim(s, env.dev, env.project.id, task_id=t["id"])
        r = await tasks.update(s, env.dev, t["id"], "done")
        assert r["status"] == "awaiting_approval"
        await s.commit()
    assert await status_of(d["id"]) == "pending"
    async with Session() as s:
        with pytest.raises(PermissionError):
            await tasks.approve(s, env.dev, t["id"])
        r = await tasks.approve(s, env.human, t["id"])
        assert r["status"] == "done"
        await s.commit()
    assert await status_of(d["id"]) == "ready"


async def test_memory_lifecycle(env):
    async with Session() as s:
        m = await memory.write(s, env.dev, env.project.id, "The deploy key rotates on zorblax day")
        assert m.status == "draft"
        await s.commit()
        assert await memory.search(s, env.dev, env.project.id, "zorblax") == []
        assert len(await memory.search(s, env.dev, env.project.id, "zorblax", include_drafts=True)) == 1
        with pytest.raises(PermissionError):
            await memory.promote(s, env.dev, m.id)
        await memory.promote(s, env.rev, m.id)
        await s.commit()
        r = await memory.search(s, env.dev, env.project.id, "zorblax")
        assert [x["id"] for x in r] == [m.id] and r[0]["status"] == "verified"

        new = await memory.supersede(s, env.rev, m.id, "Deploy key now rotates on quuxify day")
        await s.commit()
        assert new.version == 2 and new.supersedes_id == m.id and new.status == "draft"
        r = await memory.search(s, env.dev, env.project.id, "zorblax", include_drafts=True)
        assert m.id not in [x["id"] for x in r]


async def test_hybrid_ranking(env):
    async with Session() as s:
        texts = [
            "Database migrations run through alembic on startup",
            "The frontend uses tailwind for styling components",
            "Logging is configured in the main entrypoint module",
            "Retry policy uses exponential backoff for http calls",
            "Code owners review every pull request before merge",
        ]
        planted = await memory.write(s, env.dev, env.project.id, "The staging cluster password lives in vault path kestrel-9")
        ids = [planted.id]
        for t in texts:
            ids.append((await memory.write(s, env.dev, env.project.id, t)).id)
        for i in ids:
            await memory.promote(s, env.rev, i)
        await s.commit()
        r = await memory.search(s, env.dev, env.project.id, "kestrel-9 vault")
        assert r[0]["id"] == planted.id


async def test_events_immutable(env):
    async with Session() as s:
        ev = await events.publish(s, type="t.x", project_id=env.project.id)
        await s.commit()
        got = await events.tail(s, env.project.id, after_id=ev.id - 1)
        assert got[0]["type"] == "t.x"
    for sql in ("UPDATE events SET type='y' WHERE id=:i", "DELETE FROM events WHERE id=:i"):
        async with Session() as s:
            with pytest.raises(DBAPIError):
                await s.execute(text(sql), {"i": ev.id})


async def test_project_isolation(env):
    async with Session() as s:
        with pytest.raises(PermissionError):
            await memory.search(s, env.dev, env.other.id, "x")
        with pytest.raises(PermissionError):
            await tasks.list(s, env.dev, env.other.id)
        with pytest.raises(PermissionError):
            await memory.write(s, env.dev, env.other.id, "x")
        assert await tasks.list(s, env.human, env.other.id) == []


async def test_auth_roundtrip(env):
    async with Session() as s:
        a, tok = await auth.create_agent(s, f"a-{env.dev.name}", "custom", [])
        assert tok.startswith("ah_")
        assert (await auth.authenticate(s, tok)).id == a.id
        assert await auth.authenticate(s, "ah_nope") is None


async def test_resolve_project_by_cwd(env):
    import uuid

    base = f"/tmp/{uuid.uuid4().hex}"
    async with Session() as s:
        outer = await auth.get_or_create_project(s, f"o-{uuid.uuid4().hex[:8]}", base)
        inner = await auth.get_or_create_project(s, f"i-{uuid.uuid4().hex[:8]}", base + "/sub")
        await s.commit()
        assert (await auth.resolve_project(s, None, base + "/x/y")).id == outer.id
        assert (await auth.resolve_project(s, None, base + "/sub/deep")).id == inner.id
        assert (await auth.resolve_project(s, env.project.slug, "/nowhere")).id == env.project.id
        with pytest.raises(LookupError):
            await auth.resolve_project(s, None, base + "-other")


async def test_claim_rejects_foreign_role(env):
    await mk(env, "rev-only", role_required="reviewer")
    async with Session() as s:
        with pytest.raises(PermissionError):
            await tasks.claim(s, env.dev, env.project.id, role="reviewer")


async def test_failed_task_cannot_be_revived_by_claimer(env):
    t = await mk(env, "x", max_attempts=1)
    async with Session() as s:
        await tasks.claim(s, env.dev, env.project.id)
        await s.execute(
            text("UPDATE tasks SET lease_expires_at = now() - interval '1 minute' WHERE id=:i"), {"i": t["id"]}
        )
        await s.commit()
    async with Session() as s:
        await tasks.reap_expired(s)
        await s.commit()
    assert await status_of(t["id"]) == "failed"
    async with Session() as s:
        with pytest.raises(ValueError):
            await tasks.update(s, env.dev, t["id"], "done")


async def test_supersede_verified_requires_reviewer(env):
    async with Session() as s:
        m = await memory.write(s, env.dev, env.project.id, "fact", "use pg16")
        await memory.promote(s, env.rev, m.id)
        await s.commit()
    async with Session() as s:
        with pytest.raises(PermissionError):
            await memory.supersede(s, env.dev, m.id, "use pg17")
