"""Tool-calling loop for a local OpenAI-compatible model (e.g. `mlx_lm.server`).

Local models get a deliberately small tool set: search memory, write *draft* memories, and
finish the task as `review` or `failed`. No shell, no file writes, no promotion. Hub services
are called directly as the worker's agent, so the same access checks apply as over MCP.
"""
import json
import os
from dataclasses import dataclass

import httpx

from agent_hub.db.session import Session
from agent_hub.services import auth, memory, tasks

TOOLS = [
    {"type": "function", "function": {
        "name": "memory_search", "description": "Search verified project memory.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_write", "description": "Store a durable finding as a draft memory (one fact, with the reason). Never include secrets.",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}, "kind": {"type": "string", "enum": ["fact", "decision", "lesson", "note"]}},
                       "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "finish", "description": "Finish the task. Use status 'review' when done or 'failed' if you could not complete it.",
        "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["review", "failed"]}, "result": {"type": "string"}},
                       "required": ["status", "result"]}}},
]


@dataclass
class Outcome:
    status: str
    result: str
    steps: int


async def _call_tool(agent, project_id, task_id, name: str, args: dict) -> tuple[str, Outcome | None]:
    async with Session() as s:
        if name == "memory_search":
            rows = await memory.search(s, agent, project_id, str(args.get("query", "")))
            return json.dumps([{"kind": r["kind"], "content": r["content"]} for r in rows])[:6000], None
        if name == "memory_write":
            m = await memory.write(s, agent, project_id, str(args["content"]), kind=str(args.get("kind", "note")),
                                   source_ref=f"task:{task_id}")
            await s.commit()
            return f"stored draft memory {m.id}", None
        if name == "finish":
            status = args.get("status")
            if status not in ("review", "failed"):
                return "status must be 'review' or 'failed'", None
            result = str(args.get("result", ""))
            await tasks.update(s, agent, task_id, status, result=result)
            await s.commit()
            return "task updated", Outcome(status, result, 0)
    return f"unknown tool {name!r}", None


async def run_task(
    agent, project_id, task: dict, system_prompt: str, base_url: str, model: str,
    max_steps: int = 12, client: httpx.AsyncClient | None = None,
) -> Outcome:
    """Run one claimed task to completion. Marks it failed if the model never calls `finish`."""
    msgs = [
        {"role": "system", "content": f"{system_prompt}\n\nWork only through the provided tools. When done, call finish."},
        {"role": "user", "content": f"Task: {task['title']}\n\n{task.get('spec') or ''}"},
    ]
    own = client is None
    client = client or httpx.AsyncClient(timeout=600)
    try:
        for step in range(1, max_steps + 1):
            r = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                                  json={"model": model, "messages": msgs, "tools": TOOLS, "temperature": 0.2})
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []
            if not calls:
                msgs.append({"role": "user", "content": "Use a tool. Call finish when you are done."})
                continue
            for c in calls:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    out, outcome = await _call_tool(agent, project_id, task["id"], fn["name"], args)
                except Exception as e:  # noqa: BLE001 - reported back to the model, not fatal
                    out, outcome = f"error: {type(e).__name__}: {str(e)[:200]}", None
                if outcome:
                    outcome.steps = step
                    return outcome
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
        result = f"model did not finish within {max_steps} steps"
        async with Session() as s:
            await tasks.update(s, agent, task["id"], "failed", result=result)
            await s.commit()
        return Outcome("failed", result, max_steps)
    finally:
        if own:
            await client.aclose()


async def main(agent_name: str, project_slug: str, role: str, system_prompt: str) -> int:
    token = os.environ.get("AGENT_HUB_TOKEN_MLX", "").strip()
    if not token:
        print("AGENT_HUB_TOKEN_MLX is not set")
        return 2
    base = os.environ.get("MLX_BASE_URL", "http://127.0.0.1:8080/v1")
    model = os.environ.get("MLX_MODEL", "default")
    async with Session() as s:
        agent = await auth.authenticate(s, token)
        if agent is None:
            print("invalid token")
            return 2
        project = await auth.resolve_project(s, project_slug, None)
        task = await tasks.claim(s, agent, project.id, role=role)
        await s.commit()
    if task is None:
        print("no ready task")
        return 0
    out = await run_task(agent, project.id, task, system_prompt, base, model)
    print(f"{task['id']} -> {out.status} in {out.steps} step(s)")
    return 0 if out.status == "review" else 1
