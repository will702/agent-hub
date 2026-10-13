import asyncio
import json
import socket

import httpx2
import pytest
import pytest_asyncio
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from agent_hub.config import settings
from agent_hub.db.session import Session
from agent_hub.mcp_server import build_app
from agent_hub.services import auth
from conftest import uniq

EXPECTED_TOOLS = {
    "memory_search", "memory_write", "memory_promote", "memory_supersede",
    "task_create", "task_list", "task_claim", "task_heartbeat", "task_update",
    "event_publish", "event_tail", "skill_list", "skill_read", "project_context",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="session")
async def server_url():
    settings.embedder = "hash"  # session-scoped: the autouse monkeypatch is per-test
    port = _free_port()
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started, "server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await asyncio.wait_for(task, 10)


@pytest_asyncio.fixture
async def tokens():
    """Project A (dev, dev2, reviewer have access) + project B (only `onlyb` has access)."""
    async with Session() as s:
        a = await auth.get_or_create_project(s, uniq("mcpA"))
        b = await auth.get_or_create_project(s, uniq("mcpB"))
        out = {"a": a.slug, "b": b.slug}
        for key, roles, proj in [
            ("dev", ["developer"], a), ("rev", ["reviewer"], a), ("onlya", ["developer"], a),
            ("onlyb", ["developer"], b),
        ]:
            ag, tok = await auth.create_agent(s, uniq(key), "custom", roles)
            await auth.grant(s, ag.id, proj.id, ["read", "write"])
            out[key] = tok
        await s.commit()
    return out


def client(url: str, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    http = create_mcp_http_client(headers=headers)
    return Client(streamable_http_client(url, http_client=http))


def payload(res):
    """Parse a tool result into python data; raises AssertionError-friendly info on errors."""
    if res.structured_content is not None:
        sc = res.structured_content
        return sc["result"] if set(sc) == {"result"} else sc
    texts = [c.text for c in res.content]
    return json.loads(texts[0]) if len(texts) == 1 else [json.loads(t) for t in texts]


async def call(c, tool, **args):
    return await c.call_tool(tool, args)


async def test_no_token_rejected(server_url):
    async with httpx2.AsyncClient() as h:
        r = await h.post(server_url, json={}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
        r = await h.post(server_url, json={}, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
    with pytest.raises(BaseException):
        async with client(server_url, None) as c:
            await c.list_tools()
    with pytest.raises(BaseException):
        async with client(server_url, "ah_bogus") as c:
            await c.list_tools()


async def test_list_tools(server_url, tokens):
    async with client(server_url, tokens["dev"]) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert EXPECTED_TOOLS <= names
        skills = payload(await call(c, "skill_list"))
        assert any(s["name"] == "agent-hub-usage" for s in skills)
        text = payload(await call(c, "skill_read", name="agent-hub-usage"))
        assert "agent-hub" in text.lower()
        bad = await call(c, "skill_read", name="../../etc/passwd")
        assert bad.is_error
        res = await c.read_resource("skill://agent-hub-usage")
        assert res.contents


async def test_memory_roundtrip(server_url, tokens):
    async with client(server_url, tokens["dev"]) as c:
        w = await call(c, "memory_write", content="the zebra protocol uses port 4242", project=tokens["a"], kind="fact")
        assert not w.is_error
        mem = payload(w)
        assert mem["status"] == "draft"
        hits = payload(await call(c, "memory_search", query="zebra protocol port", project=tokens["a"], include_drafts=True))
        assert any("zebra" in h["content"] for h in hits)
        hits = payload(await call(c, "memory_search", query="zebra protocol port", project=tokens["a"]))
        assert not any("zebra" in h["content"] for h in hits)
        # developer cannot promote; reviewer can
        p = await call(c, "memory_promote", memory_id=mem["id"])
        assert p.is_error and "permission" in p.content[0].text
    async with client(server_url, tokens["rev"]) as c:
        assert not (await call(c, "memory_promote", memory_id=mem["id"])).is_error
        ctx = payload(await call(c, "project_context", project=tokens["a"]))
        assert ctx["project"] == tokens["a"]


async def test_task_roles(server_url, tokens):
    async with client(server_url, tokens["dev"]) as c:
        t = payload(await call(c, "task_create", title="do it", project=tokens["a"]))
        assert t["status"] == "ready"
    async with client(server_url, tokens["rev"]) as c:
        r = payload(await call(c, "task_claim", project=tokens["a"]))
        assert r.get("task", 1) is None  # reviewer role cannot claim a developer task
    async with client(server_url, tokens["dev"]) as c:
        got = payload(await call(c, "task_claim", project=tokens["a"]))
        assert got["id"] == t["id"] and got["status"] == "claimed"
        hb = await call(c, "task_heartbeat", task_id=t["id"])
        assert not hb.is_error
        up = payload(await call(c, "task_update", task_id=t["id"], status="done", result="ok"))
        assert up["status"] == "done"
        ev = payload(await call(c, "event_tail", project=tokens["a"]))
        assert any(e["type"] == "task.done" for e in ev)


async def test_project_isolation(server_url, tokens):
    async with client(server_url, tokens["dev"]) as c:
        for name, args in [
            ("memory_search", dict(query="x", project=tokens["b"])),
            ("memory_write", dict(content="x", project=tokens["b"])),
            ("task_list", dict(project=tokens["b"])),
            ("event_tail", dict(project=tokens["b"])),
            ("project_context", dict(project=tokens["b"])),
        ]:
            r = await call(c, name, **args)
            assert r.is_error, name
            assert "permission denied" in r.content[0].text, name
            assert "Traceback" not in r.content[0].text
        r = await call(c, "task_list", project="no-such-project")
        assert r.is_error and "unknown project" in r.content[0].text
        with pytest.raises(BaseException):
            await c.read_resource(f"memory://decisions/{tokens['b']}")
    # the b-only agent sees B but not A
    async with client(server_url, tokens["onlyb"]) as c:
        assert not (await call(c, "task_list", project=tokens["b"])).is_error
        assert (await call(c, "task_list", project=tokens["a"])).is_error


async def test_claim_worktree(server_url, tokens, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    async with Session() as s:
        p = await auth.get_or_create_project(s, tokens["a"], root_path=str(repo))
        p.root_path = str(repo)
        plain = await auth.get_or_create_project(s, uniq("nogit"), root_path=str(tmp_path))
        await s.commit()
    async with client(server_url, tokens["dev"]) as c:
        t = payload(await call(c, "task_create", title="wt", project=tokens["a"]))
        got = payload(await call(c, "task_claim", project=tokens["a"], worktree=True))
        short = t["id"].replace("-", "")[:8]
        assert got["branch"] == f"hub/{short}"
        assert got["worktree_path"] == f"{repo}-wt/{short}"
        assert (tmp_path / f"repo-wt/{short}").is_dir()


def test_skill_read_rejects_traversal():
    from agent_hub.services import skills

    for bad in ["..", ".", "", "a/b", "../x", "/etc/passwd", "a\\b"]:
        with pytest.raises(ValueError):
            skills.read(bad)
    with pytest.raises(LookupError):
        skills.read("does-not-exist")
    assert skills.read("agent-hub-usage").startswith("---")
