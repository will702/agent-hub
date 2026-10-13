import json

import httpx
from sqlalchemy import text

from agent_hub.db.session import Session
from agent_hub.orchestrator import AgentSpec, Spawn, build_command
from agent_hub.runners import mlx
from agent_hub.services import tasks


def scripted(*replies):
    """Fake OpenAI-compatible server: each reply is a list of (tool_name, args) or None for plain text."""
    it = iter(replies)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        calls = next(it)
        msg = {"role": "assistant", "content": "thinking"}
        if calls:
            msg["tool_calls"] = [
                {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                for i, (n, a) in enumerate(calls)
            ]
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def claimed(env):
    async with Session() as s:
        await tasks.create(s, env.dev, env.project.id, "research x", spec="find out x")
        t = await tasks.claim(s, env.dev, env.project.id)
        await s.commit()
    return t


async def fetch(sql, **p):
    async with Session() as s:
        return (await s.execute(text(sql), p)).all()


async def test_loop_writes_draft_memory_and_finishes_in_review(env):
    t = await claimed(env)
    client, seen = scripted(
        [("memory_write", {"content": "x is 42 because of y", "kind": "fact"})],
        [("finish", {"status": "review", "result": "x is 42"})],
    )
    out = await mlx.run_task(env.dev, env.project.id, t, "be brief", "http://fake/v1", "m", client=client)
    assert (out.status, out.steps) == ("review", 2)
    assert seen[0]["tools"] and {x["function"]["name"] for x in seen[0]["tools"]} == {"memory_search", "memory_write", "finish"}
    assert (await fetch("SELECT status FROM tasks WHERE id=:i", i=t["id"]))[0][0] == "review"
    rows = await fetch("SELECT status, source_agent FROM memories WHERE project_id=:p", p=env.project.id)
    assert rows == [("draft", env.dev.name)]


async def test_unknown_tool_is_reported_not_executed_and_cannot_mark_done(env):
    t = await claimed(env)
    client, seen = scripted(
        [("memory_promote", {"memory_id": "x"})],
        [("finish", {"status": "done", "result": "cheating"})],
        [("finish", {"status": "failed", "result": "gave up"})],
    )
    out = await mlx.run_task(env.dev, env.project.id, t, "p", "http://fake/v1", "m", client=client)
    assert out.status == "failed"
    tool_msgs = [m["content"] for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert any("unknown tool" in m for m in tool_msgs) and any("must be" in m for m in tool_msgs)


async def test_no_finish_marks_task_failed(env):
    t = await claimed(env)
    client, _ = scripted(None, None, None)
    out = await mlx.run_task(env.dev, env.project.id, t, "p", "http://fake/v1", "m", max_steps=3, client=client)
    assert out.status == "failed"
    assert (await fetch("SELECT status FROM tasks WHERE id=:i", i=t["id"]))[0][0] == "failed"


def test_mlx_spawn_command_has_no_shell_and_substitutes_fields():
    sp = Spawn(AgentSpec("developer-local", "mlx", ["researcher"]), "researcher", "proj")
    cmd = build_command(sp)
    assert cmd[1:3] == ["-m", "agent_hub.cli"] and "developer-local" in cmd and "proj" in cmd and "researcher" in cmd
