"""Agent Hub MCP server (mcp 2.x: `MCPServer`, stateless streamable HTTP, bearer-token ASGI middleware)."""

import asyncio
import contextlib
import contextvars
import datetime as dt
import functools
import json
import logging
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from sqlalchemy import text

from agent_hub.config import settings
from agent_hub.db.models import Agent
from agent_hub.db.session import Session
from agent_hub.services import auth, events, memory, skills, tasks

log = logging.getLogger("agent_hub.mcp")

# Set by BearerAuthMiddleware for the duration of each request (server runs stateless, so every
# request gets its own task/context and tokens can never leak across requests).
current_agent: contextvars.ContextVar[Agent | None] = contextvars.ContextVar("current_agent", default=None)

REAP_INTERVAL = 60


# ---------------------------------------------------------------- helpers

def _agent() -> Agent:
    a = current_agent.get()
    if a is None:
        raise PermissionError("unauthenticated")
    return a


def _clean(v):
    """Make service results JSON-safe (UUID/datetime -> str, drop embeddings)."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if k not in ("embedding", "tsv")}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (uuid.UUID, dt.datetime, dt.date)):
        return str(v) if isinstance(v, uuid.UUID) else v.isoformat()
    return v


def _orm_dict(m) -> dict:
    return {
        c.name: getattr(m, c.name)
        for c in m.__table__.columns
        if c.name not in ("embedding", "tsv")
    }


def guarded(fn):
    """Map service errors to clean ToolErrors; never leak stack traces or internals."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return _clean(await fn(*args, **kwargs))
        except ToolError:
            raise
        except PermissionError as e:
            raise ToolError(f"permission denied: {e}") from None
        except (ValueError, LookupError) as e:
            raise ToolError(str(e.args[0]) if isinstance(e, LookupError) and e.args else str(e)) from None
        except Exception:
            log.exception("tool %s failed", fn.__name__)
            raise ToolError("internal error") from None

    return wrapper


async def _git(*argv: str, cwd: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def _create_worktree(root_path: str | None, task_id) -> tuple[str | None, str | None, str | None]:
    """Returns (worktree_path, branch, error)."""
    if not root_path or not Path(root_path).is_dir():
        return None, None, "project root_path is not set or does not exist"
    try:
        rc, out = await _git("git", "-C", root_path, "rev-parse", "--is-inside-work-tree")
        if rc != 0 or out != "true":
            return None, None, "project root_path is not a git repository"
        short = str(task_id).replace("-", "")[:8]
        wt = f"{root_path.rstrip('/')}-wt/{short}"
        branch = f"hub/{short}"
        rc, out = await _git("git", "-C", root_path, "worktree", "add", wt, "-b", branch)
        if rc != 0:
            return None, None, f"git worktree add failed: {out[:300]}"
        return wt, branch, None
    except Exception as e:  # noqa: BLE001 - never fail the claim
        return None, None, f"worktree creation failed: {type(e).__name__}"


# ---------------------------------------------------------------- lifespan

async def _reaper() -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL)
        try:
            async with Session() as s:
                n = await tasks.reap_expired(s)
                await s.commit()
            if n:
                log.info("reaped %d expired task leases", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lease reaper failed")


async def reindex_skills() -> int:
    async with Session() as s:
        n = await skills.index(s)
        await s.commit()
    return n


async def _skill_watcher() -> None:
    async def cb():
        try:
            await reindex_skills()
        except Exception:
            log.exception("skill reindex failed")

    try:
        await skills.watch(cb)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("skill watcher stopped")


@contextlib.asynccontextmanager
async def lifespan(_server):
    try:
        await reindex_skills()
    except Exception:
        log.exception("initial skill index failed")
    bg = [asyncio.create_task(_reaper()), asyncio.create_task(_skill_watcher())]
    try:
        yield {}
    finally:
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)


mcp = MCPServer(
    "agent-hub",
    instructions="Shared memory, tasks, events and skills for coding agents. Call project_context first.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- tools

@mcp.tool()
@guarded
async def memory_search(
    query: str, project: str | None = None, limit: int = 8, include_drafts: bool = False,
    cwd: str | None = None,
) -> list[dict]:
    """Hybrid (vector + full-text) search over project memories. Verified only unless include_drafts."""
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        res = await memory.search(s, _agent(), p.id, query, limit=limit, include_drafts=include_drafts)
        await s.commit()
        return res


@mcp.tool()
@guarded
async def memory_write(
    content: str, project: str | None = None, kind: str = "note", source_ref: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Write a draft memory (kind: fact|decision|lesson|note). A reviewer must promote it."""
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        m = await memory.write(s, _agent(), p.id, content, kind=kind, source_ref=source_ref)
        await s.commit()
        return _orm_dict(m)


@mcp.tool()
@guarded
async def memory_promote(memory_id: str) -> dict:
    """Promote a draft memory to verified (reviewer/human/admin only)."""
    async with Session() as s:
        m = await memory.promote(s, _agent(), memory_id)
        await s.commit()
        return _orm_dict(m)


@mcp.tool()
@guarded
async def memory_supersede(memory_id: str, new_content: str) -> dict:
    """Replace a memory with a new draft version; the old one becomes superseded."""
    async with Session() as s:
        m = await memory.supersede(s, _agent(), memory_id, new_content)
        await s.commit()
        return _orm_dict(m)


@mcp.tool()
@guarded
async def task_create(
    title: str, project: str | None = None, spec: str = "", role_required: str = "developer",
    depends_on: list[str] = [], priority: int = 0, requires_approval: bool = False,
    cwd: str | None = None,
) -> dict:
    """Create a task. Tasks with unfinished dependencies start as pending, otherwise ready."""
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        t = await tasks.create(
            s, _agent(), p.id, title, spec=spec, role_required=role_required,
            depends_on=list(depends_on), priority=priority, requires_approval=requires_approval,
        )
        await s.commit()
        return t


@mcp.tool()
@guarded
async def task_list(project: str | None = None, status: str | None = None, cwd: str | None = None) -> list[dict]:
    """List tasks in a project, optionally filtered by status."""
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        return await tasks.list(s, _agent(), p.id, status=status)


@mcp.tool()
@guarded
async def task_claim(
    project: str | None = None, role: str | None = None, worktree: bool = False, cwd: str | None = None
) -> dict:
    """Atomically claim the next ready task for your role. With worktree=true, also create a git worktree."""
    agent = _agent()
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        root_path = p.root_path
        t = await tasks.claim(s, agent, p.id, role=role)
        await s.commit()
    if t is None:
        return {"task": None, "message": "no ready task"}
    if worktree:
        wt, branch, err = await _create_worktree(root_path, t["id"])
        if err:
            t["worktree_error"] = err
        else:
            async with Session() as s:
                await s.execute(
                    text("UPDATE tasks SET worktree_path = :w, branch = :b, updated_at = now() WHERE id = :i"),
                    {"w": wt, "b": branch, "i": t["id"]},
                )
                await s.commit()
            t["worktree_path"], t["branch"] = wt, branch
    return t


@mcp.tool()
@guarded
async def task_heartbeat(task_id: str) -> dict:
    """Extend the lease on a task you have claimed."""
    async with Session() as s:
        t = await tasks.heartbeat(s, _agent(), task_id)
        await s.commit()
        return t


@mcp.tool()
@guarded
async def task_update(task_id: str, status: str, result: str | None = None, branch: str | None = None) -> dict:
    """Update a claimed task: in_progress|review|done|failed|blocked (done may need human approval)."""
    async with Session() as s:
        t = await tasks.update(s, _agent(), task_id, status, result=result, branch=branch)
        await s.commit()
        return t


@mcp.tool()
@guarded
async def event_publish(
    type: str, payload: dict = {}, project: str | None = None, task_id: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Publish an event to the project event log."""
    agent = _agent()
    async with Session() as s:
        project_id = None
        if project or cwd:
            p = await auth.resolve_project(s, project, cwd)
            await auth.require_access(s, agent, p.id, "write")
            project_id = p.id
        elif task_id:
            row = (
                await s.execute(text("SELECT project_id FROM tasks WHERE id = :i"), {"i": uuid.UUID(task_id)})
            ).first()
            if row is None:
                raise LookupError(f"task {task_id} not found")
            await auth.require_access(s, agent, row[0], "write")
            project_id = row[0]
        elif not ({"human", "admin"} & set(agent.roles or [])):
            raise ValueError("project (or cwd/task_id) is required")
        ev = await events.publish(
            s, type=type, project_id=project_id, task_id=uuid.UUID(task_id) if task_id else None,
            agent_name=agent.name, payload=payload,
        )
        await s.commit()
        return {"id": ev.id, "type": ev.type, "project_id": ev.project_id, "task_id": ev.task_id}


@mcp.tool()
@guarded
async def event_tail(
    project: str | None = None, after_id: int = 0, limit: int = 50, cwd: str | None = None
) -> list[dict]:
    """Read events after `after_id` for a project."""
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        await auth.require_access(s, _agent(), p.id, "read")
        return await events.tail(s, project_id=p.id, after_id=after_id, limit=limit)


@mcp.tool()
@guarded
async def skill_list() -> list[dict]:
    """List shared skills available to you (name + description)."""
    async with Session() as s:
        return await skills.list_for(s, _agent().name)


@mcp.tool()
@guarded
async def skill_read(name: str) -> str:
    """Read the full SKILL.md of a skill."""
    return skills.read(name, _agent().name)


async def _decisions(s, agent: Agent, project_id, limit: int = 10) -> list[dict]:
    await auth.require_access(s, agent, project_id, "read")
    rows = (
        await s.execute(
            text("""SELECT id, content, source_agent, created_at FROM memories
                    WHERE project_id = :p AND kind = 'decision' AND status = 'verified'
                    ORDER BY created_at DESC LIMIT :n"""),
            {"p": project_id, "n": limit},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _read_doc(root: str | None, name: str) -> str | None:
    if not root:
        return None
    f = Path(root) / name
    try:
        return f.read_text(encoding="utf-8", errors="replace")[:4000] if f.is_file() else None
    except OSError:
        return None


@mcp.tool()
@guarded
async def project_context(project: str | None = None, cwd: str | None = None) -> dict:
    """Project slug, README/ARCHITECTURE excerpts (first 4000 chars) and top verified decisions."""
    async with Session() as s:
        p = await auth.resolve_project(s, project, cwd)
        decisions = await _decisions(s, _agent(), p.id)
        return {
            "project": p.slug,
            "root_path": p.root_path,
            "readme": _read_doc(p.root_path, "README.md"),
            "architecture": _read_doc(p.root_path, "ARCHITECTURE.md"),
            "decisions": decisions,
        }


# ---------------------------------------------------------------- resources

@mcp.resource("skill://{name}", mime_type="text/markdown")
async def skill_resource(name: str) -> str:
    try:
        return skills.read(name, _agent().name)
    except (ValueError, LookupError, PermissionError) as e:
        raise ResourceError(str(e)) from None


@mcp.resource("memory://decisions/{project}", mime_type="application/json")
async def decisions_resource(project: str) -> str:
    try:
        async with Session() as s:
            p = await auth.resolve_project(s, project, None)
            rows = await _decisions(s, _agent(), p.id, limit=50)
        return json.dumps(_clean(rows), indent=2)
    except (ValueError, LookupError, PermissionError) as e:
        raise ResourceError(str(e)) from None


# ---------------------------------------------------------------- ASGI app

class BearerAuthMiddleware:
    """Pure ASGI middleware: require `Authorization: Bearer <token>`, store the Agent in a contextvar."""

    def __init__(self, app):
        self.app = app

    async def _reject(self, send, msg: str) -> None:
        body = json.dumps({"error": "unauthorized", "message": msg}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="agent-hub"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan / websocket pass straight through
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = raw.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return await self._reject(send, "missing bearer token")
        try:
            async with Session() as s:
                agent = await auth.authenticate(s, token)
        except Exception:
            log.exception("authentication backend error")
            body = b'{"error":"internal error"}'
            await send({"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": body})
        if agent is None:
            return await self._reject(send, "invalid token")
        reset = current_agent.set(agent)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(reset)


def build_app():
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, host=settings.mcp_host
    )
    return BearerAuthMiddleware(inner)


def serve() -> None:
    import uvicorn

    uvicorn.run(build_app(), host=settings.mcp_host, port=settings.mcp_port, log_level="info")
